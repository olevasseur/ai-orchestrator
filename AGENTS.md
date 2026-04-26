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

## Reporting

At handoff, summarize changed files, exact tests or checks run, and remaining
risks. State clearly if no executable code was changed.
