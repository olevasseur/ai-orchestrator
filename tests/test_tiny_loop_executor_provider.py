"""Tests for tiny_loop executor provider selection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestrator.executor.base import ExecutionResult
from orchestrator.executor.cli_executor import CLIExecutor, CodexExecutor
from tiny_loop import run as run_module
from tiny_loop.executor_adapter import build_tiny_loop_executor
from tiny_loop.reviewer import PlannerResult, ReviewerDecision
from tiny_loop.state import load_state, new_iteration_record, save_state


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def _new_file_diff(name: str = "new.txt", content: str = "created by codex\n") -> str:
    return "\n".join(
        [
            f"diff --git a/{name} b/{name}",
            "new file mode 100644",
            "index 0000000..5e1c309",
            "--- /dev/null",
            f"+++ b/{name}",
            "@@ -0,0 +1 @@",
            f"+{content.rstrip()}",
            "",
        ]
    )


def test_default_tiny_loop_executor_uses_claude(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    selected = build_tiny_loop_executor()

    assert selected.provider == "claude"
    assert selected.workspace_strategy == "inplace"
    assert isinstance(selected.executor, CLIExecutor)
    assert selected.supports_resume is True


def test_codex_config_uses_codex_executor_with_worktree(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "executor_provider: codex",
                "codex_cli_path: /opt/codex",
                "executor_workspace_strategy: worktree",
                f"executor_worktree_base_dir: {tmp_path / 'worktrees'}",
                "executor_apply_policy: manual",
                "executor_timeout: 123",
            ]
        )
    )

    selected = build_tiny_loop_executor()

    assert selected.provider == "codex"
    assert selected.workspace_strategy == "worktree"
    assert selected.timeout == 123
    assert selected.supports_resume is False
    assert selected.uses_isolated_workspace is True
    assert isinstance(selected.executor, CodexExecutor)
    assert selected.executor.codex_cli_path == "/opt/codex"
    assert selected.executor.workspace_strategy == "worktree"
    assert selected.executor.worktree_base_dir == str(tmp_path / "worktrees")
    assert selected.executor.apply_policy == "manual"


class _CodexWorktreeStub:
    provider = "codex"
    workspace_strategy = "worktree"
    apply_policy = "manual"
    timeout = 600
    display_name = "Codex"
    uses_isolated_workspace = True

    def __init__(self, diff: str | None = None):
        self.diff = _new_file_diff() if diff is None else diff

    def run(self, prompt, repo_path, *, resume_session_id=None):
        assert resume_session_id is None
        return ExecutionResult(
            stdout="codex did it",
            stderr="",
            exit_code=0,
            session_id="codex-session",
            diff=self.diff,
            workspace_path="/tmp/fake-codex-worktree",
        )


class _RecordingCodexStub(_CodexWorktreeStub):
    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    def run(self, prompt, repo_path, *, resume_session_id=None):
        self.prompts.append(prompt)
        return super().run(prompt, repo_path, resume_session_id=resume_session_id)


def test_iteration_record_adds_generic_and_legacy_prompt_aliases():
    record = new_iteration_record(
        iteration=1,
        prompt="p",
        claude_output="out",
        claude_exit_code=0,
        claude_timed_out=False,
        claude_session_id="sess",
        git_diff="",
        reviewer_packet="packet",
        reviewer_decision={
            "decision": "continue",
            "rationale": "r",
            "next_prompt_for_executor": "next",
            "risk_flags": [],
            "completion_assessment": "done",
        },
        executor_provider="codex",
        executor_workspace_strategy="worktree",
    )

    decision = record["reviewer_decision"]
    assert decision["next_prompt_for_executor"] == "next"
    assert decision["next_prompt_for_claude"] == "next"
    assert record["executor_output"] == "out"
    assert record["claude_output"] == "out"


def test_load_state_backfills_prompt_aliases(tmp_path):
    path = tmp_path / "state.json"
    save_state(
        {
            "iterations": [
                {
                    "reviewer_decision": {
                        "next_prompt_for_claude": "legacy next",
                    }
                }
            ]
        },
        path,
    )

    state = load_state(path)

    decision = state["iterations"][0]["reviewer_decision"]
    assert decision["next_prompt_for_executor"] == "legacy next"
    assert decision["next_prompt_for_claude"] == "legacy next"


def test_run_records_executor_provider_and_workspace_diff(
    monkeypatch, tmp_path
):
    repo = _git_repo(tmp_path / "repo")
    out = tmp_path / "out"

    planner = PlannerResult(
        iteration_1_prompt="write a file",
        rationale="r",
        expected_remaining_steps="none",
        response_id="resp_planner",
    )
    decision = ReviewerDecision(
        decision="stop_success",
        rationale="done",
        next_prompt_for_claude=None,
        risk_flags=[],
        completion_assessment="all good",
        response_id="resp_1",
    )

    monkeypatch.setattr(
        run_module, "build_tiny_loop_executor", lambda timeout_override=None: _CodexWorktreeStub()
    )
    monkeypatch.setattr(run_module, "call_initial_planner", lambda *a, **k: planner)
    monkeypatch.setattr(run_module, "call_reviewer", lambda *a, **k: decision)

    state = run_module.run(
        repo_path=str(repo),
        objective="x",
        max_iterations=1,
        output_dir=str(out),
        openai_api_key="fake",
    )

    record = state["iterations"][0]
    assert state["executor_provider"] == "codex"
    assert state["executor_workspace_strategy"] == "worktree"
    assert record["executor_provider"] == "codex"
    assert record["executor_output"] == "codex did it"
    assert record["executor_exit_code"] == 0
    assert record["claude_output"] == "codex did it"
    assert record["executor_workspace_path"] == "/tmp/fake-codex-worktree"
    assert record["codex_patch_status"] == "skipped"

    diff_path = Path(record["executor_workspace_diff_path"])
    assert diff_path.exists()
    assert "diff --git" in diff_path.read_text()

    saved = json.loads((out / "state.json").read_text())
    assert saved["iterations"][0]["executor_provider"] == "codex"
    summary = (out / "summary.md").read_text()
    assert "**Executor:** codex" in summary
    assert "Executor output" in summary


def test_tiny_loop_applies_codex_diff_when_env_opt_in(
    monkeypatch, tmp_path
):
    repo = _git_repo(tmp_path / "repo")
    out = tmp_path / "out"
    seen_packets: list[str] = []

    planner = PlannerResult(
        iteration_1_prompt="write a file",
        rationale="r",
        expected_remaining_steps="none",
        response_id="resp_planner",
    )
    decision = ReviewerDecision(
        decision="stop_success",
        rationale="done",
        next_prompt_for_executor=None,
        risk_flags=[],
        completion_assessment="all good",
        response_id="resp_1",
    )

    monkeypatch.setenv("TINY_LOOP_APPLY_CODEX_DIFFS", "1")
    monkeypatch.setattr(
        run_module, "build_tiny_loop_executor", lambda timeout_override=None: _CodexWorktreeStub()
    )
    monkeypatch.setattr(run_module, "call_initial_planner", lambda *a, **k: planner)

    def reviewer(*args, **kwargs):
        seen_packets.append(args[2])
        return decision

    monkeypatch.setattr(run_module, "call_reviewer", reviewer)

    state = run_module.run(
        repo_path=str(repo),
        objective="x",
        max_iterations=1,
        output_dir=str(out),
        openai_api_key="fake",
    )

    record = state["iterations"][0]
    assert record["codex_patch_status"] == "applied"
    assert (repo / "new.txt").read_text() == "created by codex\n"
    assert "new.txt" in record["git_diff"]
    assert "created by codex" in seen_packets[0]


def test_tiny_loop_apply_check_failure_is_safe(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path / "repo")
    out = tmp_path / "out"
    bad_diff = "\n".join(
        [
            "diff --git a/missing.txt b/missing.txt",
            "--- a/missing.txt",
            "+++ b/missing.txt",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            "",
        ]
    )
    planner = PlannerResult(
        iteration_1_prompt="bad patch",
        rationale="r",
        expected_remaining_steps="none",
        response_id="resp_planner",
    )
    decision = ReviewerDecision(
        decision="stop_success",
        rationale="done",
        next_prompt_for_executor=None,
        risk_flags=[],
        completion_assessment="all good",
        response_id="resp_1",
    )

    monkeypatch.setenv("TINY_LOOP_APPLY_CODEX_DIFFS", "1")
    monkeypatch.setattr(
        run_module,
        "build_tiny_loop_executor",
        lambda timeout_override=None: _CodexWorktreeStub(diff=bad_diff),
    )
    monkeypatch.setattr(run_module, "call_initial_planner", lambda *a, **k: planner)
    monkeypatch.setattr(run_module, "call_reviewer", lambda *a, **k: decision)

    state = run_module.run(
        repo_path=str(repo),
        objective="x",
        max_iterations=1,
        output_dir=str(out),
        openai_api_key="fake",
    )

    record = state["iterations"][0]
    assert record["codex_patch_status"] == "failed"
    assert record["codex_patch_detail"]
    assert not (repo / "missing.txt").exists()


def test_codex_second_iteration_gets_prior_summary_without_resume(
    monkeypatch, tmp_path
):
    repo = _git_repo(tmp_path / "repo")
    out = tmp_path / "out"
    executor = _RecordingCodexStub()

    planner = PlannerResult(
        iteration_1_prompt="write first file",
        rationale="r",
        expected_remaining_steps="then continue",
        response_id="resp_planner",
    )
    decisions = [
        ReviewerDecision(
            decision="continue",
            rationale="first step complete",
            next_prompt_for_executor="write second file",
            risk_flags=[],
            completion_assessment="iteration one created the first file",
            response_id="resp_1",
        ),
        ReviewerDecision(
            decision="stop_success",
            rationale="done",
            next_prompt_for_executor=None,
            risk_flags=[],
            completion_assessment="all good",
            response_id="resp_2",
        ),
    ]

    monkeypatch.setattr(
        run_module, "build_tiny_loop_executor", lambda timeout_override=None: executor
    )
    monkeypatch.setattr(run_module, "call_initial_planner", lambda *a, **k: planner)
    monkeypatch.setattr(run_module, "call_reviewer", lambda *a, **k: decisions.pop(0))

    state = run_module.run(
        repo_path=str(repo),
        objective="x",
        max_iterations=2,
        output_dir=str(out),
        openai_api_key="fake",
    )

    assert len(executor.prompts) == 2
    assert "write second file" in executor.prompts[1]
    assert "iteration one created the first file" in executor.prompts[1]
    assert state["iterations"][1]["executor_session_id"] == "codex-session"
