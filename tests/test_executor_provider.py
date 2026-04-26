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
from orchestrator.jobs.models import RunState
from orchestrator.jobs.runner import OrchestratorRunner
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


class TestRunnerSessionContinuity:
    def test_claude_receives_stored_executor_session_id(self):
        runner = OrchestratorRunner.__new__(OrchestratorRunner)
        runner.config = Config(executor_provider="claude")
        run_state = RunState(run_id="r", repo_path="/repo", executor_session_id="sess-1")

        assert runner._resume_session_id_for_executor(run_state) == "sess-1"

    def test_codex_does_not_receive_unsupported_resume_session_id(self):
        runner = OrchestratorRunner.__new__(OrchestratorRunner)
        runner.config = Config(executor_provider="codex")
        run_state = RunState(
            run_id="r",
            repo_path="/repo",
            executor_session_id="codex-thread-1",
        )

        assert runner._resume_session_id_for_executor(run_state) is None


# ---------------------------------------------------------------------------
# Codex worktree isolation
# ---------------------------------------------------------------------------

import subprocess as _real_subprocess  # noqa: E402

from orchestrator.executor.cli_executor import CodexExecutor  # noqa: E402


def _init_repo(path: Path) -> str:
    """Create a git repo with one committed file. Return the initial HEAD sha."""
    _real_subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _real_subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    _real_subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "hello.txt").write_text("original\n")
    _real_subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    _real_subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return _real_subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _patch_codex_popen(monkeypatch, *, edits=None, exit_code=0, raise_exc=None):
    """Replace Popen so codex calls are faked but real git commands pass through.

    `edits` is an optional callable receiving the cwd Path; use it to mutate the
    worktree as if Codex had edited files. `raise_exc` lets a test simulate
    Codex blowing up mid-run.
    """
    import orchestrator.executor.cli_executor as mod
    real_popen = _real_subprocess.Popen
    captured: dict = {}

    class FakeProc:
        def __init__(self, *a, **kw):
            captured["cmd"] = kw.get("args", a[0] if a else None)
            captured["cwd"] = kw.get("cwd")
            if raise_exc is not None:
                raise raise_exc
            if edits is not None:
                edits(Path(kw["cwd"]))
            self.stdout = iter([
                '{"type":"thread.started","thread_id":"tid"}\n',
                '{"type":"item.completed","item":'
                '{"type":"agent_message","text":"done"}}\n',
            ])
            self.stderr = iter([])
            self.returncode = exit_code
        def wait(self, timeout=None): return exit_code

    def selective(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, list) and cmd and "codex" in str(cmd[0]):
            return FakeProc(*args, **kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "Popen", selective)
    return captured


class TestCodexInplaceUnchanged:
    """Direct (inplace) mode keeps prior behaviour: no worktree, no diff field."""

    def test_inplace_runs_in_repo_path(self, tmp_path: Path, monkeypatch):
        captured = _patch_codex_popen(monkeypatch)
        e = CodexExecutor(workspace_strategy="inplace")
        res = e.run(prompt="x", repo_path=str(tmp_path))
        assert captured["cwd"] == str(tmp_path.resolve())
        assert res.workspace_path == ""
        assert res.diff == ""
        assert res.exit_code == 0

    def test_default_strategy_is_inplace(self):
        # Default constructor must preserve direct-mode behaviour so existing
        # call sites that don't pass the new kwargs still get the old executor.
        e = CodexExecutor()
        assert e.workspace_strategy == "inplace"


class TestCodexWorktreeMode:
    def test_worktree_runs_under_base_dir_not_source_repo(
        self, tmp_path: Path, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        base = tmp_path / "wt-base"

        captured = _patch_codex_popen(monkeypatch, edits=lambda p: None)
        e = CodexExecutor(
            workspace_strategy="worktree",
            worktree_base_dir=str(base),
        )
        res = e.run(prompt="x", repo_path=str(src))

        # Codex was launched with cwd inside base, NOT inside src.
        assert captured["cwd"] != str(src.resolve())
        assert captured["cwd"].startswith(str(base.resolve()))
        # The reported workspace_path also lives under base.
        assert res.workspace_path.startswith(str(base.resolve()))

    def test_worktree_base_dir_is_created_if_missing(
        self, tmp_path: Path, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        base = tmp_path / "does" / "not" / "exist"
        assert not base.exists()

        _patch_codex_popen(monkeypatch, edits=lambda p: None)
        e = CodexExecutor(workspace_strategy="worktree", worktree_base_dir=str(base))
        e.run(prompt="x", repo_path=str(src))
        assert base.exists()

    def test_source_repo_unchanged_after_worktree_run(
        self, tmp_path: Path, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        head_before = _init_repo(src)
        base = tmp_path / "wt-base"

        def fake_edits(cwd: Path):
            (cwd / "hello.txt").write_text("modified by codex\n")
            (cwd / "new.txt").write_text("brand new\n")

        _patch_codex_popen(monkeypatch, edits=fake_edits)
        e = CodexExecutor(workspace_strategy="worktree", worktree_base_dir=str(base))
        e.run(prompt="x", repo_path=str(src))

        head_after = _real_subprocess.run(
            ["git", "-C", str(src), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        status = _real_subprocess.run(
            ["git", "-C", str(src), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert head_before == head_after
        assert status == ""
        assert (src / "hello.txt").read_text() == "original\n"
        assert not (src / "new.txt").exists()

    def test_diff_captures_modifications_and_new_files(
        self, tmp_path: Path, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        base = tmp_path / "wt-base"

        def fake_edits(cwd: Path):
            (cwd / "hello.txt").write_text("modified by codex\n")
            (cwd / "new.txt").write_text("brand new\n")

        _patch_codex_popen(monkeypatch, edits=fake_edits)
        e = CodexExecutor(workspace_strategy="worktree", worktree_base_dir=str(base))
        res = e.run(prompt="x", repo_path=str(src))

        assert "hello.txt" in res.diff
        assert "modified by codex" in res.diff
        assert "new.txt" in res.diff
        assert "brand new" in res.diff
        assert res.diff.startswith("diff --git")

    def test_diff_empty_when_codex_does_nothing(
        self, tmp_path: Path, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        base = tmp_path / "wt-base"

        _patch_codex_popen(monkeypatch, edits=lambda p: None)
        e = CodexExecutor(workspace_strategy="worktree", worktree_base_dir=str(base))
        res = e.run(prompt="x", repo_path=str(src))
        assert res.diff == ""
        # workspace_path is still surfaced even when no diff was produced.
        assert res.workspace_path.startswith(str(base.resolve()))

    def test_cleanup_removes_worktree_on_success(
        self, tmp_path: Path, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        base = tmp_path / "wt-base"

        def fake_edits(cwd: Path):
            (cwd / "hello.txt").write_text("changed\n")

        _patch_codex_popen(monkeypatch, edits=fake_edits)
        e = CodexExecutor(workspace_strategy="worktree", worktree_base_dir=str(base))
        res = e.run(prompt="x", repo_path=str(src))

        assert not Path(res.workspace_path).exists(), (
            f"worktree leaked: {res.workspace_path}"
        )
        # Also: nothing else under base should remain.
        leftovers = list(base.iterdir()) if base.exists() else []
        assert leftovers == []

    def test_cleanup_runs_even_when_codex_crashes(
        self, tmp_path: Path, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        base = tmp_path / "wt-base"

        _patch_codex_popen(
            monkeypatch, raise_exc=RuntimeError("simulated codex crash")
        )
        e = CodexExecutor(workspace_strategy="worktree", worktree_base_dir=str(base))
        with pytest.raises(RuntimeError, match="simulated codex crash"):
            e.run(prompt="x", repo_path=str(src))

        # The worktree must not be left behind on failure.
        leftovers = list(base.iterdir()) if base.exists() else []
        assert leftovers == [], f"worktree leaked after crash: {leftovers}"
        # And the source repo must still be clean.
        status = _real_subprocess.run(
            ["git", "-C", str(src), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert status == ""

    def test_worktree_base_dir_isolated_from_artifact_dirs(
        self, tmp_path: Path, monkeypatch
    ):
        """The worktree path must not be under common artifact locations
        (~/.orchestrator/runs, /tmp/tiny-loop-runs, the source repo)."""
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        base = tmp_path / "wt-base"

        _patch_codex_popen(monkeypatch, edits=lambda p: None)
        e = CodexExecutor(workspace_strategy="worktree", worktree_base_dir=str(base))
        res = e.run(prompt="x", repo_path=str(src))

        wt = Path(res.workspace_path).resolve()
        src_resolved = src.resolve()
        assert str(wt).startswith(str(base.resolve()))
        assert str(src_resolved) not in str(wt)
        assert "/.orchestrator/runs" not in str(wt)
        assert "/tiny-loop-runs" not in str(wt)
        # And it must not be in the user's home root either.
        assert wt.parent != Path.home()


class TestCodexExecutorValidation:
    def test_unknown_workspace_strategy_raises(self):
        with pytest.raises(ValueError, match="workspace_strategy"):
            CodexExecutor(workspace_strategy="bogus")

    def test_unknown_apply_policy_raises(self):
        with pytest.raises(ValueError, match="apply_policy"):
            CodexExecutor(apply_policy="bogus")

    def test_apply_policy_auto_still_captures_worktree_diff(
        self, tmp_path: Path, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        _patch_codex_popen(
            monkeypatch,
            edits=lambda cwd: (cwd / "hello.txt").write_text("changed\n"),
        )
        e = CodexExecutor(
            workspace_strategy="worktree",
            worktree_base_dir=str(tmp_path / "wt"),
            apply_policy="auto",
        )
        res = e.run(prompt="x", repo_path=str(src))

        assert "changed" in res.diff
        assert (src / "hello.txt").read_text() == "original\n"


class TestRunStorePersistsCodexWorkspace:
    """RunStore is the artifact handoff: the Codex diff must be written to a
    file the human can apply, and the workspace path must be recorded for
    forensics. write_codex_workspace is the only place this happens."""

    def test_writes_diff_and_path(self, tmp_path: Path):
        from orchestrator.storage.store import RunStore
        store = RunStore.create(str(tmp_path), str(tmp_path / "fake-repo"))
        diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
        diff_path = store.write_codex_workspace(0, diff, "/tmp/wt/codex-1")

        assert diff_path is not None
        assert diff_path.exists()
        assert diff_path.read_text() == diff
        assert (store.iteration_dir(0) / "codex_workspace_path.txt").read_text() \
            == "/tmp/wt/codex-1"

    def test_no_diff_no_file(self, tmp_path: Path):
        from orchestrator.storage.store import RunStore
        store = RunStore.create(str(tmp_path), str(tmp_path / "fake-repo"))
        diff_path = store.write_codex_workspace(0, "", "")
        assert diff_path is None
        assert not (store.iteration_dir(0) / "codex_workspace.diff").exists()
        assert not (store.iteration_dir(0) / "codex_workspace_path.txt").exists()

    def test_records_workspace_even_when_diff_empty(self, tmp_path: Path):
        # Codex worktree mode that produced no edits should still leave a
        # forensic breadcrumb of where it ran.
        from orchestrator.storage.store import RunStore
        store = RunStore.create(str(tmp_path), str(tmp_path / "fake-repo"))
        diff_path = store.write_codex_workspace(0, "", "/tmp/wt/empty")
        assert diff_path is None
        assert (store.iteration_dir(0) / "codex_workspace_path.txt").read_text() \
            == "/tmp/wt/empty"

    def test_round_trip_via_read_codex_workspace(self, tmp_path: Path):
        from orchestrator.storage.store import RunStore
        store = RunStore.create(str(tmp_path), str(tmp_path / "fake-repo"))
        store.write_codex_workspace(0, "DIFF", "/tmp/wt/p")
        rd = store.read_codex_workspace(0)
        assert rd["diff"] == "DIFF"
        assert rd["workspace_path"] == "/tmp/wt/p"
        assert rd["diff_path"].endswith("codex_workspace.diff")

    def test_read_executor_output_includes_codex_fields(self, tmp_path: Path):
        from orchestrator.storage.store import RunStore
        store = RunStore.create(str(tmp_path), str(tmp_path / "fake-repo"))
        store.write_executor_output(0, "out", "err", 0)
        store.write_codex_workspace(0, "DIFF", "/tmp/wt/p")
        store.write_codex_patch_status(0, "skipped", "manual review required")
        eo = store.read_executor_output(0)
        assert eo["codex_workspace_diff"] == "DIFF"
        assert eo["codex_workspace_path"] == "/tmp/wt/p"
        assert eo["codex_patch_status"] == "skipped"
        assert eo["codex_patch_status_detail"] == "manual review required"


class TestCodexPatchApplyPath:
    def _runner(self, policy: str):
        runner = object.__new__(OrchestratorRunner)
        cfg = Config()
        cfg.executor_apply_policy = policy
        runner.config = cfg
        return runner

    def _write_patch(self, path: Path, text: str) -> Path:
        path.write_text(text)
        return path

    def test_auto_apply_succeeds_when_explicitly_enabled(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        patch = self._write_patch(
            tmp_path / "codex.diff",
            "\n".join([
                "diff --git a/new.txt b/new.txt",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/new.txt",
                "@@ -0,0 +1 @@",
                "+created by codex",
                "",
            ]),
        )

        status, detail = self._runner("auto")._handle_codex_patch(
            patch.read_text(), patch, str(repo)
        )

        assert status == "applied"
        assert "applied" in detail.lower()
        assert (repo / "new.txt").read_text() == "created by codex\n"

    def test_manual_policy_skips_and_leaves_repo_unchanged(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        patch = self._write_patch(
            tmp_path / "codex.diff",
            "\n".join([
                "diff --git a/new.txt b/new.txt",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/new.txt",
                "@@ -0,0 +1 @@",
                "+created by codex",
                "",
            ]),
        )

        status, detail = self._runner("manual")._handle_codex_patch(
            patch.read_text(), patch, str(repo)
        )

        assert status == "skipped"
        assert "manual" in detail
        assert not (repo / "new.txt").exists()

    def test_apply_check_failure_is_safe(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        patch = self._write_patch(
            tmp_path / "bad.diff",
            "\n".join([
                "diff --git a/missing.txt b/missing.txt",
                "--- a/missing.txt",
                "+++ b/missing.txt",
                "@@ -1 +1 @@",
                "-old",
                "+new",
                "",
            ]),
        )

        status, detail = self._runner("auto")._handle_codex_patch(
            patch.read_text(), patch, str(repo)
        )

        assert status == "failed"
        assert detail
        assert not (repo / "missing.txt").exists()

    def test_auto_apply_refuses_dirty_target_repo(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        (repo / "dirty.txt").write_text("uncommitted\n")
        patch = self._write_patch(
            tmp_path / "codex.diff",
            "\n".join([
                "diff --git a/new.txt b/new.txt",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/new.txt",
                "@@ -0,0 +1 @@",
                "+created by codex",
                "",
            ]),
        )

        status, detail = self._runner("auto")._handle_codex_patch(
            patch.read_text(), patch, str(repo)
        )

        assert status == "failed"
        assert "uncommitted changes" in detail
        assert not (repo / "new.txt").exists()


class TestMakeExecutorForwardsCodexConfig:
    def test_make_executor_passes_worktree_kwargs(self, tmp_path: Path):
        e = make_executor(
            "cli",
            provider="codex",
            codex_workspace_strategy="worktree",
            codex_worktree_base_dir=str(tmp_path / "wt"),
            codex_apply_policy="manual",
        )
        assert isinstance(e, CodexExecutor)
        assert e.workspace_strategy == "worktree"
        assert e.worktree_base_dir == str(tmp_path / "wt")
        assert e.apply_policy == "manual"

    def test_make_executor_codex_defaults_to_inplace(self):
        # When the new kwargs are omitted, prior Codex behaviour is preserved.
        e = make_executor("cli", provider="codex")
        assert isinstance(e, CodexExecutor)
        assert e.workspace_strategy == "inplace"


# ---------------------------------------------------------------------------
# Generic executor_* workspace config (provider-agnostic).
#
# These tests pin the behaviour that the previous iteration introduced:
#   * the new generic kwargs configure the same Codex worktree machinery,
#   * legacy codex_* kwargs still work as before,
#   * when both are supplied, the generic form wins,
#   * the default remains Claude + inplace.
# ---------------------------------------------------------------------------


class TestMakeExecutorForwardsGenericConfig:
    def test_generic_kwargs_drive_codex_workspace(self, tmp_path: Path):
        e = make_executor(
            "cli",
            provider="codex",
            executor_workspace_strategy="worktree",
            executor_worktree_base_dir=str(tmp_path / "wt"),
            executor_apply_policy="manual",
        )
        assert isinstance(e, CodexExecutor)
        assert e.workspace_strategy == "worktree"
        assert e.worktree_base_dir == str(tmp_path / "wt")
        assert e.apply_policy == "manual"

    def test_generic_kwargs_omitted_defaults_to_inplace(self):
        # No kwargs at all: Codex still defaults to inplace, matching the
        # legacy codex_* default. Preserves current behaviour.
        e = make_executor("cli", provider="codex")
        assert e.workspace_strategy == "inplace"

    def test_generic_apply_policy_auto_propagates_to_executor(self, tmp_path: Path):
        # The factory must not silently swallow apply_policy='auto'; the
        # CodexExecutor itself is what raises NotImplementedError at run().
        e = make_executor(
            "cli",
            provider="codex",
            executor_workspace_strategy="worktree",
            executor_worktree_base_dir=str(tmp_path / "wt"),
            executor_apply_policy="auto",
        )
        assert e.apply_policy == "auto"


class TestMakeExecutorGenericPrecedenceOverLegacy:
    """When both forms are supplied, the generic executor_* form wins."""

    def test_strategy_precedence(self, tmp_path: Path):
        e = make_executor(
            "cli",
            provider="codex",
            executor_workspace_strategy="worktree",
            codex_workspace_strategy="inplace",
            executor_worktree_base_dir=str(tmp_path / "generic"),
            codex_worktree_base_dir=str(tmp_path / "legacy"),
        )
        assert e.workspace_strategy == "worktree"
        assert e.worktree_base_dir == str(tmp_path / "generic")

    def test_apply_policy_precedence(self):
        e = make_executor(
            "cli",
            provider="codex",
            executor_apply_policy="discard",
            codex_apply_policy="manual",
        )
        assert e.apply_policy == "discard"

    def test_legacy_alone_still_honoured(self, tmp_path: Path):
        # Back-compat: callers that still pass codex_* (no generic) get the
        # same behaviour they got before this sprint.
        e = make_executor(
            "cli",
            provider="codex",
            codex_workspace_strategy="worktree",
            codex_worktree_base_dir=str(tmp_path / "legacy"),
            codex_apply_policy="manual",
        )
        assert e.workspace_strategy == "worktree"
        assert e.worktree_base_dir == str(tmp_path / "legacy")
        assert e.apply_policy == "manual"

    def test_generic_alone_overrides_legacy_default(self, tmp_path: Path):
        # Generic kwargs alone (no codex_* passed) must reach CodexExecutor
        # without falling back to the legacy default.
        e = make_executor(
            "cli",
            provider="codex",
            executor_workspace_strategy="worktree",
            executor_worktree_base_dir=str(tmp_path / "generic-only"),
        )
        assert e.workspace_strategy == "worktree"
        assert e.worktree_base_dir == str(tmp_path / "generic-only")


class TestConfigGenericPrecedenceOverLegacy:
    """Config.load() mirrors generic <-> legacy fields. The generic form
    wins when both are written; otherwise whichever side is set populates
    the other so call sites reading either name see a consistent value."""

    def test_default_config_is_inplace_for_both_aliases(self):
        cfg = Config()
        assert cfg.executor_workspace_strategy == "inplace"
        assert cfg.codex_workspace_strategy == "inplace"
        assert cfg.executor_apply_policy == "manual"
        assert cfg.codex_apply_policy == "manual"

    def test_generic_only_yaml_populates_legacy_alias(self, tmp_path: Path):
        p = tmp_path / "config.yaml"
        p.write_text(
            "executor_workspace_strategy: worktree\n"
            "executor_worktree_base_dir: /tmp/from-generic\n"
            "executor_apply_policy: discard\n"
        )
        cfg = Config.load(p)
        assert cfg.executor_workspace_strategy == "worktree"
        assert cfg.codex_workspace_strategy == "worktree"
        assert cfg.executor_worktree_base_dir == "/tmp/from-generic"
        assert cfg.codex_worktree_base_dir == "/tmp/from-generic"
        assert cfg.executor_apply_policy == "discard"
        assert cfg.codex_apply_policy == "discard"

    def test_legacy_only_yaml_populates_generic_alias(self, tmp_path: Path):
        # Pre-existing config files using the codex_* keys must keep working
        # without edits, AND must surface through the generic field names so
        # the new make_executor wiring picks them up.
        p = tmp_path / "config.yaml"
        p.write_text(
            "codex_workspace_strategy: worktree\n"
            "codex_worktree_base_dir: /tmp/from-legacy\n"
            "codex_apply_policy: manual\n"
        )
        cfg = Config.load(p)
        assert cfg.executor_workspace_strategy == "worktree"
        assert cfg.executor_worktree_base_dir == "/tmp/from-legacy"
        assert cfg.executor_apply_policy == "manual"

    def test_both_set_generic_wins(self, tmp_path: Path):
        p = tmp_path / "config.yaml"
        p.write_text(
            "executor_workspace_strategy: worktree\n"
            "codex_workspace_strategy: inplace\n"
            "executor_apply_policy: discard\n"
            "codex_apply_policy: manual\n"
        )
        cfg = Config.load(p)
        # Generic wins on its own field; legacy field keeps whatever the
        # YAML explicitly wrote so existing readers aren't surprised.
        assert cfg.executor_workspace_strategy == "worktree"
        assert cfg.executor_apply_policy == "discard"


class TestProviderMatrix:
    """Provider behaviour matrix per sprint goal:
       * Claude + inplace   - default, unchanged
       * Codex  + inplace   - direct Codex
       * Codex  + worktree  - isolated Codex via generic config
       * Claude + worktree  - currently a soft no-op (still returns the
         in-place CLIExecutor); tested so a future sprint that switches to
         either real worktree support or an explicit unsupported error has
         to update this test deliberately."""

    def test_default_claude_inplace_returns_cli_executor(self):
        # Default Config + default make_executor must still mean Claude/CLI.
        cfg = Config()
        assert cfg.executor_provider == "claude"
        assert cfg.executor_workspace_strategy == "inplace"
        e = make_executor(
            "cli",
            cfg.claude_cli_path,
            provider=cfg.executor_provider,
            executor_workspace_strategy=cfg.executor_workspace_strategy,
            executor_worktree_base_dir=cfg.executor_worktree_base_dir,
            executor_apply_policy=cfg.executor_apply_policy,
        )
        assert isinstance(e, CLIExecutor)

    def test_codex_inplace_via_generic_config(self):
        e = make_executor(
            "cli",
            provider="codex",
            executor_workspace_strategy="inplace",
        )
        assert isinstance(e, CodexExecutor)
        assert e.workspace_strategy == "inplace"

    def test_codex_worktree_via_generic_config_runs_end_to_end(
        self, tmp_path: Path, monkeypatch
    ):
        # End-to-end: generic kwargs alone drive a real Codex worktree run
        # that captures a diff, cleans up the worktree, and leaves the
        # source repo untouched. The worktree must land under the generic
        # base dir, NOT inside the source repo or any artifact directory.
        src = tmp_path / "src"
        src.mkdir()
        _init_repo(src)
        base = tmp_path / "executor-wt-base"

        def fake_edits(cwd: Path):
            (cwd / "hello.txt").write_text("modified via generic config\n")
            (cwd / "added.txt").write_text("brand new\n")

        _patch_codex_popen(monkeypatch, edits=fake_edits)
        e = make_executor(
            "cli",
            provider="codex",
            executor_workspace_strategy="worktree",
            executor_worktree_base_dir=str(base),
            executor_apply_policy="manual",
        )
        res = e.run(prompt="x", repo_path=str(src))

        assert "hello.txt" in res.diff
        assert "added.txt" in res.diff
        assert res.workspace_path.startswith(str(base.resolve()))
        assert not Path(res.workspace_path).exists(), "worktree leaked"

        status = _real_subprocess.run(
            ["git", "-C", str(src), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert status == ""
        assert (src / "hello.txt").read_text() == "original\n"
        assert not (src / "added.txt").exists()

    def test_claude_with_worktree_config_raises_unsupported(
        self, tmp_path: Path
    ):
        # Claude + worktree is intentionally NOT implemented in this sprint.
        # Decision: fail loud (ValueError) rather than silently fall back to
        # in-place — operators who flip executor_workspace_strategy at the
        # Config level expect it to apply to whichever provider is selected.
        with pytest.raises(ValueError, match="worktree.*not supported.*claude"):
            make_executor(
                "cli",
                provider="claude",
                executor_workspace_strategy="worktree",
                executor_worktree_base_dir=str(tmp_path / "wt"),
                executor_apply_policy="manual",
            )

    def test_claude_with_legacy_codex_worktree_kwargs_also_raises(
        self, tmp_path: Path
    ):
        # The same guardrail must trip for the legacy alias, since Config
        # mirrors the two and an operator could plausibly leave a stale
        # codex_workspace_strategy: worktree behind after switching providers.
        with pytest.raises(ValueError, match="worktree.*not supported.*claude"):
            make_executor(
                "cli",
                provider="claude",
                codex_workspace_strategy="worktree",
                codex_worktree_base_dir=str(tmp_path / "wt"),
            )

    def test_claude_inplace_still_works_when_kwargs_explicit(self):
        # Explicitly setting inplace must still build a CLIExecutor — the
        # guardrail above must only fire for worktree.
        e = make_executor(
            "cli",
            provider="claude",
            executor_workspace_strategy="inplace",
        )
        assert isinstance(e, CLIExecutor)
