# tiny_loop

Bounded Claude ↔ OpenAI iteration loop. Claude implements, OpenAI reviews, hard stop after 5 iterations.

## Setup

Requires Python 3.11+ and the `claude` CLI installed.

```bash
# From the repo root (ai-orchestrator/)
pip install openai python-dotenv

# Set your OpenAI key
export OPENAI_API_KEY=sk-...
# or add it to .env in the repo root
```

## Usage

```bash
# Inline objective
python -m tiny_loop --repo /path/to/target/repo --objective "Add input validation to the signup form"

# Objective from file
python -m tiny_loop --repo . --objective-file task.md

# With options
python -m tiny_loop --repo . --objective "Fix the auth bug" --max-iterations 3 --openai-model gpt-4o
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | (required) | Path to target repository |
| `--objective` | | Task objective (inline text) |
| `--objective-file` | | Path to a file containing the objective |
| `--max-iterations` | 5 | Hard iteration cap |
| `--output-dir` | `/tmp/tiny-loop-runs/<run-id>/` | Override output directory |
| `--openai-model` | gpt-4o | OpenAI model for reviewer |
| `--claude-timeout` | 600 | Claude timeout in seconds |

## Output

Each run produces:
- `state.json` — full structured run state (all prompts, outputs, decisions)
- `summary.md` — human-readable markdown summary

Output is written to `/tmp/tiny-loop-runs/<run-id>/` by default.

## How it works

```
for each iteration (up to 5):
    1. Build Claude prompt (initial objective or reviewer's next-step)
    2. Run Claude in the target repo
    3. Capture output + git diff
    4. Send to OpenAI reviewer
    5. Reviewer decides: continue / stop_success / stop_failure / pause_for_human
    6. Save iteration record to state.json
    7. Stop or continue
```

## Reviewer decisions

| Decision | Meaning |
|----------|---------|
| `continue` | Proceed to next iteration with reviewer's next-step prompt |
| `stop_success` | Task is complete |
| `stop_failure` | Task failed or Claude is stuck |
| `pause_for_human` | Needs human judgment before continuing |
