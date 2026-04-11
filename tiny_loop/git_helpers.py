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
