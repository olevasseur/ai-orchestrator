"""
Validation command runner and result classifier.

Classification logic (checked in order):
  1. timed_out          → "timeout"
  2. exit_code == 0     → "passed"
  3. exit_code == 127   → "missing_tool"   (POSIX: command not found)
  4. stderr/stdout contains known missing-dependency patterns → "missing_tool"
  5. otherwise          → "implementation_failure"

This lets the planner (and the human) distinguish "the code is wrong" from
"the test tool wasn't installed" — two very different situations.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, asdict

# Patterns in combined stdout+stderr that indicate a missing tool/dependency
# rather than a code failure.
_MISSING_TOOL_PATTERNS = [
    "no module named",
    "modulenotfounderror",
    "importerror",
    "command not found",
    "not found",
    "no such file or directory",
    "cannot find",
    "is not recognized as",   # Windows "not recognized as an internal command"
]


@dataclass
class ValidationResult:
    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    classification: str  # passed | missing_tool | timeout | implementation_failure

    def passed(self) -> bool:
        return self.classification == "passed"

    def to_dict(self) -> dict:
        return asdict(self)


def classify(
    exit_code: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
) -> str:
    if timed_out:
        return "timeout"
    if exit_code == 0:
        return "passed"
    if exit_code == 127:
        return "missing_tool"
    combined = (stdout + stderr).lower()
    if any(pat in combined for pat in _MISSING_TOOL_PATTERNS):
        return "missing_tool"
    return "implementation_failure"


def run_validation_command(
    cmd: str,
    cwd: str,
    timeout: int = 120,
) -> ValidationResult:
    """
    Run a single validation command using the shell (shell=True) so that
    shell builtins (test, echo, [[ ]]) and substitutions ($(...)) work
    exactly as they would in a terminal.
    """
    timed_out = False
    stdout = ""
    stderr = ""
    exit_code = -1

    try:
        result = subprocess.run(
            cmd,
            shell=True,           # full shell semantics: builtins, quoting, $()
            executable="/bin/sh", # explicit shell for consistency across platforms
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = result.stdout
        stderr = result.stderr
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        timed_out = True

    classification = classify(exit_code, stdout, stderr, timed_out)

    return ValidationResult(
        cmd=cmd,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        classification=classification,
    )
