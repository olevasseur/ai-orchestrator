"""Run state: load/save a single JSON file."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_run_state(
    run_id: str,
    repo_path: str,
    objective: str,
    max_iterations: int,
    executor_provider: str = "claude",
    executor_workspace_strategy: str = "inplace",
) -> dict:
    return {
        "run_id": run_id,
        "repo_path": repo_path,
        "objective": objective,
        "executor_provider": executor_provider,
        "executor_workspace_strategy": executor_workspace_strategy,
        "status": "running",
        "max_iterations": max_iterations,
        "current_iteration": 0,
        "iterations": [],
        "started_at": _now(),
        "ended_at": None,
        "final_outcome": None,
    }


def new_iteration_record(
    iteration: int,
    prompt: str,
    claude_output: str,
    claude_exit_code: int,
    claude_timed_out: bool,
    claude_session_id: str,
    git_diff: str,
    reviewer_packet: str,
    reviewer_decision: dict,
    executor_provider: str = "claude",
    executor_workspace_strategy: str = "inplace",
) -> dict:
    reviewer_decision = _with_executor_prompt_alias(reviewer_decision)
    return {
        "iteration": iteration,
        "prompt": prompt,
        "executor_provider": executor_provider,
        "executor_workspace_strategy": executor_workspace_strategy,
        "executor_output": claude_output,
        "executor_exit_code": claude_exit_code,
        "executor_timed_out": claude_timed_out,
        "executor_session_id": claude_session_id,
        "claude_output": claude_output,
        "claude_exit_code": claude_exit_code,
        "claude_timed_out": claude_timed_out,
        "claude_session_id": claude_session_id,
        "git_diff": git_diff,
        "reviewer_packet": reviewer_packet,
        "reviewer_decision": reviewer_decision,
        "timestamp": _now(),
    }


def save_state(state: dict, path: Path) -> None:
    path.write_text(json.dumps(state, indent=2, default=str))


def load_state(path: Path) -> dict:
    state = json.loads(path.read_text())
    for iteration in state.get("iterations", []):
        reviewer_decision = iteration.get("reviewer_decision")
        if isinstance(reviewer_decision, dict):
            iteration["reviewer_decision"] = _with_executor_prompt_alias(
                reviewer_decision
            )
    return state


def _with_executor_prompt_alias(reviewer_decision: dict) -> dict:
    reviewer_decision = dict(reviewer_decision)
    old_key = "next_prompt_for_claude"
    new_key = "next_prompt_for_executor"

    if new_key not in reviewer_decision and old_key in reviewer_decision:
        reviewer_decision[new_key] = reviewer_decision[old_key]
    if old_key not in reviewer_decision and new_key in reviewer_decision:
        reviewer_decision[old_key] = reviewer_decision[new_key]

    return reviewer_decision


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
