"""
Storage layer: manages run directories and persisted artifacts.

Directory layout:
  <log_dir>/
    <run-id>/
      state.yaml              # current run state
      task.md                 # original task text
      iterations/
        <n>/
          planner_request.json
          planner_response.json
          approved_prompt.md
          executor_stdout.log
          executor_stderr.log
          executor_exit_code.txt
          git_diff.txt
          validation_stdout.log
          validation_stderr.log
          iteration_state.yaml

Run IDs are <repo-basename>-<YYYYMMDD-HHMMSS> for readability.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class RunStore:
    """Manages all filesystem I/O for a single run."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, log_dir: str, repo_path: str) -> "RunStore":
        """Create a brand-new run directory."""
        base = Path(log_dir).expanduser()
        repo_name = Path(repo_path).resolve().name
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_id = f"{repo_name}-{ts}"
        return cls(base / run_id)

    @classmethod
    def from_run_id(cls, log_dir: str, run_id: str) -> "RunStore":
        base = Path(log_dir).expanduser()
        return cls(base / run_id)

    @classmethod
    def latest(cls, log_dir: str) -> "RunStore | None":
        """Return the most-recently modified run, or None."""
        base = Path(log_dir).expanduser()
        if not base.exists():
            return None
        runs = sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        runs = [r for r in runs if r.is_dir()]
        return cls(runs[0]) if runs else None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.yaml"

    def iteration_dir(self, n: int) -> Path:
        d = self.run_dir / "iterations" / str(n)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def read_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            with self.state_path.open() as f:
                return yaml.safe_load(f) or {}
        return {}

    def write_state(self, state: dict[str, Any]) -> None:
        with self.state_path.open("w") as f:
            yaml.dump(state, f, default_flow_style=False, sort_keys=False)

    # ------------------------------------------------------------------
    # Task
    # ------------------------------------------------------------------

    def write_task(self, task: str) -> None:
        (self.run_dir / "task.md").write_text(task)

    def read_task(self) -> str:
        p = self.run_dir / "task.md"
        return p.read_text() if p.exists() else ""

    # ------------------------------------------------------------------
    # Iteration artifacts
    # ------------------------------------------------------------------

    def write_planner_request(self, n: int, data: dict) -> None:
        (self.iteration_dir(n) / "planner_request.json").write_text(
            json.dumps(data, indent=2)
        )

    def write_planner_response(self, n: int, data: dict) -> None:
        (self.iteration_dir(n) / "planner_response.json").write_text(
            json.dumps(data, indent=2)
        )

    def write_approved_prompt(self, n: int, prompt: str) -> None:
        (self.iteration_dir(n) / "approved_prompt.md").write_text(prompt)

    def write_executor_output(
        self, n: int, stdout: str, stderr: str, exit_code: int
    ) -> None:
        d = self.iteration_dir(n)
        (d / "executor_stdout.log").write_text(stdout)
        (d / "executor_stderr.log").write_text(stderr)
        (d / "executor_exit_code.txt").write_text(str(exit_code))

    def write_git_diff(self, n: int, diff: str) -> None:
        (self.iteration_dir(n) / "git_diff.txt").write_text(diff)

    def write_validation_output(self, n: int, stdout: str, stderr: str) -> None:
        d = self.iteration_dir(n)
        (d / "validation_stdout.log").write_text(stdout)
        (d / "validation_stderr.log").write_text(stderr)

    def write_iteration_state(self, n: int, state: dict) -> None:
        p = self.iteration_dir(n) / "iteration_state.yaml"
        with p.open("w") as f:
            yaml.dump(state, f, default_flow_style=False, sort_keys=False)

    def read_iteration_state(self, n: int) -> dict:
        p = self.iteration_dir(n) / "iteration_state.yaml"
        if p.exists():
            with p.open() as f:
                return yaml.safe_load(f) or {}
        return {}

    def read_executor_output(self, n: int) -> dict:
        d = self.iteration_dir(n)
        result = {}
        for key, fname in [
            ("stdout", "executor_stdout.log"),
            ("stderr", "executor_stderr.log"),
            ("exit_code", "executor_exit_code.txt"),
            ("git_diff", "git_diff.txt"),
            ("validation_stdout", "validation_stdout.log"),
        ]:
            p = d / fname
            result[key] = p.read_text() if p.exists() else ""
        return result

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_iterations(self) -> list[int]:
        itr_dir = self.run_dir / "iterations"
        if not itr_dir.exists():
            return []
        return sorted(int(d.name) for d in itr_dir.iterdir() if d.name.isdigit())
