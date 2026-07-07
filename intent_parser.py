"""
Jarvis Intent Parser — Phase 0 prototype.

Takes a raw utterance, returns a structured intent using Claude with forced tool use.
Also handles clarifying questions when confidence is low.
"""
from __future__ import annotations

import json
import os
from typing import Any

import anthropic

# Module-level singleton — avoid re-initialising the HTTP client on every call.
# 20 s timeout: intent parse should never need more than that.
_anthropic_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(timeout=20.0)
    return _anthropic_client

# Legal tier × product combinations (sourced from the brief)
LEGAL_COMBOS = {
    "quota_grant": {
        "valid_tiers": ["creator", "pro", "business"],
        "api_quota_tiers": ["any"],  # API quota not tier-gated
        "note": "credits only valid on creator|pro|business; API quota is separate",
    },
    "ent_sub_grant": {
        "valid_products": ["video_translate", "video_avatar", "video_studio", "personalized_video"],
        "requires_ae": True,
    },
}

INTENT_TOOL = {
    "name": "parse_intent",
    "description": "Parse a Slack message into a structured Jarvis intent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["quota_grant", "create_account", "lookup", "ent_sub_grant", "bulk_grant", "explain", "unknown"],
                "description": "The action to perform",
            },
            "target_email": {
                "type": "string",
                "description": "Target user email address (single-user ops only)",
            },
            "target_emails": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of email addresses for bulk_grant (max 100). Extract ALL emails found in the utterance.",
            },
            "tier": {
                "type": "string",
                "enum": ["creator", "pro", "business", "enterprise", "free", None],
                "description": "Subscription tier",
            },
            "credits": {
                "type": "integer",
                "description": "Number of credits to grant",
            },
            "duration_days": {
                "type": "integer",
                "description": "Duration in days",
            },
            "product": {
                "type": "string",
                "description": "Specific product (for API quota: 'api'; for generative: 'generative_credit')",
            },
            "reason": {
                "type": "string",
                "description": "Business reason for the action",
            },
            "ae_attribution": {
                "type": "string",
                "description": "AE name for enterprise sub attribution",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence score 0.0-1.0 for this parse",
            },
            "needs_clarification": {
                "type": "boolean",
                "description": "True if a field is ambiguous and needs clarification",
            },
            "clarifying_question": {
                "type": "string",
                "description": "Question to ask the user if needs_clarification is true",
            },
        },
        "required": ["action", "confidence"],
    },
}

SYSTEM_PROMPT = """You are the intent parser for Jarvis, HeyGen's internal ops bot.
You may receive a full conversation history (multiple turns) or a single utterance.
Your job: given everything said so far, decide whether you have enough information to
propose a concrete action, or whether you still need to ask ONE clarifying question.

Return ONE of two response shapes:
1. PROPOSE — you have enough to act:
   Set needs_clarification=false, fill all required fields, confidence >= 0.7
2. CLARIFY — something critical is missing or ambiguous:
   Set needs_clarification=true, set clarifying_question to ONE specific question,
   confidence < 0.7

Legal combinations:
- quota_grant: credits only valid with tier=creator|pro|business
- API quota grants use product="api", no tier needed
- Generative credits use product="generative_credit"
- bulk_grant: use when there are multiple target emails OR explicit "bulk"/"batch"/"all these users" language.
  Extract ALL emails from the utterance into target_emails (list). Max 100.
  If the description says "these users" with no list inline, set needs_clarification=true asking for the list.

Raw CLI mode: if utterance starts with "!raw ", set action="unknown" and needs_clarification=false
(this bypasses the LLM path in production).

Help/onboarding: if the user asks what you can do, what commands exist, or how to use you,
set action="explain" and needs_clarification=false. Examples: "what can you do",
"help", "show me commands", "how do I use this", "what are your capabilities"."""


def parse_intent(
    utterance: str,
    history: list[dict[str, str]] | None = None,
    model: str = "claude-haiku-4-5",
) -> dict[str, Any]:
    """
    Parse a raw utterance into a structured intent dict.

    If history is provided (list of {role, text} dicts), the full conversation
    context is passed to the LLM so it can resolve references and fill in
    fields mentioned earlier in the thread.
    """
    client = _get_client()

    # Build message list: history first, then current utterance
    messages: list[dict[str, Any]] = []
    if history:
        for msg in history:
            role = "user" if msg.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": msg.get("text", "")})
    # Always end with the latest user message
    if not messages or messages[-1]["content"] != utterance:
        messages.append({"role": "user", "content": utterance})

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        temperature=0,  # deterministic — same input always produces same parse
        system=SYSTEM_PROMPT,
        messages=messages,
        tools=[INTENT_TOOL],
        tool_choice={"type": "tool", "name": "parse_intent"},
    )

    # Extract tool use result
    for block in response.content:
        if block.type == "tool_use" and block.name == "parse_intent":
            intent = block.input
            # Post-parse validation
            intent = _validate_intent(intent, utterance)
            return intent

    return {"action": "unknown", "confidence": 0.0, "raw_utterance": utterance}


def _validate_intent(intent: dict[str, Any], utterance: str) -> dict[str, Any]:
    """Apply business rule validation after LLM parse."""
    intent["raw_utterance"] = utterance

    if intent.get("action") == "quota_grant":
        tier = intent.get("tier")
        credits = intent.get("credits")
        product = intent.get("product", "")

        # Credits require a valid tier
        if credits and tier and tier not in ["creator", "pro", "business"]:
            intent["needs_clarification"] = True
            intent["clarifying_question"] = (
                f"Credits can only be granted with creator, pro, or business tiers "
                f"(you said '{tier}'). Which tier did you mean?"
            )
            intent["confidence"] = min(intent.get("confidence", 0.5), 0.4)

        # Must have target email
        if not intent.get("target_email") and not intent.get("needs_clarification"):
            intent["needs_clarification"] = True
            intent["clarifying_question"] = "What email address should I target?"
            intent["confidence"] = 0.3

    if intent.get("action") == "bulk_grant":
        emails = intent.get("target_emails") or []
        # Deduplicate and strip whitespace
        emails = list(dict.fromkeys(e.strip().lower() for e in emails if "@" in e))
        intent["target_emails"] = emails
        if len(emails) == 0:
            intent["needs_clarification"] = True
            intent["clarifying_question"] = (
                "Please paste the list of emails to bulk-grant "
                "(one per line or comma-separated, max 100)."
            )
            intent["confidence"] = 0.3
        elif len(emails) > 100:
            intent["needs_clarification"] = True
            intent["clarifying_question"] = (
                f"That's {len(emails)} emails — max batch size is 100. "
                "Please split into smaller batches."
            )
            intent["confidence"] = 0.3
        elif not intent.get("tier") and not intent.get("credits"):
            intent["needs_clarification"] = True
            intent["clarifying_question"] = (
                "What should I grant? Please specify tier and/or credit amount and duration. "
                "Example: *creator tier, 500 credits, 90 days*."
            )
            intent["confidence"] = 0.4

    return intent


if __name__ == "__main__":
    # Quick smoke test
    test_cases = [
        "comp teodora@heygen.com a creator sub for a year with 9999 credits",
        "make mtoth109@gmail.com a creator for 60 days with 100 credits",
        "who is mtoth109@gmail.com and what did they do last 7 days",
        "grant 100 api credits to partner@acme.com for 30 days",
        "give someone some credits",  # should need clarification
        "14-day enterprise trial for admin@example.com, 5 seats",  # should ask AE
    ]

    for utt in test_cases:
        print(f"\n>>> {utt}")
        result = parse_intent(utt)
        print(json.dumps(result, indent=2))
