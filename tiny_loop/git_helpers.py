"""Thin git helpers. Copied from orchestrator/utils/git.py."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()
    except Exception:
        return ""


def repo_context(repo_path: str) -> str:
    """Brief snapshot: recent commits, working-tree status, shallow file tree."""
    cwd = str(Path(repo_path).resolve())
    log = _run(["git", "log", "--oneline", "-10"], cwd)
    status = _run(["git", "status", "--short"], cwd)
    try:
        tree_lines = []
        root = Path(cwd)
        for p in sorted(root.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_dir():
                tree_lines.append(f"{p.name}/")
                for sub in sorted(p.iterdir())[:8]:
                    tree_lines.append(f"  {sub.name}" + ("/" if sub.is_dir() else ""))
            else:
                tree_lines.append(p.name)
        tree = "\n".join(tree_lines[:60])
    except Exception:
        tree = "(unable to read tree)"
    return (
        f"Recent commits:\n{log or '(none)'}\n\n"
        f"Working tree:\n{status or '(clean)'}\n\n"
        f"File tree:\n{tree}"
    )


def diff_summary(repo_path: str) -> str:
    """Git diff since last commit (staged + unstaged)."""
    cwd = str(Path(repo_path).resolve())
    diff = _run(["git", "diff", "HEAD"], cwd)
    if not diff:
        diff = _run(["git", "diff"], cwd)
    return diff or "(no changes)"


def head_commit(repo_path: str) -> str:
    """Return the current HEAD commit hash."""
    cwd = str(Path(repo_path).resolve())
    return _run(["git", "rev-parse", "HEAD"], cwd)


def files_changed_since(repo_path: str, since_commit: str) -> list[str]:
    """Return list of files changed (added/modified) since a given commit."""
    cwd = str(Path(repo_path).resolve())
    # Committed changes
    out = _run(["git", "diff", "--name-only", since_commit, "HEAD"], cwd)
    files = [f for f in out.splitlines() if f.strip()] if out else []
    # Also include uncommitted changes (staged + unstaged)
    wt = _run(["git", "diff", "--name-only", "HEAD"], cwd)
    if wt:
        for f in wt.splitlines():
            f = f.strip()
            if f and f not in files:
                files.append(f)
    return sorted(files)


def has_meaningful_diff(repo_path: str) -> bool:
    """Return True if there are any actual code changes (staged or unstaged)."""
    cwd = str(Path(repo_path).resolve())
    # --stat is cheaper than full diff for a boolean check
    stat = _run(["git", "diff", "--stat", "HEAD"], cwd)
    if not stat:
        stat = _run(["git", "diff", "--stat"], cwd)
    return bool(stat)
