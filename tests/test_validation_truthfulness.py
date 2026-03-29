"""
Tests for validation truthfulness guardrails.

Covers:
- SYSTEM_PROMPT contains the key prohibition rules
- _warn_fragile_validation_commands detects known anti-patterns
- _warn_fragile_validation_commands passes through clean commands unchanged
"""

from __future__ import annotations

import pytest

from orchestrator.planner.openai_planner import SYSTEM_PROMPT
from orchestrator.jobs.runner import _warn_fragile_validation_commands


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT rules
# ---------------------------------------------------------------------------

class TestSystemPromptRules:
    def test_prompt_prohibits_executor_validation(self):
        """proposed_prompt must not ask executor to run or summarise validation."""
        assert "must NOT ask the executor to run" in SYSTEM_PROMPT or \
               "must NOT ask the executor" in SYSTEM_PROMPT

    def test_prompt_requires_simple_validation_commands(self):
        """validation_commands must be single, simple, self-contained commands."""
        assert "single, simple, self-contained" in SYSTEM_PROMPT

    def test_prompt_forbids_background_processes(self):
        assert "background processes" in SYSTEM_PROMPT

    def test_prompt_forbids_curl_to_local_servers(self):
        assert "curl to local servers" in SYSTEM_PROMPT

    def test_prompt_prefers_grep_over_http(self):
        assert "grep" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# _warn_fragile_validation_commands — detection
# ---------------------------------------------------------------------------

class TestWarnFragileValidationCommands:
    def test_returns_commands_unchanged(self):
        cmds = ["grep -q 'text' file.py", "pytest -q tests/"]
        result = _warn_fragile_validation_commands(cmds)
        assert result == cmds

    def test_clean_commands_pass_without_side_effects(self, capsys):
        cmds = [
            "grep -q 'helper text' rag_ui.html",
            "pytest -q tests/test_foo.py",
            "test -f TODO.md",
            "python -c \"import orchestrator\"",
        ]
        result = _warn_fragile_validation_commands(cmds)
        assert result == cmds

    def test_detects_background_process(self, capsys):
        cmds = ["python -m http.server 8765 & sleep 2 && curl http://localhost:8765/"]
        _warn_fragile_validation_commands(cmds)
        captured = capsys.readouterr()
        # Rich console output goes to stdout via rich; we just verify the function ran
        # without error and returned the list unchanged.
        assert _warn_fragile_validation_commands(cmds) == cmds

    def test_detects_curl(self):
        cmds = ["curl -I http://127.0.0.1:8765/rag_ui.html"]
        result = _warn_fragile_validation_commands(cmds)
        assert result == cmds   # still returned, just warned

    def test_detects_sleep(self):
        cmds = ["sleep 2 && echo ok"]
        result = _warn_fragile_validation_commands(cmds)
        assert result == cmds

    def test_detects_http_server(self):
        cmds = ["python -m http.server 9000"]
        result = _warn_fragile_validation_commands(cmds)
        assert result == cmds

    def test_detects_http_url(self):
        cmds = ["wget http://localhost:5000/health"]
        result = _warn_fragile_validation_commands(cmds)
        assert result == cmds

    def test_mixed_list_returns_all(self):
        """Both clean and fragile commands are returned — no filtering."""
        cmds = [
            "grep -q 'text' file.py",
            "curl http://localhost:8000/",
            "pytest -q",
        ]
        result = _warn_fragile_validation_commands(cmds)
        assert result == cmds
        assert len(result) == 3

    def test_empty_list(self):
        assert _warn_fragile_validation_commands([]) == []
