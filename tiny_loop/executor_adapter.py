"""Executor adapter for tiny_loop.

tiny_loop historically called Claude directly.  This module is the thin bridge
from tiny_loop to the shared orchestrator executor factory so provider selection
stays in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.executor.base import BaseExecutor, ExecutionResult
from orchestrator.executor.cli_executor import WORKSPACE_WORKTREE, make_executor
from orchestrator.utils.config import Config


@dataclass
class TinyLoopExecutor:
    provider: str
    workspace_strategy: str
    apply_policy: str
    timeout: int
    executor: BaseExecutor
    audit_worktrees_after_run: bool = True
    auto_remove_clean_merged_worktrees: bool = False

    @property
    def display_name(self) -> str:
        return self.provider.capitalize()

    @property
    def supports_resume(self) -> bool:
        # CodexExecutor intentionally rejects resume_session_id today.
        return self.provider == "claude"

    @property
    def uses_isolated_workspace(self) -> bool:
        return (
            self.provider == "codex"
            and self.workspace_strategy == WORKSPACE_WORKTREE
        )

    def run(
        self,
        prompt: str,
        repo_path: str,
        *,
        resume_session_id: str | None = None,
    ) -> ExecutionResult:
        return self.executor.run(
            prompt=prompt,
            repo_path=repo_path,
            timeout=self.timeout,
            resume_session_id=resume_session_id if self.supports_resume else None,
        )


def build_tiny_loop_executor(timeout_override: int | None = None) -> TinyLoopExecutor:
    """Build the configured tiny_loop executor from config.yaml/env."""
    cfg = Config.load()
    timeout = timeout_override if timeout_override is not None else cfg.executor_timeout
    executor = make_executor(
        cfg.executor_mode,
        cfg.claude_cli_path,
        provider=cfg.executor_provider,
        codex_cli_path=cfg.codex_cli_path,
        executor_workspace_strategy=cfg.executor_workspace_strategy,
        executor_worktree_base_dir=cfg.executor_worktree_base_dir,
        executor_apply_policy=cfg.executor_apply_policy,
    )
    return TinyLoopExecutor(
        provider=cfg.executor_provider,
        workspace_strategy=cfg.executor_workspace_strategy,
        apply_policy=cfg.executor_apply_policy,
        timeout=timeout,
        executor=executor,
        audit_worktrees_after_run=cfg.audit_worktrees_after_run,
        auto_remove_clean_merged_worktrees=cfg.auto_remove_clean_merged_worktrees,
    )


def write_workspace_diff_artifact(
    result: ExecutionResult,
    artifact_dir: Path,
    iteration: int,
    provider: str,
) -> str:
    """Persist an isolated-workspace diff, returning the artifact path."""
    if not result.diff:
        return ""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"iteration_{iteration}_{provider}_workspace.diff"
    path.write_text(result.diff)
    return str(path)
