"""
Jarvis Investigator — read-only agentic loop.

Given an email (and optional question), runs a Claude Sonnet tool-calling loop
that can call lookup, audit log, and quota history. Returns a structured
InvestigationResult with findings + proposed remediation steps.

The loop is strictly read-only. No CMS writes happen here.
Proposed actions are returned as structured intent dicts — the bot
routes each one through the normal HITL confirm flow.

Max 6 tool calls per investigation to prevent runaway inference.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import anthropic
from anthropic.types import MessageParam  # noqa: F401

import heygen_cms_api as heygen
from audit_log import query_audit

_client: anthropic.Anthropic | None = None
MAX_TOOL_CALLS = 6


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(timeout=30.0)
    return _client


# ---------------------------------------------------------------------------
# Tool definitions (read-only)
# ---------------------------------------------------------------------------

INVESTIGATE_TOOLS = [
    {
        "name": "lookup_user",
        "description": (
            "Look up a HeyGen user's current account state: tier, quota balances, "
            "active grants, subscription info. Use this first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "User's email address"},
            },
            "required": ["email"],
        },
    },
    {
        "name": "get_audit_log",
        "description": (
            "Fetch the last N Jarvis actions for a user from the audit log. "
            "Shows what grants were made, by whom, when, and the result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "User's email address"},
                "limit": {
                    "type": "integer",
                    "description": "Number of records to fetch (default 10, max 20)",
                    "default": 10,
                },
            },
            "required": ["email"],
        },
    },
    {
        "name": "lookup_space",
        "description": (
            "Look up a HeyGen space/team by space_id. Returns seat count, "
            "owner, members, AE email, trial end date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "space_id": {"type": "string", "description": "Space ID (from user lookup)"},
            },
            "required": ["space_id"],
        },
    },
    {
        "name": "finish",
        "description": (
            "End the investigation and return findings + proposed actions. "
            "Call this when you have enough information to make recommendations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-4 sentence plain-English summary of what you found. Be specific about numbers.",
                },
                "findings": {
                    "type": "array",
                    "description": "Bullet-point findings, each a short string.",
                    "items": {"type": "string"},
                },
                "proposed_actions": {
                    "type": "array",
                    "description": "Ordered list of remediation steps, each a structured intent the bot can execute.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "Short button label, e.g. 'Grant 1000 generative_credit (30d)'",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["quota_grant", "revoke_grant", "create_account", "reduce_grant"],
                                "description": "Jarvis action type",
                            },
                            "target_email": {"type": "string"},
                            "credits": {"type": "integer"},
                            "duration_days": {"type": "integer"},
                            "product": {
                                "type": "string",
                                "enum": [
                                    "generative_credit", "plan_credit", "api", "seat",
                                    "video_translate", "avatar_video", "personalized_video",
                                ],
                            },
                            "tier": {"type": "string"},
                            "quota_id": {"type": "string"},
                            "revoke_type": {
                                "type": "string",
                                "enum": ["subscription", "quota", "both"],
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["label", "action", "target_email"],
                    },
                },
                "no_action_needed": {
                    "type": "boolean",
                    "description": "True if the account looks healthy and no fix is required.",
                },
            },
            "required": ["summary", "findings"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatch (read-only)
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, args: dict[str, Any]) -> str:
    """Call the named read tool and return a JSON string result."""
    try:
        if name == "lookup_user":
            result = heygen.get_user_state(args["email"])
            return json.dumps(result, default=str)

        elif name == "get_audit_log":
            limit = min(int(args.get("limit", 10)), 20)
            rows = query_audit(target_email=args["email"], limit=limit)
            if not rows:
                return json.dumps({"records": [], "note": "No audit records found"})
            # Serialize datetime objects
            clean = []
            for r in rows:
                clean.append({k: (v.isoformat() if hasattr(v, "isoformat") else v)
                               for k, v in r.items()})
            return json.dumps({"records": clean})

        elif name == "lookup_space":
            result = heygen.get_space(args["space_id"])
            return json.dumps(result, default=str)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Main investigation entry point
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Jarvis, HeyGen's internal ops agent. You are investigating a user account.

Your job: use the available tools to understand the account's current state, identify problems,
and propose concrete remediation steps. Be specific about numbers (quota amounts, dates, IDs).

Investigation principles:
1. Always start with lookup_user to get current state.
2. Check audit log if you need to understand history (recent grants, who did what).
3. Look up space if the user is on a team plan and seat/team info is relevant.
4. Call finish() when you have a clear picture — don't keep calling tools if you already know enough.
5. Proposed actions must be concrete (specific email, specific amount, specific product).
6. If the account looks healthy, say so clearly with no_action_needed=true.

Common issues to look for:
- Zero balance on a credit type they should have
- Expired grants (quota shows 0 but audit log shows a grant that's now past expiry)
- Wrong tier (free when they should be on pro/business)
- Multiple stale grants that should be expired
- Sub granted but no quota added (tier looks right but credits are 0)"""


class InvestigationResult:
    def __init__(
        self,
        summary: str,
        findings: list[str],
        proposed_actions: list[dict[str, Any]],
        no_action_needed: bool = False,
        tool_calls: int = 0,
        elapsed_ms: int = 0,
        error: str | None = None,
    ):
        self.summary = summary
        self.findings = findings
        self.proposed_actions = proposed_actions
        self.no_action_needed = no_action_needed
        self.tool_calls = tool_calls
        self.elapsed_ms = elapsed_ms
        self.error = error


def investigate(
    email: str,
    question: str | None = None,
    progress_cb: Any | None = None,  # callable(str) — called with progress updates
) -> InvestigationResult:
    """
    Run the investigation loop for `email`.

    progress_cb is called with short status strings so the bot can
    post live thread updates ("Looking up account...", "Checking audit log...").
    """
    t0 = time.time()
    client = _get_client()

    user_question = question or f"Investigate the account for {email} and identify any issues."
    initial_message = (
        f"Please investigate the HeyGen account for: {email}\n\n"
        f"Question: {user_question}"
    )

    messages: list[Any] = [{"role": "user", "content": initial_message}]
    tool_call_count = 0

    while tool_call_count < MAX_TOOL_CALLS:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2048,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=INVESTIGATE_TOOLS,
        )

        # Append assistant turn to messages
        messages.append({"role": "assistant", "content": response.content})

        # Check stop condition
        if response.stop_reason == "end_turn":
            # No tool use — shouldn't happen if prompt is right, but handle gracefully
            text = " ".join(getattr(b, "text", "") for b in response.content)
            return InvestigationResult(
                summary=text or "Investigation complete (no structured result).",
                findings=[],
                proposed_actions=[],
                elapsed_ms=int((time.time() - t0) * 1000),
                tool_calls=tool_call_count,
            )

        # Process tool calls
        tool_results = []
        finish_result: dict[str, Any] | None = None

        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_call_count += 1
            tool_name = block.name
            tool_args = block.input

            # Live progress callback
            if progress_cb:
                _PROGRESS_LABELS = {
                    "lookup_user": f"🔍 Looking up `{tool_args.get('email', email)}`...",
                    "get_audit_log": f"📋 Checking audit log...",
                    "lookup_space": f"🏢 Looking up space `{tool_args.get('space_id', '')}`...",
                    "finish": "✍️ Writing up findings...",
                }
                progress_cb(_PROGRESS_LABELS.get(tool_name, f"⚙️ Calling {tool_name}..."))

            if tool_name == "finish":
                finish_result = tool_args
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Done.",
                })
            else:
                result_str = _dispatch_tool(tool_name, tool_args)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

        # If finish was called, we're done
        if finish_result is not None:
            return InvestigationResult(
                summary=finish_result.get("summary", ""),
                findings=finish_result.get("findings", []),
                proposed_actions=finish_result.get("proposed_actions", []),
                no_action_needed=finish_result.get("no_action_needed", False),
                tool_calls=tool_call_count,
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        # Add tool results and continue loop
        messages.append({"role": "user", "content": tool_results})

    # Max iterations hit — return whatever we know
    return InvestigationResult(
        summary=f"Investigation hit max tool calls ({MAX_TOOL_CALLS}). Partial results only.",
        findings=["Max tool call limit reached — results may be incomplete."],
        proposed_actions=[],
        elapsed_ms=int((time.time() - t0) * 1000),
        tool_calls=tool_call_count,
        error="max_tool_calls",
    )
