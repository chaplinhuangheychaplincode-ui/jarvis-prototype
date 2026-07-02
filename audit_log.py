"""
Jarvis Audit Log — ClickHouse-backed.

Every action writes one row BEFORE it acknowledges. Before-state, after-state,
NL utterance, parsed intent, confidence score, Slack timestamps.

Writes go to ClickHouse (heygen_analytics.jarvis_audit_log) with exponential
backoff on transient failures. No SQLite fallback — if CH is unreachable after
retries, an exception is raised and surfaced to the caller.

Pending confirmations remain in SQLite (pending_store.py) — mutable short-lived
state is a poor fit for ClickHouse's append-only model.
# TODO: deprecate SQLite pending_store → MySQL when bot goes to production
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import clickhouse_connect

# ---------------------------------------------------------------------------
# ClickHouse connection
# ---------------------------------------------------------------------------

_CH_CLIENT: Any = None
_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 1  # doubles each attempt: 1s, 2s, 4s


def _secret(name: str) -> str:
    return subprocess.run(
        ["python3", "/opt/genesis/manage-secrets.py", "get", name],
        capture_output=True, text=True,
    ).stdout.strip()


def _get_client() -> Any:
    global _CH_CLIENT
    if _CH_CLIENT is None:
        _CH_CLIENT = clickhouse_connect.get_client(
            host=_secret("CLICKHOUSE_HOST"),
            database=_secret("CLICKHOUSE_DATABASE"),
            username=_secret("CLICKHOUSE_USERNAME"),
            password=_secret("CLICKHOUSE_PASSWORD"),
            secure=True,
            connect_timeout=10,
            send_receive_timeout=30,
        )
    return _CH_CLIENT


def _ch_insert(row: list) -> None:
    """Insert one row with exponential backoff. Raises on final failure."""
    global _CH_CLIENT
    delay = _RETRY_BASE_SECONDS
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            client = _get_client()
            client.insert(
                "jarvis_audit_log",
                [row],
                column_names=[
                    "audit_id", "ts", "actor_slack_id", "actor_email",
                    "action", "target_email", "params_json",
                    "before_json", "after_json", "result",
                    "nl_utterance", "nl_confidence",
                    "slack_channel_id", "slack_message_ts",
                    "batch_id", "reason",
                ],
            )
            return  # success
        except Exception as exc:
            last_exc = exc
            _CH_CLIENT = None  # force reconnect on next attempt
            print(f"[audit_log] CH write attempt {attempt}/{_MAX_RETRIES} failed: {exc}")
            if attempt < _MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"ClickHouse audit write failed after {_MAX_RETRIES} attempts") from last_exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
    """Write an audit row to ClickHouse. Returns the audit_id."""
    audit_id = f"jrv_a_{uuid.uuid4().hex[:12]}"
    ts = datetime.now(timezone.utc)

    row = [
        audit_id,
        ts,
        actor_slack_id,
        intent.get("actor_email"),
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
    ]

    _ch_insert(row)
    return audit_id


def audit_has_batch_row(batch_id: str, target_email: str) -> bool:
    """Return True if this batch_id + email already has a success row (idempotency)."""
    client = _get_client()
    result = client.query(
        "SELECT count() FROM jarvis_audit_log WHERE batch_id=%(batch_id)s AND target_email=%(email)s AND result='success'",
        parameters={"batch_id": batch_id, "email": target_email},
    )
    return result.first_row[0] > 0


def query_audit(target_email: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Query audit rows, optionally filtered by target email."""
    client = _get_client()
    if target_email:
        result = client.query(
            "SELECT * FROM jarvis_audit_log WHERE target_email=%(email)s ORDER BY ts DESC LIMIT %(limit)s",
            parameters={"email": target_email, "limit": limit},
        )
    else:
        result = client.query(
            "SELECT * FROM jarvis_audit_log ORDER BY ts DESC LIMIT %(limit)s",
            parameters={"limit": limit},
        )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]
