"""
Jarvis Workflow Executor — runs an ordered list of steps deterministically.

Each step is executed in sequence. On failure the executor:
1. Posts what succeeded so far
2. Posts the failure reason
3. Returns a structured ExecutionResult for the bot to render a recovery card

Partial execution is tracked so the bot can offer retry of remaining steps.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import heygen_cms_api as heygen
from audit_log import write_audit


class StepResult:
    def __init__(
        self,
        step: dict[str, Any],
        success: bool,
        result: dict[str, Any],
        elapsed_ms: int,
    ):
        self.step = step
        self.success = success
        self.result = result
        self.elapsed_ms = elapsed_ms
        self.step_num = step.get("step", 0)
        self.action = step.get("action", "unknown")
        self.target_email = step.get("target_email", "")


class ExecutionResult:
    def __init__(
        self,
        completed: list[StepResult],
        failed: StepResult | None,
        remaining_steps: list[dict[str, Any]],
        audit_ids: list[str],
    ):
        self.completed = completed
        self.failed = failed
        self.remaining_steps = remaining_steps
        self.audit_ids = audit_ids

    @property
    def all_succeeded(self) -> bool:
        return self.failed is None and len(self.remaining_steps) == 0

    @property
    def partial(self) -> bool:
        return bool(self.completed) and self.failed is not None


def execute_workflow(
    plan: dict[str, Any],
    actor_slack_id: str,
    channel_id: str,
    message_ts: str,
    progress_cb: Callable[[str], None] | None = None,
    pre_confirm_only: bool = False,
) -> ExecutionResult:
    """
    Execute steps in the workflow plan.

    Args:
        plan: workflow dict with steps[]
        actor_slack_id: Slack user who confirmed
        channel_id: for audit
        message_ts: for audit
        progress_cb: called with short status strings
        pre_confirm_only: if True, only run steps with pre_confirm=True
    """
    steps = plan.get("steps", [])
    if pre_confirm_only:
        steps = [s for s in steps if s.get("pre_confirm", False)]

    completed: list[StepResult] = []
    audit_ids: list[str] = []
    before_states: dict[str, Any] = {}  # keyed by target_email

    for step in steps:
        action = step.get("action", "")
        email = step.get("target_email", "")
        step_num = step.get("step", "?")

        if progress_cb:
            display_email = email.replace("@", "\u200b@")  # zero-width space prevents Slack auto-linking
            progress_cb(f"⏳ Step {step_num}: `{action}` for `{display_email}`...")

        # Fetch before_state for diff (skip for pre_confirm get_info)
        if email and action not in ("get_info", "lookup") and email not in before_states:
            try:
                before_states[email] = heygen.get_user_state(email)
            except Exception:
                before_states[email] = {}

        t0 = time.time()
        try:
            result = _execute_step(step, before_states)
            elapsed_ms = int((time.time() - t0) * 1000)

            # Cache get_info result so subsequent steps can use it
            if action == "get_info" and email:
                before_states[email] = result

            sr = StepResult(step=step, success=True, result=result, elapsed_ms=elapsed_ms)
            completed.append(sr)

            # Write audit asynchronously — don't block user response on CH latency
            def _async_audit(
                _actor=actor_slack_id, _action=action, _result=result,
                _intent=step, _before=before_states.get(email, {}),
                _channel=channel_id, _msg_ts=message_ts,
            ) -> None:
                try:
                    aid = write_audit(
                        actor_slack_id=_actor,
                        action=_action,
                        result="success",
                        intent=_intent,
                        before_state=_before,
                        after_state=_result,
                        channel_id=_channel,
                        message_ts=_msg_ts,
                    )
                    audit_ids.append(aid)
                except Exception as ae:
                    print(f"[audit] WARN step {step_num}: {ae}", flush=True)
                    audit_ids.append(f"err_{step_num}")

            threading.Thread(target=_async_audit, daemon=True).start()

        except Exception as e:
            elapsed_ms = int((time.time() - t0) * 1000)
            error_result = {"error": str(e), "action": action, "email": email}
            sr = StepResult(step=step, success=False, result=error_result, elapsed_ms=elapsed_ms)
            remaining = [s for s in steps if s.get("step", 0) > step_num]
            return ExecutionResult(
                completed=completed,
                failed=sr,
                remaining_steps=remaining,
                audit_ids=audit_ids,
            )

    return ExecutionResult(
        completed=completed,
        failed=None,
        remaining_steps=[],
        audit_ids=audit_ids,
    )


def _execute_step(step: dict[str, Any], before_states: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a single step to the appropriate CMS function."""
    action = step.get("action", "")
    email = step.get("target_email", "")

    if action in ("get_info", "lookup"):
        result = heygen.get_user_state(email)
        if result.get("user_id") is None:
            raise RuntimeError(f"User `{email}` not found in HeyGen")
        return result

    elif action == "quota_grant":
        return heygen.execute_quota_grant(
            email=email,
            tier=step.get("tier"),
            credits=step.get("credits"),
            duration_days=step.get("duration_days", 30),
            product=step.get("product", "generative_credit"),
        )

    elif action == "create_account":
        # Check if account already exists (before_state fetched before this step)
        existing = before_states.get(email, {})
        if existing.get("user_id"):
            # Account exists — skip creation, surface a warning instead of failing
            return {
                "skipped": True,
                "reason": f"Account `{email}` already exists (user_id={existing['user_id']}). Skipping creation.",
                "existing_tier": existing.get("tier", "unknown"),
                "warning": True,
            }
        return heygen.execute_create_account(
            email=email,
            tier=step.get("tier"),
            duration_days=step.get("duration_days"),
            credits=step.get("credits"),
            product=step.get("product", "generative_credit"),
        )

    elif action == "revoke_grant":
        revoke_type = step.get("revoke_type", "subscription")
        quota_id = step.get("quota_id")

        # If quota revoke and no quota_id, try to get it from before_state
        if revoke_type in ("quota", "both") and not quota_id:
            bs = before_states.get(email, {})
            quotas = bs.get("quotas", {})
            product = step.get("product", "generative_credit")
            q = quotas.get(product, {})
            quota_id = q.get("quota_id") if isinstance(q, dict) else None

        results: dict[str, Any] = {"email": email, "action": "revoke_grant", "revoke_type": revoke_type}
        if revoke_type in ("subscription", "both"):
            results["subscription_result"] = heygen.execute_subscription_remove(email)
        if revoke_type in ("quota", "both") and quota_id:
            results["quota_result"] = heygen.execute_quota_expire(quota_id)
        elif revoke_type == "quota" and not quota_id:
            raise RuntimeError("quota_id required for quota revoke — run get_info first")
        return results

    elif action == "reduce_grant":
        credits = step.get("credits")
        product = step.get("product", "generative_credit")
        if not credits:
            raise RuntimeError("credits (amount to deduct) required for reduce_grant")
        return heygen.execute_quota_deduct(email=email, product=product, amount=credits)

    elif action == "bulk_grant":
        emails = step.get("target_emails", [])
        results_list = []
        for e in emails:
            r = heygen.execute_quota_grant(
                email=e,
                tier=step.get("tier"),
                credits=step.get("credits"),
                duration_days=step.get("duration_days", 30),
                product=step.get("product", "generative_credit"),
            )
            results_list.append(r)
        return {"action": "bulk_grant", "count": len(emails), "results": results_list}

    else:
        raise RuntimeError(f"Unknown action: {action}")
