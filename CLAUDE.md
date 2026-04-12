# CLAUDE.md — AI Orchestrator

## Dev setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
pip install openai python-dotenv
```

## Sprint prompts

When the user pastes a structured sprint prompt (containing sections like "Sprint title",
"Objective", "Validation plan", "Likely files involved", "Iterations", etc.), run it
through **tiny_loop** rather than implementing the changes directly:

```bash
python -m tiny_loop --repo <target-repo> --objective-file <prompt-file> --max-iterations 5
```

The target repo is typically **not** this repo — it's whatever repo the sprint's
"Likely files involved" point to. Ask the user for the target repo path if unclear.

Save the sprint prompt to a temporary file and pass it via `--objective-file`, or use
`--objective` for short objectives. The prompt should be passed as-is; do not
summarize or rewrite it.

## Tests

```bash
.venv/bin/pytest
```
