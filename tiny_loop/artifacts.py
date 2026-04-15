"""Post-sprint artifact packaging.

Deterministic packaging step that runs after the main iteration loop
on any terminal outcome.  No objective parsing — only produces artifacts
the harness can generate from data it already has.

Harness-owned packaging artifacts:
- diff_stat.txt         — git diff --stat from start commit to HEAD
- artifact_manifest.txt — categorized inventory of all run artifacts
- packaging_log.txt     — log of actions taken during packaging
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _git_diff_stat(repo: str, start_commit: str) -> str:
    """Run git diff --stat and return output."""
    cmd = (
        ["git", "diff", "--stat", start_commit, "HEAD"]
        if start_commit
        else ["git", "diff", "--stat"]
    )
    try:
        result = subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip() or "(no changes)"
    except Exception as e:
        return f"(error: {e})"


def package_artifacts(
    repo: str,
    out: Path,
    start_commit: str,
    state: dict,
) -> list[str]:
    """Run post-sprint packaging.  Returns paths of newly created files."""
    log_lines: list[str] = []
    created: list[str] = []

    def _log(msg: str) -> None:
        log_lines.append(msg)
        print(f"    {msg}")

    def _save(name: str, content: str) -> None:
        (out / name).write_text(content)
        created.append(str(out / name))

    run_id = state.get("run_id", "unknown")
    status = state.get("status", "unknown")
    _log(f"Packaging run {run_id} (status: {status})")

    # ── 1. diff_stat.txt ─────────────────────────────────────────────
    diff_stat = _git_diff_stat(repo, start_commit)
    _save("diff_stat.txt", diff_stat + "\n")
    _log(f"Generated diff_stat.txt ({len(diff_stat.splitlines())} lines)")

    # ── 2. Package captured validation / packaging iteration outputs ──
    for itr in state.get("iterations", []):
        step_type = itr.get("step_type", "implementation")
        if step_type in ("validation", "packaging"):
            itr_num = itr["iteration"]
            output = itr.get("claude_output", "")
            if output:
                name = f"{step_type}_iter_{itr_num}.txt"
                _save(name, output)
                _log(f"Packaged {step_type} output from iteration {itr_num}")

    # ── 3. artifact_manifest.txt ─────────────────────────────────────
    changed_files = state.get("files_changed", [])

    manifest_lines = [
        f"# Artifact Manifest — Run {run_id}",
        f"# Status: {status}",
        f"# Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Harness artifacts (sprint state)",
    ]
    for name in ("state.json", "summary.md"):
        present = (out / name).exists()
        manifest_lines.append(f"- {name}" + ("" if present else "  [missing]"))

    packaged_names = [Path(p).name for p in created]
    manifest_lines.extend(["", "## Harness artifacts (packaging step)"])
    for name in packaged_names:
        manifest_lines.append(f"- {name}")
    manifest_lines.append("- artifact_manifest.txt")
    manifest_lines.append("- packaging_log.txt")

    if changed_files:
        manifest_lines.extend(
            ["", f"## Repo changes ({len(changed_files)} files)"]
        )
        for f in changed_files:
            manifest_lines.append(f"- {f}")

    manifest_lines.append("")
    _save("artifact_manifest.txt", "\n".join(manifest_lines) + "\n")
    _log("Generated artifact_manifest.txt")

    # ── 4. packaging_log.txt ─────────────────────────────────────────
    _log("Packaging complete")
    _save("packaging_log.txt", "\n".join(log_lines) + "\n")

    return created
