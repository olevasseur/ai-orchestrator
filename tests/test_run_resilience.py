"""Tests for tiny_loop.run finalisation when the loop crashes mid-flight."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tiny_loop import run as run_module
from tiny_loop.artifacts import ARCHIVE_FILE_THRESHOLD
from tiny_loop.claude_runner import ClaudeResult
from tiny_loop.reviewer import PlannerResult


@pytest.fixture
def repo_path(tmp_path):
    """A throwaway git repo so head_commit / diff_summary don't blow up."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True
    )
    return repo


def _planner_stub() -> PlannerResult:
    return PlannerResult(
        iteration_1_prompt="do the thing",
        rationale="r",
        expected_remaining_steps="none",
        response_id="resp_planner",
    )


def _claude_stub() -> ClaudeResult:
    return ClaudeResult(
        stdout="claude said hello",
        stderr="",
        exit_code=0,
        session_id="sess_1",
    )


class TestFinalisationOnReviewerCrash:
    """When the reviewer raises mid-loop, packaging + zip must still run."""

    def test_state_marked_errored(self, tmp_path, repo_path):
        out = tmp_path / "out"

        with (
            patch.object(run_module, "call_initial_planner", return_value=_planner_stub()),
            patch.object(run_module, "run_claude", return_value=_claude_stub()),
            patch.object(
                run_module, "call_reviewer",
                side_effect=RuntimeError("rate-limit boom"),
            ),
        ):
            with pytest.raises(RuntimeError, match="rate-limit boom"):
                run_module.run(
                    repo_path=str(repo_path),
                    objective="x",
                    max_iterations=3,
                    output_dir=str(out),
                    openai_api_key="fake",
                )

        state = json.loads((out / "state.json").read_text())
        assert state["status"] == "errored"
        assert state["error"]["type"] == "RuntimeError"
        assert "rate-limit boom" in state["error"]["message"]
        assert state["error"]["iteration"] == 1
        assert state["ended_at"] is not None

    def test_packaging_artifacts_written_after_crash(self, tmp_path, repo_path):
        out = tmp_path / "out"

        with (
            patch.object(run_module, "call_initial_planner", return_value=_planner_stub()),
            patch.object(run_module, "run_claude", return_value=_claude_stub()),
            patch.object(
                run_module, "call_reviewer",
                side_effect=RuntimeError("boom"),
            ),
        ):
            with pytest.raises(RuntimeError):
                run_module.run(
                    repo_path=str(repo_path),
                    objective="x",
                    max_iterations=3,
                    output_dir=str(out),
                    openai_api_key="fake",
                )

        # Core packaging deliverables exist even on crash.
        assert (out / "state.json").exists()
        assert (out / "summary.md").exists()
        assert (out / "diff_stat.txt").exists()
        assert (out / "artifact_manifest.txt").exists()
        assert (out / "packaging_log.txt").exists()

    def test_zip_created_when_threshold_met_after_crash(self, tmp_path, repo_path):
        out = tmp_path / "out"
        out.mkdir()

        # Pre-populate the run dir with enough dummy artifacts that the
        # post-crash finalisation will trip the archive threshold even
        # before packaging adds its own files.
        for i in range(ARCHIVE_FILE_THRESHOLD):
            (out / f"preexisting_{i}.txt").write_text(str(i))

        with (
            patch.object(run_module, "call_initial_planner", return_value=_planner_stub()),
            patch.object(run_module, "run_claude", return_value=_claude_stub()),
            patch.object(
                run_module, "call_reviewer",
                side_effect=RuntimeError("boom"),
            ),
        ):
            with pytest.raises(RuntimeError):
                run_module.run(
                    repo_path=str(repo_path),
                    objective="x",
                    max_iterations=3,
                    output_dir=str(out),
                    openai_api_key="fake",
                )

        archive = out.parent / f"{out.name}.zip"
        assert archive.exists(), (
            "expected zip archive after crash when run dir has >= "
            f"{ARCHIVE_FILE_THRESHOLD} files"
        )

        state = json.loads((out / "state.json").read_text())
        assert state["status"] == "errored"
        assert state.get("archive") == str(archive)


class TestFinalisationOnNormalExit:
    """A clean run with enough artifacts must also produce a zip."""

    def test_zip_created_when_threshold_met_on_clean_exit(self, tmp_path, repo_path):
        out = tmp_path / "out"
        out.mkdir()

        for i in range(ARCHIVE_FILE_THRESHOLD):
            (out / f"preexisting_{i}.txt").write_text(str(i))

        from tiny_loop.reviewer import ReviewerDecision

        decision = ReviewerDecision(
            decision="stop_success",
            rationale="done",
            next_prompt_for_claude=None,
            risk_flags=[],
            completion_assessment="all good",
            response_id="resp_1",
        )

        with (
            patch.object(run_module, "call_initial_planner", return_value=_planner_stub()),
            patch.object(run_module, "run_claude", return_value=_claude_stub()),
            patch.object(run_module, "call_reviewer", return_value=decision),
        ):
            run_module.run(
                repo_path=str(repo_path),
                objective="x",
                max_iterations=3,
                output_dir=str(out),
                openai_api_key="fake",
            )

        archive = out.parent / f"{out.name}.zip"
        assert archive.exists()

        state = json.loads((out / "state.json").read_text())
        assert state["status"] == "stop_success"
        assert state.get("archive") == str(archive)
