"""
Jarvis bot — main event loop.

Listens for @mentions via Slack's Events API (polling mode for prototype;
production would use Socket Mode or HTTP webhook).

Flow:
  1. @mention received → parse intent
  2a. needs_clarification → post question, wait for reply
  2b. confidence OK → fetch before_state, post dry-run card, store pending
  3. ✅ reaction on a pending card → re-snapshot, execute, write audit, ack
  ❌ reaction → cancel pending
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
from typing import Any

# Add prototype dir to path
sys.path.insert(0, os.path.dirname(__file__))

from intent_parser import parse_intent
from pending_store import get_by_message_ts, mark_executed, write_pending, list_pending
from audit_log import write_audit, query_audit
from slack_client import (
    post_message, update_message, get_user_info,
    build_confirmation_card, build_clarifying_question_card, build_audit_ack_card,
)
import heygen_cms_api as heygen

CONFIDENCE_THRESHOLD = 0.70
BOT_USER_ID = "U0BDYHHJQTY"   # @HeyChaplinCode
OWNER_SLACK_ID = "U0BBD6002R2"  # yichi.huang — authorized to confirm
REQUEST_TIMEOUT = 30  # seconds — hard timeout on handle_mention

# Simple poll-based dedup: track event IDs we've seen
_SEEN_EVENTS: set[str] = set()
_SEEN_REACTIONS: set[str] = set()


_BOT_TOKEN_CACHE: str | None = None


def _bot_token() -> str:
    global _BOT_TOKEN_CACHE
    if _BOT_TOKEN_CACHE:
        return _BOT_TOKEN_CACHE
    env_path = os.path.expanduser("~/.hermes/.env")
    for line in open(env_path):
        if line.startswith("SLACK_BOT_TOKEN="):
            _BOT_TOKEN_CACHE = line.split("=", 1)[1].strip()
            return _BOT_TOKEN_CACHE
    raise RuntimeError("SLACK_BOT_TOKEN not found")


def _slack_get(method: str, **params) -> dict[str, Any]:
    import urllib.request, urllib.parse
    token = _bot_token()
    qs = urllib.parse.urlencode(params)
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    return json.loads(urllib.request.urlopen(req).read())


def is_authorized(user_id: str) -> bool:
    """Check if a user is authorized to confirm ops actions."""
    # For prototype: only the owner (yichi.huang) is authorized
    return user_id == OWNER_SLACK_ID


def _handle_mention_with_timeout(event: dict) -> None:
    """Run handle_mention in the current thread; post a timeout error if it takes > REQUEST_TIMEOUT s."""
    result = [None]
    exc_box: list[BaseException | None] = [None]

    def _run():
        try:
            handle_mention(event)
        except Exception as e:
            exc_box[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=REQUEST_TIMEOUT)
    if t.is_alive():
        print(f"[TIMEOUT] handle_mention exceeded {REQUEST_TIMEOUT}s for ts={event.get('ts')}")
        try:
            post_message(
                event["channel"],
                f"⏱️ Request timed out after {REQUEST_TIMEOUT}s. Please try again.",
                thread_ts=event["ts"],
            )
        except Exception:
            pass
    elif exc_box[0]:
        print(f"[ERROR] handle_mention: {exc_box[0]}")
        try:
            post_message(event["channel"], f"❌ Error: {exc_box[0]}", thread_ts=event["ts"])
        except Exception:
            pass


def handle_mention(event: dict[str, Any]) -> None:
    """Process an @mention event."""
    text = event.get("text", "")
    user_id = event.get("user", "")
    channel = event.get("channel", "")
    ts = event.get("ts", "")

    # Strip the bot mention prefix
    clean_text = text.replace(f"<@{BOT_USER_ID}>", "").strip()

    if not clean_text:
        return

    print(f"[MENTION] {user_id}: {clean_text}")

    # Raw CLI escape hatch
    if clean_text.startswith("!raw "):
        post_message(channel, "🔧 Raw CLI mode — bypassing NL parse. _(not yet wired to actual CLI in prototype)_", thread_ts=ts)
        return

    # Audit query
    if clean_text.lower().startswith("audit "):
        target_email = clean_text[6:].split()[0]
        rows = query_audit(target_email=target_email, limit=5)
        if not rows:
            post_message(channel, f"No audit records found for `{target_email}`.", thread_ts=ts)
        else:
            lines = [f"*Last {len(rows)} actions for `{target_email}`:*"]
            for r in rows:
                lines.append(f"• `{r['action']}` → `{r['result']}` at {r['ts'][:16]} (audit: `{r['audit_id']}`)")
            post_message(channel, "\n".join(lines), thread_ts=ts)
        return

    # Parse intent
    post_message(channel, "⏳ Parsing...", thread_ts=ts)
    intent = parse_intent(clean_text)
    print(f"[INTENT] {json.dumps(intent, indent=2)}")

    # Clarification needed
    if intent.get("needs_clarification") or intent.get("confidence", 0) < CONFIDENCE_THRESHOLD:
        question = intent.get("clarifying_question") or (
            "I'm not sure I understood that correctly. Could you rephrase with "
            "the target email, action, amount, and duration?"
        )
        blocks = build_clarifying_question_card(question)
        post_message(channel, question, thread_ts=ts, blocks=blocks)
        return

    # ALL actions (including lookup) go through the dry-run card + ✅ confirmation flow
    # so the user always sees what will happen before it executes.
    target_email = intent.get("target_email", "")
    before_state = heygen.get_user_state(target_email)

    # Post the dry-run card
    resp = post_message(
        channel,
        f"Action preview for `{target_email}` — react ✅ to confirm",
        thread_ts=ts,
        blocks=build_confirmation_card(intent, before_state, "pending"),
    )
    card_ts = resp["ts"]

    # Store pending
    pending_id = write_pending(
        actor_slack_id=user_id,
        intent=intent,
        before_state=before_state,
        channel_id=channel,
        thread_ts=ts,
        message_ts=card_ts,
    )

    # Update card with real pending_id
    update_message(
        channel, card_ts,
        f"Action preview for `{target_email}` — react ✅ to confirm",
        blocks=build_confirmation_card(intent, before_state, pending_id),
    )

    print(f"[PENDING] {pending_id} stored, waiting for ✅ on {card_ts}")


def handle_reaction(event: dict[str, Any]) -> None:
    """Process a reaction_added event on a pending confirmation card."""
    reaction = event.get("reaction", "")
    user_id = event.get("user", "")
    item = event.get("item", {})
    channel = item.get("channel", "")
    item_ts = item.get("ts", "")

    if reaction not in ("white_check_mark", "x"):
        return  # only care about ✅ and ❌

    # Dedup
    dedup_key = f"{item_ts}:{reaction}:{user_id}"
    if dedup_key in _SEEN_REACTIONS:
        return
    _SEEN_REACTIONS.add(dedup_key)

    pending = get_by_message_ts(item_ts)
    if not pending:
        return  # not a pending confirmation card (or expired)

    intent = json.loads(pending["intent_json"])
    before_state = json.loads(pending["before_json"])

    print(f"[REACTION] {reaction} from {user_id} on pending {pending['pending_id']}")

    if reaction == "x":
        mark_executed(pending["pending_id"])  # mark cancelled
        post_message(channel, "❌ Cancelled.", thread_ts=pending["thread_ts"])
        return

    # ✅ — check authorization
    if not is_authorized(user_id):
        user_info = get_user_info(user_id)
        name = user_info.get("real_name", user_id)
        post_message(
            channel,
            f"⛔ <@{user_id}> ({name}) is not authorized to confirm ops actions.",
            thread_ts=pending["thread_ts"],
        )
        return

    # TOCTOU: re-snapshot before executing
    target_email = intent.get("target_email", "")
    current_state = heygen.get_user_state(target_email)
    if current_state != before_state:
        post_message(
            channel,
            f"⚠️ State changed since dry-run. Updated preview — react ✅ again to confirm with new state.",
            thread_ts=pending["thread_ts"],
            blocks=build_confirmation_card(intent, current_state, pending["pending_id"]),
        )
        # Update stored before_state
        import sqlite3
        import json as _json
        from pending_store import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE pending_confirmations SET before_json=? WHERE pending_id=?",
            (_json.dumps(current_state), pending["pending_id"]),
        )
        conn.commit()
        conn.close()
        return

    # Execute
    post_message(channel, "⚙️ Executing...", thread_ts=pending["thread_ts"])
    after_state = _execute_intent(intent)

    # Write audit row BEFORE acknowledging
    audit_id = write_audit(
        actor_slack_id=user_id,
        action=intent.get("action", "unknown"),
        result="success",
        intent=intent,
        before_state=before_state,
        after_state=after_state,
        channel_id=channel,
        message_ts=item_ts,
    )
    mark_executed(pending["pending_id"])

    # Ack
    blocks = build_audit_ack_card(audit_id, intent.get("action", ""), target_email, after_state)
    post_message(
        channel,
        f"✅ Done. Audit: `{audit_id}`",
        thread_ts=pending["thread_ts"],
        blocks=blocks,
    )
    # Separate audit trail message (searchable)
    post_message(channel,
        f":white_check_mark: *Audit trail* | `{audit_id}` | `{intent.get('action')}` | `{target_email}` | by <@{user_id}>",
        thread_ts=pending["thread_ts"])
    print(f"[EXECUTED] audit_id={audit_id}")


def _execute_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Execute a validated intent against the mock HeyGen API."""
    action = intent.get("action")
    email = intent.get("target_email", "")

    if action == "lookup":
        return heygen.lookup_user(email)

    elif action == "quota_grant":
        return heygen.execute_quota_grant(
            email=email,
            tier=intent.get("tier"),
            credits=intent.get("credits"),
            duration_days=intent.get("duration_days"),
            product=intent.get("product", "credits"),
        )
    elif action == "create_account":
        return heygen.execute_create_account(
            email=email,
            tier=intent.get("tier", "creator"),
            duration_days=intent.get("duration_days", 30),
        )
    else:
        return {"action": action, "status": "mock_executed"}


def poll_once() -> None:
    """Poll for new events. Used in the prototype polling loop."""
    # Poll mentions
    result = _slack_get("conversations.history", channel=os.environ.get("SLACK_HOME_CHANNEL", "D0BDUSZBB7V"), limit=5)
    for msg in reversed(result.get("messages", [])):
        event_id = msg.get("ts", "")
        if event_id in _SEEN_EVENTS:
            continue
        _SEEN_EVENTS.add(event_id)

        text = msg.get("text", "")
        if f"<@{BOT_USER_ID}>" in text and msg.get("user") != BOT_USER_ID:
            event = {
                "text": text,
                "user": msg.get("user", ""),
                "channel": os.environ.get("SLACK_HOME_CHANNEL", "D0BDUSZBB7V"),
                "ts": msg["ts"],
            }
            t = threading.Thread(target=_handle_mention_with_timeout, args=(event,), daemon=True)
            t.start()

    # Poll reactions on pending messages
    for pending in list_pending():
        resp = _slack_get(
            "reactions.get",
            channel=pending["channel_id"],
            timestamp=pending["message_ts"],
            full=1,
        )
        msg = resp.get("message", {})
        for reaction_info in msg.get("reactions", []):
            reaction = reaction_info.get("name", "")
            for uid in reaction_info.get("users", []):
                handle_reaction({
                    "reaction": reaction,
                    "user": uid,
                    "item": {"channel": pending["channel_id"], "ts": pending["message_ts"]},
                })


if __name__ == "__main__":
    print("🤖 Jarvis prototype starting — polling for @mentions and reactions...")
    print(f"   Bot: {BOT_USER_ID} | Authorized: {OWNER_SLACK_ID}")
    print(f"   Confidence threshold: {CONFIDENCE_THRESHOLD:.0%}")
    print()

    # Seed seen events with existing messages so we don't replay history
    channel = os.environ.get("SLACK_HOME_CHANNEL", "D0BDUSZBB7V")
    result = _slack_get("conversations.history", channel=channel, limit=20)
    for msg in result.get("messages", []):
        _SEEN_EVENTS.add(msg.get("ts", ""))
    print(f"   Seeded {len(_SEEN_EVENTS)} existing events as seen")
    print("   Ready — send me a message like: @HeyChaplinCode comp teodora@heygen.com a creator sub for 365 days with 9999 credits")
    print()

    while True:
        try:
            poll_once()
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(3)
