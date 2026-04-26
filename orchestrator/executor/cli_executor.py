"""
Claude Code CLI executor.

Runs `claude --print "<prompt>"` as a subprocess in the target repo directory.
This is the v1 default.  The Claude Agent SDK path is a future extension.

Key design decisions:
- We use --print flag so Claude Code runs non-interactively and exits.
- stdout/stderr are streamed to log files AND captured for the return value.
- PID is persisted so a crashed orchestrator can detect orphaned processes.
- Timeout kills the subprocess and sets timed_out=True.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from orchestrator.executor.base import BaseExecutor, ExecutionResult


# Workspace-strategy values accepted by CodexExecutor.
WORKSPACE_INPLACE = "inplace"
WORKSPACE_WORKTREE = "worktree"
_VALID_WORKSPACE_STRATEGIES = {WORKSPACE_INPLACE, WORKSPACE_WORKTREE}

# Apply-policy values understood today. CodexExecutor only captures worktree
# diffs; the runner owns any source-repo apply step after artifact persistence.
APPLY_MANUAL = "manual"
APPLY_DISCARD = "discard"
APPLY_AUTO = "auto"
_VALID_APPLY_POLICIES = {APPLY_MANUAL, APPLY_DISCARD, APPLY_AUTO}

DEFAULT_CODEX_WORKTREE_BASE_DIR = "/tmp/ai-orchestrator-executor-worktrees"


class CLIExecutor(BaseExecutor):
    def __init__(self, claude_cli_path: str = "claude") -> None:
        self.claude_cli_path = claude_cli_path

    def run(
        self,
        prompt: str,
        repo_path: str,
        timeout: int = 600,
        log_stdout_path: str | None = None,
        log_stderr_path: str | None = None,
        resume_session_id: str | None = None,
    ) -> ExecutionResult:
        cmd = [
            self.claude_cli_path,
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
        ]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        cmd.append(prompt)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        timed_out = False

        stdout_file = open(log_stdout_path, "w") if log_stdout_path else None
        stderr_file = open(log_stderr_path, "w") if log_stderr_path else None

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(Path(repo_path).resolve()),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def _read_stream(stream, lines, file):
                for line in stream:
                    lines.append(line)
                    if file:
                        file.write(line)
                        file.flush()

            t_out = threading.Thread(
                target=_read_stream, args=(proc.stdout, stdout_lines, stdout_file)
            )
            t_err = threading.Thread(
                target=_read_stream, args=(proc.stderr, stderr_lines, stderr_file)
            )
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                timed_out = True

            t_out.join()
            t_err.join()
            exit_code = proc.returncode if not timed_out else -1

        finally:
            if stdout_file:
                stdout_file.close()
            if stderr_file:
                stderr_file.close()

        # Parse stream-json output: scan lines in reverse for the result event.
        raw_stdout = "".join(stdout_lines)
        result_text = raw_stdout
        session_id = ""
        for line in reversed(stdout_lines):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "result":
                    session_id = event.get("session_id", "")
                    result_text = event.get("result", raw_stdout)
                    break
            except json.JSONDecodeError:
                continue

        return ExecutionResult(
            stdout=result_text,
            stderr="".join(stderr_lines),
            exit_code=exit_code,
            timed_out=timed_out,
            session_id=session_id,
        )


class DemoExecutor(BaseExecutor):
    """
    Fake executor used in --demo mode.
    Returns a canned response without invoking Claude Code.
    """

    def run(
        self,
        prompt: str,
        repo_path: str,
        timeout: int = 600,
        log_stdout_path: str | None = None,
        log_stderr_path: str | None = None,
        resume_session_id: str | None = None,
    ) -> ExecutionResult:
        output = (
            "[DEMO] Claude Code would execute the following prompt:\n\n"
            + prompt[:500]
            + ("\n..." if len(prompt) > 500 else "")
            + "\n\n[DEMO] No actual changes were made."
        )
        if log_stdout_path:
            Path(log_stdout_path).write_text(output)
        if log_stderr_path:
            Path(log_stderr_path).write_text("")
        return ExecutionResult(stdout=output, stderr="", exit_code=0)


class CodexExecutor(BaseExecutor):
    """
    Experimental Codex CLI executor.

    Invokes the Codex CLI's non-interactive `exec` subcommand. Session
    continuation (resume_session_id) is not yet wired up — Codex's session
    model differs from Claude's and needs its own design.

    Workspace isolation
    -------------------
    Because Codex runs with --dangerously-bypass-approvals-and-sandbox, it can
    execute arbitrary shell commands in the directory it's pointed at. Two
    strategies are supported:

      * ``workspace_strategy="inplace"`` (default) — run Codex directly in the
        caller's ``repo_path``. Matches the prior behaviour; intended only for
        repos that are themselves disposable / sandboxed.
      * ``workspace_strategy="worktree"`` — create a disposable git worktree
        under ``worktree_base_dir`` from the source repo's HEAD, run Codex
        there, capture the resulting unified diff, then dispose of the
        worktree. The source repo is never written to.

    The captured diff is returned on ``ExecutionResult.diff`` so the caller
    (runner / UI) can persist it as an artifact and let a human apply it.
    """

    def __init__(
        self,
        codex_cli_path: str = "codex",
        *,
        workspace_strategy: str = WORKSPACE_INPLACE,
        worktree_base_dir: str = DEFAULT_CODEX_WORKTREE_BASE_DIR,
        apply_policy: str = APPLY_MANUAL,
    ) -> None:
        if workspace_strategy not in _VALID_WORKSPACE_STRATEGIES:
            raise ValueError(
                f"Unknown codex workspace_strategy: {workspace_strategy!r}. "
                f"Use one of {sorted(_VALID_WORKSPACE_STRATEGIES)}."
            )
        if apply_policy not in _VALID_APPLY_POLICIES:
            raise ValueError(
                f"Unknown codex apply_policy: {apply_policy!r}. "
                f"Use one of {sorted(_VALID_APPLY_POLICIES)}."
            )
        self.codex_cli_path = codex_cli_path
        self.workspace_strategy = workspace_strategy
        self.worktree_base_dir = worktree_base_dir
        self.apply_policy = apply_policy

    def run(
        self,
        prompt: str,
        repo_path: str,
        timeout: int = 600,
        log_stdout_path: str | None = None,
        log_stderr_path: str | None = None,
        resume_session_id: str | None = None,
    ) -> ExecutionResult:
        if resume_session_id:
            raise NotImplementedError(
                "Codex executor does not yet support session resumption; "
                "resume_session_id must be None."
            )
        if self.workspace_strategy == WORKSPACE_WORKTREE:
            return self._run_in_worktree(
                prompt=prompt,
                repo_path=repo_path,
                timeout=timeout,
                log_stdout_path=log_stdout_path,
                log_stderr_path=log_stderr_path,
            )
        return self._run_codex(
            prompt=prompt,
            cwd=str(Path(repo_path).resolve()),
            timeout=timeout,
            log_stdout_path=log_stdout_path,
            log_stderr_path=log_stderr_path,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_in_worktree(
        self,
        *,
        prompt: str,
        repo_path: str,
        timeout: int,
        log_stdout_path: str | None,
        log_stderr_path: str | None,
    ) -> ExecutionResult:
        repo_root = _resolve_git_root(repo_path)
        base_dir = Path(self.worktree_base_dir).expanduser().resolve()
        base_dir.mkdir(parents=True, exist_ok=True)

        worktree_path = base_dir / f"codex-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        # Sanity check: the worktree must land *under* the configured base dir
        # and nowhere near the source repo or the user's home.
        if base_dir not in worktree_path.parents and worktree_path.parent != base_dir:
            raise RuntimeError(
                f"Refusing to create Codex worktree outside base dir: {worktree_path}"
            )

        _git_worktree_add(repo_root, worktree_path)

        result: ExecutionResult
        diff_text = ""
        try:
            result = self._run_codex(
                prompt=prompt,
                cwd=str(worktree_path),
                timeout=timeout,
                log_stdout_path=log_stdout_path,
                log_stderr_path=log_stderr_path,
            )
            diff_text = _capture_worktree_diff(worktree_path)
        finally:
            # apply_policy="manual"/"discard" both dispose of the worktree once
            # the diff is captured — the diff artifact is the handoff. We
            # always attempt cleanup so we never leak worktrees on success or
            # failure; the path is still surfaced on the result for debugging.
            _git_worktree_remove(repo_root, worktree_path)

        result.diff = diff_text
        result.workspace_path = str(worktree_path)
        return result

    def _run_codex(
        self,
        *,
        prompt: str,
        cwd: str,
        timeout: int,
        log_stdout_path: str | None,
        log_stderr_path: str | None,
    ) -> ExecutionResult:
        cmd = [
            self.codex_cli_path,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ]

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        timed_out = False

        stdout_file = open(log_stdout_path, "w") if log_stdout_path else None
        stderr_file = open(log_stderr_path, "w") if log_stderr_path else None

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def _read_stream(stream, lines, file):
                for line in stream:
                    lines.append(line)
                    if file:
                        file.write(line)
                        file.flush()

            t_out = threading.Thread(
                target=_read_stream, args=(proc.stdout, stdout_lines, stdout_file)
            )
            t_err = threading.Thread(
                target=_read_stream, args=(proc.stderr, stderr_lines, stderr_file)
            )
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                timed_out = True

            t_out.join()
            t_err.join()
            exit_code = proc.returncode if not timed_out else -1

        finally:
            if stdout_file:
                stdout_file.close()
            if stderr_file:
                stderr_file.close()

        result_text, session_id = _parse_codex_jsonl(stdout_lines)

        return ExecutionResult(
            stdout=result_text,
            stderr="".join(stderr_lines),
            exit_code=exit_code,
            timed_out=timed_out,
            session_id=session_id,
        )


def _parse_codex_jsonl(stdout_lines: list[str]) -> tuple[str, str]:
    """Parse Codex `exec --json` JSONL stream.

    Codex emits one JSON event per line. We extract:
    - session_id from the `thread.started` event's `thread_id`
    - result text from the last `item.completed` event whose item is an
      `agent_message` (Codex may emit multiple turns / messages; the final
      agent_message is the user-visible answer).

    Falls back to raw concatenated stdout when the stream is not valid JSONL
    (e.g. Codex was invoked without --json, or crashed before any event).
    """
    raw_stdout = "".join(stdout_lines)
    session_id = ""
    last_message = ""
    saw_any_event = False

    for line in stdout_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        saw_any_event = True
        etype = event.get("type")
        if etype == "thread.started":
            session_id = event.get("thread_id", "") or session_id
        elif etype == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text", "")
                if isinstance(text, str) and text:
                    last_message = text

    if not saw_any_event:
        return raw_stdout, ""
    return (last_message or raw_stdout), session_id


def _resolve_git_root(path: str) -> Path:
    """Return the top-level directory of the git repo containing `path`."""
    out = subprocess.run(
        ["git", "-C", str(Path(path).resolve()), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip()).resolve()


def _git_worktree_add(repo_root: Path, worktree_path: Path) -> None:
    """Create a detached worktree at `worktree_path` from `repo_root`'s HEAD."""
    subprocess.run(
        [
            "git", "-C", str(repo_root),
            "worktree", "add", "--detach", str(worktree_path), "HEAD",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _capture_worktree_diff(worktree_path: Path) -> str:
    """Return a unified diff of all changes Codex made inside the worktree.

    Stages everything (including new and deleted files) so untracked additions
    show up in the diff. The worktree is about to be discarded, so staging is
    purely a vehicle for `git diff --cached`.
    """
    subprocess.run(
        ["git", "-C", str(worktree_path), "add", "-A"],
        capture_output=True, text=True, check=False,
    )
    out = subprocess.run(
        ["git", "-C", str(worktree_path), "diff", "--cached", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return out.stdout


def _git_worktree_remove(repo_root: Path, worktree_path: Path) -> None:
    """Best-effort cleanup of a Codex worktree.

    Tries `git worktree remove --force` first (so git's internal bookkeeping
    stays consistent), then falls back to rmtree + `worktree prune` if git
    refuses (e.g. the directory was already gone). Never raises — cleanup
    failures are surfaced via the still-present workspace_path on the result.
    """
    if not worktree_path.exists():
        # Nothing to remove on disk; still prune git's metadata.
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "prune"],
            capture_output=True, text=True, check=False,
        )
        return
    res = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree_path)],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0 and worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "prune"],
            capture_output=True, text=True, check=False,
        )


def make_executor(
    mode: str,
    claude_cli_path: str = "claude",
    *,
    provider: str = "claude",
    codex_cli_path: str = "codex",
    # Generic workspace-isolation settings (preferred). These apply to any
    # provider whose adapter supports worktree mode; today that is only Codex.
    executor_workspace_strategy: str | None = None,
    executor_worktree_base_dir: str | None = None,
    executor_apply_policy: str | None = None,
    # Legacy Codex-specific aliases. Kept so existing callers and tests that
    # pass `codex_*` keep working. When both forms are supplied, the generic
    # `executor_*` form wins.
    codex_workspace_strategy: str | None = None,
    codex_worktree_base_dir: str | None = None,
    codex_apply_policy: str | None = None,
) -> BaseExecutor:
    """Factory: return the right executor for the configured mode and provider.

    `mode` selects the execution surface (cli vs demo). `provider` selects
    which agent adapter to build for cli mode. Demo mode ignores provider.

    The `executor_*` kwargs configure workspace isolation for providers that
    support it (currently CodexExecutor). They are ignored when the chosen
    provider does not support worktree mode, so call sites can pass them
    unconditionally from Config. The legacy `codex_*` kwargs are still
    accepted for backward compatibility but the generic form wins when both
    are provided.
    """
    workspace_strategy = _coalesce(
        executor_workspace_strategy, codex_workspace_strategy, WORKSPACE_INPLACE,
    )
    worktree_base_dir = _coalesce(
        executor_worktree_base_dir,
        codex_worktree_base_dir,
        DEFAULT_CODEX_WORKTREE_BASE_DIR,
    )
    apply_policy = _coalesce(
        executor_apply_policy, codex_apply_policy, APPLY_MANUAL,
    )

    if mode == "demo":
        return DemoExecutor()
    if mode == "cli":
        if provider == "claude":
            if workspace_strategy == WORKSPACE_WORKTREE:
                # Claude + worktree is not implemented yet (Claude's session
                # model and validation flow need their own design). Fail loud
                # rather than silently running in-place — operators who flip
                # executor_workspace_strategy at the Config level expect it
                # to apply to whichever provider is selected.
                raise ValueError(
                    "executor_workspace_strategy='worktree' is not supported "
                    "for executor_provider='claude' yet. Either switch to "
                    "executor_provider='codex' or set "
                    "executor_workspace_strategy='inplace'."
                )
            return CLIExecutor(claude_cli_path=claude_cli_path)
        if provider == "codex":
            return CodexExecutor(
                codex_cli_path=codex_cli_path,
                workspace_strategy=workspace_strategy,
                worktree_base_dir=worktree_base_dir,
                apply_policy=apply_policy,
            )
        raise ValueError(
            f"Unknown executor provider: {provider!r}. Use 'claude' or 'codex'."
        )
    raise ValueError(f"Unknown executor mode: {mode!r}. Use 'cli' or 'demo'.")


def _coalesce(*values):
    """Return the first non-None argument; the last value is the default."""
    for v in values[:-1]:
        if v is not None:
            return v
    return values[-1]
