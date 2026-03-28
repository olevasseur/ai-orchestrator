"""
Tests for the active_objective authority model.

Covers:
- RunState default fields
- active_objective set on run creation
- runner uses active_objective, not original task, when planning
- queuing next does not replace active
- persist / load cycle preserves both fields
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.jobs.models import RunState, Status
from orchestrator.storage.store import RunStore


# ---------------------------------------------------------------------------
# RunState fields
# ---------------------------------------------------------------------------

class TestRunStateFields:
    def test_default_active_objective_is_empty(self):
        rs = RunState(run_id="x", repo_path="/tmp")
        assert rs.active_objective == ""

    def test_default_queued_next_is_empty(self):
        rs = RunState(run_id="x", repo_path="/tmp")
        assert rs.queued_next_objective == ""

    def test_set_active_objective_on_construction(self):
        rs = RunState(run_id="x", repo_path="/tmp", active_objective="do the thing")
        assert rs.active_objective == "do the thing"

    def test_set_queued_next_on_construction(self):
        rs = RunState(run_id="x", repo_path="/tmp", queued_next_objective="then this")
        assert rs.queued_next_objective == "then this"

    def test_to_dict_includes_fields(self):
        rs = RunState(run_id="x", repo_path="/tmp",
                      active_objective="work A", queued_next_objective="work B")
        d = rs.to_dict()
        assert d["active_objective"] == "work A"
        assert d["queued_next_objective"] == "work B"

    def test_from_dict_round_trip(self):
        rs = RunState(run_id="x", repo_path="/tmp",
                      active_objective="work A", queued_next_objective="work B")
        rs2 = RunState.from_dict(rs.to_dict())
        assert rs2.active_objective == "work A"
        assert rs2.queued_next_objective == "work B"

    def test_from_dict_missing_fields_use_defaults(self):
        """Old state.yaml without the new fields should load cleanly."""
        d = {"run_id": "x", "repo_path": "/tmp", "status": "queued",
             "current_iteration": 0, "created_at": "t", "updated_at": "t"}
        rs = RunState.from_dict(d)
        assert rs.active_objective == ""
        assert rs.queued_next_objective == ""


# ---------------------------------------------------------------------------
# RunStore persistence round-trip
# ---------------------------------------------------------------------------

class TestRunStorePersistence:
    def test_active_objective_survives_disk_round_trip(self, tmp_path):
        store = RunStore(tmp_path / "run1")
        rs = RunState(run_id="run1", repo_path="/tmp",
                      active_objective="solve world hunger")
        store.write_state(rs.to_dict())

        loaded = store.read_state()
        assert loaded["active_objective"] == "solve world hunger"

    def test_queued_next_survives_disk_round_trip(self, tmp_path):
        store = RunStore(tmp_path / "run1")
        rs = RunState(run_id="run1", repo_path="/tmp",
                      queued_next_objective="next big thing")
        store.write_state(rs.to_dict())

        loaded = store.read_state()
        assert loaded["queued_next_objective"] == "next big thing"

    def test_promote_next_pattern(self, tmp_path):
        """Promote: queued_next → active_objective, clear queued."""
        store = RunStore(tmp_path / "run1")
        rs = RunState(run_id="run1", repo_path="/tmp",
                      active_objective="old work",
                      queued_next_objective="new work")
        store.write_state(rs.to_dict())

        # Simulate promote action
        state = store.read_state()
        state["active_objective"] = state.pop("queued_next_objective")
        state["queued_next_objective"] = ""
        store.write_state(state)

        loaded = store.read_state()
        assert loaded["active_objective"] == "new work"
        assert loaded["queued_next_objective"] == ""


# ---------------------------------------------------------------------------
# Runner uses active_objective for planning
# ---------------------------------------------------------------------------

class TestRunnerUsesActiveObjective:
    """
    Verify _call_planner reads active_objective from state.yaml and passes it
    to planner.plan() rather than the original task.md content.
    """

    def _make_runner(self, tmp_path: Path, task: str, active_objective: str):
        from orchestrator.jobs.runner import OrchestratorRunner
        from orchestrator.utils.config import Config

        store = RunStore(tmp_path / "run")
        store.write_task(task)

        rs = RunState(run_id="run", repo_path=str(tmp_path),
                      status=Status.QUEUED, active_objective=active_objective)
        store.write_state(rs.to_dict())

        planner = MagicMock()
        planner.plan.return_value = {
            "objective": "test", "proposed_prompt": "do it",
            "validation_commands": [], "risks": "", "next_step_framing": "", "done": False,
        }

        cfg = MagicMock(spec=Config)
        cfg.executor_timeout = 60
        cfg.validation_timeout = 30
        cfg.command_allowlist = []
        cfg.command_denylist = []
        cfg.memory_refresh_interval = 5
        cfg.openai_api_key = "x"

        executor = MagicMock()
        runner = OrchestratorRunner(
            store=store, planner=planner, executor=executor, config=cfg, yes=True,
        )
        # Attach a minimal memory mock
        runner.memory = MagicMock()
        runner.memory.load_project_memory.return_value = ""
        runner.memory.load_working_memory.return_value = ""
        runner.memory.load_exec_note.return_value = ""
        return runner, store, planner, rs

    def test_planner_receives_active_objective_not_original_task(self, tmp_path):
        with patch("orchestrator.utils.git.repo_context", return_value=""):
            runner, store, planner, rs = self._make_runner(
                tmp_path,
                task="original task: build review system",
                active_objective="updated: wrap up review-eval, move to fiction",
            )
            runner._call_planner(rs, 0)

        call_args = planner.plan.call_args
        actual_task_arg = call_args[0][0]  # first positional arg
        assert actual_task_arg == "updated: wrap up review-eval, move to fiction"
        assert actual_task_arg != "original task: build review system"

    def test_planner_falls_back_to_task_when_active_objective_empty(self, tmp_path):
        with patch("orchestrator.utils.git.repo_context", return_value=""):
            runner, store, planner, rs = self._make_runner(
                tmp_path,
                task="original task: build review system",
                active_objective="",
            )
            runner._call_planner(rs, 0)

        call_args = planner.plan.call_args
        actual_task_arg = call_args[0][0]
        assert actual_task_arg == "original task: build review system"

    def test_active_objective_update_mid_run_is_picked_up(self, tmp_path):
        """Simulates the web UI updating active_objective in state.yaml while runner is live."""
        with patch("orchestrator.utils.git.repo_context", return_value=""):
            runner, store, planner, rs = self._make_runner(
                tmp_path,
                task="original task",
                active_objective="original task",
            )
            # Simulate web UI writing a new active_objective before next planning call
            state = store.read_state()
            state["active_objective"] = "new steering from human"
            store.write_state(state)

            runner._call_planner(rs, 0)

        call_args = planner.plan.call_args
        assert call_args[0][0] == "new steering from human"

    def test_queuing_next_does_not_change_planner_input(self, tmp_path):
        """queued_next_objective must NOT affect the current planning call."""
        with patch("orchestrator.utils.git.repo_context", return_value=""):
            runner, store, planner, rs = self._make_runner(
                tmp_path,
                task="original task",
                active_objective="current work",
            )
            # Simulate web UI queuing next objective
            state = store.read_state()
            state["queued_next_objective"] = "future work"
            store.write_state(state)

            runner._call_planner(rs, 0)

        call_args = planner.plan.call_args
        assert call_args[0][0] == "current work"
        assert "future work" not in str(call_args)
