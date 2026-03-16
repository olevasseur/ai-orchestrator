"""
Context memory layer for the orchestrator.

Three files live in <repo>/.orchestrator/:
  project_memory.md     — stable facts, architecture, constraints
                          (human-maintained; NEVER overwritten by refresh)
  working_memory.md     — rolling per-iteration context (auto-updated, no LLM)
  memory_snapshots/     — archived working_memory on each refresh

Per-iteration updates are deterministic: built from IterationState fields,
appended as a markdown block. No LLM call in the hot path.

Compression (refresh) makes exactly one LLM call on working_memory only.
project_memory.md is read as context for the compressor but never overwritten.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.jobs.models import IterationState


_PROJECT_MEMORY_TEMPLATE = """\
# Project Memory

Stable facts, architecture decisions, and constraints for this project.
Edit this file manually when fundamental project facts change.
The orchestrator reads this on every planning call.

## Tech stack
<!-- e.g. Python 3.11, FastAPI, PostgreSQL -->

## Architecture
<!-- Key design decisions and module boundaries -->

## Constraints
<!-- Hard requirements, things that must not change -->

## Key file paths
<!-- Important files/dirs the planner should know about -->
"""

_WORKING_MEMORY_TEMPLATE = """\
# Working Memory

Rolling context updated after each completed iteration.
Compressed automatically at the configured refresh interval.

"""

_SATURATION_THRESHOLDS = {
    # (char_count, iterations_in_memory) → recommendation
    "refresh now":  (6000, 5),
    "refresh soon": (4000, 4),
    "monitor":      (2000, 3),
}


class MemoryManager:
    def __init__(self, repo_path: str) -> None:
        self.root = Path(repo_path).resolve() / ".orchestrator"

    @property
    def project_memory_path(self) -> Path:
        return self.root / "project_memory.md"

    @property
    def working_memory_path(self) -> Path:
        return self.root / "working_memory.md"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "memory_snapshots"

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Create .orchestrator/ and seed memory files if not present."""
        self.root.mkdir(exist_ok=True)
        self.snapshots_dir.mkdir(exist_ok=True)
        if not self.project_memory_path.exists():
            self.project_memory_path.write_text(_PROJECT_MEMORY_TEMPLATE)
        if not self.working_memory_path.exists():
            self.working_memory_path.write_text(_WORKING_MEMORY_TEMPLATE)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load_project_memory(self) -> str:
        return self.project_memory_path.read_text() if self.project_memory_path.exists() else ""

    def load_working_memory(self) -> str:
        return self.working_memory_path.read_text() if self.working_memory_path.exists() else ""

    # ------------------------------------------------------------------
    # Update — deterministic, no LLM
    # ------------------------------------------------------------------

    def update_working_memory(self, itr_state: IterationState) -> None:
        """Append a structured block for the completed iteration."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        val_line = _summarise_validation(itr_state)
        open_qs = _extract_open_questions(itr_state)

        open_q_section = (
            f"**Open questions:**\n{open_qs}\n\n" if open_qs != "- None" else ""
        )
        block = (
            f"## Iteration {itr_state.number} · {ts}\n\n"
            f"**Progress:** {itr_state.objective or '(no objective recorded)'}\n\n"
            f"**Decisions:** {itr_state.human_decision}"
            f" — executor exit {itr_state.executor_exit_code}."
            f"{(' ' + val_line) if val_line else ''}\n\n"
            f"**Assumptions:** {itr_state.risks or 'None recorded.'}\n\n"
            + open_q_section
            + f"**Next:** {itr_state.next_step_framing or 'See next planner output.'}\n\n"
            f"---\n\n"
        )
        current = self.load_working_memory()
        self.working_memory_path.write_text(current + block)

    # ------------------------------------------------------------------
    # Saturation
    # ------------------------------------------------------------------

    def saturation_status(self) -> dict:
        """Compute saturation metrics from working_memory.md contents."""
        working = self.load_working_memory()
        char_count = len(working)
        project_char_count = len(self.load_project_memory())

        # Count iteration blocks
        iterations_in_memory = working.count("\n## Iteration ")

        # Count open question lines (non-"None" entries)
        open_q_count = 0
        in_open_q = False
        for line in working.splitlines():
            stripped = line.strip()
            if stripped.startswith("**Open questions:**"):
                in_open_q = True
                continue
            if in_open_q:
                if stripped.startswith("**") or stripped == "---":
                    in_open_q = False
                elif stripped and stripped not in ("None", "- None"):
                    open_q_count += 1

        # Stale-item detection: similar consecutive "Next:" lines
        next_lines = [l for l in working.splitlines() if l.startswith("**Next:**")]
        stale = (
            len(next_lines) >= 2
            and _word_overlap(next_lines[-1], next_lines[-2]) > 0.7
        )

        # Recommendation
        recommendation = "healthy"
        for label, (char_thresh, itr_thresh) in _SATURATION_THRESHOLDS.items():
            if char_count > char_thresh or iterations_in_memory >= itr_thresh:
                recommendation = label
                break

        return {
            "char_count": char_count,
            "project_char_count": project_char_count,
            "iterations_in_memory": iterations_in_memory,
            "open_questions": open_q_count,
            "stale_items_detected": stale,
            "recommendation": recommendation,
        }

    # ------------------------------------------------------------------
    # Refresh / compression — one LLM call
    # ------------------------------------------------------------------

    def refresh(
        self,
        compress_fn: Callable[[str, str], str],
    ) -> Path:
        """
        Archive working_memory.md, compress via LLM, replace in place.

        project_memory.md is passed to compress_fn as read-only context but
        is never overwritten — it remains human-maintained.

        compress_fn: (working_memory, project_memory) -> new_working_memory
        Returns the path of the created snapshot file.
        """
        current_working = self.load_working_memory()
        current_project = self.load_project_memory()

        # Archive
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        snapshot_path = self.snapshots_dir / f"snapshot-{ts}.md"
        snapshot_path.write_text(
            f"# Memory Snapshot — {ts}\n\n"
            f"## Working Memory\n\n{current_working}\n\n"
            f"## Project Memory (at time of snapshot)\n\n{current_project}\n"
        )

        # Compress working memory only; project memory is not touched
        new_working = compress_fn(current_working, current_project)
        self.working_memory_path.write_text(new_working)

        return snapshot_path

    # ------------------------------------------------------------------
    # Snapshot listing
    # ------------------------------------------------------------------

    def list_snapshots(self) -> list[Path]:
        if not self.snapshots_dir.exists():
            return []
        return sorted(self.snapshots_dir.glob("snapshot-*.md"), reverse=True)


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _summarise_validation(itr_state: IterationState) -> str:
    results = getattr(itr_state, "validation_results", None) or []
    if not results:
        return ""
    parts = [f"`{r.get('cmd', '?')}` → {r.get('classification', '?')}" for r in results]
    return "Validation: " + "; ".join(parts) + "."


def _extract_open_questions(itr_state: IterationState) -> str:
    questions = []
    for r in (getattr(itr_state, "validation_results", None) or []):
        cls = r.get("classification", "")
        cmd = r.get("cmd", "?")
        if cls == "missing_tool":
            questions.append(f"- Missing tool/dep for `{cmd}` — needs env fix")
        elif cls == "timeout":
            questions.append(f"- `{cmd}` timed out — investigate performance")
        elif cls == "implementation_failure":
            questions.append(f"- `{cmd}` failed — code fix needed")
    return "\n".join(questions) if questions else "- None"


def _word_overlap(a: str, b: str) -> float:
    """Rough word-overlap ratio, 0–1. Used for stale-item detection."""
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))
