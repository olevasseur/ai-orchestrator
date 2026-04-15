# CLAUDE.md — AI Orchestrator

## Dev setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pip install openai python-dotenv
```

**Always use `.venv/bin/python`** (not bare `python` or `python3`) to run commands.
The system does not have a global Python on `PATH`, so bare `python` will fail with
exit code 127.

## Sprint prompts

When the user pastes a structured sprint prompt (containing sections like "Sprint title",
"Objective", "Validation plan", "Likely files involved", "Iterations", etc.), run it
through **tiny_loop** rather than implementing the changes directly:

```bash
.venv/bin/python -m tiny_loop --repo <target-repo> --objective-file <prompt-file> --max-iterations 5
```

The target repo is typically **not** this repo — it's whatever repo the sprint's
"Likely files involved" point to. **Always confirm the target repo path with the
user before running tiny_loop**, even if the sprint prompt mentions a specific repo
or path. Do not assume.

Save the sprint prompt to a temporary file and pass it via `--objective-file`, or use
`--objective` for short objectives. The prompt should be passed as-is; do not
summarize or rewrite it.

## Tests

```bash
.venv/bin/pytest
```
