"""
HeyGen CMS API client — real calls to cms-api.heygendev.com.

Confirmed endpoints (all POST, auth via x-api-key header):

  READ:
    /v1/internal/movio/user.get
        body: {"email": str}

  WRITE — quota (credits only, no tier change):
    /v1/internal/movio/gift_quota.add
        body: {"email": str, "feature": str, "total": int, "expired_days": int, "note"?: str}
        → returns {quota_id, total, remaining, expires, message}
        features: "generative_credit" | "plan_credit" | "api" | "seat" |
                  "regular" | "unlimited_regular" | "video_translate" |
                  "avatar_video" | "personalized_video"
    /v1/internal/movio/gift_quota.expire
        body: {"quota_id": str}   — revoke a specific grant by ID
    /v1/internal/movio/gift_quota.deduct
        body: {"quota_id": str, "amount": int}

  WRITE — subscription (full comp: tier + quota bundle, replaces existing sub):
    /v1/internal/movio/gift_subscription.add
        body: {
            "email": str,           # required (or "username")
            "tier": str,            # optional: "creator"|"pro"|"business" (default creator)
            "expired_days": int,    # optional: duration (default system default)
            "quotas": dict,         # REQUIRED (may be empty {}); {"generative_credit": N, ...}
            "trial": bool,          # optional, default True
            "api_sub": bool,        # optional, default False
            "cancel_self_serve": bool,  # optional
        }
        → returns {space_id, api_sub, seat_limit, usage_mode, granted_seconds}
        Side effects: cancels existing subscription, creates new one, adds quotas,
                      applies seat limit, brand kit, oracle adoption triggers.
    /v1/internal/movio/gift_subscription.remove
        body: {"email": str}   — strips sub back to free tier
    /v1/internal/movio/gift_subscription.upgrade
        body: {"email": str, "tier": str, "expired_days"?: int, "quotas"?: dict}
        Note: requires the user to have an existing active subscription.

  WRITE — account:
    /v1/internal/create_account
        body: {"email": str}   → {email, password, space_id}

Decision logic for execute_quota_grant:
  - tier specified AND credits specified → gift_subscription.add (bundled)
  - tier specified, no credits           → gift_subscription.add (tier default quota)
  - credits only, no tier                → gift_quota.add (top-up, no plan change)
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
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
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return json.loads(body)
        except Exception:
            return {"code": e.code, "message": body.decode()[:300]}


def _get(path: str) -> dict[str, Any]:
    key = _get_api_key()
    req = urllib.request.Request(
        f"{CMS_BASE}{path}",
        headers={"x-api-key": key},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return resp


# ---------------------------------------------------------------------------
# Feature name normalisation
# ---------------------------------------------------------------------------
_FEATURE_MAP = {
    "credits": "generative_credit",
    "generative_credit": "generative_credit",
    "generative": "generative_credit",
    "plan_credit": "plan_credit",
    "plan": "plan_credit",
    "api": "api",
    "seat": "seat",
    "video_translate": "video_translate",
    "avatar_video": "avatar_video",
    "personalized_video": "personalized_video",
}

VALID_TIERS = {"creator", "pro", "business", "enterprise"}


def _normalize_feature(product: str | None) -> str:
    return _FEATURE_MAP.get((product or "generative_credit").lower(), "generative_credit")


# ---------------------------------------------------------------------------
# Public read API
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
        "user_id": spaces[0].get("owner") if spaces else None,
        "space_id": space_id,
        "tier": d.get("tier", "free"),
        "api_tier": d.get("api_tier", "free"),
        "internal": d.get("internal", False),
        "country_code": d.get("country_code"),
        "registration_ts": d.get("registration_ts"),
        "quotas": d.get("quotas", {}),
        "spaces": spaces,
    }


def lookup_user(email: str) -> dict[str, Any]:
    """Lookup user — read only."""
    return get_user_state(email)


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def execute_quota_grant(
    email: str,
    tier: str | None,
    credits: int | None,
    duration_days: int | None,
    product: str = "generative_credit",
) -> dict[str, Any]:
    """
    Grant credits/subscription to a user.

    Decision:
      - tier specified → gift_subscription.add (full comp: tier + optional credit bundle)
      - credits only   → gift_quota.add (top-up credits, no plan change)
    """
    has_tier = tier and tier.lower() in VALID_TIERS
    duration_days = duration_days or 30

    if has_tier:
        return _execute_subscription_grant(
            email=email,
            tier=tier,
            duration_days=duration_days,
            credits=credits,
            product=product,
        )
    else:
        return _execute_credit_top_up(
            email=email,
            credits=credits or 0,
            duration_days=duration_days,
            product=product,
            tier_note=tier,
        )


def _execute_subscription_grant(
    email: str,
    tier: str,
    duration_days: int,
    credits: int | None,
    product: str,
) -> dict[str, Any]:
    """
    Full comp via gift_subscription.add.
    Cancels existing sub, creates new one, bundles quota.
    """
    quotas: dict[str, int] = {}
    if credits:
        feature = _normalize_feature(product)
        quotas[feature] = credits

    resp = _post("/v1/internal/movio/gift_subscription.add", {
        "email": email,
        "tier": tier.lower(),
        "expired_days": duration_days,
        "quotas": quotas,
        "trial": True,
    })

    if resp.get("code") != 100:
        return {"email": email, "error": resp, "granted": False, "action": "subscription_grant"}

    data = resp.get("data", {})
    result: dict[str, Any] = {
        "email": email,
        "granted": True,
        "action": "subscription_grant",
        "tier": tier,
        "duration_days": duration_days,
        "space_id": data.get("space_id"),
        "api_sub": data.get("api_sub"),
        "seat_limit": data.get("seat_limit"),
    }
    if credits:
        result["credits_granted"] = credits
        result["credits_feature"] = _normalize_feature(product)
    return result


def _execute_credit_top_up(
    email: str,
    credits: int,
    duration_days: int,
    product: str,
    tier_note: str | None = None,
) -> dict[str, Any]:
    """
    Credits-only top-up via gift_quota.add (no tier change).
    """
    feature = _normalize_feature(product)
    resp = _post("/v1/internal/movio/gift_quota.add", {
        "email": email,
        "feature": feature,
        "total": credits,
        "expired_days": duration_days,
        "note": f"jarvis credit top-up tier_note={tier_note}",
    })
    if resp.get("code") != 100:
        return {"email": email, "error": resp, "granted": False, "action": "credit_top_up"}

    data = resp.get("data", {})
    return {
        "email": email,
        "granted": True,
        "action": "credit_top_up",
        "feature": feature,
        "quota_id": data.get("quota_id"),
        "total_after": data.get("total"),
        "remaining_after": data.get("remaining"),
        "expires": data.get("expires"),
        "warning": data.get("message", ""),
    }


def execute_subscription_remove(email: str) -> dict[str, Any]:
    """Strip user's subscription back to free tier."""
    resp = _post("/v1/internal/movio/gift_subscription.remove", {"email": email})
    return {
        "email": email,
        "removed": resp.get("code") == 100,
        "response": resp,
    }


def execute_create_account(email: str, tier: str | None = None, duration_days: int | None = None) -> dict[str, Any]:
    """
    Create a new HeyGen account, then optionally comp a subscription.
    Step 1: create_account → gets credentials
    Step 2: if tier != free, gift_subscription.add
    """
    # Step 1: create the account
    resp = _post("/v1/internal/create_account", {"email": email})
    if resp.get("code") != 100:
        # already exists is a common case — try to comp anyway
        if "already exists" not in str(resp.get("message", "")):
            return {"email": email, "error": resp, "created": False}
        account_data: dict[str, Any] = {}
        created = False
    else:
        account_data = resp.get("data", {})
        created = True

    result: dict[str, Any] = {
        "email": account_data.get("email", email),
        "space_id": account_data.get("space_id"),
        "created": created,
        "tier": tier,
        "subscription_days_remaining": duration_days,
    }

    # Step 2: if non-free tier, comp the subscription
    if tier and tier.lower() in VALID_TIERS and tier.lower() != "free":
        sub_resp = _post("/v1/internal/movio/gift_subscription.add", {
            "email": email,
            "tier": tier.lower(),
            "expired_days": duration_days,
            "quotas": {},
            "trial": True,
        })
        result["subscription_granted"] = sub_resp.get("code") == 100
        result["subscription_response"] = sub_resp.get("data", {})

    return result
