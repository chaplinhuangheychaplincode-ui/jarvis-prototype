"""
Jarvis Intent Parser — Phase 0 prototype.

Takes a raw utterance, returns a structured intent using Claude with forced tool use.
Also handles clarifying questions when confidence is low.
"""
from __future__ import annotations

import json
import os
import re
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
                "enum": ["quota_grant", "create_account", "lookup", "ent_sub_grant", "bulk_grant", "explain", "revoke_grant", "reduce_grant", "investigate", "unknown"],
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
                "enum": ["generative_credit", "plan_credit", "api", "seat", "video_translate", "avatar_video", "personalized_video"],
                "description": "Credit/quota type for quota_grant. Default: generative_credit. Use 'api' for API quota, 'seat' for seats, 'video_translate' for translation quota, etc.",
            },
            "lookup_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For lookup only: if the user asks about specific fields (e.g. 'what are their generative credits', 'show me their tier'), list the field names here (e.g. ['generative_credit'], ['tier'], ['api_tier']). Quota field names match the product types above. Top-level fields: tier, api_tier, internal, country_code.",
            },
            "reason": {
                "type": "string",
                "description": "Business reason for the action",
            },
            "ae_attribution": {
                "type": "string",
                "description": "AE name for enterprise sub attribution",
            },
            "quota_id": {
                "type": "string",
                "description": "Specific quota_id to expire (for revoke_grant of a credit grant)",
            },
            "revoke_type": {
                "type": "string",
                "enum": ["subscription", "quota", "both"],
                "description": "For revoke_grant: what to revoke — subscription, a specific quota grant, or both",
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

Credit/quota product types (use in `product` field):
- generative_credit — standard AI generation credits (DEFAULT; also accepts "credits")
- plan_credit — credits bundled with a subscription plan
- api — API call quota
- seat — team/workspace seats
- video_translate — video translation quota
- avatar_video — avatar video quota
- personalized_video — personalized video quota

Legal combinations:
- quota_grant: credits only valid with tier=creator|pro|business
- API quota grants use product="api", no tier needed
- Generative credits use product="generative_credit"
- bulk_grant: use when there are multiple target emails OR explicit "bulk"/"batch"/"all these users" language.
  Extract ALL emails from the utterance into target_emails (list). Max 100.
  If the description says "these users" with no list inline, set needs_clarification=true asking for the list.

Lookup field extraction:
- If the user asks about a SPECIFIC field (e.g. "what are their generative credits?", "show me their API quota",
  "what tier are they on?"), set lookup_fields to the list of field names they want.
- Quota field names match the product types: generative_credit, plan_credit, api, seat, video_translate, avatar_video, personalized_video.
- Top-level state fields: tier, api_tier, internal, country_code, registration_ts.
- If the user just asks "who is X" or "look up X" with no specific field, leave lookup_fields empty.

Raw CLI mode: if utterance starts with "!raw ", set action="unknown" and needs_clarification=false
(this bypasses the LLM path in production).

Thread context: if you see a [Context] note at the start of the conversation with a
"[Prior completed op: ...]" marker, use it to resolve references in the new utterance.
The prior op is DONE — do not re-propose it. Use the email/tier/product from it to fill
in fields the user omitted (e.g. "now look them up" → target_email from prior op).
If the new utterance clearly refers to a different target, ignore the prior context.

Help/onboarding: if the user asks what you can do, what commands exist, or how to use you,
set action="explain" and needs_clarification=false. Examples: "what can you do",
"help", "show me commands", "how do I use this", "what are your capabilities".

Revoke: if the user wants to cancel, undo, revoke, or remove a grant set action="revoke_grant".
- If they mention a quota_id, set quota_id and revoke_type="quota".
- If they say "cancel sub" or "remove subscription", set revoke_type="subscription".
- If no quota_id given and they say "cancel everything" or "revoke all", set revoke_type="both".
- target_email is always required for revoke_grant.

Reduce grant: if the user wants to reduce, decrease, lower, or subtract a specific number of credits from a user's balance, set action="reduce_grant".
- Requires target_email, credits (how many to deduct), and product (which credit type — default generative_credit).
- No quota_id needed.
- If credits (amount to deduct) is missing, set needs_clarification=true asking for the amount.
- target_email is always required.

Investigate: if the user asks to investigate, diagnose, audit, troubleshoot, or "why can't they X" — anything requiring reasoning over account state rather than a direct action — set action="investigate".
- Requires target_email.
- Put the user's question or context in the `reason` field.
- Examples: "why can't user X export?", "something's wrong with this account", "investigate X", "what's going on with Y", "audit X's credits".
- Do NOT set investigate for a simple lookup — lookup is for read-only state fetch, investigate is for diagnosis + remediation."""


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
            role = msg.get("role", "user")
            text = msg.get("text", "")
            if role == "system":
                # System/context markers — prepend as user context note
                # (Anthropic API requires alternating user/assistant roles)
                messages.append({"role": "user", "content": f"[Context] {text}"})
                messages.append({"role": "assistant", "content": "Understood, I'll keep that context in mind."})
            elif role == "user":
                messages.append({"role": "user", "content": text})
            else:
                messages.append({"role": "assistant", "content": text})
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

    if intent.get("action") == "reduce_grant":
        if not intent.get("credits") and not intent.get("needs_clarification"):
            intent["needs_clarification"] = True
            intent["clarifying_question"] = "How many credits should I deduct?"
            intent["confidence"] = 0.3
        elif not intent.get("target_email") and not intent.get("needs_clarification"):
            intent["needs_clarification"] = True
            intent["clarifying_question"] = "What email address should I deduct from?"
            intent["confidence"] = 0.3

    return intent


def _try_parse_cli(text: str) -> dict[str, Any] | None:
    """
    Fast-path CLI parser — no LLM, no API call.

    Recognises well-formed command syntax and returns an intent dict with
    confidence=1.0.  Returns None if the text doesn't match any known pattern,
    which causes the caller to fall through to the LLM.

    Supported syntax:
        lookup <email>
        vip <email> <credits> [--tier <tier>] [--days <days>] [--feature <feature>]
        grant <email> <credits> [credits] [--tier <tier>] [--days <days>] [--feature <feature>]
        create <email> [--tier <tier>] [--days <days>] [--credits <N>]
        bulk grant <credits> credits <email> [<email> ...] [--tier <tier>] [--days <days>]
        bulk grant credits=<N> <email> [<email> ...]
    """
    import shlex

    # Normalise: strip leading whitespace, collapse internal whitespace
    text = text.strip()

    EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    FEATURE_MAP = {
        "credits": "generative_credit",
        "generative": "generative_credit",
        "generative_credit": "generative_credit",
        "plan_credit": "plan_credit",
        "plan": "plan_credit",
        "api": "api",
        "seat": "seat",
        "seats": "seat",
        "video_translate": "video_translate",
        "avatar_video": "avatar_video",
        "personalized_video": "personalized_video",
    }
    VALID_TIERS = {"creator", "pro", "business", "enterprise", "free"}

    def _parse_flags(tokens: list[str]) -> dict:
        """Extract --flag value pairs and positional leftovers from a token list."""
        flags: dict[str, Any] = {}
        positional: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("--"):
                key = tok[2:]
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    flags[key] = tokens[i + 1]
                    i += 2
                else:
                    flags[key] = True
                    i += 1
            else:
                positional.append(tok)
                i += 1
        return {"flags": flags, "positional": positional}

    def _base(action: str, **kwargs) -> dict:
        return {
            "action": action,
            "confidence": 1.0,
            "needs_clarification": False,
            "_cli": True,
            **kwargs,
        }

    try:
        # Tokenise safely (handles quoted strings)
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()

    if not tokens:
        return None

    cmd = tokens[0].lower()
    rest = tokens[1:]

    # ── lookup <email> ─────────────────────────────────────────────────────────
    if cmd == "lookup":
        emails = EMAIL_RE.findall(" ".join(rest))
        if emails:
            return _base("lookup", target_email=emails[0])
        return None

    # ── vip / grant ────────────────────────────────────────────────────────────
    if cmd in ("vip", "grant"):
        parsed = _parse_flags(rest)
        flags = parsed["flags"]
        pos = parsed["positional"]

        emails = EMAIL_RE.findall(" ".join(pos))
        if not emails:
            return None  # can't identify target — fall through to LLM

        email = emails[0]
        # Remove email tokens from positional so we can find the credit number
        pos_no_email = [t for t in pos if not EMAIL_RE.fullmatch(t)]

        credits: int | None = None
        for tok in pos_no_email:
            if tok.isdigit():
                credits = int(tok)
                break

        tier = flags.get("tier") or flags.get("t")
        if tier and tier.lower() not in VALID_TIERS:
            tier = None  # bad tier — let LLM handle clarification

        days = flags.get("days") or flags.get("d")
        duration_days: int | None = None
        if days:
            try:
                duration_days = int(days)
            except ValueError:
                pass

        feature_raw = flags.get("feature") or flags.get("f") or "credits"
        feature = FEATURE_MAP.get(feature_raw.lower(), "generative_credit")

        if not credits and not tier:
            return None  # ambiguous — no amount, no tier

        intent = _base(
            "quota_grant",
            target_email=email,
            product=feature,
        )
        if credits:
            intent["credits"] = credits
        if tier:
            intent["tier"] = tier.lower()
        if duration_days:
            intent["duration_days"] = duration_days
        return intent

    # ── create <email> ─────────────────────────────────────────────────────────
    if cmd in ("create", "create_account") or (cmd == "create" and "account" in [t.lower() for t in rest]):
        emails = EMAIL_RE.findall(" ".join(rest))
        if not emails:
            # "create account" with no email → return a clarification intent rather than auto-generating
            return {
                "action": "create_account",
                "confidence": 0.95,
                "needs_clarification": True,
                "clarifying_question": "What email address should I create the account for?",
                "_cli": True,
            }

        parsed = _parse_flags(rest)
        flags = parsed["flags"]

        tier = flags.get("tier")
        if tier and tier.lower() not in VALID_TIERS:
            tier = None

        days = flags.get("days")
        duration_days = None
        if days:
            try:
                duration_days = int(days)
            except ValueError:
                pass

        credits_flag = flags.get("credits")
        credits = None
        if credits_flag:
            try:
                credits = int(credits_flag)
            except ValueError:
                pass

        intent = _base("create_account", target_email=emails[0])
        if tier:
            intent["tier"] = tier.lower()
        if credits:
            intent["credits"] = credits
        if duration_days:
            intent["duration_days"] = duration_days
        return intent

    # ── bulk grant ─────────────────────────────────────────────────────────────
    if cmd == "bulk" and rest and rest[0].lower() == "grant":
        bulk_rest = rest[1:]  # drop "grant"
        joined = " ".join(bulk_rest)

        emails = EMAIL_RE.findall(joined)
        if not emails:
            return None

        parsed = _parse_flags([t for t in bulk_rest if not EMAIL_RE.search(t)])
        flags = parsed["flags"]
        pos = parsed["positional"]

        # Support: bulk grant 200 credits  OR  bulk grant credits=200
        credits: int | None = None
        for tok in pos:
            if tok.isdigit():
                credits = int(tok)
                break
        if credits is None:
            for tok in pos:
                if tok.lower().startswith("credits="):
                    try:
                        credits = int(tok.split("=")[1])
                    except (IndexError, ValueError):
                        pass

        if credits is None and not flags.get("credits"):
            return None  # no amount — fall through

        if credits is None:
            try:
                credits = int(flags["credits"])
            except (ValueError, TypeError):
                return None

        tier = flags.get("tier")
        if tier and tier.lower() not in VALID_TIERS:
            tier = None

        days = flags.get("days")
        duration_days = None
        if days:
            try:
                duration_days = int(days)
            except ValueError:
                pass

        intent = _base(
            "bulk_grant",
            target_emails=emails,
            credits=credits,
            product="generative_credit",
        )
        if tier:
            intent["tier"] = tier.lower()
        if duration_days:
            intent["duration_days"] = duration_days
        return intent

    return None


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
