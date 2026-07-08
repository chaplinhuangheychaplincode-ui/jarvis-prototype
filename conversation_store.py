"""
Jarvis Conversation Store — SQLite-backed per-thread conversation state.

States:
  GATHERING  — collecting info, asking clarifying questions
  PLANNING   — workflow plan shown, waiting for Confirm/Revise/thread replies
  EXECUTING  — plan confirmed, steps running
  DONE       — all steps executed or cancelled
  EXPIRED    — TTL elapsed

New columns vs original:
  current_plan  — live workflow being refined (JSON)
  final_plan    — confirmed workflow (JSON, set on Confirm)
  plan_card_ts  — Slack message ts of the plan card (for updates)
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

_env = os.environ.get("JARVIS_ENV", "dev")
_suffix = "_prod" if _env == "prod" else ""
DB_PATH = os.path.expanduser(f"~/.hermes/jarvis_conversations{_suffix}.sqlite")
EXPIRY_MINUTES = 15

STATES = ("GATHERING", "PLANNING", "EXECUTING", "DONE", "EXPIRED")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            thread_ts       TEXT PRIMARY KEY,
            channel_id      TEXT NOT NULL,
            state           TEXT NOT NULL DEFAULT 'GATHERING',
            messages_json   TEXT NOT NULL DEFAULT '[]',
            final_intent    TEXT,
            current_plan    TEXT,
            final_plan      TEXT,
            plan_card_ts    TEXT,
            created_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL
        )
    """)
    # Migrate existing DBs that don't have new columns yet
    for col, typedef in [
        ("current_plan", "TEXT"),
        ("final_plan", "TEXT"),
        ("plan_card_ts", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # already exists
    # Allow PLANNING in expiry sweep
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def upsert_conversation(
    thread_ts: str,
    channel_id: str,
    messages: list[dict[str, Any]],
    state: str = "GATHERING",
    final_intent: dict[str, Any] | None = None,
    current_plan: dict[str, Any] | None = None,
    final_plan: dict[str, Any] | None = None,
    plan_card_ts: str | None = None,
) -> None:
    """Create or update a conversation row, refreshing TTL."""
    now = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=EXPIRY_MINUTES)).isoformat()
    conn = _conn()
    conn.execute("""
        INSERT INTO conversations
            (thread_ts, channel_id, state, messages_json, final_intent,
             current_plan, final_plan, plan_card_ts, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_ts) DO UPDATE SET
            state         = excluded.state,
            messages_json = excluded.messages_json,
            final_intent  = excluded.final_intent,
            current_plan  = excluded.current_plan,
            final_plan    = excluded.final_plan,
            plan_card_ts  = COALESCE(excluded.plan_card_ts, conversations.plan_card_ts),
            expires_at    = excluded.expires_at
    """, (
        thread_ts, channel_id, state,
        json.dumps(messages),
        json.dumps(final_intent) if final_intent is not None else None,
        json.dumps(current_plan) if current_plan is not None else None,
        json.dumps(final_plan) if final_plan is not None else None,
        plan_card_ts,
        now, expires,
    ))
    conn.commit()
    conn.close()


def append_message(
    thread_ts: str,
    channel_id: str,
    role: str,
    text: str,
    ts: str | None = None,
) -> list[dict[str, Any]]:
    """Append a message to an existing conversation. Returns updated messages list."""
    conv = get_conversation(thread_ts)
    messages: list[dict[str, Any]] = conv["messages"] if conv else []
    messages.append({"role": role, "text": text, "ts": ts or ""})
    state = conv["state"] if conv else "GATHERING"
    upsert_conversation(thread_ts, channel_id, messages, state=state,
                        current_plan=conv.get("current_plan") if conv else None)
    return messages


def set_state(thread_ts: str, state: str, final_intent: dict[str, Any] | None = None) -> None:
    """Update conversation state, preserving other fields."""
    conv = get_conversation(thread_ts)
    if not conv:
        return
    upsert_conversation(
        thread_ts, conv["channel_id"], conv["messages"],
        state=state,
        final_intent=final_intent if final_intent is not None else conv.get("final_intent"),
        current_plan=conv.get("current_plan"),
        final_plan=conv.get("final_plan"),
    )


def set_plan(
    thread_ts: str,
    plan: dict[str, Any],
    card_ts: str | None = None,
    state: str = "PLANNING",
) -> None:
    """Store the current workflow plan and transition to PLANNING."""
    conv = get_conversation(thread_ts)
    if not conv:
        return
    upsert_conversation(
        thread_ts, conv["channel_id"], conv["messages"],
        state=state,
        final_intent=conv.get("final_intent"),
        current_plan=plan,
        final_plan=conv.get("final_plan"),
        plan_card_ts=card_ts or conv.get("plan_card_ts"),
    )


def confirm_plan(thread_ts: str) -> None:
    """Move current_plan → final_plan, transition to EXECUTING."""
    conv = get_conversation(thread_ts)
    if not conv:
        return
    upsert_conversation(
        thread_ts, conv["channel_id"], conv["messages"],
        state="EXECUTING",
        final_intent=conv.get("final_intent"),
        current_plan=conv.get("current_plan"),
        final_plan=conv.get("current_plan"),  # freeze
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_conversation(thread_ts: str) -> dict[str, Any] | None:
    """Return the conversation row, or None if not found."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM conversations WHERE thread_ts=?", (thread_ts,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["messages"] = json.loads(d["messages_json"])
    d["final_intent"] = json.loads(d["final_intent"]) if d.get("final_intent") else None
    d["current_plan"] = json.loads(d["current_plan"]) if d.get("current_plan") else None
    d["final_plan"] = json.loads(d["final_plan"]) if d.get("final_plan") else None
    return d


def is_active(thread_ts: str) -> bool:
    """True if thread has an active conversation (not DONE/EXPIRED)."""
    conv = get_conversation(thread_ts)
    if not conv:
        return False
    if conv["state"] not in ("GATHERING", "PLANNING", "EXECUTING"):
        return False
    now = datetime.now(timezone.utc).isoformat()
    return conv["expires_at"] > now


# ---------------------------------------------------------------------------
# Expiry sweep
# ---------------------------------------------------------------------------

def list_expired_conversations() -> list[dict[str, Any]]:
    """Return active rows that have passed expires_at."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute("""
        SELECT * FROM conversations
        WHERE state IN ('GATHERING', 'PLANNING', 'EXECUTING') AND expires_at <= ?
    """, (now,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        d["messages"] = json.loads(d["messages_json"])
        d["final_intent"] = json.loads(d["final_intent"]) if d.get("final_intent") else None
        d["current_plan"] = json.loads(d["current_plan"]) if d.get("current_plan") else None
        result.append(d)
    return result


def expire_conversation(thread_ts: str) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE conversations SET state='EXPIRED' WHERE thread_ts=? AND state IN ('GATHERING','PLANNING','EXECUTING')",
        (thread_ts,)
    )
    conn.commit()
    conn.close()
