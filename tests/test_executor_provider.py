"""
Tests for executor provider selection.

Provider selection is the surface area added in the Codex feasibility sprint:
the runner stays generic, and the only thing that knows about Claude vs Codex
is the `make_executor` factory plus the `executor_provider` config field.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from orchestrator.executor.base import BaseExecutor
from orchestrator.executor.cli_executor import (
    CLIExecutor,
    CodexExecutor,
    DemoExecutor,
    make_executor,
)
from orchestrator.utils.config import Config


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_default_provider_is_claude(self):
        cfg = Config()
        assert cfg.executor_provider == "claude"

    def test_default_paths(self):
        cfg = Config()
        assert cfg.claude_cli_path == "claude"
        assert cfg.codex_cli_path == "codex"

    def test_load_missing_file_keeps_claude_default(self, tmp_path: Path):
        cfg = Config.load(tmp_path / "does-not-exist.yaml")
        assert cfg.executor_provider == "claude"


# ---------------------------------------------------------------------------
# Config YAML parsing
# ---------------------------------------------------------------------------

class TestConfigYAMLParsing:
    def test_flat_executor_provider_key(self, tmp_path: Path):
        p = tmp_path / "config.yaml"
        p.write_text("executor_provider: codex\ncodex_cli_path: /opt/codex\n")
        cfg = Config.load(p)
        assert cfg.executor_provider == "codex"
        assert cfg.codex_cli_path == "/opt/codex"

    def test_nested_executor_block(self, tmp_path: Path):
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""
            executor:
              mode: cli
              provider: codex
              claude:
                command: /usr/local/bin/claude
              codex:
                command: /usr/local/bin/codex
        """).strip())
        cfg = Config.load(p)
        assert cfg.executor_mode == "cli"
        assert cfg.executor_provider == "codex"
        assert cfg.claude_cli_path == "/usr/local/bin/claude"
        assert cfg.codex_cli_path == "/usr/local/bin/codex"

    def test_flat_keys_take_precedence_over_nested(self, tmp_path: Path):
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent("""
            executor_provider: claude
            executor:
              provider: codex
        """).strip())
        cfg = Config.load(p)
        assert cfg.executor_provider == "claude"

    def test_env_var_overrides_yaml(self, tmp_path: Path, monkeypatch):
        p = tmp_path / "config.yaml"
        p.write_text("executor_provider: claude\n")
        monkeypatch.setenv("EXECUTOR_PROVIDER", "codex")
        cfg = Config.load(p)
        assert cfg.executor_provider == "codex"


# ---------------------------------------------------------------------------
# make_executor() factory
# ---------------------------------------------------------------------------

class TestMakeExecutorDefaults:
    def test_default_provider_returns_claude_cli_executor(self):
        e = make_executor("cli", "claude")
        assert isinstance(e, CLIExecutor)
        assert e.claude_cli_path == "claude"

    def test_default_provider_kwarg_is_claude(self):
        # Calling without provider= must still build CLIExecutor (Claude).
        e = make_executor("cli")
        assert isinstance(e, CLIExecutor)

    def test_factory_returns_baseexecutor_subclass(self):
        e = make_executor("cli")
        assert isinstance(e, BaseExecutor)


class TestMakeExecutorClaude:
    def test_explicit_claude_provider(self):
        e = make_executor("cli", "claude", provider="claude")
        assert isinstance(e, CLIExecutor)

    def test_claude_uses_configured_cli_path(self):
        e = make_executor("cli", "/custom/claude", provider="claude")
        assert isinstance(e, CLIExecutor)
        assert e.claude_cli_path == "/custom/claude"


class TestMakeExecutorCodex:
    def test_explicit_codex_provider_constructs(self):
        e = make_executor("cli", "claude", provider="codex", codex_cli_path="codex")
        assert isinstance(e, CodexExecutor)
        assert e.codex_cli_path == "codex"

    def test_codex_uses_configured_cli_path(self):
        e = make_executor("cli", "claude", provider="codex", codex_cli_path="/opt/codex")
        assert e.codex_cli_path == "/opt/codex"

    def test_codex_run_invokes_exec_subcommand(self, tmp_path: Path, monkeypatch):
        # Verify run() shells out to `codex exec ... <prompt>` rather than the
        # old NotImplementedError. We intercept Popen so no real CLI is needed.
        import orchestrator.executor.cli_executor as mod

        captured = {}

        class FakeProc:
            def __init__(self, *a, **kw):
                captured["cmd"] = kw.get("args", a[0] if a else None)
                captured["cwd"] = kw.get("cwd")
                self.stdout = iter(["ok\n"])
                self.stderr = iter([])
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(mod.subprocess, "Popen", FakeProc)
        e = make_executor("cli", provider="codex", codex_cli_path="codex")
        result = e.run(prompt="hello", repo_path=str(tmp_path))
        assert result.exit_code == 0
        assert captured["cmd"][0] == "codex"
        assert "exec" in captured["cmd"]
        assert "--json" in captured["cmd"]
        assert captured["cmd"][-1] == "hello"


class TestCodexJSONLParsing:
    """The Codex executor invokes `codex exec --json`, which emits one JSON
    event per stdout line. The parser must surface the final agent message
    as `stdout` and the `thread_id` as `session_id` — analogous to how
    CLIExecutor pulls the `result` event from Claude's stream-json."""

    def _run_with_fake_stdout(self, monkeypatch, tmp_path: Path, lines: list[str]):
        import orchestrator.executor.cli_executor as mod

        class FakeProc:
            def __init__(self, *a, **kw):
                self.stdout = iter(lines)
                self.stderr = iter([])
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(mod.subprocess, "Popen", FakeProc)
        e = make_executor("cli", provider="codex")
        return e.run(prompt="x", repo_path=str(tmp_path))

    def test_extracts_thread_id_as_session_id(self, tmp_path: Path, monkeypatch):
        result = self._run_with_fake_stdout(monkeypatch, tmp_path, [
            '{"type":"thread.started","thread_id":"abc-123"}\n',
            '{"type":"turn.started"}\n',
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"hello"}}\n',
            '{"type":"turn.completed","usage":{}}\n',
        ])
        assert result.session_id == "abc-123"
        assert result.stdout == "hello"

    def test_uses_last_agent_message_when_multiple(self, tmp_path: Path, monkeypatch):
        result = self._run_with_fake_stdout(monkeypatch, tmp_path, [
            '{"type":"thread.started","thread_id":"t1"}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}\n',
            '{"type":"turn.completed"}\n',
        ])
        assert result.stdout == "final answer"
        assert result.session_id == "t1"

    def test_ignores_non_message_item_completed(self, tmp_path: Path, monkeypatch):
        # Codex also emits item.completed for shell calls etc.; only
        # agent_message items contribute to the surfaced result.
        result = self._run_with_fake_stdout(monkeypatch, tmp_path, [
            '{"type":"thread.started","thread_id":"t2"}\n',
            '{"type":"item.completed","item":{"type":"shell_call","output":"ls"}}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
        ])
        assert result.stdout == "done"

    def test_skips_blank_and_malformed_lines(self, tmp_path: Path, monkeypatch):
        result = self._run_with_fake_stdout(monkeypatch, tmp_path, [
            '\n',
            'not json at all\n',
            '{"type":"thread.started","thread_id":"t3"}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        ])
        assert result.session_id == "t3"
        assert result.stdout == "ok"

    def test_falls_back_to_raw_stdout_when_no_json_events(self, tmp_path: Path, monkeypatch):
        # If Codex was somehow invoked without --json (or crashed before any
        # event), we still want users to see whatever it printed.
        result = self._run_with_fake_stdout(monkeypatch, tmp_path, [
            "plain text line one\n",
            "plain text line two\n",
        ])
        assert result.stdout == "plain text line one\nplain text line two\n"
        assert result.session_id == ""

    def test_session_id_empty_when_thread_started_missing(self, tmp_path: Path, monkeypatch):
        result = self._run_with_fake_stdout(monkeypatch, tmp_path, [
            '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n',
        ])
        assert result.session_id == ""
        assert result.stdout == "hi"


class TestMakeExecutorErrors:
    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown executor provider"):
            make_executor("cli", provider="gemini")

    def test_unknown_mode_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown executor mode"):
            make_executor("sdk")  # not currently supported

    def test_unknown_provider_message_lists_supported_values(self):
        with pytest.raises(ValueError) as exc_info:
            make_executor("cli", provider="bogus")
        msg = str(exc_info.value)
        assert "claude" in msg
        assert "codex" in msg


class TestMakeExecutorDemoMode:
    def test_demo_mode_returns_demo_executor_regardless_of_provider(self):
        # Demo is independent of the agent provider.
        assert isinstance(make_executor("demo"), DemoExecutor)
        assert isinstance(make_executor("demo", provider="claude"), DemoExecutor)
        assert isinstance(make_executor("demo", provider="codex"), DemoExecutor)


# ---------------------------------------------------------------------------
# Session metadata is provider-opaque
# ---------------------------------------------------------------------------

class TestSessionMetadataIsProviderOpaque:
    """The runner threads `resume_session_id` as an opaque string. Codex must
    not silently accept a Claude-issued id and pretend it works — its `.run()`
    raises before any session handling happens, which is the desired behaviour
    until a real Codex session strategy is implemented."""

    def test_codex_run_raises_even_with_claude_session_id(self, tmp_path: Path):
        e = make_executor("cli", provider="codex")
        with pytest.raises(NotImplementedError):
            e.run(
                prompt="x",
                repo_path=str(tmp_path),
                resume_session_id="claude-issued-session-abc123",
            )

    def test_demo_executor_ignores_session_id(self, tmp_path: Path):
        # DemoExecutor must remain happy with or without a session id —
        # it represents the no-session baseline that any future provider
        # should also tolerate.
        e = make_executor("demo")
        result = e.run(
            prompt="x",
            repo_path=str(tmp_path),
            resume_session_id="anything",
        )
        assert result.exit_code == 0
        assert result.session_id == ""  # demo never issues session ids
