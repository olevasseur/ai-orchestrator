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
    # Populated only by executors that stage their work in an isolated workspace
    # (currently CodexExecutor with workspace_strategy="worktree"). `diff` is the
    # unified-diff patch produced inside that workspace; `workspace_path` is the
    # filesystem path where execution actually ran. Empty string means "ran in
    # the caller's repo_path, no separate workspace".
    diff: str = ""
    workspace_path: str = ""


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
