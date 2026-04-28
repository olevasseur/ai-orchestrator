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
import shlex
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ARCHIVE_FILE_THRESHOLD = 20


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


def _git_changed_files(repo: str, start_commit: str) -> dict[str, list[str]]:
    """Return actual changed files from git, split by category.

    Keys: ``committed`` (start_commit..HEAD), ``uncommitted`` (tracked edits
    not yet committed), and ``untracked`` (new files not yet added).
    On any failure the corresponding list is empty.
    """

    def _run(args: list[str]) -> list[str]:
        try:
            r = subprocess.run(
                args, cwd=repo, capture_output=True, text=True, timeout=30
            )
            return [line for line in r.stdout.splitlines() if line.strip()]
        except Exception:
            return []

    committed = (
        _run(["git", "diff", "--name-only", start_commit, "HEAD"])
        if start_commit
        else []
    )
    uncommitted = _run(["git", "diff", "--name-only", "HEAD"])
    untracked = _run(["git", "ls-files", "--others", "--exclude-standard"])

    return {
        "committed": committed,
        "uncommitted": uncommitted,
        "untracked": untracked,
    }


def _git_run(
    repo: str | Path,
    args: list[str],
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_text(repo: str | Path, args: list[str]) -> str:
    result = _git_run(repo, args)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _parse_worktree_porcelain(output: str) -> list[dict[str, str]]:
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree" and current:
            worktrees.append(current)
            current = {}
        current[key] = value
    if current:
        worktrees.append(current)
    return worktrees


def _branch_name(worktree: dict[str, str], path: str) -> str:
    branch = _git_text(path, ["branch", "--show-current"])
    if branch:
        return branch
    porcelain_branch = worktree.get("branch", "")
    if porcelain_branch.startswith("refs/heads/"):
        return porcelain_branch.removeprefix("refs/heads/")
    return porcelain_branch or "(detached)"


def _is_ancestor(repo: str | Path, ancestor: str, descendant: str) -> bool | None:
    result = _git_run(repo, ["merge-base", "--is-ancestor", ancestor, descendant])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _format_bool(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _is_success_status(status: object) -> bool:
    return str(status) in {"success", "stop_success"}


def _md_cell(value: object) -> str:
    text = str(value) if value is not None else ""
    return text.replace("|", "\\|").replace("\n", " ").strip() or "-"


def audit_worktree_cleanup(repo: str | Path) -> str:
    """Return a markdown cleanup recommendation for git worktrees.

    This audit is read-only. It never removes worktrees, deletes branches, or
    fetches from remotes. ``origin/main`` checks use the local remote-tracking
    ref if it exists.
    """

    repo_path = str(Path(repo).resolve())
    main_head = _git_text(repo_path, ["rev-parse", "--short", "main"])
    main_ref_available = bool(main_head)
    origin_main_head = _git_text(repo_path, ["rev-parse", "--short", "origin/main"])
    origin_main_available = bool(origin_main_head)
    worktree_result = _git_run(repo_path, ["worktree", "list", "--porcelain"])

    lines = [
        "# Worktree Cleanup Recommendation",
        "",
        f"- Target repo: `{repo_path}`",
        f"- Local main HEAD: `{main_head or 'unavailable'}`",
        f"- Local origin/main HEAD: `{origin_main_head or 'unavailable'}`",
        "- Network fetch performed: no",
        "- Removal performed: no",
        "- Branch deletion performed: no",
        "",
    ]

    if worktree_result.returncode != 0:
        lines.extend(
            [
                "## Audit Error",
                "",
                "Could not list git worktrees.",
                "",
                "```text",
                (worktree_result.stderr or worktree_result.stdout).strip(),
                "```",
                "",
                "No cleanup recommendation could be produced.",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    worktrees = _parse_worktree_porcelain(worktree_result.stdout)
    rows: list[dict[str, object]] = []
    for worktree in worktrees:
        path = worktree.get("worktree", "")
        if not path:
            continue
        is_primary_checkout = Path(path).resolve() == Path(repo_path).resolve()
        head = _git_text(path, ["rev-parse", "--short", "HEAD"]) or worktree.get(
            "HEAD", "unknown"
        )[:12]
        branch = _branch_name(worktree, path)
        status_lines = _git_text(path, ["status", "--short"]).splitlines()
        untracked = [line for line in status_lines if line.startswith("??")]
        clean = len(status_lines) == 0
        merged_main = (
            _is_ancestor(path, "HEAD", "main") if main_ref_available else None
        )
        merged_origin_main = (
            _is_ancestor(path, "HEAD", "origin/main")
            if origin_main_available
            else None
        )
        safe_to_remove = (
            not is_primary_checkout
            and clean
            and not untracked
            and merged_main is True
        )
        reason_parts: list[str] = []
        if is_primary_checkout:
            reason_parts.append("primary checkout")
        if not clean:
            reason_parts.append("dirty working tree")
        if untracked:
            reason_parts.append("has untracked files")
        if merged_main is not True:
            reason_parts.append("not confirmed merged into main")
        if not reason_parts:
            reason_parts.append("clean and merged into main")

        rows.append(
            {
                "path": path,
                "branch": branch,
                "head": head,
                "status": "\\n".join(status_lines) if status_lines else "clean",
                "has_untracked": bool(untracked),
                "merged_main": merged_main,
                "merged_origin_main": merged_origin_main,
                "safe_to_remove": safe_to_remove,
                "reason": "; ".join(reason_parts),
                "remove_command": (
                    f"git -C {shlex.quote(repo_path)} worktree remove "
                    f"{shlex.quote(path)}"
                ),
            }
        )

    lines.extend(
        [
            "## Summary",
            "",
            "| Worktree | Branch | HEAD | Status | Untracked | Merged into main | Merged into origin/main | Safe to remove |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| {path} | {branch} | {head} | {status} | {untracked} | {merged_main} | {merged_origin_main} | {safe} |".format(
                path=_md_cell(row["path"]),
                branch=_md_cell(row["branch"]),
                head=_md_cell(row["head"]),
                status=_md_cell(row["status"]),
                untracked="yes" if row["has_untracked"] else "no",
                merged_main=_format_bool(row["merged_main"]),
                merged_origin_main=_format_bool(row["merged_origin_main"]),
                safe="yes" if row["safe_to_remove"] else "no",
            )
        )

    lines.extend(["", "## Details", ""])
    for row in rows:
        lines.extend(
            [
                f"### `{row['path']}`",
                "",
                f"- Branch: `{row['branch']}`",
                f"- HEAD: `{row['head']}`",
                f"- Git status summary: `{row['status']}`",
                f"- Branch merged into main: {_format_bool(row['merged_main'])}",
                (
                    "- Branch appears merged into origin/main: "
                    f"{_format_bool(row['merged_origin_main'])}"
                ),
                f"- Safe to remove: {'yes' if row['safe_to_remove'] else 'no'}",
                f"- Reason: {row['reason']}",
            ]
        )
        if row["safe_to_remove"]:
            lines.extend(
                [
                    "- Exact removal command:",
                    "",
                    "```bash",
                    str(row["remove_command"]),
                    "```",
                ]
            )
        lines.append("")

    lines.extend(
        [
            "## Safety Note",
            "",
            "This is an audit-only artifact. No worktrees were removed, no branches were deleted, and no remote fetch was performed.",
            "If `origin/main` is stale, the origin/main result may be stale too; fetch explicitly before relying on remote-tracking status.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def generate_diff_stat(repo: str, out: Path, start_commit: str) -> Path:
    """Capture the repository's current change state and write diff_stat.txt.

    Combines committed changes (start_commit..HEAD) with any uncommitted
    working-tree changes so the artifact reflects the full delta produced
    by the run, not just what was committed.
    """
    sections: list[str] = []

    committed = _git_diff_stat(repo, start_commit)
    sections.append("# Committed changes (start_commit..HEAD)")
    sections.append(committed)

    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        uncommitted = result.stdout.strip() or "(no changes)"
    except Exception as e:
        uncommitted = f"(error: {e})"

    sections.append("")
    sections.append("# Uncommitted working-tree changes")
    sections.append(uncommitted)

    path = out / "diff_stat.txt"
    path.write_text("\n".join(sections) + "\n")
    return path


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
    diff_path = generate_diff_stat(repo, out, start_commit)
    created.append(str(diff_path))
    line_count = len(diff_path.read_text().splitlines())
    _log(f"Generated diff_stat.txt ({line_count} lines)")

    # ── 2. Package captured validation / packaging iteration outputs ──
    for itr in state.get("iterations", []):
        step_type = itr.get("step_type", "implementation")
        if step_type in ("validation", "packaging"):
            itr_num = itr["iteration"]
            output = itr.get("executor_output") or itr.get("claude_output", "")
            if output:
                name = f"{step_type}_iter_{itr_num}.txt"
                _save(name, output)
                _log(f"Packaged {step_type} output from iteration {itr_num}")

    # ── 3. Derive authoritative repo-state view (shared by manifest + summary) ─
    git_files = _git_changed_files(repo, start_commit)
    state_files = list(state.get("files_changed", []))

    # Union of git-derived categories and whatever the harness tracked in
    # state, deduplicated while preserving first-seen order.
    seen: set[str] = set()
    all_changed: list[str] = []
    for source in (
        git_files["committed"],
        git_files["uncommitted"],
        git_files["untracked"],
        state_files,
    ):
        for f in source:
            if f not in seen:
                seen.add(f)
                all_changed.append(f)

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

    if all_changed:
        manifest_lines.extend(
            ["", f"## Repo changes ({len(all_changed)} files)"]
        )
        for f in all_changed:
            manifest_lines.append(f"- {f}")

        def _section(title: str, files: list[str]) -> None:
            if not files:
                return
            manifest_lines.extend(["", f"### {title} ({len(files)})"])
            for f in files:
                manifest_lines.append(f"- {f}")

        _section("Committed (start_commit..HEAD)", git_files["committed"])
        _section("Uncommitted (tracked)", git_files["uncommitted"])
        _section("Untracked (new files)", git_files["untracked"])

    manifest_lines.append("")
    _save("artifact_manifest.txt", "\n".join(manifest_lines) + "\n")
    _log("Generated artifact_manifest.txt")

    # ── 4. Update summary.md with authoritative repo-state section ───
    summary_updated = update_summary_repo_state(
        out / "summary.md", git_files, all_changed
    )
    if summary_updated:
        _log("Appended authoritative repo-state section to summary.md")
    else:
        _log("summary.md absent — skipped repo-state append")

    # ── 5. Worktree cleanup recommendation ───────────────────────────
    if (
        _is_success_status(state.get("status"))
        and state.get("executor_workspace_strategy") == "worktree"
        and state.get("audit_worktrees_after_run", True)
    ):
        _save("worktree_cleanup_recommendation.md", audit_worktree_cleanup(repo))
        _log("Generated worktree_cleanup_recommendation.md")
        if state.get("auto_remove_clean_merged_worktrees"):
            _log(
                "auto_remove_clean_merged_worktrees is set, but automatic "
                "removal is not implemented; audit-only recommendation written"
            )

    # ── 6. packaging_log.txt ─────────────────────────────────────────
    _log("Packaging complete")
    _save("packaging_log.txt", "\n".join(log_lines) + "\n")

    return created


def update_summary_repo_state(
    summary_path: Path,
    git_files: dict[str, list[str]],
    all_changed: list[str],
) -> bool:
    """Append an authoritative "Repo state (packaging)" section to summary.md.

    The summary is initially written before the run's final git state is
    known, so its own "Repo files changed" list can be empty even when
    files were modified.  This function appends the same authoritative
    view used by ``artifact_manifest.txt`` so the packaged summary is
    internally consistent with the manifest and ``diff_stat.txt``.

    Returns True if the summary was updated, False if it was not present.
    """
    if not summary_path.exists():
        return False

    lines: list[str] = ["", "## Repo state (packaging — authoritative)", ""]

    if not all_changed:
        lines.append("_No repo changes detected by git or state._")
        lines.append("")
    else:
        lines.append(f"**Total changed files:** {len(all_changed)}")
        lines.append("")

        def _section(title: str, files: list[str]) -> None:
            if not files:
                return
            lines.append(f"### {title} ({len(files)})")
            lines.append("")
            for f in files:
                lines.append(f"- `{f}`")
            lines.append("")

        _section("Committed (start_commit..HEAD)", git_files["committed"])
        _section("Uncommitted (tracked)", git_files["uncommitted"])
        _section("Untracked (new files)", git_files["untracked"])

    lines.append(
        "_Source: `artifact_manifest.txt` + `diff_stat.txt` — "
        "derived from git at packaging time._"
    )
    lines.append("")

    existing = summary_path.read_text()
    if not existing.endswith("\n"):
        existing += "\n"
    summary_path.write_text(existing + "\n".join(lines))
    return True


def archive_run_dir(
    out: Path, threshold: int = ARCHIVE_FILE_THRESHOLD
) -> Path | None:
    """Zip the run directory into a sibling archive when it has many files.

    Returns the archive path when created, None when below threshold or
    the directory is empty/missing. Existing archives are overwritten so
    the file count and zip stay consistent across re-finalisation.
    """
    if not out.exists() or not out.is_dir():
        return None

    files = [p for p in out.rglob("*") if p.is_file()]
    if len(files) < threshold:
        return None

    archive_path = out.parent / f"{out.name}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.relative_to(out.parent))
    return archive_path
