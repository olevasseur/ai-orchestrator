# AI Orchestrator

A local, terminal-first, human-in-the-loop coding orchestrator.

**Loop:** OpenAI planner → human review → Claude Code executor → validation → repeat.

---

## Quick start

```bash
# 1. Install (Python 3.11+)
pip install -e .

# 2. Configure
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY

# 3. Start a run
orchestrator start --repo /path/to/your/repo --task "Add a calculator module with tests"

# Or from a markdown file
orchestrator start --repo /path/to/your/repo --task-file examples/task.md

# Demo mode (no real API calls, no real Claude Code)
orchestrator start --repo . --task "Hello world" --demo
```

---

## Commands

| Command | Description |
|---|---|
| `orchestrator start` | Start a new run |
| `orchestrator status` | Show status of the most recent run |
| `orchestrator resume` | Resume an interrupted run |
| `orchestrator review` | Process a pending review step |

### Options for `start`

```
--repo PATH          Target repository path (required)
--task TEXT          Inline task description
--task-file PATH     Path to a markdown task file
--demo               Demo mode: skip real API/executor calls
--config PATH        Path to config.yaml (default: ./config.yaml)
```

---

## How it works

```
┌─────────────┐     ┌──────────────┐     ┌───────────────┐
│ OpenAI      │────▶│ Human review │────▶│ Claude Code   │
│ Planner     │     │ (terminal)   │     │ Executor      │
└─────────────┘     └──────────────┘     └───────────────┘
       ▲                                          │
       │         validation + git diff            │
       └──────────────────────────────────────────┘
```

1. **Planner** (OpenAI GPT-4o) analyses the task and repo context, then
   proposes a small implementation increment.
2. **Human review** shows the plan in the terminal. You can:
   - `[a]` approve
   - `[e]` edit the proposed prompt
   - `[q]` ask the planner a follow-up question
   - `[s]` stop the run
3. **Executor** (Claude Code CLI) runs the approved prompt in the target repo.
4. **Validation** runs the suggested shell commands (pytest, etc.).
5. Results are sent back to the planner; the loop repeats.

---

## Configuration

### `.env` (secrets, never commit)

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o     # optional
EXECUTOR_MODE=cli        # cli | demo
```

### `config.yaml` (tool settings)

See `config.yaml` for all options. Key settings:

```yaml
openai_model: gpt-4o
executor_mode: cli          # cli | demo
executor_timeout: 600       # seconds
command_allowlist:
  - pytest
  - python
command_denylist:
  - "rm -rf /"
  - sudo
```

---

## Artifact layout

Each run creates a directory under `~/.orchestrator/runs/<run-id>/`:

```
<run-id>/
  state.yaml                  # run state
  task.md                     # original task
  iterations/
    0/
      planner_request.json    # what was sent to OpenAI
      planner_response.json   # OpenAI response
      approved_prompt.md      # human-approved prompt
      executor_stdout.log
      executor_stderr.log
      executor_exit_code.txt
      git_diff.txt
      validation_stdout.log
      iteration_state.yaml
    1/
      ...
```

---

## Resumability

If the orchestrator is interrupted (crash, Ctrl-C), resume with:

```bash
orchestrator resume
# or
orchestrator resume --run-id <run-id>
```

The run restarts from exactly where it left off (queued → awaiting_review →
running) based on the persisted `iteration_state.yaml`.

---

## Executor modes

| Mode | Description |
|---|---|
| `cli` | Subprocess wrapper around `claude --print` (default) |
| `demo` | Fake executor, no real changes — safe for testing |

The executor is behind a `BaseExecutor` abstraction (`orchestrator/executor/base.py`).
To add the **Claude Agent SDK**, implement `BaseExecutor.run()` and set
`executor_mode: sdk` in config.

---

## Adding mobile / Slack approval (extension point)

The review step lives in `orchestrator/ui/review.py :: run_review()`.
It returns a dict `{"decision": "approved"|"stopped", "prompt": str}`.

To add Slack/Telegram/web approval:
1. Implement a new function (e.g., `slack_review()`) with the same signature.
2. In `orchestrator/jobs/runner.py`, replace the `ui.run_review()` call with
   your function based on a config flag (e.g., `approval_mode: slack`).
3. No other changes needed.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

---

## Project structure

```
orchestrator/
  cli/          # Typer CLI commands
  planner/      # OpenAI planner integration
  executor/     # BaseExecutor + CLIExecutor + DemoExecutor
  jobs/         # RunState, IterationState, OrchestratorRunner loop
  storage/      # RunStore: all filesystem I/O
  ui/           # Rich terminal review UI
  utils/        # Config, git helpers, safety checks
```
