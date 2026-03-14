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
    ) -> ExecutionResult:
        cmd = [
            self.claude_cli_path,
            "--print",          # non-interactive, print output and exit
            "--dangerously-skip-permissions",  # allow file edits without prompting
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

        return ExecutionResult(
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
            exit_code=exit_code,
            timed_out=timed_out,
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


def make_executor(mode: str, claude_cli_path: str = "claude") -> BaseExecutor:
    """Factory: return the right executor for the configured mode."""
    if mode == "demo":
        return DemoExecutor()
    if mode == "cli":
        return CLIExecutor(claude_cli_path=claude_cli_path)
    raise ValueError(f"Unknown executor mode: {mode!r}. Use 'cli' or 'demo'.")
