"""
Jarvis Slack client — thin wrapper over the Slack Web API.
Handles posting Block Kit confirmation cards and reading thread context.
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
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    return _call("chat.postMessage", payload)


def update_message(channel: str, ts: str, text: str, blocks: list | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
    if blocks:
        payload["blocks"] = blocks
    return _call("chat.update", payload)


def get_user_info(user_id: str) -> dict[str, Any]:
    token = _bot_token()
    url = f"https://slack.com/api/users.info?user={user_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp.get("user", {})


def build_confirmation_card(intent: dict[str, Any], before_state: dict[str, Any],
                             pending_id: str) -> list[dict[str, Any]]:
    """Build a Block Kit confirmation card for a pending action."""
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

    # Diff fields
    diff_fields = _build_diff_fields(intent, before_state)
    if diff_fields:
        blocks.append({"type": "section", "fields": diff_fields})

    # Confidence + utterance
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
            "elements": [{"type": "mrkdwn", "text": f"💬 _\"{utterance}\"_"}],
        })

    blocks.append({"type": "divider"})

    # Confirm instruction
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "React with ✅ to execute · ❌ to cancel · or reply to ask a question.",
        },
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
                          after_state: dict[str, Any]) -> list[dict[str, Any]]:
    if action == "lookup":
        # Show human-readable user info for lookups
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
            # Slack allows max 10 fields per section block
            for i in range(0, len(fields), 10):
                blocks.append({"type": "section", "fields": fields[i:i+10]})
        # Quotas as a separate context line if present
        quotas = after_state.get("quotas")
        if quotas:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"📊 Quotas: `{json.dumps(quotas)}`"}],
            })
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"🔎 Audit: `{audit_id}` _(read logged)_"}],
        })
        return blocks

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ *Done.* `{action}` on `{target}`\n"
                    f"Audit: `{audit_id}`\n"
                    f"After: `{json.dumps(after_state)}`"
                ),
            },
        }
    ]


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
        tier = intent.get("tier", "?")
        days = intent.get("duration_days", "?")
        return f"*Create Account* — `{target}` as *{tier}* for *{days}d*"

    elif action == "lookup":
        return f"*Lookup* — `{target}` _(read-only, no changes)_"

    elif action == "ent_sub_grant":
        ae = intent.get("ae_attribution", "?")
        return f"*Enterprise Sub Grant* for `{target}` · AE: *{ae}*"

    elif action == "bulk_grant":
        count = intent.get("user_count", "?")
        return f"*Bulk Grant* — *{count}* users"

    return f"*{action}* for `{target}`"


def _build_diff_fields(intent: dict[str, Any], before: dict[str, Any]) -> list[dict[str, Any]]:
    fields = []

    if before:
        fields.append({
            "type": "mrkdwn",
            "text": f"*Before:*\n```{json.dumps(before, indent=None)}```",
        })

    after_preview = {}
    if intent.get("tier"):
        after_preview["tier"] = intent["tier"]
    if intent.get("credits"):
        after_preview["credits"] = intent["credits"]
    if intent.get("duration_days"):
        after_preview["duration_days"] = intent["duration_days"]

    if after_preview:
        fields.append({
            "type": "mrkdwn",
            "text": f"*After (preview):*\n```{json.dumps(after_preview, indent=None)}```",
        })

    return fields
