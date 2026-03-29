"""
Regression tests for iteration-status grounding in validation results.

Root cause fixed: itr_state.status was unconditionally set to SUCCEEDED
regardless of validation results. A failed validation step could coexist
with a "succeeded" badge in the UI.

Fix: OrchestratorRunner._status_from_validation() derives status from the
recorded validation result classifications.
"""

from __future__ import annotations

import pytest

from orchestrator.jobs.models import Status
from orchestrator.jobs.runner import OrchestratorRunner


class TestStatusFromValidation:
    # ------------------------------------------------------------------
    # No commands
    # ------------------------------------------------------------------

    def test_no_validation_commands_is_succeeded(self):
        assert OrchestratorRunner._status_from_validation([]) == Status.SUCCEEDED

    # ------------------------------------------------------------------
    # All pass
    # ------------------------------------------------------------------

    def test_all_passed_is_succeeded(self):
        results = [
            {"cmd": "pytest", "classification": "passed", "exit_code": 0},
            {"cmd": "test -f foo.md", "classification": "passed", "exit_code": 0},
        ]
        assert OrchestratorRunner._status_from_validation(results) == Status.SUCCEEDED

    # ------------------------------------------------------------------
    # implementation_failure → FAILED
    # ------------------------------------------------------------------

    def test_single_implementation_failure_is_failed(self):
        results = [{"cmd": "pytest", "classification": "implementation_failure", "exit_code": 1}]
        assert OrchestratorRunner._status_from_validation(results) == Status.FAILED

    def test_mixed_pass_and_failure_is_failed(self):
        """One green check and one red X must still produce FAILED — not SUCCEEDED."""
        results = [
            {"cmd": "grep -q 'text' file.py", "classification": "passed", "exit_code": 0},
            {"cmd": "pytest -q",              "classification": "implementation_failure", "exit_code": 1},
        ]
        assert OrchestratorRunner._status_from_validation(results) == Status.FAILED

    def test_multiple_failures_is_failed(self):
        results = [
            {"cmd": "cmd1", "classification": "implementation_failure", "exit_code": 1},
            {"cmd": "cmd2", "classification": "implementation_failure", "exit_code": 2},
        ]
        assert OrchestratorRunner._status_from_validation(results) == Status.FAILED

    # ------------------------------------------------------------------
    # timeout → FAILED
    # ------------------------------------------------------------------

    def test_timeout_is_failed(self):
        results = [{"cmd": "pytest", "classification": "timeout", "exit_code": -1, "timed_out": True}]
        assert OrchestratorRunner._status_from_validation(results) == Status.FAILED

    # ------------------------------------------------------------------
    # missing_tool → NOT FAILED (environment issue, not code failure)
    # ------------------------------------------------------------------

    def test_missing_tool_is_succeeded(self):
        """missing_tool means the test runner wasn't installed, not that code is wrong."""
        results = [{"cmd": "pytest", "classification": "missing_tool", "exit_code": 127}]
        assert OrchestratorRunner._status_from_validation(results) == Status.SUCCEEDED

    def test_missing_tool_alongside_pass_is_succeeded(self):
        results = [
            {"cmd": "grep -q 'x' file", "classification": "passed",      "exit_code": 0},
            {"cmd": "pytest",            "classification": "missing_tool", "exit_code": 127},
        ]
        assert OrchestratorRunner._status_from_validation(results) == Status.SUCCEEDED

    # ------------------------------------------------------------------
    # denied / skipped → NOT FAILED
    # ------------------------------------------------------------------

    def test_denied_command_is_succeeded(self):
        results = [{"cmd": "rm -rf /", "classification": "denied", "exit_code": -2}]
        assert OrchestratorRunner._status_from_validation(results) == Status.SUCCEEDED

    def test_skipped_command_is_succeeded(self):
        results = [{"cmd": "some cmd", "classification": "skipped", "exit_code": -3}]
        assert OrchestratorRunner._status_from_validation(results) == Status.SUCCEEDED

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_missing_classification_key_does_not_crash(self):
        """Malformed result dicts should not crash the helper."""
        results = [{"cmd": "pytest"}]   # no 'classification' key
        assert OrchestratorRunner._status_from_validation(results) == Status.SUCCEEDED

    def test_implementation_failure_beats_missing_tool(self):
        """Even one implementation_failure among other non-fatal results → FAILED."""
        results = [
            {"cmd": "cmd1", "classification": "missing_tool",           "exit_code": 127},
            {"cmd": "cmd2", "classification": "implementation_failure",  "exit_code": 1},
            {"cmd": "cmd3", "classification": "passed",                  "exit_code": 0},
        ]
        assert OrchestratorRunner._status_from_validation(results) == Status.FAILED
