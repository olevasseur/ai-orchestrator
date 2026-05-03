# AGENTS.md - ai-orchestrator

This repo is `ai-orchestrator`: a local, human-in-the-loop coding orchestrator.
Its loop is planner -> human review -> executor -> validation -> repeat.

## Agent Expectations

- Keep changes scoped to the user request. Do not make broad code changes or
  modify orchestrator behavior unless explicitly asked.
- Do not install dependencies unless the user explicitly asks.
- Prefer existing patterns in `orchestrator/`, `tiny_loop/`, and `tests/`.
- Read files before editing them and preserve unrelated user changes.

## Python And Tests

- Use `.venv/bin/python` for Python commands when available.
- Use `.venv/bin/pytest` for tests when available.
- Do not assume the virtualenv is activated.

## Sprint Prompts

When the user provides a structured sprint prompt with sections such as
`Sprint title`, `Objective`, `Validation plan`, `Likely files involved`, or
`Iterations`, do not implement it directly by default.

Instead:

1. Confirm the target repo path with the user.
2. Route the prompt through `tiny_loop` unless the user explicitly asks for
   direct implementation.
3. Pass the sprint prompt as-is via `--objective-file` when practical.

Typical command:

```bash
.venv/bin/python -m tiny_loop --repo <target-repo> --objective-file <prompt-file> --max-iterations 5
```

## APE Sprint Execution-Mode Rule

When the user asks for an APE sprint, APE implementation task, APE validation
task, or work targeting `/Users/aiagent/repos/ape`, you must confirm the
execution mode before doing implementation work.

If you are running from:

```text
/Users/aiagent/repos/ai-orchestrator
```

then default to using `tiny_loop`.

Before starting, explicitly state one of:

- Mode A: tiny_loop orchestrated run
- Mode B: direct Codex implementation

Mode A is the default for APE sprint work.

Do not silently bypass `tiny_loop`.

Fresh APE worktrees do not imply direct execution. `tiny_loop` can target a
fresh worktree.

Mode B is allowed only if:

- the user explicitly requests direct execution, or
- `tiny_loop` is unavailable/broken and you explain why, or
- you ask for and receive confirmation before proceeding.

If using Mode A, create normal `tiny_loop` artifacts and summaries.

If using Mode B, still create a handoff package with:

- `summary.md`
- `diff_stat.txt`
- `changed_files.txt`
- `validation.txt`
- `artifact_manifest.txt`
- `final_review_notes.md`

For APE work, always confirm the target repo/worktree path before running
commands. Do not accidentally run APE implementation work in
`/Users/aiagent/repos/ai-orchestrator`.

## Reporting

At handoff, summarize changed files, exact tests or checks run, and remaining
risks. State clearly if no executable code was changed.
