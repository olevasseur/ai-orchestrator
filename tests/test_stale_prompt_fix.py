"""
Regression tests for the stale approved-prompt bug.

Root cause: /continue did not clear session.current_plan, so a subsequent
/approve with no edited prompt could fall back to the previous iteration's
proposed_prompt via (session.current_plan or {}).get("proposed_prompt", "").

Fix: session.current_plan = None at the start of /continue.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from orchestrator.web.server import app, session


@pytest.fixture(autouse=True)
def reset_session():
    """Ensure a clean session before and after every test."""
    session.reset()
    yield
    session.reset()


client = TestClient(app, raise_server_exceptions=True)


class TestContinueClearsCurrentPlan:
    def test_continue_clears_current_plan(self):
        """After /continue, session.current_plan must be None."""
        # Arrange: put session into paused state with a stale plan loaded
        session.status = "paused"
        session.current_plan = {
            "objective": "old objective",
            "proposed_prompt": "old prompt from previous iteration",
            "validation_commands": [],
            "risks": "",
            "next_step_framing": "",
        }
        # post_iter_event.wait() would block — pre-set it so the route doesn't hang
        session.post_iter_event.set()

        resp = client.post("/continue")

        assert resp.status_code in (200, 303)
        assert session.current_plan is None

    def test_continue_does_not_clear_plan_when_not_paused(self):
        """Guard: /continue while not paused is a no-op and must not crash."""
        session.status = "planning"
        session.current_plan = {"proposed_prompt": "something"}

        resp = client.post("/continue")

        # Redirects to /run without touching current_plan
        assert resp.status_code in (200, 303)
        # current_plan unchanged because the route returned early
        assert session.current_plan == {"proposed_prompt": "something"}


class TestApproveCannotReuseStalePrompt:
    def test_approve_without_edit_after_continue_uses_empty_not_stale(self):
        """
        Simulate the exact failure mode: user continues from iteration 0,
        then immediately approves iteration 1 without editing the prompt.

        Before the fix, /approve would fall back to current_plan (stale).
        After the fix, current_plan is None so the fallback yields "".
        """
        # Simulate state after /continue ran (current_plan cleared, status=planning)
        session.status = "awaiting_review"
        session.current_plan = None   # this is what /continue now guarantees

        resp = client.post("/approve", data={"prompt": ""})

        assert resp.status_code in (200, 303)
        # The approved prompt must be "" (empty), not the stale previous plan
        assert session.review_decision is not None
        assert session.review_decision["decision"] == "approved"
        assert session.review_decision["prompt"] == ""

    def test_approve_without_edit_uses_current_plan_when_freshly_set(self):
        """
        Positive case: if review_fn correctly sets a new current_plan for
        iteration N, approving without editing should use that new plan.
        """
        session.status = "awaiting_review"
        session.current_plan = {
            "proposed_prompt": "fresh plan for new iteration",
            "objective": "new objective",
        }

        resp = client.post("/approve", data={"prompt": ""})

        assert resp.status_code in (200, 303)
        assert session.review_decision["prompt"] == "fresh plan for new iteration"

    def test_approve_with_explicit_prompt_always_wins(self):
        """Explicit textarea input always overrides current_plan, stale or not."""
        session.status = "awaiting_review"
        session.current_plan = {"proposed_prompt": "stale prompt"}

        resp = client.post("/approve", data={"prompt": "user edited this prompt"})

        assert resp.status_code in (200, 303)
        assert session.review_decision["prompt"] == "user edited this prompt"
