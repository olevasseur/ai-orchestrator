"""
Regression tests for the stale approved-prompt bug.

Root cause: /continue did not clear session.current_plan, so a subsequent
/approve with no edited prompt could fall back to the previous iteration's
proposed_prompt via (session.current_plan or {}).get("proposed_prompt", "").

Fix: session.current_plan = None at the start of /continue.

Also includes tests for the json_object mode requirement in tiny_loop/prompts.py:
when json_mode=True the word 'json' must appear in the generated prompt text,
satisfying the OpenAI API constraint for json_object response format.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from orchestrator.web.server import app, session
from tiny_loop.prompts import build_initial_prompt, build_continuation_prompt
from tiny_loop.reviewer import build_reviewer_packet, REVIEWER_SYSTEM_PROMPT, INITIAL_PLANNER_SYSTEM_PROMPT


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


class TestJsonModePrompts:
    """Ensure 'json' appears in prompt text when json_mode=True.

    The OpenAI API raises an error if response_format={"type": "json_object"}
    is requested but the word 'json' does not appear anywhere in the prompt.
    """

    # ------------------------------------------------------------------ #
    # build_initial_prompt                                                 #
    # ------------------------------------------------------------------ #

    def test_initial_prompt_json_mode_contains_json(self):
        """json_mode=True must produce a prompt that contains the word 'json'."""
        prompt = build_initial_prompt(
            step="Do something",
            repo_context="main branch",
            json_mode=True,
        )
        assert "json" in prompt.lower()

    def test_initial_prompt_default_mode_no_json_suffix(self):
        """Without json_mode the prompt should not contain the json reminder."""
        prompt = build_initial_prompt(
            step="Do something",
            repo_context="main branch",
        )
        # The base prompt text doesn't mention json
        assert "json" not in prompt.lower()

    def test_initial_prompt_json_mode_false_no_json_suffix(self):
        """Explicit json_mode=False is the same as the default."""
        prompt = build_initial_prompt(
            step="Do something",
            repo_context="main branch",
            json_mode=False,
        )
        assert "json" not in prompt.lower()

    # ------------------------------------------------------------------ #
    # build_continuation_prompt                                            #
    # ------------------------------------------------------------------ #

    def test_continuation_prompt_json_mode_contains_json(self):
        """json_mode=True must produce a continuation prompt with 'json'."""
        prompt = build_continuation_prompt(
            objective="Ship the feature",
            next_step="Write the test",
            previous_summaries=[
                {
                    "iteration": 1,
                    "reviewer_decision": {"completion_assessment": "good start"},
                }
            ],
            json_mode=True,
        )
        assert "json" in prompt.lower()

    def test_continuation_prompt_default_mode_no_json_suffix(self):
        """Without json_mode the continuation prompt has no json reminder."""
        prompt = build_continuation_prompt(
            objective="Ship the feature",
            next_step="Write the test",
            previous_summaries=[],
        )
        assert "json" not in prompt.lower()

    def test_continuation_prompt_json_mode_false_no_json_suffix(self):
        """Explicit json_mode=False for continuation prompt."""
        prompt = build_continuation_prompt(
            objective="Ship the feature",
            next_step="Write the test",
            previous_summaries=[],
            json_mode=False,
        )
        assert "json" not in prompt.lower()

    def test_continuation_prompt_json_mode_preserves_step(self):
        """json_mode suffix does not overwrite the step content."""
        step = "Add the frobnicate() function"
        prompt = build_continuation_prompt(
            objective="Frobnicate",
            next_step=step,
            previous_summaries=[],
            json_mode=True,
        )
        assert step in prompt
        assert "json" in prompt.lower()


class TestJsonModeReviewerPacket:
    """Ensure 'json' appears in the reviewer packet when json_mode=True.

    The OpenAI Responses API requires the word 'json' in the input (user)
    message when text={"format": {"type": "json_object"}} is in use.
    The reviewer system prompts already contain 'JSON' but the input message
    (built by build_reviewer_packet) must also contain it to be safe.
    """

    _BASE_KWARGS = dict(
        objective="Do a thing",
        iteration_number=1,
        max_iterations=5,
        claude_output="Claude did the thing.",
        git_diff="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+x = 1",
        previous_summaries=[],
        current_step="Implement the thing",
    )

    def test_reviewer_packet_json_mode_contains_json(self):
        """json_mode=True must produce a packet containing the word 'json'."""
        packet = build_reviewer_packet(**self._BASE_KWARGS, json_mode=True)
        assert "json" in packet.lower()

    def test_reviewer_packet_default_no_json_reminder(self):
        """Without json_mode the packet contains no json reminder suffix."""
        packet = build_reviewer_packet(**self._BASE_KWARGS)
        # The base packet text itself (objective, diff, etc.) doesn't include 'json'
        # unless the objective or output happens to mention it.
        assert "json" not in packet.lower()

    def test_reviewer_packet_json_mode_false_no_json_reminder(self):
        """Explicit json_mode=False is identical to the default."""
        packet = build_reviewer_packet(**self._BASE_KWARGS, json_mode=False)
        assert "json" not in packet.lower()

    def test_reviewer_packet_json_mode_preserves_objective(self):
        """json_mode suffix must not overwrite or truncate core packet content."""
        packet = build_reviewer_packet(**self._BASE_KWARGS, json_mode=True)
        assert "Do a thing" in packet
        assert "Implement the thing" in packet

    # ------------------------------------------------------------------ #
    # System-prompt sanity checks (no API call needed)                    #
    # ------------------------------------------------------------------ #

    def test_reviewer_system_prompt_contains_json(self):
        """The reviewer system prompt itself satisfies the 'json' requirement."""
        assert "json" in REVIEWER_SYSTEM_PROMPT.lower()

    def test_initial_planner_system_prompt_contains_json(self):
        """The initial planner system prompt itself satisfies the 'json' requirement."""
        assert "json" in INITIAL_PLANNER_SYSTEM_PROMPT.lower()
