"""Git utilities: collect repo context and diff summaries."""

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


def repo_context(repo_path: str, max_log_lines: int = 20) -> str:
    """Return a brief snapshot of the repo for planner context."""
    cwd = str(Path(repo_path).resolve())
    log = _run(["git", "log", "--oneline", f"-{max_log_lines}"], cwd)
    status = _run(["git", "status", "--short"], cwd)
    # Shallow file tree (top-level + one level deep, ignoring .git)
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
        f"## Recent commits\n{log or '(none)'}\n\n"
        f"## Working tree status\n{status or '(clean)'}\n\n"
        f"## File tree\n{tree}"
    )


def diff_summary(repo_path: str) -> str:
    """Return the git diff since the last commit (staged + unstaged)."""
    cwd = str(Path(repo_path).resolve())
    diff = _run(["git", "diff", "HEAD"], cwd)
    if not diff:
        diff = _run(["git", "diff"], cwd)
    return diff or "(no changes)"
