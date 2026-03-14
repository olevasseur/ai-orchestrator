"""
Tests for validation command execution and result classification.
"""

import pytest
from orchestrator.utils.validation import classify, run_validation_command
from orchestrator.utils.safety import is_allowed, _first_token


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

class TestClassify:
    def test_exit_0_is_passed(self):
        assert classify(0, "", "", timed_out=False) == "passed"

    def test_timed_out_beats_exit_code(self):
        assert classify(0, "", "", timed_out=True) == "timeout"
        assert classify(1, "", "", timed_out=True) == "timeout"

    def test_exit_127_is_missing_tool(self):
        # POSIX standard: 127 = command not found
        assert classify(127, "", "", timed_out=False) == "missing_tool"

    def test_no_module_named_in_stderr_is_missing_tool(self):
        assert classify(1, "", "No module named 'pytest'", timed_out=False) == "missing_tool"

    def test_modulenotfounderror_in_stderr_is_missing_tool(self):
        assert classify(1, "", "ModuleNotFoundError: No module named 'foo'", timed_out=False) == "missing_tool"

    def test_command_not_found_in_stderr_is_missing_tool(self):
        assert classify(1, "", "pytest: command not found", timed_out=False) == "missing_tool"

    def test_generic_nonzero_is_implementation_failure(self):
        assert classify(1, "AssertionError", "", timed_out=False) == "implementation_failure"
        assert classify(2, "", "some error", timed_out=False) == "implementation_failure"

    def test_missing_tool_pattern_in_stdout(self):
        # Some tools print to stdout
        assert classify(1, "No module named 'requests'", "", timed_out=False) == "missing_tool"


# ---------------------------------------------------------------------------
# run_validation_command() — integration tests using real shell
# ---------------------------------------------------------------------------

class TestRunValidationCommand:
    def test_passing_command(self, tmp_path):
        vr = run_validation_command("echo hello", cwd=str(tmp_path))
        assert vr.classification == "passed"
        assert vr.exit_code == 0
        assert "hello" in vr.stdout

    def test_failing_command(self, tmp_path):
        vr = run_validation_command("exit 1", cwd=str(tmp_path))
        assert vr.classification == "implementation_failure"
        assert vr.exit_code == 1

    def test_missing_tool_exit_127(self, tmp_path):
        vr = run_validation_command("__no_such_command_xyz__", cwd=str(tmp_path))
        assert vr.classification == "missing_tool"
        assert vr.exit_code == 127

    def test_shell_builtin_test(self, tmp_path):
        # 'test' is a shell builtin — must work with shell=True
        vr = run_validation_command('test "hello" = "hello"', cwd=str(tmp_path))
        assert vr.classification == "passed"

    def test_shell_builtin_test_failing(self, tmp_path):
        vr = run_validation_command('test "hello" = "world"', cwd=str(tmp_path))
        assert vr.classification == "implementation_failure"

    def test_command_substitution(self, tmp_path):
        # Ensure $(...) works — requires shell=True with a real shell
        vr = run_validation_command(
            'test "$(echo hello)" = "hello"', cwd=str(tmp_path)
        )
        assert vr.classification == "passed"

    def test_timeout(self, tmp_path):
        vr = run_validation_command("sleep 10", cwd=str(tmp_path), timeout=1)
        assert vr.classification == "timeout"
        assert vr.timed_out is True


# ---------------------------------------------------------------------------
# is_allowed() + _first_token() — safety allowlist matching
# ---------------------------------------------------------------------------

ALLOWLIST = ["pytest", "python", "git status", "git diff", "echo", "test", "[", "sh"]


class TestIsAllowed:
    def test_exact_match(self):
        assert is_allowed("pytest", ALLOWLIST)
        assert is_allowed("echo hello", ALLOWLIST)

    def test_prefix_match_with_args(self):
        assert is_allowed("pytest -q tests/", ALLOWLIST)
        assert is_allowed("python hello.py", ALLOWLIST)

    def test_multiword_allowlist_entry(self):
        assert is_allowed("git status --short", ALLOWLIST)
        assert is_allowed("git diff HEAD", ALLOWLIST)

    def test_shell_builtin_test(self):
        assert is_allowed('test "$(python hello.py)" = "Hello, world!"', ALLOWLIST)

    def test_not_allowed(self):
        assert not is_allowed("rm -rf /tmp/x", ALLOWLIST)
        assert not is_allowed("curl http://example.com", ALLOWLIST)


class TestFirstToken:
    def test_simple(self):
        assert _first_token("pytest -q") == "pytest"

    def test_quoted_args(self):
        assert _first_token('test "$(python hello.py)" = "Hello, world!"') == "test"

    def test_empty(self):
        assert _first_token("") == ""

    def test_unmatched_quote_fallback(self):
        # shlex can't parse this; should fall back gracefully
        result = _first_token("cmd 'unclosed")
        assert result == "cmd"
