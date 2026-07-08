"""
Tests for the workflow architecture — Phases 1-4.
Run: python3 test_workflow.py
"""
import json
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Add project root
sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Unit tests: workflow_parser._validate_plan
# ---------------------------------------------------------------------------

class TestValidatePlan(unittest.TestCase):
    def setUp(self):
        from workflow_parser import _validate_plan
        self.vp = _validate_plan

    def test_sequential_step_numbers(self):
        plan = {"steps": [
            {"step": 99, "action": "lookup", "target_email": "a@b.com"},
            {"step": 1,  "action": "quota_grant", "target_email": "a@b.com"},
        ]}
        out = self.vp(plan)
        self.assertEqual([s["step"] for s in out["steps"]], [1, 2])

    def test_default_product_set(self):
        plan = {"steps": [{"step": 1, "action": "quota_grant", "target_email": "a@b.com", "credits": 100}]}
        out = self.vp(plan)
        self.assertEqual(out["steps"][0]["product"], "generative_credit")

    def test_default_duration_set(self):
        plan = {"steps": [{"step": 1, "action": "quota_grant", "target_email": "a@b.com"}]}
        out = self.vp(plan)
        self.assertEqual(out["steps"][0]["duration_days"], 30)

    def test_invalid_tier_stripped(self):
        plan = {"steps": [{"step": 1, "action": "quota_grant", "target_email": "a@b.com", "tier": "ultimate"}]}
        out = self.vp(plan)
        self.assertNotIn("tier", out["steps"][0])

    def test_reduce_grant_auto_injects_get_info(self):
        plan = {"steps": [
            {"step": 1, "action": "reduce_grant", "target_email": "x@y.com", "credits": 100}
        ]}
        out = self.vp(plan)
        self.assertEqual(len(out["steps"]), 2)
        self.assertEqual(out["steps"][0]["action"], "get_info")
        self.assertTrue(out["steps"][0]["pre_confirm"])
        self.assertEqual(out["steps"][1]["action"], "reduce_grant")

    def test_reduce_grant_no_duplicate_get_info(self):
        """If get_info already precedes reduce_grant, don't inject again."""
        plan = {"steps": [
            {"step": 1, "action": "get_info", "target_email": "x@y.com", "pre_confirm": True},
            {"step": 2, "action": "reduce_grant", "target_email": "x@y.com", "credits": 100},
        ]}
        out = self.vp(plan)
        actions = [s["action"] for s in out["steps"]]
        self.assertEqual(actions.count("get_info"), 1)

    def test_pre_confirm_defaults_to_false(self):
        plan = {"steps": [{"step": 1, "action": "lookup", "target_email": "a@b.com"}]}
        out = self.vp(plan)
        self.assertFalse(out["steps"][0]["pre_confirm"])


# ---------------------------------------------------------------------------
# Unit tests: workflow_parser.parse_workflow (mocked LLM)
# ---------------------------------------------------------------------------

class TestParseWorkflow(unittest.TestCase):
    def _make_mock_response(self, tool_input: dict):
        block = MagicMock()
        block.type = "tool_use"
        block.name = "build_workflow"
        block.input = tool_input
        resp = MagicMock()
        resp.content = [block]
        return resp

    @patch("workflow_parser._get_client")
    def test_single_step_quota_grant(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._make_mock_response({
            "summary": "Grant 1000 credits to a@b.com",
            "steps": [{"step": 1, "action": "quota_grant", "target_email": "a@b.com", "credits": 1000}],
            "needs_clarification": False,
            "confidence": 0.95,
        })
        mock_client_fn.return_value = mock_client

        from workflow_parser import parse_workflow
        plan = parse_workflow("give a@b.com 1000 credits")

        self.assertFalse(plan["needs_clarification"])
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["action"], "quota_grant")
        self.assertEqual(plan["steps"][0]["credits"], 1000)

    @patch("workflow_parser._get_client")
    def test_multi_step_create_with_credits(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._make_mock_response({
            "summary": "Create pro account with 50k credits",
            "steps": [
                {"step": 1, "action": "create_account", "target_email": "d@y.com"},
                {"step": 2, "action": "quota_grant", "target_email": "d@y.com",
                 "credits": 50000, "tier": "pro"},
            ],
            "needs_clarification": False,
            "confidence": 0.95,
        })
        mock_client_fn.return_value = mock_client

        from workflow_parser import parse_workflow
        plan = parse_workflow("create pro account with 50k credits for d@y.com")

        self.assertEqual(len(plan["steps"]), 2)
        self.assertEqual(plan["steps"][0]["action"], "create_account")
        self.assertEqual(plan["steps"][1]["action"], "quota_grant")
        self.assertEqual(plan["steps"][1]["credits"], 50000)

    @patch("workflow_parser._get_client")
    def test_clarification_needed(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._make_mock_response({
            "summary": "Grant credits",
            "steps": [],
            "needs_clarification": True,
            "clarifying_question": "How many credits?",
            "confidence": 0.4,
        })
        mock_client_fn.return_value = mock_client

        from workflow_parser import parse_workflow
        plan = parse_workflow("give them a lot of credits")

        self.assertTrue(plan["needs_clarification"])
        self.assertEqual(plan["clarifying_question"], "How many credits?")


# ---------------------------------------------------------------------------
# Unit tests: workflow_executor
# ---------------------------------------------------------------------------

class TestWorkflowExecutor(unittest.TestCase):
    @patch("workflow_executor.heygen")
    @patch("workflow_executor.write_audit")
    def test_single_step_success(self, mock_audit, mock_heygen):
        mock_heygen.get_user_state.return_value = {"user_id": "u1", "quotas": {}}
        mock_heygen.execute_quota_grant.return_value = {"granted": True, "action": "credit_top_up"}
        mock_audit.return_value = "audit_123"

        from workflow_executor import execute_workflow
        plan = {
            "steps": [{"step": 1, "action": "quota_grant", "target_email": "a@b.com",
                       "credits": 100, "product": "generative_credit", "duration_days": 30}]
        }
        result = execute_workflow(plan, actor_slack_id="U1", channel_id="C1", message_ts="ts1")

        self.assertTrue(result.all_succeeded)
        self.assertIsNone(result.failed)
        self.assertEqual(len(result.completed), 1)
        mock_heygen.execute_quota_grant.assert_called_once()

    @patch("workflow_executor.heygen")
    @patch("workflow_executor.write_audit")
    def test_multi_step_first_fails(self, mock_audit, mock_heygen):
        mock_heygen.get_user_state.return_value = {"user_id": "u1"}
        mock_heygen.execute_create_account.side_effect = RuntimeError("CMS 500")
        mock_audit.return_value = "audit_123"

        from workflow_executor import execute_workflow
        plan = {
            "steps": [
                {"step": 1, "action": "create_account", "target_email": "a@b.com"},
                {"step": 2, "action": "quota_grant", "target_email": "a@b.com", "credits": 100},
            ]
        }
        result = execute_workflow(plan, actor_slack_id="U1", channel_id="C1", message_ts="ts1")

        self.assertFalse(result.all_succeeded)
        self.assertTrue(result.partial is False)  # nothing completed
        self.assertIsNotNone(result.failed)
        self.assertEqual(result.failed.action, "create_account")
        self.assertEqual(len(result.remaining_steps), 1)
        self.assertEqual(result.remaining_steps[0]["action"], "quota_grant")

    @patch("workflow_executor.heygen")
    @patch("workflow_executor.write_audit")
    def test_multi_step_second_fails(self, mock_audit, mock_heygen):
        mock_heygen.get_user_state.return_value = {"user_id": "u1"}
        mock_heygen.execute_create_account.return_value = {"created": True, "email": "a@b.com"}
        mock_heygen.execute_quota_grant.side_effect = RuntimeError("quota error")
        mock_audit.return_value = "audit_123"

        from workflow_executor import execute_workflow
        plan = {
            "steps": [
                {"step": 1, "action": "create_account", "target_email": "a@b.com"},
                {"step": 2, "action": "quota_grant", "target_email": "a@b.com", "credits": 100,
                 "product": "generative_credit", "duration_days": 30},
            ]
        }
        result = execute_workflow(plan, actor_slack_id="U1", channel_id="C1", message_ts="ts1")

        self.assertFalse(result.all_succeeded)
        self.assertTrue(result.partial)  # step 1 completed
        self.assertEqual(len(result.completed), 1)
        self.assertEqual(result.completed[0].action, "create_account")
        self.assertEqual(result.failed.action, "quota_grant")
        self.assertEqual(len(result.remaining_steps), 0)

    @patch("workflow_executor.heygen")
    @patch("workflow_executor.write_audit")
    def test_pre_confirm_only_mode(self, mock_audit, mock_heygen):
        mock_heygen.get_user_state.return_value = {"user_id": "u1", "tier": "pro"}

        from workflow_executor import execute_workflow
        plan = {
            "steps": [
                {"step": 1, "action": "get_info", "target_email": "a@b.com", "pre_confirm": True},
                {"step": 2, "action": "quota_grant", "target_email": "a@b.com", "credits": 100},
            ]
        }
        result = execute_workflow(plan, actor_slack_id="U1", channel_id="C1", message_ts="ts1",
                                  pre_confirm_only=True)

        self.assertTrue(result.all_succeeded)
        self.assertEqual(len(result.completed), 1)
        self.assertEqual(result.completed[0].action, "get_info")
        # quota_grant must NOT have been called
        mock_heygen.execute_quota_grant.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests: conversation_store
# ---------------------------------------------------------------------------

class TestConversationStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        import conversation_store as cs
        cs.DB_PATH = os.path.join(self.tmpdir, "test_conv.sqlite")
        from conversation_store import upsert_conversation, get_conversation, is_active
        from conversation_store import set_plan, confirm_plan, set_state
        self.upsert = upsert_conversation
        self.get = get_conversation
        self.is_active = is_active
        self.set_plan = set_plan
        self.confirm_plan = confirm_plan
        self.set_state = set_state

    def test_basic_upsert_and_get(self):
        self.upsert("ts1", "C1", [], state="GATHERING")
        conv = self.get("ts1")
        self.assertIsNotNone(conv)
        self.assertEqual(conv["state"], "GATHERING")

    def test_is_active_gathering(self):
        self.upsert("ts2", "C1", [], state="GATHERING")
        self.assertTrue(self.is_active("ts2"))

    def test_is_active_done(self):
        self.upsert("ts3", "C1", [], state="DONE")
        self.assertFalse(self.is_active("ts3"))

    def test_planning_is_active(self):
        self.upsert("ts4", "C1", [], state="PLANNING")
        self.assertTrue(self.is_active("ts4"))

    def test_set_plan_transitions_to_planning(self):
        self.upsert("ts5", "C1", [], state="GATHERING")
        plan = {"summary": "test", "steps": [{"step": 1, "action": "lookup"}]}
        self.set_plan("ts5", plan, card_ts="card_ts1")
        conv = self.get("ts5")
        self.assertEqual(conv["state"], "PLANNING")
        self.assertIsNotNone(conv["current_plan"])
        self.assertEqual(conv["current_plan"]["summary"], "test")

    def test_confirm_plan_transitions_to_executing(self):
        self.upsert("ts6", "C1", [], state="PLANNING")
        plan = {"summary": "test", "steps": []}
        self.set_plan("ts6", plan)
        self.confirm_plan("ts6")
        conv = self.get("ts6")
        self.assertEqual(conv["state"], "EXECUTING")
        self.assertIsNotNone(conv["final_plan"])

    def test_done_not_active(self):
        self.upsert("ts7", "C1", [], state="GATHERING")
        self.set_state("ts7", "DONE")
        self.assertFalse(self.is_active("ts7"))


# ---------------------------------------------------------------------------
# Unit tests: slack_client plan card builders
# ---------------------------------------------------------------------------

class TestPlanCard(unittest.TestCase):
    def test_build_plan_card_structure(self):
        from slack_client import build_plan_card
        plan = {
            "summary": "Create account and grant credits",
            "steps": [
                {"step": 1, "action": "create_account", "target_email": "a@b.com"},
                {"step": 2, "action": "quota_grant", "target_email": "a@b.com",
                 "credits": 1000, "product": "generative_credit", "duration_days": 30},
            ]
        }
        blocks = build_plan_card(plan, "jrv_p_test123")
        # Should have header, summary section, divider, 2 step sections, pre_confirm context?, divider, actions, context
        types = [b["type"] for b in blocks]
        self.assertIn("header", types)
        self.assertIn("actions", types)
        # Actions block should have confirm and cancel buttons
        actions_block = next(b for b in blocks if b["type"] == "actions")
        action_ids = [e["action_id"] for e in actions_block["elements"]]
        self.assertIn("confirm_plan", action_ids)
        self.assertIn("cancel_plan", action_ids)
        # Pending ID in button values
        values = [e["value"] for e in actions_block["elements"]]
        self.assertIn("jrv_p_test123", values)

    def test_pre_confirm_steps_hidden(self):
        from slack_client import build_plan_card
        plan = {
            "summary": "Deduct credits",
            "steps": [
                {"step": 1, "action": "get_info", "target_email": "a@b.com", "pre_confirm": True},
                {"step": 2, "action": "reduce_grant", "target_email": "a@b.com", "credits": 100},
            ]
        }
        blocks = build_plan_card(plan, "pid1")
        # get_info should NOT appear as a step section (it's pre_confirm)
        section_texts = [b.get("text", {}).get("text", "") for b in blocks if b["type"] == "section"]
        self.assertFalse(any("get_info" in t for t in section_texts))
        self.assertTrue(any("reduce_grant" in t for t in section_texts))

    def test_fmt_step_all_fields(self):
        from slack_client import _fmt_step
        step = {
            "action": "quota_grant",
            "target_email": "x@y.com",
            "credits": 5000,
            "product": "generative_credit",
            "duration_days": 90,
            "tier": "pro",
        }
        result = _fmt_step(step)
        self.assertIn("quota_grant", result)
        self.assertIn("x@y.com", result)
        self.assertIn("5,000", result)
        self.assertIn("90d", result)


# ---------------------------------------------------------------------------
# Integration smoke test: refine_workflow (mocked)
# ---------------------------------------------------------------------------

class TestRefineWorkflow(unittest.TestCase):
    def _make_mock_response(self, tool_input: dict):
        block = MagicMock()
        block.type = "tool_use"
        block.name = "refine_workflow"
        block.input = tool_input
        resp = MagicMock()
        resp.content = [block]
        return resp

    @patch("workflow_parser._get_client")
    def test_answer_type(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._make_mock_response({
            "type": "answer",
            "answer": "generative_credit is used for AI video generation.",
        })
        mock_client_fn.return_value = mock_client

        from workflow_parser import refine_workflow
        plan = {"summary": "test", "steps": []}
        result = refine_workflow("what is generative_credit?", current_plan=plan)

        self.assertEqual(result["type"], "answer")
        self.assertIn("generative_credit", result["answer"])

    @patch("workflow_parser._get_client")
    def test_update_type(self, mock_client_fn):
        updated = {
            "summary": "Updated plan",
            "steps": [{"step": 1, "action": "quota_grant", "target_email": "a@b.com",
                       "credits": 1000, "duration_days": 90}],
            "needs_clarification": False,
            "confidence": 0.95,
        }
        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._make_mock_response({
            "type": "update",
            "updated_plan": updated,
        })
        mock_client_fn.return_value = mock_client

        from workflow_parser import refine_workflow
        plan = {"summary": "original", "steps": [{"step": 1, "action": "quota_grant",
                "target_email": "a@b.com", "credits": 1000, "duration_days": 30}]}
        result = refine_workflow("make it 90 days", current_plan=plan)

        self.assertEqual(result["type"], "update")
        self.assertEqual(result["updated_plan"]["steps"][0]["duration_days"], 90)


if __name__ == "__main__":
    print("=" * 60)
    print("Jarvis Workflow Architecture Tests")
    print("=" * 60)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestValidatePlan, TestParseWorkflow, TestWorkflowExecutor,
        TestConversationStore, TestPlanCard, TestRefineWorkflow,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
