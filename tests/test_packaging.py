"""Unit tests for tiny_loop post-run artifact packaging."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tiny_loop.artifacts import package_artifacts, _git_diff_stat


@pytest.fixture
def tmp_out(tmp_path):
    """Create a minimal output directory with state.json and summary.md."""
    (tmp_path / "state.json").write_text("{}")
    (tmp_path / "summary.md").write_text("# Summary")
    return tmp_path


@pytest.fixture
def base_state():
    return {
        "run_id": "test-run-001",
        "status": "success",
        "iterations": [],
        "files_changed": [],
    }


class TestGitDiffStat:
    def test_returns_output_on_success(self, tmp_path):
        """_git_diff_stat returns stdout from git diff --stat."""
        # Use the orchestrator repo itself as a known git repo
        repo = str(Path(__file__).resolve().parent.parent)
        result = _git_diff_stat(repo, "")
        # Should return something (even if "(no changes)")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_no_changes_on_empty(self, tmp_path):
        """Empty diff output should produce '(no changes)'."""
        with patch("tiny_loop.artifacts.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = _git_diff_stat(str(tmp_path), "abc123")
            assert result == "(no changes)"

    def test_returns_error_on_exception(self, tmp_path):
        """Exceptions should be caught and reported."""
        with patch("tiny_loop.artifacts.subprocess.run", side_effect=OSError("boom")):
            result = _git_diff_stat(str(tmp_path), "abc123")
            assert result.startswith("(error:")
            assert "boom" in result


class TestPackageArtifacts:
    def test_creates_three_core_files(self, tmp_out, base_state):
        """The three core packaging artifacts must always be created."""
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            created = package_artifacts("/fake/repo", tmp_out, "", base_state)

        names = {Path(p).name for p in created}
        assert "diff_stat.txt" in names
        assert "artifact_manifest.txt" in names
        assert "packaging_log.txt" in names

    def test_diff_stat_content(self, tmp_out, base_state):
        with patch(
            "tiny_loop.artifacts._git_diff_stat",
            return_value=" file.py | 3 +++\n 1 file changed, 3 insertions(+)",
        ):
            package_artifacts("/fake/repo", tmp_out, "abc123", base_state)

        content = (tmp_out / "diff_stat.txt").read_text()
        assert "file.py" in content
        assert "3 insertions" in content

    def test_manifest_marks_missing_state_files(self, tmp_path, base_state):
        """If state.json or summary.md are absent, manifest marks them [missing]."""
        # tmp_path has NO state.json or summary.md
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            package_artifacts("/fake/repo", tmp_path, "", base_state)

        manifest = (tmp_path / "artifact_manifest.txt").read_text()
        assert "state.json  [missing]" in manifest
        assert "summary.md  [missing]" in manifest

    def test_manifest_does_not_mark_present_files(self, tmp_out, base_state):
        """Present state files should NOT have [missing] markers."""
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            package_artifacts("/fake/repo", tmp_out, "", base_state)

        manifest = (tmp_out / "artifact_manifest.txt").read_text()
        assert "state.json  [missing]" not in manifest
        assert "summary.md  [missing]" not in manifest
        # But they should still be listed
        assert "state.json" in manifest
        assert "summary.md" in manifest

    def test_manifest_lists_repo_changes(self, tmp_out, base_state):
        base_state["files_changed"] = ["src/main.py", "tests/test_main.py"]
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            package_artifacts("/fake/repo", tmp_out, "", base_state)

        manifest = (tmp_out / "artifact_manifest.txt").read_text()
        assert "src/main.py" in manifest
        assert "tests/test_main.py" in manifest
        assert "2 files" in manifest

    def test_validation_iteration_outputs_extracted(self, tmp_out, base_state):
        """Validation step outputs are extracted to separate files."""
        base_state["iterations"] = [
            {"iteration": 1, "step_type": "implementation", "claude_output": "built it"},
            {"iteration": 2, "step_type": "validation", "claude_output": "all tests pass"},
            {"iteration": 3, "step_type": "packaging", "claude_output": "packaged"},
        ]
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            created = package_artifacts("/fake/repo", tmp_out, "", base_state)

        names = {Path(p).name for p in created}
        assert "validation_iter_2.txt" in names
        assert "packaging_iter_3.txt" in names
        # implementation outputs should NOT be extracted
        assert "implementation_iter_1.txt" not in names

        assert (tmp_out / "validation_iter_2.txt").read_text() == "all tests pass"
        assert (tmp_out / "packaging_iter_3.txt").read_text() == "packaged"

    def test_empty_claude_output_not_extracted(self, tmp_out, base_state):
        """Validation iterations with empty output are skipped."""
        base_state["iterations"] = [
            {"iteration": 1, "step_type": "validation", "claude_output": ""},
        ]
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            created = package_artifacts("/fake/repo", tmp_out, "", base_state)

        names = {Path(p).name for p in created}
        assert "validation_iter_1.txt" not in names

    def test_packaging_log_content(self, tmp_out, base_state):
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            package_artifacts("/fake/repo", tmp_out, "", base_state)

        log = (tmp_out / "packaging_log.txt").read_text()
        assert "test-run-001" in log
        assert "Packaging complete" in log
        assert "diff_stat.txt" in log

    def test_manifest_includes_run_metadata(self, tmp_out, base_state):
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            package_artifacts("/fake/repo", tmp_out, "", base_state)

        manifest = (tmp_out / "artifact_manifest.txt").read_text()
        assert "test-run-001" in manifest
        assert "success" in manifest
        assert "Generated:" in manifest

    def test_returns_created_paths(self, tmp_out, base_state):
        with patch("tiny_loop.artifacts._git_diff_stat", return_value="(no changes)"):
            created = package_artifacts("/fake/repo", tmp_out, "", base_state)

        # All returned paths should be real files
        for p in created:
            assert Path(p).exists(), f"Returned path does not exist: {p}"
        assert len(created) >= 3  # at minimum the 3 core files
