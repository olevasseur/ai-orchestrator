"""Abstract executor interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    session_id: str = ""


class BaseExecutor(ABC):
    """
    Abstract base for code executors.

    Extension point: implement this interface to add SDK-based execution,
    remote agents, or sandboxed environments.
    """

    @abstractmethod
    def run(
        self,
        prompt: str,
        repo_path: str,
        timeout: int = 600,
        log_stdout_path: str | None = None,
        log_stderr_path: str | None = None,
        resume_session_id: str | None = None,
    ) -> ExecutionResult:
        """Execute the prompt against the repo and return results."""
        ...
