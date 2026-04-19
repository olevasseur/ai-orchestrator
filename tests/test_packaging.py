"""Unit tests for tiny_loop post-run artifact packaging."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tiny_loop.artifacts import (
    _git_changed_files,
    _git_diff_stat,
    generate_diff_stat,
    package_artifacts,
    update_summary_repo_state,
)


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


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: real git repo with known committed, uncommitted, and untracked state
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def real_git_repo(tmp_path):
    """Build a throw-away git repo with a known start_commit and
    deterministic committed / uncommitted / untracked change sets.

    Layout after setup:
      - initial.txt            (in start_commit)
      - committed_file.py      (changed between start_commit..HEAD)
      - tracked_edit.py        (tracked, modified, NOT committed)
      - untracked_new.py       (untracked)
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "tester")
    run("config", "commit.gpgsign", "false")

    (repo / "initial.txt").write_text("hello\n")
    (repo / "committed_file.py").write_text("original = 1\n")
    (repo / "tracked_edit.py").write_text("keep = 1\n")
    run("add", ".")
    run("commit", "-q", "-m", "initial")

    start_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (repo / "committed_file.py").write_text("original = 2\n")
    run("add", "committed_file.py")
    run("commit", "-q", "-m", "sprint change")

    (repo / "tracked_edit.py").write_text("keep = 2\n")
    (repo / "untracked_new.py").write_text("new = 1\n")

    return repo, start_commit


class TestGitChangedFiles:
    """Verify _git_changed_files splits files into the three categories."""

    def test_categorizes_committed_uncommitted_untracked(self, real_git_repo):
        repo, start_commit = real_git_repo
        result = _git_changed_files(str(repo), start_commit)

        assert result["committed"] == ["committed_file.py"]
        assert result["uncommitted"] == ["tracked_edit.py"]
        assert result["untracked"] == ["untracked_new.py"]

    def test_no_committed_when_start_commit_blank(self, real_git_repo):
        """Without a start_commit the committed list is empty but the
        uncommitted/untracked views are still populated."""
        repo, _ = real_git_repo
        result = _git_changed_files(str(repo), "")

        assert result["committed"] == []
        assert "tracked_edit.py" in result["uncommitted"]
        assert "untracked_new.py" in result["untracked"]

    def test_returns_empty_lists_on_bad_repo(self, tmp_path):
        """A non-git path must degrade to empty lists, not raise."""
        result = _git_changed_files(str(tmp_path), "abc123")
        assert result == {"committed": [], "uncommitted": [], "untracked": []}


class TestManifestCategories:
    """Manifest must surface committed / uncommitted / untracked subsections
    and dedupe against state['files_changed']."""

    def test_manifest_includes_all_three_git_categories(
        self, tmp_out, base_state, real_git_repo
    ):
        repo, start_commit = real_git_repo
        package_artifacts(str(repo), tmp_out, start_commit, base_state)

        manifest = (tmp_out / "artifact_manifest.txt").read_text()
        assert "Committed (start_commit..HEAD)" in manifest
        assert "Uncommitted (tracked)" in manifest
        assert "Untracked (new files)" in manifest
        assert "committed_file.py" in manifest
        assert "tracked_edit.py" in manifest
        assert "untracked_new.py" in manifest

    def test_manifest_dedupes_state_and_git_files(
        self, tmp_out, base_state, real_git_repo
    ):
        """A file appearing in both state['files_changed'] and git must be
        listed only once in the top-level 'Repo changes' count."""
        repo, start_commit = real_git_repo
        base_state["files_changed"] = [
            "committed_file.py",  # overlaps with git's committed list
            "only_in_state.txt",  # state-only
        ]
        package_artifacts(str(repo), tmp_out, start_commit, base_state)

        manifest = (tmp_out / "artifact_manifest.txt").read_text()
        # Total unique = 3 git files + 1 state-only = 4
        assert "Repo changes (4 files)" in manifest
        # overlapping file appears exactly once in the top-level list —
        # count how many times it appears as a bare list item
        assert manifest.count("- committed_file.py") == 2  # top-level + committed subsection
        assert "only_in_state.txt" in manifest

    def test_manifest_omits_repo_changes_when_nothing_changed(
        self, tmp_path, base_state
    ):
        """Clean repo with no state files → no 'Repo changes' section."""
        clean_repo = tmp_path / "clean"
        clean_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=clean_repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"],
            cwd=clean_repo, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "t"], cwd=clean_repo, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "empty"],
            cwd=clean_repo, check=True,
        )

        out = tmp_path / "out"
        out.mkdir()
        (out / "state.json").write_text("{}")
        (out / "summary.md").write_text("# S")

        package_artifacts(str(clean_repo), out, "", base_state)
        manifest = (out / "artifact_manifest.txt").read_text()
        assert "Repo changes (" not in manifest


class TestUpdateSummaryRepoState:
    """Direct tests for the summary-append helper."""

    def test_appends_section_when_summary_exists(self, tmp_path):
        summary = tmp_path / "summary.md"
        summary.write_text("# Run foo\n\nbody\n")

        git_files = {
            "committed": ["a.py"],
            "uncommitted": ["b.py"],
            "untracked": ["c.py"],
        }
        updated = update_summary_repo_state(summary, git_files, ["a.py", "b.py", "c.py"])

        assert updated is True
        text = summary.read_text()
        assert text.startswith("# Run foo")  # original content preserved
        assert "Repo state (packaging — authoritative)" in text
        assert "Total changed files:** 3" in text
        assert "Committed (start_commit..HEAD) (1)" in text
        assert "Uncommitted (tracked) (1)" in text
        assert "Untracked (new files) (1)" in text
        assert "`a.py`" in text
        assert "`b.py`" in text
        assert "`c.py`" in text

    def test_returns_false_when_summary_missing(self, tmp_path):
        missing = tmp_path / "summary.md"
        assert not missing.exists()
        updated = update_summary_repo_state(
            missing,
            {"committed": [], "uncommitted": [], "untracked": []},
            [],
        )
        assert updated is False
        assert not missing.exists()

    def test_reports_no_changes_when_all_empty(self, tmp_path):
        summary = tmp_path / "summary.md"
        summary.write_text("# Run\n")
        updated = update_summary_repo_state(
            summary,
            {"committed": [], "uncommitted": [], "untracked": []},
            [],
        )
        assert updated is True
        text = summary.read_text()
        assert "Repo state (packaging — authoritative)" in text
        assert "No repo changes detected" in text

    def test_omits_empty_category_subsections(self, tmp_path):
        """Only non-empty categories get rendered as subsections."""
        summary = tmp_path / "summary.md"
        summary.write_text("# Run\n")
        git_files = {
            "committed": ["only_committed.py"],
            "uncommitted": [],
            "untracked": [],
        }
        update_summary_repo_state(summary, git_files, ["only_committed.py"])

        text = summary.read_text()
        assert "Committed (start_commit..HEAD) (1)" in text
        assert "Uncommitted (tracked)" not in text
        assert "Untracked (new files)" not in text


class TestPackageArtifactsAppendsSummary:
    """End-to-end: package_artifacts wires git state into the live summary.md."""

    def test_summary_receives_authoritative_section(
        self, tmp_out, base_state, real_git_repo
    ):
        repo, start_commit = real_git_repo
        package_artifacts(str(repo), tmp_out, start_commit, base_state)

        text = (tmp_out / "summary.md").read_text()
        assert "Repo state (packaging — authoritative)" in text
        assert "committed_file.py" in text
        assert "tracked_edit.py" in text
        assert "untracked_new.py" in text

    def test_summary_absent_is_tolerated(self, tmp_path, base_state, real_git_repo):
        """If summary.md doesn't exist (e.g., aborted run), packaging must
        still succeed and just log that it was skipped."""
        repo, start_commit = real_git_repo
        out = tmp_path / "out"
        out.mkdir()
        # No summary.md written.
        created = package_artifacts(str(repo), out, start_commit, base_state)

        log = (out / "packaging_log.txt").read_text()
        assert "summary.md absent" in log
        # Core artifacts still produced
        names = {Path(p).name for p in created}
        assert "diff_stat.txt" in names
        assert "artifact_manifest.txt" in names


class TestDiffStatSections:
    """generate_diff_stat must label committed vs uncommitted sections."""

    def test_diff_stat_has_both_sections(self, tmp_path, real_git_repo):
        repo, start_commit = real_git_repo
        out = tmp_path / "out"
        out.mkdir()

        generate_diff_stat(str(repo), out, start_commit)
        text = (out / "diff_stat.txt").read_text()

        assert "# Committed changes (start_commit..HEAD)" in text
        assert "# Uncommitted working-tree changes" in text
        assert "committed_file.py" in text
        assert "tracked_edit.py" in text


# ─────────────────────────────────────────────────────────────────────────────
# Summary-ordering tests: the initial `_write_summary` call in run.py now
# happens AFTER state['files_changed'] and state['artifacts'] are populated,
# so the first-written summary should already contain repo-state info
# without relying on the packaging-time authoritative append.
# ─────────────────────────────────────────────────────────────────────────────


def _minimal_state(**overrides):
    """Build a minimal state dict acceptable to tiny_loop.run._write_summary."""
    state = {
        "run_id": "test-ordering-001",
        "objective": "stub objective",
        "repo_path": "/fake/repo",
        "status": "stop_success",
        "iterations": [],
        "max_iterations": 5,
        "started_at": "2026-04-19T00:00:00+00:00",
        "ended_at": "2026-04-19T00:05:00+00:00",
        "final_outcome": "done",
        "files_changed": [],
        "artifacts": [],
    }
    state.update(overrides)
    return state


class TestSummaryOrdering:
    """The pre-packaging summary write must already reflect repo-state fields."""

    def test_write_summary_includes_files_changed(self, tmp_path):
        from tiny_loop.run import _write_summary

        state = _minimal_state(files_changed=["a/b.py", "c.py"])
        path = tmp_path / "summary.md"
        _write_summary(state, path)

        text = path.read_text()
        assert "## Repo files changed (2)" in text
        assert "`a/b.py`" in text
        assert "`c.py`" in text

    def test_write_summary_includes_artifacts(self, tmp_path):
        from tiny_loop.run import _write_summary

        state = _minimal_state(artifacts=["/out/state.json", "/out/summary.md"])
        path = tmp_path / "summary.md"
        _write_summary(state, path)

        text = path.read_text()
        assert "## Sprint artifacts to upload (2)" in text
        assert "`/out/state.json`" in text
        assert "`/out/summary.md`" in text

    def test_write_summary_omits_sections_when_empty(self, tmp_path):
        """With empty files_changed / artifacts, those sections are omitted."""
        from tiny_loop.run import _write_summary

        state = _minimal_state()
        path = tmp_path / "summary.md"
        _write_summary(state, path)

        text = path.read_text()
        assert "Repo files changed" not in text
        assert "Sprint artifacts to upload" not in text

    def test_initial_summary_precedes_and_survives_packaging_append(
        self, tmp_out, real_git_repo
    ):
        """Simulates the run.py ordering: write summary with populated
        files_changed, THEN run package_artifacts. The resulting summary
        must contain BOTH the pre-packaging section (from _write_summary)
        AND the authoritative append (from packaging)."""
        from tiny_loop.run import _write_summary

        repo, start_commit = real_git_repo
        state = _minimal_state(
            repo_path=str(repo),
            files_changed=["committed_file.py", "tracked_edit.py", "untracked_new.py"],
            artifacts=[str(tmp_out / "state.json"), str(tmp_out / "summary.md")],
        )

        # Step 1: pre-packaging summary write (new ordering)
        summary_path = tmp_out / "summary.md"
        _write_summary(state, summary_path)
        pre_packaging_text = summary_path.read_text()

        # Confirm the pre-packaging summary already has repo-state info
        assert "## Repo files changed (3)" in pre_packaging_text
        assert "`committed_file.py`" in pre_packaging_text
        assert "`tracked_edit.py`" in pre_packaging_text
        assert "`untracked_new.py`" in pre_packaging_text
        assert "## Sprint artifacts to upload (2)" in pre_packaging_text
        # The packaging-time section must NOT yet be present
        assert "Repo state (packaging — authoritative)" not in pre_packaging_text

        # Step 2: packaging runs and appends its authoritative section
        package_artifacts(str(repo), tmp_out, start_commit, state)
        final_text = summary_path.read_text()

        # Both sections now coexist
        assert "## Repo files changed (3)" in final_text
        assert "Repo state (packaging — authoritative)" in final_text
        # Pre-packaging content was preserved, not overwritten
        assert final_text.startswith(pre_packaging_text.rstrip() + "\n") or \
            pre_packaging_text.rstrip() in final_text
