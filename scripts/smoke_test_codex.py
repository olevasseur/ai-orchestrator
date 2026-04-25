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
    # Direct (inplace) mode — codex edits the throwaway repo directly:
    CODEX_SMOKE_TEST_OK=1 .venv/bin/python scripts/smoke_test_codex.py

    # Worktree isolation mode — codex edits a disposable worktree, the
    # throwaway repo stays clean, and the diff is captured:
    CODEX_SMOKE_TEST_OK=1 .venv/bin/python scripts/smoke_test_codex.py --worktree
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from orchestrator.executor.cli_executor import (
    DEFAULT_CODEX_WORKTREE_BASE_DIR,
    make_executor,
)

GUARD_ENV = "CODEX_SMOKE_TEST_OK"
PROMPT = "Respond with the single word: hello. Do not invoke any shell commands."
WORKTREE_PROMPT = (
    "Create a new file called HELLO.txt in the current directory containing "
    "exactly the text 'hi from codex' followed by a newline. Do not run any "
    "shell commands, do not stage or commit anything, do not modify any other "
    "file. Reply with the single word: done."
)


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
         "add", "."],
        cwd=repo,
    )
    subprocess.check_call(
        ["git", "-c", "user.email=smoke@test", "-c", "user.name=smoke",
         "commit", "-q", "-m", "init"],
        cwd=repo,
    )
    return repo


def _git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--worktree", action="store_true",
        help="Run Codex in worktree isolation mode and assert the source repo "
             "is left untouched.",
    )
    parser.add_argument(
        "--worktree-base-dir", default=DEFAULT_CODEX_WORKTREE_BASE_DIR,
        help="Where Codex worktrees are created (worktree mode only).",
    )
    args = parser.parse_args()

    _require_guard()
    _require_codex_on_path()

    with tempfile.TemporaryDirectory(prefix="codex-smoke-") as tmp:
        repo = _make_disposable_repo(Path(tmp))
        head_before = _git_head(repo)

        if args.worktree:
            executor = make_executor(
                "cli", provider="codex",
                codex_workspace_strategy="worktree",
                codex_worktree_base_dir=args.worktree_base_dir,
                codex_apply_policy="manual",
            )
            prompt = WORKTREE_PROMPT
        else:
            executor = make_executor("cli", provider="codex")
            prompt = PROMPT

        result = executor.run(prompt=prompt, repo_path=str(repo), timeout=300)

        head_after = _git_head(repo)
        status_after = _git_status(repo)
        hello_in_source = (repo / "HELLO.txt").exists()

    print(f"mode        = {'worktree' if args.worktree else 'inplace'}")
    print(f"exit_code   = {result.exit_code}")
    print(f"timed_out   = {result.timed_out}")
    print(f"session_id  = {result.session_id!r}")
    print(f"workspace   = {result.workspace_path!r}")
    print(f"diff_bytes  = {len(result.diff)}")
    print("-- stdout (first 500 chars) --")
    print(result.stdout[:500])
    print("-- stderr (first 500 chars) --")
    print(result.stderr[:500])

    if args.worktree:
        # Compare resolved paths because macOS exposes /tmp as /private/tmp.
        resolved_base = str(Path(args.worktree_base_dir).resolve())
        resolved_workspace = str(Path(result.workspace_path).resolve())
        under_base = resolved_workspace.startswith(resolved_base)

        print("\n-- isolation checks --")
        print(f"source HEAD unchanged?      {head_before == head_after}")
        print(f"source working tree clean?  {status_after == ''}")
        print(f"HELLO.txt in source repo?   {hello_in_source}  (must be False)")
        print(f"workspace cleaned up?       {not Path(result.workspace_path).exists()}")
        print(f"workspace under base dir?   {under_base}")
        print(f"  base (resolved):          {resolved_base}")
        print(f"  workspace (resolved):     {resolved_workspace}")
        print("\n-- captured diff --")
        print(result.diff or "(empty)")

        ok = (
            result.exit_code == 0
            and not result.timed_out
            and head_before == head_after
            and status_after == ""
            and not hello_in_source
            and not Path(result.workspace_path).exists()
            and under_base
            and "HELLO.txt" in result.diff
        )
        return 0 if ok else 1

    return 0 if result.exit_code == 0 and not result.timed_out else 1


if __name__ == "__main__":
    sys.exit(main())
