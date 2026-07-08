"""
Jarvis Workflow Parser — replaces intent_parser.py for multi-step workflows.

Two modes:
  initial: NL utterance → full workflow plan (ordered steps)
  refine:  user follow-up on existing plan → answer|update|clarify

A single-step request still produces a one-element steps array so all
downstream code can treat every workflow uniformly.

Step schema:
  {
    "step": 1,
    "action": "create_account" | "quota_grant" | "lookup" | "get_info" |
              "revoke_grant" | "reduce_grant" | "bulk_grant" | "investigate",
    "target_email": "...",
    "tier": "...",             (optional)
    "credits": 1000,           (optional)
    "duration_days": 30,       (optional)
    "product": "generative_credit",  (optional)
    "quota_id": "...",         (optional)
    "revoke_type": "...",      (optional)
    "lookup_fields": [...],    (optional)
    "reason": "...",           (optional)
    "pre_confirm": false,      (true = execute before showing plan to user, read-only only)
  }

Workflow response:
  {
    "summary": "Short plain-English description of what will happen",
    "steps": [...],
    "needs_clarification": false,
    "clarifying_question": null,
    "confidence": 0.95,
  }

Refine response (mode=refine):
  {
    "type": "answer" | "update" | "clarify",
    "answer": "...",           (if type=answer)
    "updated_plan": {...},     (if type=update — full workflow response)
    "clarifying_question": "..." (if type=clarify)
  }
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(timeout=30.0)
    return _client


# ---------------------------------------------------------------------------
# Primitive actions catalogue (used in system prompt + validation)
# ---------------------------------------------------------------------------

PRIMITIVES = {
    "get_info": "Read-only account lookup. Always pre-confirm (runs before plan is shown).",
    "lookup": "Read-only account lookup shown to user.",
    "create_account": "Create a new HeyGen account.",
    "quota_grant": "Grant credits or subscription tier to a user.",
    "revoke_grant": "Remove a subscription or expire a quota grant.",
    "reduce_grant": "Deduct credits from a user's balance.",
    "bulk_grant": "Grant credits/tier to multiple users.",
    "investigate": "Agentic investigation of account issues.",
}

VALID_TIERS = ["creator", "pro", "business", "enterprise", "free"]
VALID_PRODUCTS = [
    "generative_credit", "plan_credit", "api", "seat",
    "video_translate", "avatar_video", "personalized_video",
]

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "step": {"type": "integer"},
        "action": {"type": "string", "enum": list(PRIMITIVES.keys())},
        "target_email": {"type": "string"},
        "target_emails": {"type": "array", "items": {"type": "string"}},
        "tier": {"type": "string", "enum": VALID_TIERS + [None]},
        "credits": {"type": "integer"},
        "duration_days": {"type": "integer"},
        "product": {"type": "string", "enum": VALID_PRODUCTS},
        "quota_id": {"type": "string"},
        "revoke_type": {"type": "string", "enum": ["subscription", "quota", "both"]},
        "lookup_fields": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "pre_confirm": {
            "type": "boolean",
            "description": "If true, this step runs BEFORE the plan is shown to the user. Only for read-only steps like get_info.",
        },
    },
    "required": ["step", "action"],
}

WORKFLOW_TOOL = {
    "name": "build_workflow",
    "description": "Build a structured workflow plan from a natural language request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-sentence plain-English summary of what the workflow will do.",
            },
            "steps": {
                "type": "array",
                "items": STEP_SCHEMA,
                "description": "Ordered list of primitive actions to execute.",
            },
            "needs_clarification": {
                "type": "boolean",
                "description": "True if something critical is missing and a question must be asked first.",
            },
            "clarifying_question": {
                "type": "string",
                "description": "ONE specific question to ask if needs_clarification=true.",
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0 confidence in this plan.",
            },
        },
        "required": ["summary", "steps", "needs_clarification", "confidence"],
    },
}

REFINE_TOOL = {
    "name": "refine_workflow",
    "description": "Classify a user follow-up message on an existing plan and optionally update it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["answer", "update", "clarify"],
                "description": (
                    "answer: user asked a question, answer it inline, plan unchanged. "
                    "update: user requested a change, return updated_plan. "
                    "clarify: user's intent is ambiguous, ask ONE follow-up question."
                ),
            },
            "answer": {
                "type": "string",
                "description": "Plain-English answer to the user's question (type=answer only).",
            },
            "clarifying_question": {
                "type": "string",
                "description": "Question to ask (type=clarify only).",
            },
            "updated_plan": {
                "type": "object",
                "description": "Full updated workflow (type=update only), same shape as build_workflow output.",
                "properties": {
                    "summary": {"type": "string"},
                    "steps": {"type": "array", "items": STEP_SCHEMA},
                    "needs_clarification": {"type": "boolean"},
                    "clarifying_question": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "required": ["type"],
    },
}

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

INITIAL_SYSTEM = """You are Jarvis, HeyGen's internal ops bot workflow planner.

Given a natural language request, build an ordered workflow plan using these primitives:

PRIMITIVES:
- get_info: Read-only account lookup — use as step 1 whenever you need account state to complete the plan (e.g. before quota_deduct). pre_confirm=true means it runs silently before the plan card is shown.
- lookup: Read-only lookup shown to the user in the plan card.
- create_account: Create a new HeyGen account. Fields: target_email (required).
- quota_grant: Grant credits or tier. Fields: target_email, tier?, credits?, product?, duration_days?
  - tier+credits → gift_subscription.add (bundled)
  - credits only → gift_quota.add (top-up, no plan change)
  - Default product: generative_credit. Default duration: 30 days.
- revoke_grant: Remove sub or expire quota. Fields: target_email, revoke_type (subscription|quota|both), quota_id?
- reduce_grant: Deduct credits. Fields: target_email, credits (required), product?
- bulk_grant: Grant to multiple users. Fields: target_emails[], tier?, credits?, product?, duration_days?
- investigate: Agentic investigation. Fields: target_email, reason?

GUARDRAILS (enforce these by adding steps automatically):
- reduce_grant must be preceded by get_info (pre_confirm=true) to verify account exists.
- revoke_grant on type=quota must have quota_id OR be preceded by get_info to retrieve it.
- If the request mentions "a lot of credits" or similar vague amounts, set needs_clarification=true.

CONTEXT CARRY-FORWARD (CRITICAL):
- If you see a [Context] message containing [Prior completed op: action=X, email=Y, ...], that Y is the known target email for this thread.
- Use Y as target_email for ALL steps in the new plan unless the user explicitly provides a DIFFERENT email.
- Do NOT ask for the email again if it is already in the prior op context.
- "how many credits does it have now", "check the balance", "what tier is it on" — all refer to Y without needing re-confirmation.

MULTI-STEP EXAMPLES:
- "create pro account with 50k credits for x@y.com"
    → [create_account(x@y.com), quota_grant(x@y.com, pro, 50000, generative_credit, 30d)]
- "deduct 100 credits from x@y.com"
    → [get_info(x@y.com, pre_confirm=true), reduce_grant(x@y.com, 100)]
- "revoke everything and re-grant 1000 credits for x@y.com"
    → [revoke_grant(x@y.com, both), quota_grant(x@y.com, 1000, generative_credit, 30d)]

SINGLE-STEP:
- "look up x@y.com" → [lookup(x@y.com)]
- "grant x@y.com 500 credits" → [quota_grant(x@y.com, 500, generative_credit, 30d)]

CLARIFY when: email is missing AND not available from prior op context, credit amount is vague ("a lot"), action is ambiguous.
DO NOT clarify when: duration is missing (default 30d), product is missing (default generative_credit), email is available from prior op context.

Thread context: if you see a [Context] note with a [Prior completed op: ...] marker,
use it to resolve "them", "that account", "it", etc. The prior op is DONE — do not re-propose it."""

REFINE_SYSTEM = """You are Jarvis, HeyGen's internal ops bot. The user is reviewing a workflow plan and has sent a follow-up message.

Classify the message as one of:
- answer: User asked a question about the plan (e.g. "what is generative_credit?", "how long will this last?")
  → Return a plain-English answer. Plan is UNCHANGED.
- update: User requested a modification (e.g. "make it 90 days", "use business tier instead", "add another step")
  → Return the full updated plan.
- clarify: User's intent is ambiguous and you need ONE follow-up question before you can answer or update.

Be precise. "What does X mean?" is always answer. "Change X to Y" is always update.
If the user confirms readiness ("looks good", "yes", "ok") treat as implicitly confirming — do NOT classify as update; return type=answer with answer="Ready to confirm — click ✅ to execute." so the UI knows to highlight the Confirm button."""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_workflow(
    utterance: str,
    history: list[dict[str, Any]] | None = None,
    model: str = "claude-haiku-4-5",
) -> dict[str, Any]:
    """
    Initial mode: NL utterance → workflow plan.
    Returns a workflow dict with summary, steps[], needs_clarification, confidence.
    """
    client = _get_client()
    messages: list[Any] = _build_messages(history, utterance)
    context_email = _extract_context_email(history)

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        temperature=0,
        system=INITIAL_SYSTEM,
        messages=messages,
        tools=[WORKFLOW_TOOL],
        tool_choice={"type": "tool", "name": "build_workflow"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "build_workflow":
            plan = dict(block.input)
            plan["raw_utterance"] = utterance
            plan = _validate_plan(plan, context_email=context_email)
            return plan

    return {
        "summary": "Could not parse request.",
        "steps": [],
        "needs_clarification": True,
        "clarifying_question": "I didn't understand that. Could you rephrase with the target email and action?",
        "confidence": 0.0,
        "raw_utterance": utterance,
    }


def refine_workflow(
    utterance: str,
    current_plan: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    model: str = "claude-haiku-4-5",
) -> dict[str, Any]:
    """
    Refine mode: classify user follow-up on an existing plan.
    Returns {type: answer|update|clarify, answer?, updated_plan?, clarifying_question?}
    """
    client = _get_client()

    # Inject current plan as context
    plan_json = json.dumps(current_plan, indent=2)
    plan_context = f"[Current workflow plan]\n```json\n{plan_json}\n```"

    messages: list[Any] = []
    messages.append({"role": "user", "content": plan_context})
    messages.append({"role": "assistant", "content": "I have the current plan in context."})

    if history:
        for msg in history:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            if role == "system":
                messages.append({"role": "user", "content": f"[Context] {text}"})
                messages.append({"role": "assistant", "content": "Noted."})
            elif role == "user":
                messages.append({"role": "user", "content": text})
            else:
                messages.append({"role": "assistant", "content": text})

    if not messages or messages[-1].get("content") != utterance:
        messages.append({"role": "user", "content": utterance})

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        temperature=0,
        system=REFINE_SYSTEM,
        messages=messages,
        tools=[REFINE_TOOL],
        tool_choice={"type": "tool", "name": "refine_workflow"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "refine_workflow":
            result = dict(block.input)
            # Validate updated_plan if present
            if result.get("type") == "update" and result.get("updated_plan"):
                result["updated_plan"] = _validate_plan(result["updated_plan"])
            return result

    return {
        "type": "clarify",
        "clarifying_question": "I didn't follow that. Could you rephrase?",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_messages(
    history: list[dict[str, Any]] | None,
    utterance: str,
) -> list[Any]:
    messages: list[Any] = []
    if history:
        for msg in history:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            if role == "system":
                messages.append({"role": "user", "content": f"[Context] {text}"})
                messages.append({"role": "assistant", "content": "Understood."})
            elif role == "user":
                messages.append({"role": "user", "content": text})
            else:
                messages.append({"role": "assistant", "content": text})
    if not messages or messages[-1].get("content") != utterance:
        messages.append({"role": "user", "content": utterance})
    return messages


def _extract_context_email(history: list[dict[str, Any]] | None) -> str:
    """Extract target_email from a [Prior completed op: ..., email=X] context marker in history."""
    if not history:
        return ""
    import re
    for msg in history:
        if msg.get("role") == "system":
            m = re.search(r"email=([^\s,\]]+)", msg.get("text", ""))
            if m:
                return m.group(1)
    return ""


def _validate_plan(plan: dict[str, Any], context_email: str = "") -> dict[str, Any]:
    """Post-parse validation and normalization."""
    steps = plan.get("steps", [])

    # Backfill missing target_email from context if available
    if context_email:
        for step in steps:
            action = step.get("action", "")
            if action != "bulk_grant" and not step.get("target_email"):
                step["target_email"] = context_email

    # Ensure step numbers are sequential
    for i, step in enumerate(steps):
        step["step"] = i + 1
        # Default pre_confirm=False
        step.setdefault("pre_confirm", False)
        # Default product
        if step.get("action") in ("quota_grant", "reduce_grant") and not step.get("product"):
            step["product"] = "generative_credit"
        # Default duration
        if step.get("action") == "quota_grant" and not step.get("duration_days"):
            step["duration_days"] = 30
        # Validate tier
        tier = step.get("tier")
        if tier and tier not in VALID_TIERS:
            step.pop("tier", None)

    # Guardrail: reduce_grant must have get_info before it
    actions = [s["action"] for s in steps]
    if "reduce_grant" in actions:
        rg_idx = actions.index("reduce_grant")
        # Check if there's a get_info step before it
        pre_steps = [s for s in steps[:rg_idx] if s["action"] == "get_info"]
        if not pre_steps:
            email = steps[rg_idx].get("target_email", "")
            get_info_step = {
                "step": 0,  # will be renumbered
                "action": "get_info",
                "target_email": email,
                "pre_confirm": True,
                "reason": "Auto-injected: verify account exists before deduction",
            }
            steps.insert(rg_idx, get_info_step)
            # Renumber
            for i, s in enumerate(steps):
                s["step"] = i + 1

    # Guardrail: quota_grant with no prior account check → inject get_info pre-confirm
    # so the approver sees current balance before granting
    actions = [s["action"] for s in steps]
    if "quota_grant" in actions:
        qg_idx = actions.index("quota_grant")
        pre_info = [s for s in steps[:qg_idx] if s["action"] in ("get_info", "create_account")]
        if not pre_info:
            email = steps[qg_idx].get("target_email", "")
            get_info_step = {
                "step": 0,
                "action": "get_info",
                "target_email": email,
                "pre_confirm": True,
                "reason": "Auto-injected: check current balance before granting",
            }
            steps.insert(qg_idx, get_info_step)
            for i, s in enumerate(steps):
                s["step"] = i + 1

    # Credit limit guardrails (based on CMS backend rules)
    # MAX_GIFT_QUOTA_PER_90_DAYS = 1000 for bot endpoint
    CMS_CREDIT_CAP = 1_000
    FREE_TIER_CREDIT_WARN = 500  # free accounts: warn above 500, hard CMS limit is 1000

    guardrails_applied = list(plan.get("guardrails_applied", []))
    for step in steps:
        if step.get("action") != "quota_grant":
            continue
        credits = step.get("credits", 0) or 0
        tier = step.get("tier", "") or ""

        if tier == "free" and credits > FREE_TIER_CREDIT_WARN:
            msg = (
                f"⚠️ *Guardrail:* Free-tier accounts are limited to {CMS_CREDIT_CAP} credits "
                f"per 90 days by the CMS backend. Requesting {credits} credits on a free account "
                f"may be rejected. Consider upgrading the tier first."
            )
            guardrails_applied.append(msg)
            step["pre_confirm"] = True  # Force explicit confirmation for this step

        elif credits > CMS_CREDIT_CAP:
            msg = (
                f"⚠️ *Guardrail:* CMS hard cap is {CMS_CREDIT_CAP} credits per user per 90 days "
                f"(bot endpoint). Requesting {credits} may be rejected if prior grants exist."
            )
            guardrails_applied.append(msg)
            step["pre_confirm"] = True

    plan["guardrails_applied"] = guardrails_applied

    plan["steps"] = steps
    return plan
