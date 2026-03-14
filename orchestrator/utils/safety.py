"""
Command safety: allowlist / denylist checking with explicit confirmation for
destructive commands.

Extension point: swap check() for a remote approval call (Slack / Telegram).
"""

from __future__ import annotations


def is_denied(cmd: str, denylist: list[str]) -> bool:
    return any(bad in cmd for bad in denylist)


def is_allowed(cmd: str, allowlist: list[str]) -> bool:
    return any(cmd.strip().startswith(ok) for ok in allowlist)


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
