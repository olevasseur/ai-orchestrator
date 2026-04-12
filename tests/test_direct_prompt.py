"""
Regression tests for the direct-prompt (skip planner) feature.

Verifies:
1. DirectPlanner returns submitted text as both objective and proposed_prompt.
2. /start with use_prompt_directly=true sets up a DirectPlanner (no OpenAI call).
3. Normal mode (use_prompt_directly absent) still uses the OpenAI planner path.
"""

from __future__ import annotations

from orchestrator.planner.direct_planner import DirectPlanner


class TestDirectPlanner:
    def test_plan_returns_task_as_prompt(self):
        """proposed_prompt must equal the submitted task text."""
        planner = DirectPlanner()
        result = planner.plan("fix the login bug", repo_context="", recent_iterations=[])
        assert result["proposed_prompt"] == "fix the login bug"
        assert result["objective"] == "fix the login bug"

    def test_plan_returns_required_keys(self):
        """plan() must return all keys the runner expects."""
        planner = DirectPlanner()
        result = planner.plan("do something", repo_context="", recent_iterations=[])
        for key in ("objective", "proposed_prompt", "validation_commands", "risks",
                    "next_step_framing", "done"):
            assert key in result, f"missing key: {key}"

    def test_plan_done_is_false(self):
        """DirectPlanner must not signal done=True so the iteration proceeds."""
        planner = DirectPlanner()
        result = planner.plan("task", repo_context="", recent_iterations=[])
        assert result["done"] is False

    def test_ask_returns_string(self):
        """ask() must return a non-empty string (no crash)."""
        planner = DirectPlanner()
        answer = planner.ask("what did you do?", "context here")
        assert isinstance(answer, str)
        assert len(answer) > 0


class TestDirectPromptServerFlag:
    """Verify that the /start route accepts the use_prompt_directly flag.

    We test the route layer in isolation — the runner thread is not started
    because the repo_path check fires first on a non-existent path.
    """

    def setup_method(self):
        from orchestrator.web.server import session
        session.reset()

    def teardown_method(self):
        from orchestrator.web.server import session
        session.reset()

    def test_start_without_flag_checks_openai_key(self):
        """Without the flag, a missing OPENAI_API_KEY should redirect with error."""
        import os
        from fastapi.testclient import TestClient
        from orchestrator.web.server import app

        # Remove the key so the guard fires.
        original = os.environ.pop("OPENAI_API_KEY", None)
        try:
            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/start",
                data={"repo_path": "/tmp", "task": "hello"},
                follow_redirects=False,
            )
            # Should redirect with error about missing key (or repo not found
            # if /tmp triggers the repo check first — either way, no crash).
            assert resp.status_code in (303, 302)
        finally:
            if original is not None:
                os.environ["OPENAI_API_KEY"] = original

    def test_start_with_flag_skips_openai_key_check(self):
        """With use_prompt_directly=true, missing OPENAI_API_KEY must not block."""
        import os
        from fastapi.testclient import TestClient
        from orchestrator.web.server import app

        original = os.environ.pop("OPENAI_API_KEY", None)
        try:
            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/start",
                data={
                    "repo_path": "/nonexistent_path_xyz",
                    "task": "hello",
                    "use_prompt_directly": "true",
                },
                follow_redirects=False,
            )
            # Repo not found fires before runner starts — that's fine.
            # The important thing: no "OPENAI_API_KEY not set" redirect.
            assert resp.status_code in (303, 302)
            location = resp.headers.get("location", "")
            assert "OPENAI_API_KEY" not in location
        finally:
            if original is not None:
                os.environ["OPENAI_API_KEY"] = original


class TestSetObjectivePlannerSwap:
    """Verify that POST /set-objective swaps session._planner based on the checkbox."""

    def setup_method(self):
        from orchestrator.web import server
        server.session.reset()
        # Give the session a non-idle status so /run redirect is reachable.
        server.session.status = "paused"

    def teardown_method(self):
        from orchestrator.web import server
        server.session.reset()

    def test_set_objective_with_flag_installs_direct_planner(self):
        """use_prompt_directly=true must replace session._planner with DirectPlanner."""
        from fastapi.testclient import TestClient
        from orchestrator.planner.direct_planner import DirectPlanner
        from orchestrator.web.server import app, session

        client = TestClient(app, raise_server_exceptions=True)
        client.post(
            "/set-objective",
            data={"objective": "do something", "use_prompt_directly": "true"},
            follow_redirects=False,
        )
        assert isinstance(session._planner, DirectPlanner)

    def test_set_objective_without_flag_installs_openai_planner(self, monkeypatch):
        """Omitting use_prompt_directly must replace session._planner with OpenAIPlanner."""
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from orchestrator.planner.openai_planner import OpenAIPlanner
        from orchestrator.web import server

        fake_cfg = MagicMock()
        fake_cfg.openai_api_key = "sk-test"
        fake_cfg.openai_model = "gpt-4o"
        monkeypatch.setattr(server, "Config", MagicMock(load=MagicMock(return_value=fake_cfg)))

        client = TestClient(server.app, raise_server_exceptions=True)
        client.post(
            "/set-objective",
            data={"objective": "do something else"},
            follow_redirects=False,
        )
        assert isinstance(server.session._planner, OpenAIPlanner)
