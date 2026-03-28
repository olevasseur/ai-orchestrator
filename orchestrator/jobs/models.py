"""
Job and iteration state models.

Statuses (shared by runs and individual commands):
  queued | running | awaiting_review | succeeded | failed | timed_out | stopped
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional


class Status:
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"


@dataclass
class IterationState:
    number: int
    status: str = Status.QUEUED
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    # Planner outputs
    objective: str = ""
    proposed_prompt: str = ""
    validation_commands: list[str] = field(default_factory=list)
    risks: str = ""
    next_step_framing: str = ""

    # Executor outputs
    executor_exit_code: Optional[int] = None
    validation_exit_codes: list[int] = field(default_factory=list)
    # Per-command validation detail: [{cmd, exit_code, classification, timed_out}, ...]
    validation_results: list[dict] = field(default_factory=list)

    # Human decision
    human_decision: str = ""  # approved | edited | rejected | stopped

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "IterationState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class RunState:
    run_id: str
    repo_path: str
    status: str = Status.QUEUED
    current_iteration: int = 0
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    # PID of any long-running executor subprocess
    executor_pid: Optional[int] = None
    # Path to executor log (for resume)
    executor_log_path: Optional[str] = None
    active_objective: str = ""
    queued_next_objective: str = ""

    def touch(self) -> None:
        self.updated_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RunState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
