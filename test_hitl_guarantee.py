"""
HITL Guarantee Tests
====================
Proves that _execute_intent and all heygen mutating calls can ONLY be reached
after an explicit ✅ reaction on a stored pending confirmation.

All patches target bot's OWN namespace so from-imports are intercepted.

Test matrix:
  T1  handle_mention lookup        → heygen.lookup_user        NEVER called
  T2  handle_mention quota_grant   → heygen.execute_quota_grant NEVER called
  T3  handle_mention create_acct   → heygen.execute_create_account NEVER called
  T4  handle_reaction ❌            → _execute_intent            NEVER called
  T5  handle_reaction ✅ unauth    → _execute_intent            NEVER called
  T6  handle_reaction ✅ auth      → execute_quota_grant        CALLED exactly once
  T7  reaction on unknown ts       → _execute_intent            NEVER called
  T8  audit written BEFORE mark_executed (SOC2 ordering)
  T9  double-confirm idempotent    → executes exactly once

Run: python3 -m pytest test_hitl_guarantee.py -v
"""
from __future__ import annotations

import json
import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------
FAKE_BEFORE_STATE = {
    "email": "test@example.com",
    "user_id": "usr_abc",
    "tier": "free",
    "internal": False,
    "spaces": [],
    "quotas": {},
}

FAKE_PENDING = {
    "pending_id": "jrv_p_test001",
    "actor_slack_id": "UAUTHOR",
    "intent_json": json.dumps({
        "action": "quota_grant",
        "target_email": "test@example.com",
        "tier": "creator",
        "credits": 100,
        "duration_days": 30,
        "confidence": 0.95,
        "raw_utterance": "grant 100 credits to test@example.com",
    }),
    "before_json": json.dumps(FAKE_BEFORE_STATE),
    "channel_id": "CCHANNEL",
    "thread_ts": "1000.0001",
    "message_ts": "1000.0002",
}

AUTHORIZED_USER = "U0BBD6002R2"   # matches OWNER_SLACK_ID in bot.py
UNAUTHORIZED_USER = "USTRANGER1"


def _make_mention_event(action: str, email: str = "test@example.com") -> dict:
    text_map = {
        "lookup":         f"who is {email}",
        "quota_grant":    f"grant 100 credits to {email} for 30 days as creator",
        "create_account": f"create account for {email} as creator for 30 days",
    }
    return {
        "text": f"<@U0BDYHHJQTY> {text_map[action]}",
        "user": "UAUTHOR",
        "channel": "CCHANNEL",
        "ts": "1000.0001",
    }


def _make_reaction_event(reaction: str = "white_check_mark",
                          user: str = AUTHORIZED_USER) -> dict:
    return {
        "reaction": reaction,
        "user": user,
        "item": {"channel": "CCHANNEL", "ts": FAKE_PENDING["message_ts"]},
    }


def _make_intent(action: str, email: str = "test@example.com") -> dict:
    return {
        "action": action,
        "target_email": email,
        "tier": "creator",
        "credits": 100,
        "duration_days": 30,
        "confidence": 0.95,
        "needs_clarification": False,
        "raw_utterance": "test utterance",
    }


# ---------------------------------------------------------------------------
# Base class — patches via patch.object(bot, ...) so from-imports are hit
# ---------------------------------------------------------------------------
class HITLBase(unittest.TestCase):

    def setUp(self):
        # Fresh import so module-level caches (_SEEN_REACTIONS etc.) reset
        for mod in list(sys.modules.keys()):
            if mod in ("bot", "heygen_cms_api", "intent_parser",
                       "pending_store", "audit_log", "slack_client"):
                del sys.modules[mod]

        import bot as _bot
        self.bot = _bot

        # patch.object(module, attr) correctly intercepts from-imports
        # because we're replacing the name in the module where it's USED.
        self.patches = [
            # heygen CMS — accessed as heygen.X (module reference, not from-import)
            patch.object(_bot.heygen, "get_user_state",         return_value=FAKE_BEFORE_STATE),
            patch.object(_bot.heygen, "lookup_user",             return_value=FAKE_BEFORE_STATE),
            patch.object(_bot.heygen, "execute_quota_grant",     return_value=FAKE_BEFORE_STATE),
            patch.object(_bot.heygen, "execute_create_account",  return_value=FAKE_BEFORE_STATE),
            # Slack / cards — from-imported into bot namespace
            patch.object(_bot, "post_message",                  return_value={"ts": "1000.0002", "ok": True}),
            patch.object(_bot, "update_message",                 return_value={"ok": True}),
            patch.object(_bot, "get_user_info",                  return_value={"real_name": "Stranger"}),
            patch.object(_bot, "build_confirmation_card",        return_value=[]),
            patch.object(_bot, "build_clarifying_question_card", return_value=[]),
            patch.object(_bot, "build_audit_ack_card",           return_value=[]),
            # pending store — from-imported into bot namespace
            patch.object(_bot, "write_pending",                  return_value="jrv_p_test001"),
            patch.object(_bot, "mark_executed",                  return_value=None),
            patch.object(_bot, "list_pending",                   return_value=[]),
            patch.object(_bot, "get_by_message_ts",              return_value=None),  # default: no pending
            # audit log — from-imported into bot namespace
            patch.object(_bot, "write_audit",                    return_value="jrv_a_test001"),
            patch.object(_bot, "query_audit",                    return_value=[]),
        ]
        self.mocks: dict[str, MagicMock] = {}
        for p in self.patches:
            m = p.start()
            self.mocks[p.attribute] = m

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _parse_intent_returns(self, action: str, email: str = "test@example.com"):
        """Context manager: make bot.parse_intent return a specific action."""
        return patch.object(self.bot, "parse_intent", return_value=_make_intent(action, email))

    def _with_pending(self):
        """Context manager: make bot.get_by_message_ts return FAKE_PENDING."""
        return patch.object(self.bot, "get_by_message_ts", return_value=FAKE_PENDING)


# ---------------------------------------------------------------------------
# T1 — handle_mention lookup must NOT call heygen.lookup_user
# ---------------------------------------------------------------------------
class TestT1_MentionLookupNoExec(HITLBase):
    def test_lookup_not_executed_on_mention(self):
        with self._parse_intent_returns("lookup"):
            self.bot.handle_mention(_make_mention_event("lookup"))

        self.mocks["lookup_user"].assert_not_called()
        self.mocks["write_pending"].assert_called_once(), (
            "A confirmation card must be stored even for lookup")


# ---------------------------------------------------------------------------
# T2 — handle_mention quota_grant must NOT call execute_quota_grant
# ---------------------------------------------------------------------------
class TestT2_MentionQuotaGrantNoExec(HITLBase):
    def test_quota_grant_not_executed_on_mention(self):
        with self._parse_intent_returns("quota_grant"):
            self.bot.handle_mention(_make_mention_event("quota_grant"))

        self.mocks["execute_quota_grant"].assert_not_called()
        self.mocks["write_pending"].assert_called_once()


# ---------------------------------------------------------------------------
# T3 — handle_mention create_account must NOT call execute_create_account
# ---------------------------------------------------------------------------
class TestT3_MentionCreateAccountNoExec(HITLBase):
    def test_create_account_not_executed_on_mention(self):
        with self._parse_intent_returns("create_account"):
            self.bot.handle_mention(_make_mention_event("create_account"))

        self.mocks["execute_create_account"].assert_not_called()
        self.mocks["write_pending"].assert_called_once()


# ---------------------------------------------------------------------------
# T4 — ❌ reaction must NOT execute anything
# ---------------------------------------------------------------------------
class TestT4_CancelReactionNoExec(HITLBase):
    def test_cancel_reaction_does_not_execute(self):
        with self._with_pending():
            self.bot.handle_reaction(_make_reaction_event(reaction="x",
                                                           user=AUTHORIZED_USER))

        self.mocks["execute_quota_grant"].assert_not_called()
        self.mocks["lookup_user"].assert_not_called()
        self.mocks["execute_create_account"].assert_not_called()
        self.mocks["write_audit"].assert_not_called()


# ---------------------------------------------------------------------------
# T5 — ✅ from unauthorized user must NOT execute
# ---------------------------------------------------------------------------
class TestT5_UnauthorizedConfirmNoExec(HITLBase):
    def test_unauthorized_confirm_does_not_execute(self):
        with self._with_pending():
            self.bot.handle_reaction(_make_reaction_event(reaction="white_check_mark",
                                                           user=UNAUTHORIZED_USER))

        self.mocks["execute_quota_grant"].assert_not_called()
        self.mocks["write_audit"].assert_not_called()


# ---------------------------------------------------------------------------
# T6 — ✅ from authorized user MUST execute exactly once
# ---------------------------------------------------------------------------
class TestT6_AuthorizedConfirmExecutes(HITLBase):
    def test_authorized_confirm_executes_once(self):
        with self._with_pending():
            self.bot.handle_reaction(_make_reaction_event(reaction="white_check_mark",
                                                           user=AUTHORIZED_USER))

        self.mocks["execute_quota_grant"].assert_called_once()
        self.mocks["write_audit"].assert_called_once()
        self.mocks["mark_executed"].assert_called_once()


# ---------------------------------------------------------------------------
# T7 — reaction on unknown ts (no pending record) must NOT execute
# ---------------------------------------------------------------------------
class TestT7_NoPendingRecordNoExec(HITLBase):
    def test_reaction_with_no_pending_record_does_not_execute(self):
        # get_by_message_ts returns None by default in setUp
        self.bot.handle_reaction(_make_reaction_event(reaction="white_check_mark",
                                                       user=AUTHORIZED_USER))

        self.mocks["execute_quota_grant"].assert_not_called()
        self.mocks["write_audit"].assert_not_called()


# ---------------------------------------------------------------------------
# T8 — audit_log.write_audit must be called BEFORE mark_executed (SOC2)
# ---------------------------------------------------------------------------
class TestT8_AuditWrittenBeforeMarkExecuted(HITLBase):
    def test_audit_written_before_mark_executed(self):
        call_order: list[str] = []
        self.mocks["write_audit"].side_effect   = lambda **kw: (call_order.append("write_audit"), "jrv_a_001")[1]
        self.mocks["mark_executed"].side_effect = lambda *a:    call_order.append("mark_executed")

        with self._with_pending():
            self.bot.handle_reaction(_make_reaction_event(reaction="white_check_mark",
                                                           user=AUTHORIZED_USER))

        self.assertIn("write_audit",   call_order, "write_audit must be called")
        self.assertIn("mark_executed", call_order, "mark_executed must be called")
        self.assertLess(
            call_order.index("write_audit"),
            call_order.index("mark_executed"),
            "write_audit must happen BEFORE mark_executed (SOC2: audit row before state change)",
        )


# ---------------------------------------------------------------------------
# T9 — double-confirm (replayed reaction) must NOT execute twice
# ---------------------------------------------------------------------------
class TestT9_DoubleConfirmIdempotent(HITLBase):
    def test_double_confirm_executes_only_once(self):
        evt = _make_reaction_event(reaction="white_check_mark", user=AUTHORIZED_USER)
        with self._with_pending():
            self.bot.handle_reaction(evt)
            self.bot.handle_reaction(evt)  # same event replayed

        self.assertEqual(
            self.mocks["execute_quota_grant"].call_count, 1,
            "Replayed ✅ must be deduped — execute_quota_grant must run exactly once",
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
