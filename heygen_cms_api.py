"""
HeyGen CMS API client — replaces the mock with real calls to cms-api.heygendev.com.

Discovered endpoints (all under https://cms-api.heygendev.com):
  READ:
    POST /v1/internal/movio/user.get           {"email": str}
  WRITE (quota grants):
    POST /v1/internal/movio/gift_quota.add     {"email": str, "feature": str, "total": int, "expired_days": int, "note"?: str}
    POST /v1/internal/movio/gift_quota.expire  {"quota_id": str}   — revoke a specific grant
    POST /v1/internal/movio/gift_quota.deduct  {"quota_id": str, "amount": int}
  WRITE (account):
    POST /v1/internal/create_account           {"email": str}   → returns {email, password, space_id}
  WRITE (subscription — shape TBD, "quotas" field required):
    POST /v1/internal/movio/gift_subscription.add
    POST /v1/internal/movio/gift_subscription.remove
    POST /v1/internal/movio/gift_subscription.upgrade

Confirmed quota features (for gift_quota.add):
    "generative_credit", "plan_credit", "api", "seat",
    "regular", "unlimited_regular", "video_translate",
    "avatar_video", "personalized_video"

All functions preserve the same interface as mock_heygen_api.py so bot.py needs
no changes.
"""
from __future__ import annotations

import copy
import json
import subprocess
import time
import urllib.request
from typing import Any

CMS_BASE = "https://cms-api.heygendev.com"

# ---------------------------------------------------------------------------
# Auth — cached to avoid spawning a subprocess on every call
# ---------------------------------------------------------------------------
_API_KEY_CACHE: str | None = None
_API_KEY_FETCHED_AT: float = 0.0
_API_KEY_TTL = 300  # re-fetch every 5 minutes


def _get_api_key() -> str:
    global _API_KEY_CACHE, _API_KEY_FETCHED_AT
    now = time.monotonic()
    if _API_KEY_CACHE and (now - _API_KEY_FETCHED_AT) < _API_KEY_TTL:
        return _API_KEY_CACHE
    result = subprocess.run(
        ["python3", "/opt/genesis/manage-secrets.py", "get", "HEYGEN_CMS_API_KEY"],
        capture_output=True, text=True, timeout=10,
    )
    key = result.stdout.strip()
    if not key or key.startswith("no such secret"):
        raise RuntimeError("HEYGEN_CMS_API_KEY not found in secrets store")
    _API_KEY_CACHE = key
    _API_KEY_FETCHED_AT = now
    return key


def _post(path: str, data: dict[str, Any]) -> dict[str, Any]:
    key = _get_api_key()
    req = urllib.request.Request(
        f"{CMS_BASE}{path}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key},
        method="POST",
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return resp


def _get(path: str) -> dict[str, Any]:
    key = _get_api_key()
    req = urllib.request.Request(
        f"{CMS_BASE}{path}",
        headers={"x-api-key": key},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return resp


# ---------------------------------------------------------------------------
# Public API (same interface as mock_heygen_api.py)
# ---------------------------------------------------------------------------

def get_user_state(email: str) -> dict[str, Any]:
    """Fetch current user state from CMS."""
    resp = _post("/v1/internal/movio/user.get", {"email": email})
    if resp.get("code") != 100:
        return {"email": email, "user_id": None, "tier": "unknown", "error": resp}
    d = resp.get("data", {})
    spaces = d.get("spaces", [])
    space_id = spaces[0].get("owner") if spaces else None
    return {
        "email": email,
        "user_id": spaces[0].get("username") if spaces else None,
        "space_id": space_id,
        "tier": d.get("api_tier", "free"),
        "internal": d.get("internal", False),
        "country_code": d.get("country_code"),
        "registration_ts": d.get("registration_ts"),
        "quotas": d.get("quotas", {}),
        "spaces": spaces,
    }


def lookup_user(email: str) -> dict[str, Any]:
    """Lookup user — read only."""
    return get_user_state(email)


def execute_quota_grant(
    email: str,
    tier: str | None,
    credits: int | None,
    duration_days: int | None,
    product: str = "generative_credit",
) -> dict[str, Any]:
    """
    Grant quota/credits to a user via CMS gift_quota.add.

    feature mapping:
      product="credits" or "generative_credit" → feature="generative_credit"
      product="plan_credit"                     → feature="plan_credit"
      product="api"                             → feature="api"
    """
    feature_map = {
        "credits": "generative_credit",
        "generative_credit": "generative_credit",
        "plan_credit": "plan_credit",
        "api": "api",
        "seat": "seat",
    }
    feature = feature_map.get(product or "credits", "generative_credit")
    if credits is None:
        credits = 0
    if duration_days is None:
        duration_days = 30

    resp = _post("/v1/internal/movio/gift_quota.add", {
        "email": email,
        "feature": feature,
        "total": credits,
        "expired_days": duration_days,
        "note": f"jarvis grant: tier={tier}",
    })
    if resp.get("code") != 100:
        return {"email": email, "error": resp, "granted": False}

    data = resp.get("data", {})
    return {
        "email": email,
        "granted": True,
        "feature": feature,
        "quota_id": data.get("quota_id"),
        "total_after": data.get("total"),
        "remaining_after": data.get("remaining"),
        "expires": data.get("expires"),
        "warning": data.get("message", ""),
    }


def execute_create_account(email: str, tier: str, duration_days: int) -> dict[str, Any]:
    """
    Create a new HeyGen account via CMS.
    Returns {email, user_id, space_id, created}.
    """
    resp = _post("/v1/internal/create_account", {"email": email})
    if resp.get("code") == 100:
        d = resp.get("data", {})
        return {
            "email": d.get("email", email),
            "user_id": d.get("space_id"),   # space_id doubles as user identifier
            "space_id": d.get("space_id"),
            "tier": tier,
            "subscription_days_remaining": duration_days,
            "created": True,
        }
    # Already exists or other error
    return {"email": email, "error": resp, "created": False}
