"""Run Claude Code as a subprocess and capture structured output.

Core logic adapted from orchestrator/executor/cli_executor.py.
Flattened: no ABC, no factory, no demo mode — just the function.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class ClaudeResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    session_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def run_claude(
    prompt: str,
    repo_path: str,
    timeout: int = 600,
    resume_session_id: str | None = None,
) -> ClaudeResult:
    """Run `claude --print` in the target repo and return parsed result."""
    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format", "stream-json",
    ]
    model = os.environ.get("CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    effort = os.environ.get("CLAUDE_EFFORT")
    if effort:
        cmd += ["--effort", effort]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd.append(prompt)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    timed_out = False

    proc = subprocess.Popen(
        cmd,
        cwd=str(Path(repo_path).resolve()),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def _drain(stream, lines):
        for line in stream:
            lines.append(line)

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_lines))
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_lines))
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

    # Parse stream-json: find the result event (last one wins).
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

    return ClaudeResult(
        stdout=result_text,
        stderr="".join(stderr_lines),
        exit_code=exit_code,
        timed_out=timed_out,
        session_id=session_id,
    )
