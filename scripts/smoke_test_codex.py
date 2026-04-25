"""Optional smoke test for the real Codex CLI executor.

This script is intentionally NOT run by pytest — the unit tests in
`tests/test_executor_provider.py` mock subprocess and do not require Codex.
This script invokes the real `codex` binary, which means it runs Codex with
`--dangerously-bypass-approvals-and-sandbox`. See the README "Executor
providers (Codex experimental)" section before using it.

Guardrails:
- Refuses to run unless CODEX_SMOKE_TEST_OK=1 is set in the environment.
- Creates a fresh throwaway git repo under a tempdir; never points Codex at
  the orchestrator repo or any path the user supplies.
- Prints the captured ExecutionResult (exit code, session_id, stdout, stderr
  preview) so a human can eyeball the adapter end-to-end.

Usage:
    CODEX_SMOKE_TEST_OK=1 .venv/bin/python scripts/smoke_test_codex.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from orchestrator.executor.cli_executor import make_executor

GUARD_ENV = "CODEX_SMOKE_TEST_OK"
PROMPT = "Respond with the single word: hello. Do not invoke any shell commands."


def _require_guard() -> None:
    if os.environ.get(GUARD_ENV) != "1":
        sys.stderr.write(
            f"Refusing to run: set {GUARD_ENV}=1 to acknowledge that this\n"
            "invokes the real `codex` binary with\n"
            "--dangerously-bypass-approvals-and-sandbox in a throwaway repo.\n"
        )
        sys.exit(2)


def _require_codex_on_path() -> None:
    if shutil.which("codex") is None:
        sys.stderr.write("`codex` not found on PATH — install Codex CLI first.\n")
        sys.exit(3)


def _make_disposable_repo(root: Path) -> Path:
    repo = root / "codex-smoke-repo"
    repo.mkdir()
    (repo / "README.md").write_text("smoke test\n")
    subprocess.check_call(["git", "init", "-q"], cwd=repo)
    subprocess.check_call(
        ["git", "-c", "user.email=smoke@test", "-c", "user.name=smoke",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo,
    )
    return repo


def main() -> int:
    _require_guard()
    _require_codex_on_path()

    with tempfile.TemporaryDirectory(prefix="codex-smoke-") as tmp:
        repo = _make_disposable_repo(Path(tmp))
        executor = make_executor("cli", provider="codex")
        result = executor.run(prompt=PROMPT, repo_path=str(repo), timeout=120)

    print(f"exit_code   = {result.exit_code}")
    print(f"timed_out   = {result.timed_out}")
    print(f"session_id  = {result.session_id!r}")
    print("-- stdout (first 500 chars) --")
    print(result.stdout[:500])
    print("-- stderr (first 500 chars) --")
    print(result.stderr[:500])

    return 0 if result.exit_code == 0 and not result.timed_out else 1


if __name__ == "__main__":
    sys.exit(main())
