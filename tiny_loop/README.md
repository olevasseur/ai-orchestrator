# tiny_loop

Bounded executor ↔ OpenAI iteration loop. A configured coding executor implements, OpenAI reviews, hard stop after 5 iterations.

## Setup

Requires Python 3.11+. By default tiny_loop uses the Claude CLI. If `config.yaml`
selects Codex, it uses the Codex CLI instead.

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
| `--claude-timeout` | `executor_timeout` from config | Executor timeout in seconds. Deprecated flag name kept for compatibility. |

## Executor Provider

tiny_loop reads the same executor settings as the main orchestrator:

```yaml
executor_provider: claude                  # claude | codex
executor_workspace_strategy: inplace       # inplace | worktree
executor_worktree_base_dir: /tmp/ai-orchestrator-executor-worktrees
executor_apply_policy: manual
claude_cli_path: claude
codex_cli_path: codex
```

Claude remains the default when no config is present. To run implementation
steps with Codex in an isolated worktree:

```yaml
executor_provider: codex
executor_workspace_strategy: worktree
```

In Codex worktree mode, tiny_loop reviews the diff returned from the temporary
worktree and writes it under the run's `artifacts/` directory. The target repo is
not modified by the executor unless the diff is applied later by a human.

## Output

Each run produces:
- `state.json` — full structured run state (all prompts, outputs, decisions)
- `summary.md` — human-readable markdown summary

Output is written to `/tmp/tiny-loop-runs/<run-id>/` by default.

## How it works

```
for each iteration (up to 5):
    1. Build executor prompt (initial objective or reviewer's next-step)
    2. Run configured executor in the target repo or isolated worktree
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
| `stop_failure` | Task failed or the executor is stuck |
| `pause_for_human` | Needs human judgment before continuing |
