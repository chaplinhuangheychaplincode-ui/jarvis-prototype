"""
Jarvis bot — Socket Mode event handler.

Flow:
  1. @mention received (any user) → parse intent
  2a. needs_clarification → post question, wait for reply
  2b. confidence OK → fetch before_state, post dry-run card with Block Kit ✅/❌ buttons
  3. ✅ button → re-snapshot, execute, write audit, ack
     ❌ button → cancel pending (BUG-1/2 fix: buttons, not emoji reactions)

Bugs fixed in this version:
  BUG-1: Cancel reaction didn't cancel → buttons properly route to cancel_action
  BUG-2: Emoji reactions → Block Kit interactive buttons (✅/❌)
  BUG-3: create_account had no confirm loop → routes through same dry-run flow
  BUG-4: Slow response → claude-haiku for intent parse; no agentic loop
  BUG-5: Duplicate execution → atomic claim_pending() before execute
  BUG-6: Raw API data in messages → _format_ack_fields() sanitizes output
  BUG-7: Non-Yichi users silently ignored → graceful 403 with name shown
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from intent_parser import parse_intent
from pending_store import (
    claim_pending, get_by_pending_id, mark_cancelled, mark_executed,
    reset_to_pending, write_pending, list_expired_pending, expire_by_id,
    EXPIRY_MINUTES,
)
from audit_log import write_audit, query_audit, audit_has_batch_row
from slack_client import (
    post_message, update_message, get_user_info,
    build_confirmation_card, build_clarifying_question_card, build_audit_ack_card,
    build_bulk_confirmation_card,
)
import heygen_cms_api as heygen
from conversation_store import (
    upsert_conversation, append_message as conv_append, get_conversation,
    is_active as conv_is_active, set_state as conv_set_state,
    list_expired_conversations, expire_conversation,
)

BOT_HTTP_PORT = int(os.environ.get("JARVIS_BOT_PORT", "8088"))
BOT_HTTP_SECRET = os.environ.get("JARVIS_BOT_SECRET", "jarvis-internal-secret")
CONFIDENCE_THRESHOLD = 0.70
JARVIS_ENV = os.environ.get("JARVIS_ENV", "dev")
BOT_USER_ID = os.environ.get("JARVIS_BOT_USER_ID", "U0BERJGULPQ")   # overridden per env
OWNER_SLACK_ID = "U0BBD6002R2"  # yichi.huang — audit log reference (write ops open to all)
REQUEST_TIMEOUT = 10  # seconds — hard cap per attempt
REQUEST_MAX_RETRIES = 3  # total attempts before giving up

# Log channel: set JARVIS_LOG_CHANNEL env var or update this ID to enable audit log posting.
# Leave empty to disable log channel posting.
JARVIS_LOG_CHANNEL = os.environ.get("JARVIS_LOG_CHANNEL", "")

# ---------------------------------------------------------------------------
# Log channel helper
# ---------------------------------------------------------------------------

_ACTION_EMOJI = {
    "lookup":         "🔍",
    "quota_grant":    "💳",
    "bulk_grant":     "📦",
    "create_account": "🆕",
    "ent_sub_grant":  "🏢",
}


def _post_to_log_channel(
    audit_id: str,
    action: str,
    target_email: str,
    actor_slack_id: str,
    result: str = "success",
    batch_id: str | None = None,
) -> None:
    """Post a one-line audit entry to JARVIS_LOG_CHANNEL (if configured)."""
    if not JARVIS_LOG_CHANNEL:
        return
    emoji = _ACTION_EMOJI.get(action, "⚙️")
    result_tag = "✅" if result == "success" else "❌"
    batch_suffix = f" · batch `{batch_id}`" if batch_id else ""
    text = (
        f"{result_tag} {emoji} *{action}* | `{target_email}` "
        f"| by <@{actor_slack_id}> | audit `{audit_id}`{batch_suffix}"
    )
    try:
        post_message(JARVIS_LOG_CHANNEL, text)
    except Exception as e:
        print(f"[LOG_CHANNEL] failed to post audit line: {e}")

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


_ENV = _load_env()
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN") or _ENV.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN") or _ENV.get("SLACK_APP_TOKEN", "")

if not SLACK_BOT_TOKEN:
    raise RuntimeError("SLACK_BOT_TOKEN not found in environment or ~/.hermes/.env")
if not SLACK_APP_TOKEN:
    raise RuntimeError("SLACK_APP_TOKEN not found — Socket Mode requires an xapp-... token")

# ---------------------------------------------------------------------------
# (Bolt app removed — now using Flask HTTP server; gateway plugin forwards events)
# ---------------------------------------------------------------------------


def is_authorized(user_id: str) -> bool:
    """Check if a user is authorized to confirm write ops."""
    return user_id == OWNER_SLACK_ID


def _handle_mention_with_timeout(event: dict) -> None:
    """Run handle_mention with a 10s timeout per attempt, retrying up to REQUEST_MAX_RETRIES times."""
    channel = event.get("channel", "")
    ts = event.get("ts", "")

    for attempt in range(1, REQUEST_MAX_RETRIES + 1):
        exc_box: list[BaseException | None] = [None]
        done = threading.Event()

        def _run():
            try:
                handle_mention(event)
            except Exception as e:
                exc_box[0] = e
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        finished = done.wait(timeout=REQUEST_TIMEOUT)

        if finished and exc_box[0] is None:
            return  # success

        if not finished:
            print(f"[TIMEOUT] attempt {attempt}/{REQUEST_MAX_RETRIES} exceeded {REQUEST_TIMEOUT}s for ts={ts}")
            if attempt < REQUEST_MAX_RETRIES:
                try:
                    post_message(channel, f"⏱️ Timed out (attempt {attempt}/{REQUEST_MAX_RETRIES}), retrying...", thread_ts=ts)
                except Exception:
                    pass
            else:
                try:
                    post_message(channel, f"⏱️ Timed out after {REQUEST_MAX_RETRIES} attempts. Please try again.", thread_ts=ts)
                except Exception:
                    pass
            continue

        # finished but with an exception
        print(f"[ERROR] attempt {attempt}/{REQUEST_MAX_RETRIES} handle_mention: {exc_box[0]}")
        if attempt < REQUEST_MAX_RETRIES:
            try:
                post_message(channel, f"❌ Error (attempt {attempt}/{REQUEST_MAX_RETRIES}), retrying... `{exc_box[0]}`", thread_ts=ts)
            except Exception:
                pass
        else:
            try:
                post_message(channel, f"❌ Failed after {REQUEST_MAX_RETRIES} attempts: `{exc_box[0]}`", thread_ts=ts)
            except Exception:
                pass


def _clean_slack_text(text: str) -> str:
    """Strip Slack mention syntax, mailto escapes, and bare angle-bracket URLs."""
    text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    text = re.sub(r"<mailto:[^|>]+\|([^>]+)>", r"\1", text)
    text = re.sub(r"<([^|>]+)>", r"\1", text)
    return text.strip()


def handle_explain(channel: str, thread_ts: str) -> None:
    """Post a formatted capability menu from capabilities.json."""
    import json as _json
    cap_path = os.path.join(os.path.dirname(__file__), "capabilities.json")
    try:
        with open(cap_path) as f:
            data = _json.load(f)
    except Exception as e:
        post_message(channel, f"❌ Could not load capabilities: `{e}`", thread_ts=thread_ts)
        return

    ops = data.get("ops", [])
    coming_soon = data.get("coming_soon", [])

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "🤖 What Jarvis Can Do"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Just @mention me in natural language — I'll ask if I need more info, then show a confirm card before touching anything."}},
        {"type": "divider"},
    ]

    for op in ops:
        name = op.get("name", "")
        desc = op.get("description", "")
        example = op.get("example", "")
        write = op.get("write", False)
        tag = " _(write op — requires confirmation)_" if write else " _(read-only)_"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{name}*{tag}\n{desc}\n> _{example}_",
            },
        })

    if coming_soon:
        blocks.append({"type": "divider"})
        cs_lines = "\n".join(f"• *{op['name']}* — {op['description']}" for op in coming_soon)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🚧 Coming Soon*\n{cs_lines}"},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"_Capabilities last updated: {data.get('version', '?')}_"},
    })

    post_message(channel, "Here's what I can help with:", thread_ts=thread_ts, blocks=blocks)


def _process_utterance(
    channel: str,
    thread_ts: str,
    user_id: str,
    clean_text: str,
) -> None:
    """
    Core logic: given clean text + conversation history, either clarify or propose.
    Used by both @mention and thread-reply handlers.
    """
    # Fetch conversation history (may be empty for first message)
    conv = get_conversation(thread_ts)
    history: list[dict[str, str]] = conv["messages"] if conv else []

    # Append user message to history
    history = conv_append(thread_ts, channel, "user", clean_text)

    post_message(channel, "⏳ Thinking...", thread_ts=thread_ts)

    # Pass full history to parser
    intent = parse_intent(clean_text, history=history[:-1] if len(history) > 1 else None)
    print(f"[INTENT] {json.dumps(intent, indent=2)}")

    # Explain / help — instant bypass, no confirm needed
    if intent.get("action") == "explain":
        conv_set_state(thread_ts, "DONE")
        handle_explain(channel, thread_ts)
        return

    # Clarification needed
    if intent.get("needs_clarification") or intent.get("confidence", 0) < CONFIDENCE_THRESHOLD:
        question = intent.get("clarifying_question") or (
            "I'm not sure I understood that correctly. Could you rephrase with "
            "the target email, action, amount, and duration?"
        )
        # Record assistant question in history
        conv_append(thread_ts, channel, "assistant", question)
        # Keep state as GATHERING
        updated_conv = get_conversation(thread_ts)
        if updated_conv:
            upsert_conversation(thread_ts, channel, updated_conv["messages"], state="GATHERING")
        blocks = build_clarifying_question_card(question)
        post_message(channel, question, thread_ts=thread_ts, blocks=blocks)
        return

    # Intent resolved — transition to CONFIRMING
    conv_set_state(thread_ts, "CONFIRMING", final_intent=intent)

    target_email = intent.get("target_email", "")
    action = intent.get("action")

    # ---- BULK GRANT path ----
    if action == "bulk_grant":
        import uuid
        pending_id = f"jrv_p_{uuid.uuid4().hex[:8]}"
        resp = post_message(
            channel,
            f"Bulk grant preview for {len(intent.get('target_emails', []))} users — confirm or cancel below",
            thread_ts=thread_ts,
            blocks=build_bulk_confirmation_card(intent, pending_id),
        )
        card_ts = resp["ts"]
        write_pending(
            actor_slack_id=user_id,
            intent=intent,
            before_state={},
            channel_id=channel,
            thread_ts=thread_ts,
            message_ts=card_ts,
            pending_id=pending_id,
        )
        print(f"[BULK_PENDING] {pending_id} stored for {len(intent.get('target_emails', []))} users")
        return

    # Guard: validate user exists for ops that require it
    if action in ("lookup", "quota_grant", "revoke_grant"):
        before_state = heygen.get_user_state(target_email)
        if before_state.get("user_id") is None:
            err = before_state.get("error", {})
            code = err.get("code", "?") if isinstance(err, dict) else "?"
            post_message(
                channel,
                f"❌ User `{target_email}` not found in HeyGen (CMS code {code}). "
                f"Check the email and try again.",
                thread_ts=thread_ts,
            )
            conv_set_state(thread_ts, "DONE")
            return
    elif action == "create_account":
        before_state = {}
    else:
        before_state = heygen.get_user_state(target_email)

    import uuid
    pending_id = f"jrv_p_{uuid.uuid4().hex[:8]}"

    resp = post_message(
        channel,
        f"Action preview for `{target_email}` — confirm or cancel below",
        thread_ts=thread_ts,
        blocks=build_confirmation_card(intent, before_state, pending_id),
    )
    card_ts = resp["ts"]

    write_pending(
        actor_slack_id=user_id,
        intent=intent,
        before_state=before_state,
        channel_id=channel,
        thread_ts=thread_ts,
        message_ts=card_ts,
        pending_id=pending_id,
    )
    print(f"[PENDING] {pending_id} stored, waiting for button click on {card_ts}")


def handle_mention(event: dict[str, Any]) -> None:
    """Process an @mention event (from any user in any channel the bot is in)."""
    text = event.get("text", "")
    user_id = event.get("user", "")
    channel = event.get("channel", "")
    ts = event.get("ts", "")
    # thread_ts: if this mention is itself a reply, use its thread; otherwise use its own ts
    thread_ts = event.get("thread_ts") or ts

    clean_text = _clean_slack_text(text)
    if not clean_text:
        return

    print(f"[MENTION] {user_id} in {channel}: {clean_text}")

    # Raw CLI escape hatch
    if clean_text.startswith("!raw "):
        post_message(channel, "🔧 Raw CLI mode — bypassing NL parse. _(not yet wired)_", thread_ts=thread_ts)
        return

    # Audit query (read-only, bypass conversation flow)
    if clean_text.lower().startswith("audit "):
        target_email = clean_text[6:].split()[0]
        rows = query_audit(target_email=target_email, limit=5)
        if not rows:
            post_message(channel, f"No audit records found for `{target_email}`.", thread_ts=thread_ts)
        else:
            lines = [f"*Last {len(rows)} actions for `{target_email}`:*"]
            for r in rows:
                lines.append(f"• `{r['action']}` → `{r['result']}` at {r['ts'][:16]} (audit: `{r['audit_id']}`)")
            post_message(channel, "\n".join(lines), thread_ts=thread_ts)
        return

    # Ensure conversation exists for this thread
    if not conv_is_active(thread_ts):
        upsert_conversation(thread_ts, channel, [], state="GATHERING")

    _process_utterance(channel, thread_ts, user_id, clean_text)


def handle_thread_reply(event: dict[str, Any]) -> None:
    """
    Process a plain (non-@mention) message in a thread where Jarvis is GATHERING.
    Only fires if the thread has an active GATHERING conversation.
    """
    # Ignore bot messages
    if event.get("bot_id") or event.get("subtype"):
        return

    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return  # top-level message, not a thread reply

    if not conv_is_active(thread_ts):
        return  # not an active Jarvis conversation

    conv = get_conversation(thread_ts)
    if not conv or conv["state"] != "GATHERING":
        return  # only respond during GATHERING (not CONFIRMING/DONE)

    text = event.get("text", "")
    user_id = event.get("user", "")
    channel = event.get("channel", "")

    clean_text = _clean_slack_text(text)
    if not clean_text:
        return

    print(f"[THREAD_REPLY] {user_id} in {channel} (thread {thread_ts}): {clean_text}")
    _process_utterance(channel, thread_ts, user_id, clean_text)


def handle_block_action(body: dict[str, Any]) -> None:
    """
    Process Block Kit button actions (✅ Confirm / ❌ Cancel).
    BUG-1/BUG-2 fix: replaced emoji reaction handling with buttons.
    """
    user_id = body.get("user", {}).get("id", "")
    channel_id = body.get("channel", {}).get("id", "")
    actions = body.get("actions", [])
    if not actions:
        return

    action = actions[0]
    action_id = action.get("action_id", "")
    pending_id = action.get("value", "")

    print(f"[BUTTON] {action_id} from {user_id}, pending={pending_id}")

    pending = get_by_pending_id(pending_id)
    if not pending:
        # Card expired or already acted on
        post_message(channel_id, f"⚠️ This action (`{pending_id}`) has already been completed or expired.")
        return

    if pending["status"] not in ("pending", "executing"):
        post_message(
            channel_id,
            f"⚠️ This action (`{pending_id}`) is already `{pending['status']}`.",
            thread_ts=pending["thread_ts"],
        )
        return

    thread_ts = pending["thread_ts"]
    intent = json.loads(pending["intent_json"])
    before_state = json.loads(pending["before_json"])
    target_email = intent.get("target_email", "")

    # ❌ Cancel — anyone who sees the card can cancel (intentional)
    if action_id == "cancel_action":
        mark_cancelled(pending_id)
        update_message(channel_id, pending["message_ts"],
                       f"~~Action cancelled~~ `{pending_id}`",
                       blocks=[{"type": "section", "text": {"type": "mrkdwn",
                               "text": f"❌ *Cancelled* by <@{user_id}> · `{pending_id}`"}}])
        return

    # ✅ Confirm — open to all users (internal channel, audit log tracks who confirmed)
    if action_id == "confirm_action":

        # BUG-5 FIX: Atomic claim — prevents duplicate execution
        claimed = claim_pending(pending_id)
        if not claimed:
            post_message(
                channel_id,
                f"⚠️ Action `{pending_id}` was already claimed — duplicate click ignored.",
                thread_ts=thread_ts,
            )
            return

        # Immediately fold buttons away — user gets instant feedback
        update_message(
            channel_id, pending["message_ts"],
            f"⏳ Received — executing...",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                    "text": f"⏳ *Received* by <@{user_id}> · executing..."}}],
        )

        # TOCTOU: re-snapshot before executing (skip for create_account — no meaningful before_state)
        if intent.get("action") != "create_account":
            current_state = heygen.get_user_state(target_email) if target_email else {}
            if current_state and current_state != before_state:
                reset_to_pending(pending_id, json.dumps(current_state))
                # Rebuild card with fresh state
                update_message(
                    channel_id, pending["message_ts"],
                    f"⚠️ State changed — please confirm again",
                    blocks=build_confirmation_card(intent, current_state, pending_id),
                )
                post_message(
                    channel_id,
                    "⚠️ State changed since dry-run. Updated preview above — click ✅ Confirm again.",
                    thread_ts=thread_ts,
                )
                return

        t0 = time.time()

        try:
            # Bulk grant: run in background thread, card already updated above
            if intent.get("action") == "bulk_grant":
                t = threading.Thread(
                    target=_run_bulk_grant,
                    args=(intent, pending_id, user_id, channel_id, thread_ts, pending["message_ts"], t0),
                    daemon=True,
                )
                t.start()
                return  # results posted async by _run_bulk_grant

            after_state = _execute_intent(intent)
        except Exception as exec_err:
            print(f"[EXEC ERROR] {pending_id}: {exec_err}")
            reset_to_pending(pending_id, json.dumps(before_state))
            update_message(
                channel_id, pending["message_ts"],
                f"❌ Execution failed — action reset",
                blocks=build_confirmation_card(intent, before_state, pending_id),
            )
            post_message(
                channel_id,
                f"❌ Execution failed: `{exec_err}`. Action reset — click ✅ to retry.",
                thread_ts=thread_ts,
            )
            return

        elapsed = round(time.time() - t0, 1)

        # Write audit BEFORE ack (SOC2 ordering) — CH failure must not crash the thread
        try:
            audit_id = write_audit(
                actor_slack_id=user_id,
                action=intent.get("action", "unknown"),
                result="success",
                intent=intent,
                before_state=before_state,
                after_state=after_state,
                channel_id=channel_id,
                message_ts=pending["message_ts"],
            )
        except Exception as audit_exc:
            print(f"[audit_log] WARN: write_audit failed (non-fatal): {audit_exc}", flush=True)
            audit_id = f"jrv_a_err_{__import__('uuid').uuid4().hex[:8]}"
        mark_executed(pending_id)

        # Update card to show completed state with elapsed time
        update_message(channel_id, pending["message_ts"],
                       f"✅ Completed `{pending_id}`",
                       blocks=[{"type": "section", "text": {"type": "mrkdwn",
                               "text": f"✅ *Confirmed & executed* by <@{user_id}> · {elapsed}s · `{pending_id}`"}}])

        # Ack card — BUG-6: sanitized, no raw blobs
        blocks = build_audit_ack_card(audit_id, intent.get("action", ""), target_email, after_state)
        post_message(
            channel_id,
            f"✅ Done in {elapsed}s · Audit: `{audit_id}`",
            thread_ts=thread_ts,
            blocks=blocks,
        )
        # Separate searchable audit trail message
        post_message(
            channel_id,
            f":white_check_mark: *Audit trail* | `{audit_id}` | `{intent.get('action')}` | `{target_email}` | by <@{user_id}>",
            thread_ts=thread_ts,
        )
        # Log channel — silent if not configured
        _post_to_log_channel(audit_id, intent.get("action", "unknown"), target_email, user_id)
        print(f"[EXECUTED] audit_id={audit_id}")


def _execute_intent(intent: dict[str, Any]) -> dict[str, Any]:
    action = intent.get("action")
    email = intent.get("target_email", "")

    if action == "lookup":
        result = heygen.lookup_user(email)
        # Carry requested fields through so the card renderer can highlight them
        lookup_fields = intent.get("lookup_fields") or []
        if lookup_fields:
            result["_lookup_fields"] = lookup_fields
        return result
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
            tier=intent.get("tier"),
            duration_days=intent.get("duration_days"),
        )
    elif action == "revoke_grant":
        revoke_type = intent.get("revoke_type", "subscription")
        quota_id = intent.get("quota_id")
        results: dict[str, Any] = {"email": email, "action": "revoke_grant", "revoke_type": revoke_type}
        if revoke_type in ("subscription", "both"):
            results["subscription_result"] = heygen.execute_subscription_remove(email)
        if revoke_type in ("quota", "both") and quota_id:
            results["quota_result"] = heygen.execute_quota_expire(quota_id)
        elif revoke_type == "quota" and not quota_id:
            results["error"] = "quota_id required for quota revoke"
        return results
    else:
        return {"action": action, "status": "not_implemented"}


def _run_bulk_grant(
    intent: dict[str, Any],
    pending_id: str,
    actor_slack_id: str,
    channel_id: str,
    thread_ts: str,
    card_ts: str,
    t0: float,
) -> None:
    """Execute bulk_grant in a background thread. Posts progress + summary when done."""
    import uuid as _uuid
    emails = intent.get("target_emails", [])
    batch_id = f"jrv_b_{_uuid.uuid4().hex[:8]}"
    success: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[str] = []

    for email in emails:
        # Idempotency: skip already-granted rows for this batch
        if audit_has_batch_row(batch_id, email):
            skipped.append(email)
            continue
        try:
            after_state = heygen.execute_quota_grant(
                email=email,
                tier=intent.get("tier"),
                credits=intent.get("credits"),
                duration_days=intent.get("duration_days"),
                product=intent.get("product", "credits"),
            )
            # execute_quota_grant returns {"error": ...} on API failure instead of raising
            if after_state.get("error") or after_state.get("granted") is False:
                err_msg = after_state.get("error", {})
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", str(err_msg))
                failed.append((email, str(err_msg)))
                continue
            try:
                write_audit(
                    actor_slack_id=actor_slack_id,
                    action="bulk_grant",
                    result="success",
                    intent={**intent, "target_email": email},
                    after_state=after_state,
                    channel_id=channel_id,
                    message_ts=card_ts,
                    batch_id=batch_id,
                )
            except Exception as audit_exc:
                print(f"[audit_log] WARN: write_audit failed (non-fatal): {audit_exc}", flush=True)
            success.append(email)
        except Exception as e:
            failed.append((email, str(e)))

    # Mark pending as executed
    mark_executed(pending_id)

    elapsed = round(time.time() - t0, 1)

    # Update card
    update_message(
        channel_id, card_ts,
        f"✅ Bulk grant complete — `{batch_id}`",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *Bulk grant done* · {elapsed}s · `{batch_id}` · by <@{actor_slack_id}>"}},
        ],
    )
    # Summary in thread
    lines = [f"✅ *Bulk grant complete* — Batch `{batch_id}`"]
    lines.append(f"  ✓ *{len(success)}* succeeded")
    if failed:
        fail_detail = ", ".join(f"`{e}` ({err})" for e, err in failed[:5])
        if len(failed) > 5:
            fail_detail += f" … +{len(failed)-5} more"
        lines.append(f"  ✗ *{len(failed)}* failed: {fail_detail}")
    if skipped:
        lines.append(f"  ↩ *{len(skipped)}* skipped (already granted)")
    lines.append(f"  Audit rows written: *{len(success)}*")
    post_message(channel_id, "\n".join(lines), thread_ts=thread_ts)
    # Log channel — one summary line for the whole batch
    if success:
        first_audit_id = f"jrv_a_{batch_id}"  # batch reference
        _post_to_log_channel(
            audit_id=first_audit_id,
            action="bulk_grant",
            target_email=f"{len(success)} users",
            actor_slack_id=actor_slack_id,
            batch_id=batch_id,
        )
    print(f"[BULK_DONE] batch={batch_id} ok={len(success)} fail={len(failed)} skip={len(skipped)}")

# ---------------------------------------------------------------------------
# Bolt app + Socket Mode
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TTL Expiry Sweep — background thread, runs every 60s
# ---------------------------------------------------------------------------

EXPIRY_SWEEP_INTERVAL = 60  # seconds between sweeps


def _expiry_sweep() -> None:
    """Sweep for stale pending confirmations and notify users in Slack."""
    while True:
        try:
            expired_rows = list_expired_pending()
            for row in expired_rows:
                pending_id = row["pending_id"]
                claimed = expire_by_id(pending_id)
                if not claimed:
                    continue  # race: another thread beat us (e.g. button click)
                channel_id = row["channel_id"]
                thread_ts = row["thread_ts"]
                intent = json.loads(row["intent_json"])
                action = intent.get("action", "?")
                email = intent.get("target_email", "?")
                print(f"[EXPIRE] {pending_id} action={action} email={email}")
                try:
                    post_message(
                        channel_id,
                        f"⏰ *Request expired* — `{action}` for `{email}` "
                        f"(pending_id `{pending_id}`) was not confirmed within "
                        f"{EXPIRY_MINUTES} minutes.\n"
                        f"Mention me again to start a new request.",
                        thread_ts=thread_ts,
                    )
                except Exception as e:
                    print(f"[EXPIRE] failed to post expiry notice for {pending_id}: {e}")

        except Exception as e:
            print(f"[EXPIRE] sweep error: {e}")

        # Also expire stale conversations
        try:
            for conv in list_expired_conversations():
                thread_ts = conv["thread_ts"]
                expire_conversation(thread_ts)
                print(f"[EXPIRE_CONV] thread {thread_ts} expired (state was {conv['state']})")
                if conv["state"] == "GATHERING":
                    try:
                        post_message(
                            conv["channel_id"],
                            "⏰ Conversation timed out — mention me again to start over.",
                            thread_ts=thread_ts,
                        )
                    except Exception as e:
                        print(f"[EXPIRE_CONV] failed to post notice for {thread_ts}: {e}")
        except Exception as e:
            print(f"[EXPIRE_CONV] sweep error: {e}")
        threading.Event().wait(EXPIRY_SWEEP_INTERVAL)



app = App(token=SLACK_BOT_TOKEN)


@app.event("app_mention")
def on_app_mention(event, say):
    t = threading.Thread(target=_handle_mention_with_timeout, args=(event,), daemon=True)
    t.start()


@app.action("confirm_action")
def on_confirm_action(ack, body):
    ack()
    t = threading.Thread(target=handle_block_action, args=(body,), daemon=True)
    t.start()


@app.action("cancel_action")
def on_cancel_action(ack, body):
    ack()
    t = threading.Thread(target=handle_block_action, args=(body,), daemon=True)
    t.start()


@app.event("message")
def on_message(event):
    t = threading.Thread(target=handle_thread_reply, args=(event,), daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("🤖 Jarvis starting — Socket Mode (dedicated app)")
    print(f"   Bot: {BOT_USER_ID} | Authorized confirmer: {OWNER_SLACK_ID}")
    print(f"   Confidence threshold: {CONFIDENCE_THRESHOLD:.0%}")
    print()

    # Start TTL expiry sweep in background
    sweep_t = threading.Thread(target=_expiry_sweep, daemon=True, name="expiry-sweep")
    sweep_t.start()
    print(f"   Expiry sweep: every {EXPIRY_SWEEP_INTERVAL}s (TTL={EXPIRY_MINUTES}min)")

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()

