"""
Command safety: allowlist / denylist checking with explicit confirmation for
destructive commands.

Extension point: swap check() for a remote approval call (Slack / Telegram).
"""

from __future__ import annotations

import shlex


def is_denied(cmd: str, denylist: list[str]) -> bool:
    return any(bad in cmd for bad in denylist)


def _first_token(cmd: str) -> str:
    """
    Extract the first shell token from a command string.

    Examples:
      'pytest -q'                          → 'pytest'
      'test "$(python hello.py)" = "Hi"'  → 'test'
      'git status --short'                 → 'git'
    """
    try:
        tokens = shlex.split(cmd.strip())
        return tokens[0] if tokens else ""
    except ValueError:
        # shlex can't parse (e.g. unmatched quotes) — fall back to split on space
        return cmd.strip().split()[0] if cmd.strip() else ""


def is_allowed(cmd: str, allowlist: list[str]) -> bool:
    """
    A command is allowed if:
      - it starts with an allowlist entry (handles multi-word entries like "git status"), OR
      - its first shell token matches an allowlist entry exactly.

    This correctly handles shell-style commands such as:
      test "$(python hello.py)" = "Hello, world!"
    """
    stripped = cmd.strip()
    first = _first_token(stripped)
    for ok in allowlist:
        if stripped.startswith(ok):
            return True
        if first == ok:
            return True
    return False


def check_command(cmd: str, allowlist: list[str], denylist: list[str]) -> str:
    """
    Return "ok" | "denied" | "needs_confirmation".
    Callers must prompt the user when "needs_confirmation" is returned.
    """
    if is_denied(cmd, denylist):
        return "denied"
    if is_allowed(cmd, allowlist):
        return "ok"
    return "needs_confirmation"
