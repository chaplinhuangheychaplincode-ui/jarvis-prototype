"""
Jarvis Feedback Store — SQLite-backed.

Captures user feedback signals (complaints, praise, corrections) so the team
can review them async and improve the bot over time.

Schema
------
jarvis_feedback(
    id          TEXT PRIMARY KEY,   -- jrv_fb_<hex8>
    ts          TEXT,               -- ISO-8601 UTC
    user_id     TEXT,               -- Slack user ID
    channel     TEXT,
    thread_ts   TEXT,               -- thread the feedback came from
    raw_text    TEXT,               -- full utterance as typed
    sentiment   TEXT,               -- positive | negative | neutral
    extracted   TEXT,               -- cleaned feedback extracted by LLM
    pending_id  TEXT,               -- most recent pending_id in this thread (if any)
    env         TEXT,               -- dev | prod
)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

_DB_PATH = os.environ.get("JARVIS_FEEDBACK_DB", "jarvis_feedback.sqlite")

# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jarvis_feedback (
            id          TEXT PRIMARY KEY,
            ts          TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            channel     TEXT NOT NULL,
            thread_ts   TEXT NOT NULL,
            raw_text    TEXT NOT NULL,
            sentiment   TEXT NOT NULL DEFAULT 'neutral',
            extracted   TEXT NOT NULL DEFAULT '',
            pending_id  TEXT NOT NULL DEFAULT '',
            env         TEXT NOT NULL DEFAULT 'dev'
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Heuristic detector — fast, no LLM
# ---------------------------------------------------------------------------

_FEEDBACK_TRIGGERS = re.compile(
    r"\b("
    r"feedback[:\-]?|this is (wrong|incorrect|broken|bad|off)|"
    r"that('s| is| was| didn'?t)? (wrong|incorrect|broken|not right|not what|off|terrible|bad)|"
    r"not what i (wanted|asked|meant|expected)|"
    r"you (missed|misunderstood|got it wrong|screwed up|messed up)|"
    r"(wrong|incorrect|bad) (email|amount|tier|user|account|plan|action)|"
    r"(issue|problem|bug|error)[:\-]|"
    r"(this|that) (doesn'?t|didn'?t) work|"
    r"(please |can you )?fix (this|that)|"
    r"(great|perfect|good job|well done|nice|love it|works great|exactly right|"
    r"thank(s| you)|that'?s? (it|right|correct|perfect)|awesome)"
    r")\b",
    re.IGNORECASE,
)


def is_feedback_intent(text: str) -> bool:
    """Heuristic: does this utterance look like feedback rather than an action request?"""
    return bool(_FEEDBACK_TRIGGERS.search(text))


# ---------------------------------------------------------------------------
# LLM extraction — pull clean feedback + sentiment
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """\
You are a feedback parser for Jarvis, an internal Slack bot that manages HeyGen user accounts.

Given a user message, extract:
1. sentiment: "positive", "negative", or "neutral"
2. extracted: a clean 1-2 sentence summary of the feedback, stripped of filler. If the user is \
praising, say what they liked. If complaining, say what was wrong. Keep it factual.

Respond ONLY with valid JSON: {"sentiment": "...", "extracted": "..."}
"""


def extract_feedback(raw_text: str, model: str = "claude-haiku-4-5") -> dict[str, str]:
    """Call LLM to extract sentiment + clean feedback text."""
    try:
        import anthropic
        import subprocess as _sp

        def _secret(name: str) -> str:
            return _sp.run(
                ["python3", "/opt/genesis/manage-secrets.py", "get", name],
                capture_output=True, text=True,
            ).stdout.strip()

        client = anthropic.Anthropic(api_key=_secret("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=model,
            max_tokens=128,
            temperature=0,
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": raw_text}],
        )
        for block in resp.content:
            if block.type == "text":
                return json.loads(block.text.strip())
    except Exception as e:
        print(f"[FEEDBACK] extraction failed: {e}", flush=True)
    return {"sentiment": "neutral", "extracted": raw_text[:200]}


# ---------------------------------------------------------------------------
# Write / query
# ---------------------------------------------------------------------------

def write_feedback(
    user_id: str,
    channel: str,
    thread_ts: str,
    raw_text: str,
    sentiment: str = "neutral",
    extracted: str = "",
    pending_id: str = "",
) -> str:
    """Insert a feedback row. Returns the generated feedback ID."""
    fb_id = f"jrv_fb_{uuid.uuid4().hex[:8]}"
    ts = datetime.now(timezone.utc).isoformat()
    env = os.environ.get("JARVIS_ENV", "dev")
    with _conn() as conn:
        conn.execute(
            """INSERT INTO jarvis_feedback
               (id, ts, user_id, channel, thread_ts, raw_text, sentiment, extracted, pending_id, env)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fb_id, ts, user_id, channel, thread_ts, raw_text, sentiment, extracted, pending_id, env),
        )
    return fb_id


def list_feedback(limit: int = 20, env: str | None = None) -> list[dict[str, Any]]:
    """Return recent feedback rows, newest first."""
    conn = _conn()
    if env:
        rows = conn.execute(
            "SELECT * FROM jarvis_feedback WHERE env=? ORDER BY ts DESC LIMIT ?",
            (env, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jarvis_feedback ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
