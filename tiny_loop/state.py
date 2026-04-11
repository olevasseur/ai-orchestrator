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
) -> dict:
    return {
        "run_id": run_id,
        "repo_path": repo_path,
        "objective": objective,
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
) -> dict:
    return {
        "iteration": iteration,
        "prompt": prompt,
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
    return json.loads(path.read_text())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
