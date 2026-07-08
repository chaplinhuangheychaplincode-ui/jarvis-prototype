"""
Jarvis Slack client — thin wrapper over the Slack Web API.
Handles posting Block Kit confirmation cards and reading thread context.

Confirmation flow uses Block Kit interactive buttons (not emoji reactions):
  ✅ Confirm  /  ❌ Cancel
This fixes BUG-1 and BUG-2 — reactions are lossy and anyone can react.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any


def _bot_token() -> str:
    raw = os.environ.get("SLACK_BOT_TOKEN", "")
    if raw:
        return raw
    # Fall back to .env file
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("SLACK_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("SLACK_BOT_TOKEN not found")


def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = _bot_token()
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    if not resp.get("ok"):
        raise RuntimeError(f"Slack API {method} error: {resp.get('error')} — {resp}")
    return resp


def post_message(channel: str, text: str, thread_ts: str | None = None,
                 blocks: list | None = None) -> dict[str, Any]:
    # prefix removed — bot identity is clear from the app name
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    return _call("chat.postMessage", payload)


def update_message(channel: str, ts: str, text: str, blocks: list | None = None) -> dict[str, Any]:
    # Slack rejects empty text even when blocks are provided
    safe_text = text if text and text.strip() else "_ _"
    payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": safe_text}
    if blocks:
        payload["blocks"] = blocks
    return _call("chat.update", payload)


def get_user_info(user_id: str) -> dict[str, Any]:
    token = _bot_token()
    url = f"https://slack.com/api/users.info?user={user_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp.get("user", {})


def build_bulk_confirmation_card(intent: dict[str, Any], pending_id: str) -> list[dict[str, Any]]:
    """Build a confirmation card for bulk_grant operations."""
    emails = intent.get("target_emails", [])
    n = len(emails)
    tier = intent.get("tier", "")
    credits = intent.get("credits")
    days = intent.get("duration_days")
    product = intent.get("product", "generative_credit")
    reason = intent.get("reason", "")
    confidence = intent.get("confidence", 0)

    # Build grant description
    grant_parts = []
    if tier:
        grant_parts.append(f"*Tier:* {tier}")
    if credits:
        grant_parts.append(f"*Credits:* {credits:,} {product}")
    if days:
        grant_parts.append(f"*Duration:* {days}d")
    grant_str = " · ".join(grant_parts) if grant_parts else "_no grant details_"

    # Recipient preview (show first 5, then "+N more")
    preview = ", ".join(f"`{e}`" for e in emails[:5])
    if n > 5:
        preview += f" … +{n - 5} more"

    confidence_emoji = "🟢" if confidence >= 0.85 else "🟡" if confidence >= 0.6 else "🔴"
    utterance = intent.get("raw_utterance", "")

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🤖 Jarvis — Bulk Grant Preview ({n} users)", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Grant:* {grant_str}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Recipients ({n}):*\n{preview}"},
        },
    ]
    if reason:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reason:* _{reason}_"},
        })
    blocks += [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": (
                f"{confidence_emoji} Confidence: *{confidence:.0%}* · "
                f"ID: `{pending_id}` · _Expires in 15 min_"
            )}],
        },
    ]
    if utterance:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"💬 _{utterance}_"}],
        })
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "block_id": f"confirm_actions_{pending_id}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": f"✅ Confirm ({n} users)", "emoji": True},
                "style": "primary",
                "value": pending_id,
                "action_id": "confirm_action",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ Cancel", "emoji": True},
                "style": "danger",
                "value": pending_id,
                "action_id": "cancel_action",
            },
        ],
    })
    return blocks


def build_confirmation_card(intent: dict[str, Any], before_state: dict[str, Any],
                             pending_id: str) -> list[dict[str, Any]]:
    """Build a Block Kit confirmation card with interactive ✅/❌ buttons."""
    action = intent.get("action", "unknown")
    target = intent.get("target_email", "unknown")
    confidence = intent.get("confidence", 0)

    # Header
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🤖 Jarvis — Action Preview", "emoji": True},
        },
        {"type": "divider"},
    ]

    # Action summary
    summary = _format_action_summary(intent, before_state)
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": summary},
    })

    # Before state + request body blocks
    diff_blocks = _build_diff_blocks(intent, before_state)
    blocks.extend(diff_blocks)

    # Confidence + pending ID
    confidence_emoji = "🟢" if confidence >= 0.85 else "🟡" if confidence >= 0.6 else "🔴"
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"{confidence_emoji} Confidence: *{confidence:.0%}* · "
                    f"ID: `{pending_id}` · "
                    f"_Expires in 15 min_"
                ),
            }
        ],
    })

    # Original utterance
    utterance = intent.get("raw_utterance", "")
    if utterance:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"💬 _{utterance}_"}],
        })

    blocks.append({"type": "divider"})

    # BUG-1/BUG-2 FIX: Block Kit buttons, not emoji instructions
    blocks.append({
        "type": "actions",
        "block_id": f"confirm_actions_{pending_id}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Confirm", "emoji": True},
                "style": "primary",
                "value": pending_id,
                "action_id": "confirm_action",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ Cancel", "emoji": True},
                "style": "danger",
                "value": pending_id,
                "action_id": "cancel_action",
            },
        ],
    })

    return blocks


def build_clarifying_question_card(question: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🤔 *Jarvis needs a bit more info:*\n\n{question}",
            },
        }
    ]


def build_audit_ack_card(audit_id: str, action: str, target: str,
                          after_state: dict[str, Any],
                          before_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build a post-execution summary card — no raw API blobs."""
    if action == "lookup":
        # Show human-readable user info for lookups (BUG-6: no raw JSON)
        fields = []
        display_keys = ["user_id", "space_id", "tier", "internal", "country_code", "registration_ts"]
        for k in display_keys:
            v = after_state.get(k)
            if v is not None:
                fields.append({"type": "mrkdwn", "text": f"*{k}:*\n`{v}`"})
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🔍 User Info: {target}", "emoji": True},
            },
            {"type": "divider"},
        ]
        if fields:
            for i in range(0, len(fields), 10):
                blocks.append({"type": "section", "fields": fields[i:i+10]})

        lookup_fields = after_state.get("_lookup_fields", [])

        # Surface any specifically requested top-level fields (tier, api_tier, etc.) that aren't in quotas
        TOP_LEVEL_FIELDS = {"tier", "api_tier", "internal", "country_code", "registration_ts", "user_id", "space_id"}
        pinned_top = [f for f in lookup_fields if f in TOP_LEVEL_FIELDS and after_state.get(f) is not None]
        if pinned_top:
            pin_parts = [f"*{k}:* `{after_state[k]}`" for k in pinned_top]
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "📌 *Requested:* " + " · ".join(pin_parts)},
            })
        lookup_fields = after_state.get("_lookup_fields", [])
        quotas = after_state.get("quotas", {})
        if quotas and isinstance(quotas, dict):
            def _fmt_quota(val: Any) -> str:
                if isinstance(val, dict):
                    # CMS returns {amount, quota_type} — single value, no total
                    if "amount" in val and "remaining" not in val and "total" not in val:
                        amount = val["amount"]
                        exp = val.get("expire_at") or val.get("expired_at") or val.get("expires", "")
                        s = str(amount)
                        if exp:
                            s += f" (exp {str(exp)[:10]})"
                        return s
                    rem = val.get("remaining", val.get("remain", "?"))
                    tot = val.get("total", val.get("limit", "?"))
                    exp = val.get("expire_at") or val.get("expired_at") or val.get("expires", "")
                    s = f"{rem} / {tot}"
                    if exp:
                        s += f" (exp {str(exp)[:10]})"
                    return s
                return str(val)

            # If specific fields were requested, show those prominently first
            pinned_keys = [f for f in lookup_fields if f in quotas]
            other_keys = [k for k in quotas if k not in pinned_keys]
            ordered_keys = pinned_keys + other_keys

            if pinned_keys:
                # Highlighted section for specifically requested fields
                pin_parts = [f"*{k}:* `{_fmt_quota(quotas[k])}`" for k in pinned_keys]
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "📌 *Requested:* " + " · ".join(pin_parts)},
                })

            quota_parts = [f"`{k}`: {_fmt_quota(quotas[k])}" for k in other_keys[:8]]
            if len(other_keys) > 8:
                quota_parts.append(f"… +{len(other_keys)-8} more")
            if quota_parts:
                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "📊 Quotas: " + " · ".join(quota_parts)}],
                })
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"🔎 Audit: `{audit_id}` _(read logged)_"}],
        })
        return blocks

    # Write ops (quota_grant, create_account, etc.) — show structured result
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "✅ Jarvis — Done", "emoji": True},
        },
        {"type": "divider"},
    ]
    summary_fields = _format_ack_fields(action, target, after_state)
    if summary_fields:
        blocks.append({"type": "section", "fields": summary_fields})
    else:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"`{action}` applied to `{target}`",
            },
        })

    # Before / After quota diff for grant and revoke operations
    if action in ("quota_grant", "revoke_grant", "reduce_grant") and before_state:
        diff_lines: list[str] = []
        before_quotas = before_state.get("quotas", {}) or {}
        after_quotas = after_state.get("quotas", {}) if after_state.get("quotas") else {}
        # Build a unified set of all quota keys
        all_keys = sorted(set(list(before_quotas.keys()) + list(after_quotas.keys())))

        def _fmt_q(v: Any) -> str:
            if isinstance(v, dict):
                amt = v.get("amount", "?")
                exp = v.get("expire_at") or v.get("expires", "")
                return f"{amt}" + (f"  exp {str(exp)[:10]}" if exp else "")
            return str(v)

        for k in all_keys:
            b = before_quotas.get(k)
            a = after_quotas.get(k)
            b_str = _fmt_q(b) if b is not None else "—"
            a_str = _fmt_q(a) if a is not None else "—"
            changed = b_str != a_str
            marker = "* " if changed else "  "
            diff_lines.append(f"{marker}{k}: {b_str} → {a_str}" if changed else f"{marker}{k}: {b_str}")

        if diff_lines:
            text = "Quotas (before → after):\n" + "\n".join(diff_lines)
            if len(text) > 2900:
                text = text[:2900] + "\n… (truncated)"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{text}```"}})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"🔎 Audit: `{audit_id}`"}],
    })
    return blocks


def _format_ack_fields(action: str, target: str, after: dict[str, Any]) -> list[dict[str, Any]]:
    """Format structured key/value fields for the ack card — no raw API dumps."""
    fields = []
    if action in ("quota_grant", "subscription_grant", "credit_top_up"):
        if after.get("tier"):
            fields.append({"type": "mrkdwn", "text": f"*Tier:*\n`{after['tier']}`"})
        req = after.get("credits_requested")
        granted = after.get("credits_granted") if after.get("credits_granted") is not None else after.get("credits_granted")
        if after.get("capped") and req and granted is not None:
            fields.append({"type": "mrkdwn", "text": f"*Credits requested:*\n`{req:,}`"})
            fields.append({"type": "mrkdwn", "text": f"*Credits actually granted:*\n⚠️ `{granted:,}` _(capped by CMS 90-day limit)_"})
        elif granted is not None:
            fields.append({"type": "mrkdwn", "text": f"*Credits granted:*\n`{granted:,}`"})
        elif after.get("credits_granted"):
            fields.append({"type": "mrkdwn", "text": f"*Credits granted:*\n`{after['credits_granted']:,}`"})
        if after.get("duration_days"):
            fields.append({"type": "mrkdwn", "text": f"*Duration:*\n`{after['duration_days']}d`"})
        if after.get("expires"):
            fields.append({"type": "mrkdwn", "text": f"*Expires:*\n`{after['expires']}`"})
        if after.get("quota_id"):
            fields.append({"type": "mrkdwn", "text": f"*Quota ID:*\n`{after['quota_id']}`"})
    elif action == "create_account":
        if after.get("email"):
            fields.append({"type": "mrkdwn", "text": f"*Email:*\n`{after['email']}`"})
        if after.get("space_id"):
            fields.append({"type": "mrkdwn", "text": f"*Space ID:*\n`{after['space_id']}`"})
        if after.get("password"):
            fields.append({"type": "mrkdwn", "text": f"*Password:*\n`{after['password']}`"})
        fields.append({"type": "mrkdwn", "text": f"*Created:*\n`{after.get('created', False)}`"})
        if after.get("tier"):
            fields.append({"type": "mrkdwn", "text": f"*Tier:*\n`{after['tier']}`"})
    elif action == "reduce_grant":
        feature = after.get("feature", "?")
        fields.append({"type": "mrkdwn", "text": f"*Credit type:*\n`{feature}`"})
        fields.append({"type": "mrkdwn", "text": f"*Amount deducted:*\n`{after.get('amount_deducted', '?'):,}`"})
        fields.append({"type": "mrkdwn", "text": f"*Success:*\n`{after.get('deducted', False)}`"})
    elif action == "revoke_grant":
        revoke_type = after.get("revoke_type", "subscription")
        sub_result = after.get("subscription_result", {})
        if sub_result:
            ok = sub_result.get("removed", False)
            fields.append({"type": "mrkdwn", "text": f"*Subscription removed:*\n`{ok}`"})
        quota_result = after.get("quota_result", {})
        if quota_result:
            ok = quota_result.get("expired", False)
            fields.append({"type": "mrkdwn", "text": f"*Quota expired:*\n`{ok}`"})
            fields.append({"type": "mrkdwn", "text": f"*Quota ID:*\n`{after.get('quota_id', '?')}`"})
    return fields


_FEATURE_MAP_CLIENT = {
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
_VALID_TIERS_CLIENT = {"creator", "pro", "business", "enterprise"}


def _normalize_product_client(product: str | None) -> str:
    return _FEATURE_MAP_CLIENT.get((product or "generative_credit").lower(), "generative_credit")


def _format_request_body(intent: dict[str, Any]) -> str:
    """Return the exact endpoint + JSON body that will be POSTed to CMS."""
    action = intent.get("action")
    email = intent.get("target_email", "?")

    if action == "quota_grant":
        tier = intent.get("tier", "")
        credits = intent.get("credits")
        days = intent.get("duration_days", 30)
        product = _normalize_product_client(intent.get("product"))
        quota_type = intent.get("quota_type", None)
        has_tier = tier and tier.lower() in _VALID_TIERS_CLIENT
        if has_tier:
            quotas = {product: credits} if credits else {}
            # All fields per AddGiftSubscriptionRequest (defaults shown explicitly)
            body = {
                "email": email,           # required
                "tier": tier.lower(),     # required
                "day": days,              # default=30
                "quotas": quotas,         # default={}
                "trial": True,            # default=True
            }
            return f"POST /v1/internal/movio/gift_subscription.add\n{json.dumps(body, indent=2)}"
        else:
            # All fields per AddGiftQuotaRequest (defaults shown explicitly)
            body = {
                "email": email,           # required
                "feature": product,       # required, QuotaFeature enum
                "quota": credits,         # required, ge=1 le=100000
                "expire_days": days,      # default=30, ge=1 le=1825
                "quota_type": quota_type, # null=expires on date; "withsubscrition"=permanent
            }
            return f"POST /v1/internal/movio/gift_quota.add\n{json.dumps(body, indent=2)}"

    elif action == "revoke_grant":
        revoke_type = intent.get("revoke_type", "subscription")
        quota_id = intent.get("quota_id")
        if revoke_type == "quota" and quota_id:
            body = {"quota_id": quota_id}
            return f"POST /v1/internal/movio/gift_quota.expire\n{json.dumps(body, indent=2)}"
        body = {"email": email}
        return f"POST /v1/internal/movio/gift_subscription.remove\n{json.dumps(body, indent=2)}"

    elif action == "create_account":
        body: dict[str, Any] = {"email": email}
        tier = intent.get("tier")
        days = intent.get("duration_days", 30)
        credits = intent.get("credits")
        product = _normalize_product_client(intent.get("product"))
        lines = [f"POST /v1/internal/create_account\n{json.dumps(body, indent=2)}"]
        if tier and tier.lower() in _VALID_TIERS_CLIENT:
            quotas = {product: credits} if credits else {}
            # All fields per AddGiftSubscriptionRequest
            sub_body = {
                "email": email,
                "tier": tier.lower(),     # required
                "day": days,              # default=30
                "quotas": quotas,         # credits bundled here
                "trial": True,            # default=True
            }
            lines.append(f"\nPOST /v1/internal/movio/gift_subscription.add\n{json.dumps(sub_body, indent=2)}")
        return "\n".join(lines)

    elif action == "bulk_grant":
        emails = intent.get("target_emails", [])
        n = len(emails)
        tier = intent.get("tier", "")
        credits = intent.get("credits")
        days = intent.get("duration_days", 30)
        product = _normalize_product_client(intent.get("product"))
        quota_type = intent.get("quota_type", None)
        has_tier = tier and tier.lower() in _VALID_TIERS_CLIENT
        if has_tier:
            sample = {
                "email": "<each of %d>" % n,
                "tier": tier.lower(),
                "day": days,
                "quotas": {product: credits} if credits else {},
                "trial": True,
            }
            return f"POST /v1/internal/movio/gift_subscription.add × {n}\n{json.dumps(sample, indent=2)}"
        sample = {
            "email": "<each of %d>" % n,
            "feature": product,
            "quota": credits,
            "expire_days": days,
            "quota_type": quota_type,
        }
        return f"POST /v1/internal/movio/gift_quota.add × {n}\n{json.dumps(sample, indent=2)}"

    elif action == "reduce_grant":
        product = intent.get("product", "generative_credit")
        credits = intent.get("credits", "?")
        # All fields per DeductGiftQuotaRequest
        body = {
            "email": email,       # required
            "feature": product,   # required, QuotaFeature enum
            "quota": credits,     # required (was: wrong field "amount")
        }
        return f"POST /v1/internal/movio/gift_quota.deduct\n{json.dumps(body, indent=2)}"

    return ""


def _format_action_summary(intent: dict[str, Any], before: dict[str, Any]) -> str:
    action = intent.get("action")
    target = intent.get("target_email", "?")

    if action == "quota_grant":
        tier = intent.get("tier", "")
        credits = intent.get("credits", "")
        days = intent.get("duration_days", "")
        product = intent.get("product", "credits")
        parts = []
        if tier:
            parts.append(f"tier → *{tier}*")
        if credits:
            parts.append(f"{product} → *{credits:,}*" if isinstance(credits, int) else f"{product} → *{credits}*")
        if days:
            parts.append(f"duration → *{days}d*")
        change_str = " · ".join(parts) if parts else "no changes parsed"
        return f"*Quota Grant* for `{target}`\n{change_str}"

    elif action == "create_account":
        reason = intent.get("reason")
        detail = f"\nreason → _{reason}_" if reason else ""
        return f"*Create Account* — `{target}`{detail}"

    elif action == "lookup":
        return f"*Lookup* — `{target}` _(read-only, no changes)_"

    elif action == "revoke_grant":
        revoke_type = intent.get("revoke_type", "subscription")
        quota_id = intent.get("quota_id")
        detail = f" · quota_id `{quota_id}`" if quota_id else ""
        return f"*Revoke Grant* for `{target}` · type: *{revoke_type}*{detail}"

    elif action == "reduce_grant":
        product = intent.get("product", "generative_credit")
        credits = intent.get("credits", "?")
        amt = f"{credits:,}" if isinstance(credits, int) else str(credits)
        return f"*Reduce Grant* for `{target}` · deduct *{amt}* `{product}`"

    elif action == "ent_sub_grant":
        ae = intent.get("ae_attribution", "?")
        return f"*Enterprise Sub Grant* for `{target}` · AE: *{ae}*"

    elif action == "bulk_grant":
        count = intent.get("user_count", "?")
        return f"*Bulk Grant* — *{count}* users"

    return f"*{action}* for `{target}`"


def _build_diff_blocks(intent: dict[str, Any], before: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Block Kit blocks for full before state + API request preview."""
    action = intent.get("action")
    result_blocks: list[dict[str, Any]] = []

    # ── Before state ─────────────────────────────────────────────────────────
    if before and action not in ("create_account",) and before.get("tier") not in (None, "unknown"):
        target_product = _normalize_product_client(intent.get("product"))
        lines: list[str] = []
        for k in ("tier", "api_tier"):
            v = before.get(k)
            if v:
                lines.append(f"  {k}: {v}")
        quotas = before.get("quotas", {})
        if quotas and isinstance(quotas, dict):
            for k, v in quotas.items():
                amt = v.get("amount", "?") if isinstance(v, dict) else v
                exp_raw = (v.get("expire_at") or v.get("expires", "")) if isinstance(v, dict) else ""
                exp_str = f"  exp {str(exp_raw)[:10]}" if exp_raw else ""
                marker = "→ " if k == target_product else "  "
                lines.append(f"{marker}{k}: {amt}{exp_str}")
        if lines:
            text = "Before:\n" + "\n".join(lines)
            # truncate to stay within Slack 3000 char limit
            if len(text) > 2900:
                text = text[:2900] + "\n  … (truncated)"
            result_blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{text}```"},
            })
    # ── API request body ──────────────────────────────────────────────────────
    if action not in ("lookup",):
        req = _format_request_body(intent)
        if req:
            if len(req) > 2900:
                req = req[:2900] + "\n… (truncated)"
            result_blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Request:*\n```{req}```"},
            })
    return result_blocks


# ---------------------------------------------------------------------------
# Investigation card — agentic investigation results + suggested action buttons
# ---------------------------------------------------------------------------

def build_investigation_card(
    result: Any,   # InvestigationResult (avoid circular import)
    pending_ids: list[str],
) -> list[dict[str, Any]]:
    """
    Build a Block Kit card for an investigation result.

    Each proposed action gets a ✅ button that maps to a pre-written pending_id.
    The caller is responsible for writing those pendings to the store before
    posting this card, so the button handler can find them immediately.

    Args:
        result: InvestigationResult instance from investigator.py
        pending_ids: list of pending_ids, one per proposed_action (same order)
    """
    blocks: list[dict[str, Any]] = []

    # Header
    icon = "✅" if result.no_action_needed else "🔍"
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"{icon} Investigation Complete"},
    })

    # Summary
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": result.summary},
    })

    blocks.append({"type": "divider"})

    # Findings
    if result.findings:
        finding_lines = "\n".join(f"• {f}" for f in result.findings)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Findings:*\n{finding_lines}"},
        })

    # No action needed
    if result.no_action_needed:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ Account looks healthy — no action required."},
        })
        return blocks

    # Proposed actions — each as its own section with a confirm button
    if result.proposed_actions:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Suggested Actions:*"},
        })

        for i, (action_intent, pending_id) in enumerate(
            zip(result.proposed_actions, pending_ids)
        ):
            label = action_intent.get("label", f"Action {i + 1}")
            action_type = action_intent.get("action", "")
            email = action_intent.get("target_email", "")

            # Build a one-line detail string from the intent fields
            detail_parts = []
            if action_intent.get("credits"):
                detail_parts.append(f"{action_intent['credits']:,} {action_intent.get('product', 'credits')}")
            if action_intent.get("duration_days"):
                detail_parts.append(f"{action_intent['duration_days']}d")
            if action_intent.get("tier"):
                detail_parts.append(f"tier={action_intent['tier']}")
            if action_intent.get("quota_id"):
                detail_parts.append(f"quota_id={action_intent['quota_id']}")
            detail_str = " · ".join(detail_parts)

            description = f"`{action_type}` for `{email}`"
            if detail_str:
                description += f"\n  {detail_str}"
            if action_intent.get("reason"):
                description += f"\n  _{action_intent['reason']}_"

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{i + 1}. {label}*\n{description}"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Confirm"},
                    "style": "primary",
                    "action_id": "confirm_action",
                    "value": pending_id,
                },
            })

    # Footer: metadata
    elapsed_s = result.elapsed_ms / 1000
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"🤖 Agent used {result.tool_calls} tool call(s) · "
                f"{elapsed_s:.1f}s · "
                f"Actions above flow through normal HITL confirm"
            ),
        }],
    })

    return blocks



# ---------------------------------------------------------------------------
# Workflow plan cards (multi-step workflow architecture)
# ---------------------------------------------------------------------------

def _fmt_step(step: dict) -> str:
    action = step.get("action", "")
    email = step.get("target_email", "")
    pre = " _(auto, read-only)_" if step.get("pre_confirm") else ""
    parts = [f"`{action}`"]
    if email:
        parts.append(f"`{email}`")
    if step.get("tier"):
        parts.append(f"tier=`{step['tier']}`")
    if step.get("credits"):
        parts.append(f"{step['credits']:,} `{step.get('product','generative_credit')}`")
    if step.get("duration_days") and action not in ("get_info", "lookup"):
        parts.append(f"{step['duration_days']}d")
    if step.get("revoke_type"):
        parts.append(f"revoke=`{step['revoke_type']}`")
    if step.get("quota_id"):
        parts.append(f"quota_id=`{step['quota_id']}`")
    if step.get("target_emails"):
        parts.append(f"{len(step['target_emails'])} users")
    return " · ".join(parts) + pre


def build_plan_card(plan: dict, pending_id: str) -> list:
    blocks = []
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "📋 Workflow Plan"}})
    if plan.get("summary"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": plan["summary"]}})
    blocks.append({"type": "divider"})
    steps = plan.get("steps", [])
    for step in steps:
        if step.get("pre_confirm"):
            continue
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Step {step.get('step','?')}:* {_fmt_step(step)}"},
        })
    pre_steps = [s for s in steps if s.get("pre_confirm")]
    if pre_steps:
        pre_desc = ", ".join(f"`{s['action']}`" for s in pre_steps)
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_Auto-runs before execution: {pre_desc}_"}]})
    # Show guardrail warnings on the plan card
    guardrails = plan.get("guardrails_applied", [])
    if guardrails:
        blocks.append({"type": "divider"})
        for g in guardrails:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": g}})
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Confirm"},
             "style": "primary", "action_id": "confirm_plan", "value": pending_id},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Cancel"},
             "style": "danger", "action_id": "cancel_plan", "value": pending_id},
        ],
    })
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"Reply in this thread to ask questions or request changes · `{pending_id}`"}]})
    return blocks


def build_execution_complete_card(completed: list, audit_ids: list, actor_slack_id: str, elapsed_s: float) -> list:
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "✅ Workflow Complete"}}]
    warnings = []
    for sr in completed:
        if sr.step.get("pre_confirm"):
            continue
        result = sr.result or {}
        if result.get("skipped") and result.get("warning"):
            # Skipped step with a warning — show as warning row, not success
            blocks.append({"type": "section",
                "text": {"type": "mrkdwn", "text": f"⚠️ Step {sr.step_num}: {_fmt_step(sr.step)} _(skipped)_\n  _{result.get('reason', '')}_"}})
            warnings.append(result.get("reason", ""))
        else:
            blocks.append({"type": "section",
                "text": {"type": "mrkdwn", "text": f"✅ Step {sr.step_num}: {_fmt_step(sr.step)} _{sr.elapsed_ms}ms_"}})
    blocks.append({"type": "divider"})
    audit_str = " · ".join(f"`{a}`" for a in audit_ids if not a.startswith("err_"))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"Confirmed by <@{actor_slack_id}> · {elapsed_s:.1f}s · {audit_str}"}]})
    return blocks


def build_execution_failure_card(completed: list, failed: object, remaining_steps: list, recovery_pending_id: str) -> list:
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "⚠️ Workflow Partially Failed"}}]
    for sr in completed:
        if sr.step.get("pre_confirm"):
            continue
        blocks.append({"type": "section",
            "text": {"type": "mrkdwn", "text": f"✅ Step {sr.step_num}: {_fmt_step(sr.step)}"}})
    err_msg = getattr(failed, "result", {}).get("error", "Unknown error")
    step_num = getattr(failed, "step_num", "?")
    step_dict = getattr(failed, "step", {})
    blocks.append({"type": "section",
        "text": {"type": "mrkdwn", "text": f"❌ Step {step_num}: {_fmt_step(step_dict)}\n  _{err_msg}_"}})
    if remaining_steps:
        blocks.append({"type": "divider"})
        for step in remaining_steps:
            blocks.append({"type": "section",
                "text": {"type": "mrkdwn", "text": f"⏭ Step {step.get('step','?')}: {_fmt_step(step)} _(skipped)_"}})
        blocks.append({"type": "divider"})
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔁 Retry remaining"},
             "style": "primary", "action_id": "confirm_plan", "value": recovery_pending_id},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Abandon"},
             "style": "danger", "action_id": "cancel_plan", "value": recovery_pending_id},
        ]})
    return blocks
