"""
Tests for the context memory layer.

All tests use tmp_path — no API keys required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.jobs.models import IterationState
from orchestrator.memory.manager import MemoryManager, _word_overlap


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mem(tmp_path) -> MemoryManager:
    m = MemoryManager(str(tmp_path))
    m.init()
    return m


def _make_itr(
    number: int = 0,
    objective: str = "Do something",
    risks: str = "Minor risk",
    next_step_framing: str = "Then do the next thing",
    human_decision: str = "approved",
    executor_exit_code: int = 0,
    validation_results: list | None = None,
) -> IterationState:
    itr = IterationState(number=number)
    itr.objective = objective
    itr.risks = risks
    itr.next_step_framing = next_step_framing
    itr.human_decision = human_decision
    itr.executor_exit_code = executor_exit_code
    itr.validation_results = validation_results or []
    return itr


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_directory_and_files(self, tmp_path):
        m = MemoryManager(str(tmp_path))
        m.init()
        assert (tmp_path / ".orchestrator").is_dir()
        assert (tmp_path / ".orchestrator" / "project_memory.md").exists()
        assert (tmp_path / ".orchestrator" / "working_memory.md").exists()
        assert (tmp_path / ".orchestrator" / "memory_snapshots").is_dir()

    def test_idempotent_does_not_overwrite(self, tmp_path):
        m = MemoryManager(str(tmp_path))
        m.init()
        m.project_memory_path.write_text("custom content")
        m.init()  # second call
        assert m.project_memory_path.read_text() == "custom content"

    def test_templates_contain_expected_sections(self, tmp_path):
        m = MemoryManager(str(tmp_path))
        m.init()
        project = m.load_project_memory()
        working = m.load_working_memory()
        assert "## Tech stack" in project
        assert "## Architecture" in project
        assert "Working Memory" in working


# ---------------------------------------------------------------------------
# update_working_memory
# ---------------------------------------------------------------------------

class TestUpdateWorkingMemory:
    def test_appends_iteration_block(self, mem):
        itr = _make_itr(number=0, objective="Add hello.py")
        mem.update_working_memory(itr)
        content = mem.load_working_memory()
        assert "## Iteration 0" in content
        assert "Add hello.py" in content

    def test_multiple_iterations_appended_in_order(self, mem):
        for i in range(3):
            mem.update_working_memory(_make_itr(number=i, objective=f"Step {i}"))
        content = mem.load_working_memory()
        assert content.index("## Iteration 0") < content.index("## Iteration 1")
        assert content.index("## Iteration 1") < content.index("## Iteration 2")

    def test_validation_results_summarised(self, mem):
        itr = _make_itr(
            number=0,
            validation_results=[
                {"cmd": "pytest", "classification": "passed", "exit_code": 0, "timed_out": False},
                {"cmd": "missing-tool", "classification": "missing_tool", "exit_code": 127, "timed_out": False},
            ],
        )
        mem.update_working_memory(itr)
        content = mem.load_working_memory()
        assert "pytest" in content
        assert "passed" in content
        assert "missing_tool" in content

    def test_open_questions_from_missing_tool(self, mem):
        itr = _make_itr(
            number=0,
            validation_results=[
                {"cmd": "pytest -q", "classification": "missing_tool", "exit_code": 127, "timed_out": False},
            ],
        )
        mem.update_working_memory(itr)
        content = mem.load_working_memory()
        assert "Missing tool" in content or "missing" in content.lower()

    def test_open_questions_omitted_when_all_passed(self, mem):
        itr = _make_itr(
            number=0,
            validation_results=[
                {"cmd": "echo ok", "classification": "passed", "exit_code": 0, "timed_out": False},
            ],
        )
        mem.update_working_memory(itr)
        content = mem.load_working_memory()
        # When no failures, the Open questions section is omitted entirely to reduce bloat
        assert "Open questions" not in content
        assert "- None" not in content


# ---------------------------------------------------------------------------
# saturation_status
# ---------------------------------------------------------------------------

class TestSaturationStatus:
    def test_fresh_memory_is_healthy(self, mem):
        sat = mem.saturation_status()
        assert sat["recommendation"] == "healthy"
        assert sat["iterations_in_memory"] == 0
        assert sat["open_questions"] == 0
        assert "project_char_count" in sat
        assert sat["project_char_count"] > 0  # template is non-empty

    def test_threshold_monitor(self, mem):
        for i in range(3):
            mem.update_working_memory(_make_itr(number=i))
        sat = mem.saturation_status()
        assert sat["recommendation"] in ("monitor", "refresh soon", "refresh now")
        assert sat["iterations_in_memory"] >= 3

    def test_threshold_refresh_now_by_iteration_count(self, mem):
        for i in range(5):
            mem.update_working_memory(_make_itr(number=i))
        sat = mem.saturation_status()
        assert sat["recommendation"] == "refresh now"

    def test_threshold_refresh_now_by_char_count(self, mem):
        # Write a large block directly
        mem.working_memory_path.write_text("x" * 7000)
        sat = mem.saturation_status()
        assert sat["recommendation"] == "refresh now"

    def test_open_questions_counted(self, mem):
        itr = _make_itr(
            number=0,
            validation_results=[
                {"cmd": "pytest", "classification": "missing_tool", "exit_code": 127, "timed_out": False},
                {"cmd": "mypy", "classification": "implementation_failure", "exit_code": 1, "timed_out": False},
            ],
        )
        mem.update_working_memory(itr)
        sat = mem.saturation_status()
        assert sat["open_questions"] >= 2

    def test_stale_detection_similar_next_lines(self, mem):
        # Write two very similar "Next:" lines
        content = (
            "# Working Memory\n\n"
            "## Iteration 0 · ts\n**Next:** Add type hints and verify edge cases.\n---\n\n"
            "## Iteration 1 · ts\n**Next:** Add type hints and verify edge cases.\n---\n\n"
        )
        mem.working_memory_path.write_text(content)
        sat = mem.saturation_status()
        assert sat["stale_items_detected"] is True

    def test_stale_detection_different_next_lines(self, mem):
        content = (
            "# Working Memory\n\n"
            "## Iteration 0 · ts\n**Next:** Add type hints.\n---\n\n"
            "## Iteration 1 · ts\n**Next:** Write integration tests for the API layer.\n---\n\n"
        )
        mem.working_memory_path.write_text(content)
        sat = mem.saturation_status()
        assert sat["stale_items_detected"] is False


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

class TestRefresh:
    def _fake_compress(self, working: str, project: str) -> str:
        return "# Compressed\n\nFresh summary."

    def test_refresh_creates_snapshot(self, mem):
        mem.update_working_memory(_make_itr(number=0))
        snapshot = mem.refresh(self._fake_compress)
        assert snapshot.exists()
        assert "snapshot-" in snapshot.name

    def test_snapshot_contains_original_content(self, mem):
        mem.update_working_memory(_make_itr(number=0, objective="Original objective"))
        snapshot = mem.refresh(self._fake_compress)
        snap_text = snapshot.read_text()
        assert "Original objective" in snap_text

    def test_working_memory_replaced_with_compressed(self, mem):
        mem.update_working_memory(_make_itr(number=0))
        mem.refresh(self._fake_compress)
        assert mem.load_working_memory() == "# Compressed\n\nFresh summary."

    def test_project_memory_not_modified_on_refresh(self, mem):
        original_project = mem.load_project_memory()
        mem.update_working_memory(_make_itr(number=0))
        mem.refresh(self._fake_compress)
        # project_memory must remain exactly as it was — never overwritten by refresh
        assert mem.load_project_memory() == original_project

    def test_multiple_refreshes_accumulate_snapshots(self, mem):
        for i in range(3):
            mem.update_working_memory(_make_itr(number=i))
            mem.refresh(self._fake_compress)
        assert len(mem.list_snapshots()) == 3

    def test_list_snapshots_sorted_newest_first(self, mem):
        for i in range(2):
            mem.update_working_memory(_make_itr(number=i))
            mem.refresh(self._fake_compress)
        snaps = mem.list_snapshots()
        assert snaps[0].name > snaps[1].name  # lexicographic = chronological for ts names


# ---------------------------------------------------------------------------
# exec_note checkpoint
# ---------------------------------------------------------------------------

class TestExecNote:
    def test_save_and_load(self, mem):
        mem.save_exec_note(itr_n=3, objective="Fix the bug", executor_stdout="All clean.")
        note = mem.load_exec_note()
        assert "iteration 3" in note
        assert "Fix the bug" in note
        assert "All clean." in note

    def test_load_returns_empty_when_absent(self, mem):
        assert mem.load_exec_note() == ""

    def test_clear_removes_file(self, mem):
        mem.save_exec_note(itr_n=0, objective="x", executor_stdout="y")
        mem.clear_exec_note()
        assert mem.load_exec_note() == ""
        assert not mem.exec_note_path.exists()

    def test_clear_is_safe_when_absent(self, mem):
        mem.clear_exec_note()  # should not raise

    def test_long_stdout_is_truncated_to_600_chars(self, mem):
        long_out = "x" * 2000
        mem.save_exec_note(itr_n=0, objective="obj", executor_stdout=long_out)
        note = mem.load_exec_note()
        excerpt_start = note.index("**Output excerpt:**\n") + len("**Output excerpt:**\n")
        excerpt = note[excerpt_start:].split("\n\n")[0]
        assert len(excerpt) <= 600


# ---------------------------------------------------------------------------
# helper: _word_overlap
# ---------------------------------------------------------------------------

class TestWordOverlap:
    def test_identical(self):
        assert _word_overlap("add type hints", "add type hints") == 1.0

    def test_no_overlap(self):
        assert _word_overlap("add type hints", "write integration tests") == 0.0

    def test_partial(self):
        score = _word_overlap("add type hints now", "add type annotations")
        assert 0.0 < score < 1.0

    def test_empty(self):
        assert _word_overlap("", "hello") == 0.0
