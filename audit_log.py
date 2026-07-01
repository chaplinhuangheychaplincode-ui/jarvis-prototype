"""
Jarvis Audit Log — SQLite-backed (prototype; production extends enterprise_audit_log_2).

Every action writes one row BEFORE it acknowledges. Before-state, after-state,
NL utterance, parsed intent, confidence score, Slack timestamps.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.path.expanduser("~/.hermes/jarvis_audit.sqlite")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jarvis_audit_log (
            audit_id            TEXT PRIMARY KEY,
            ts                  TEXT NOT NULL,
            actor_slack_id      TEXT NOT NULL,
            actor_email         TEXT,
            action              TEXT NOT NULL,
            target_email        TEXT,
            params_json         TEXT,
            before_json         TEXT,
            after_json          TEXT,
            result              TEXT NOT NULL,
            nl_utterance        TEXT,
            nl_confidence       REAL,
            slack_channel_id    TEXT,
            slack_message_ts    TEXT,
            batch_id            TEXT,
            reason              TEXT
        )
    """)
    # Dedup: same Slack message_ts + action + target can only produce one audit row
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_dedup
            ON jarvis_audit_log(slack_message_ts, action, target_email)
            WHERE slack_message_ts IS NOT NULL
        """)
    except sqlite3.OperationalError:
        pass  # index already exists — safe to ignore
    conn.commit()
    return conn


def write_audit(
    actor_slack_id: str,
    action: str,
    result: str,
    intent: dict[str, Any],
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    channel_id: str | None = None,
    message_ts: str | None = None,
    batch_id: str | None = None,
) -> str:
    """Write an audit row. Returns the audit_id."""
    audit_id = f"jrv_a_{uuid.uuid4().hex[:12]}"
    conn = _conn()
    conn.execute(
        """INSERT OR IGNORE INTO jarvis_audit_log
           (audit_id, ts, actor_slack_id, action, target_email, params_json,
            before_json, after_json, result, nl_utterance, nl_confidence,
            slack_channel_id, slack_message_ts, batch_id, reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            audit_id,
            datetime.now(timezone.utc).isoformat(),
            actor_slack_id,
            action,
            intent.get("target_email"),
            json.dumps(intent),
            json.dumps(before_state) if before_state else None,
            json.dumps(after_state) if after_state else None,
            result,
            intent.get("raw_utterance"),
            intent.get("confidence"),
            channel_id,
            message_ts,
            batch_id,
            intent.get("reason"),
        ),
    )
    conn.commit()
    conn.close()
    return audit_id


def query_audit(target_email: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    conn = _conn()
    if target_email:
        rows = conn.execute(
            "SELECT * FROM jarvis_audit_log WHERE target_email=? ORDER BY ts DESC LIMIT ?",
            (target_email, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jarvis_audit_log ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
