"""
Jarvis Conversation Store — SQLite-backed per-thread conversation state.

Each row tracks one active conversation thread:
  - GATHERING: Jarvis is asking clarifying questions, collecting context
  - CONFIRMING: intent is fully resolved, confirm card is shown
  - DONE: executed or cancelled
  - EXPIRED: TTL elapsed without resolution

TTL is 15 minutes (shared with pending_store.EXPIRY_MINUTES).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

DB_PATH = os.path.expanduser("~/.hermes/jarvis_conversations.sqlite")
EXPIRY_MINUTES = 15

STATES = ("GATHERING", "CONFIRMING", "DONE", "EXPIRED")


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
            created_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL
        )
    """)
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
) -> None:
    """Create or update a conversation row, refreshing TTL."""
    now = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=EXPIRY_MINUTES)).isoformat()
    conn = _conn()
    conn.execute("""
        INSERT INTO conversations
            (thread_ts, channel_id, state, messages_json, final_intent, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_ts) DO UPDATE SET
            state         = excluded.state,
            messages_json = excluded.messages_json,
            final_intent  = excluded.final_intent,
            expires_at    = excluded.expires_at
    """, (
        thread_ts, channel_id, state,
        json.dumps(messages),
        json.dumps(final_intent) if final_intent else None,
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
    """
    Append a message to an existing conversation (or create it).
    Returns updated messages list.
    """
    conv = get_conversation(thread_ts)
    messages: list[dict[str, Any]] = conv["messages"] if conv else []
    messages.append({"role": role, "text": text, "ts": ts or ""})
    state = conv["state"] if conv else "GATHERING"
    upsert_conversation(thread_ts, channel_id, messages, state=state)
    return messages


def set_state(thread_ts: str, state: str, final_intent: dict[str, Any] | None = None) -> None:
    """Update conversation state."""
    conv = get_conversation(thread_ts)
    if not conv:
        return
    upsert_conversation(
        thread_ts, conv["channel_id"], conv["messages"],
        state=state, final_intent=final_intent if final_intent is not None else conv.get("final_intent"),
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_conversation(thread_ts: str) -> dict[str, Any] | None:
    """Return the conversation row, or None if not found / expired."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM conversations WHERE thread_ts=?", (thread_ts,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["messages"] = json.loads(d["messages_json"])
    d["final_intent"] = json.loads(d["final_intent"]) if d["final_intent"] else None
    return d


def is_active(thread_ts: str) -> bool:
    """True if the thread has an active (GATHERING or CONFIRMING) conversation."""
    conv = get_conversation(thread_ts)
    if not conv:
        return False
    if conv["state"] not in ("GATHERING", "CONFIRMING"):
        return False
    now = datetime.now(timezone.utc).isoformat()
    return conv["expires_at"] > now


# ---------------------------------------------------------------------------
# Expiry sweep
# ---------------------------------------------------------------------------

def list_expired_conversations() -> list[dict[str, Any]]:
    """Return GATHERING/CONFIRMING rows that have passed expires_at."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute("""
        SELECT * FROM conversations
        WHERE state IN ('GATHERING', 'CONFIRMING') AND expires_at <= ?
    """, (now,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        d["messages"] = json.loads(d["messages_json"])
        d["final_intent"] = json.loads(d["final_intent"]) if d["final_intent"] else None
        result.append(d)
    return result


def expire_conversation(thread_ts: str) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE conversations SET state='EXPIRED' WHERE thread_ts=? AND state IN ('GATHERING','CONFIRMING')",
        (thread_ts,)
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    # Smoke test
    upsert_conversation("1234.5678", "C123", [], state="GATHERING")
    msgs = append_message("1234.5678", "C123", "user", "give alice@x.com 500 credits")
    msgs = append_message("1234.5678", "C123", "assistant", "What tier?")
    msgs = append_message("1234.5678", "C123", "user", "creator")
    conv = get_conversation("1234.5678")
    print(f"state={conv['state']} messages={len(conv['messages'])}")
    print(f"is_active={is_active('1234.5678')}")
    set_state("1234.5678", "DONE")
    print(f"is_active after DONE={is_active('1234.5678')}")
