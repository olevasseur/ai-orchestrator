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
import subprocess
import threading
from pathlib import Path

from orchestrator.executor.base import BaseExecutor, ExecutionResult


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
    Experimental Codex CLI executor — scaffolding only.

    The exact non-interactive invocation, output format, and session model
    for the Codex CLI are not yet pinned down (see codex_executor_feasibility.md).
    Construction is allowed so provider selection and config wiring can be
    tested, but `run()` raises until a concrete adapter is implemented.
    """

    def __init__(self, codex_cli_path: str = "codex") -> None:
        self.codex_cli_path = codex_cli_path

    def run(
        self,
        prompt: str,
        repo_path: str,
        timeout: int = 600,
        log_stdout_path: str | None = None,
        log_stderr_path: str | None = None,
        resume_session_id: str | None = None,
    ) -> ExecutionResult:
        raise NotImplementedError(
            "Codex executor is experimental: provider selection is wired up "
            "but the Codex CLI invocation (argv, output format, session "
            "continuation) has not yet been implemented. Set "
            "executor_provider=claude (the default) to run."
        )


def make_executor(
    mode: str,
    claude_cli_path: str = "claude",
    *,
    provider: str = "claude",
    codex_cli_path: str = "codex",
) -> BaseExecutor:
    """Factory: return the right executor for the configured mode and provider.

    `mode` selects the execution surface (cli vs demo). `provider` selects
    which agent adapter to build for cli mode. Demo mode ignores provider.
    """
    if mode == "demo":
        return DemoExecutor()
    if mode == "cli":
        if provider == "claude":
            return CLIExecutor(claude_cli_path=claude_cli_path)
        if provider == "codex":
            return CodexExecutor(codex_cli_path=codex_cli_path)
        raise ValueError(
            f"Unknown executor provider: {provider!r}. Use 'claude' or 'codex'."
        )
    raise ValueError(f"Unknown executor mode: {mode!r}. Use 'cli' or 'demo'.")
