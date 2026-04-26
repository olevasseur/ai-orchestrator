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
      git_diff.txt                # diff of the source repo (empty in Codex worktree mode)
      codex_workspace.diff        # Codex worktree-mode patch (only when Codex modified files)
      codex_workspace_path.txt    # absolute path of the disposable worktree (cleaned up post-run)
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

## Executor providers (Codex experimental)

`executor_provider` selects the agent backend used by `executor_mode: cli`.
Claude is the default and behaves exactly as before.

| Provider | Status | Command shape |
|---|---|---|
| `claude` (default) | Stable | `claude --print --dangerously-skip-permissions --output-format stream-json <prompt>` |
| `codex` | **Experimental** | `codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox <prompt>` |

### ⚠️ Safety warning for the Codex provider

The Codex provider invokes `codex exec` with
`--dangerously-bypass-approvals-and-sandbox`. As Codex's own help text says:

> Skip all confirmation prompts and execute commands without sandboxing.
> EXTREMELY DANGEROUS. Intended solely for running in environments that
> are externally sandboxed.

This means Codex can run arbitrary shell commands, modify files anywhere
the orchestrator process can reach, and make outbound network calls — with
no per-action prompt. Treat it like running untrusted code as your user.

**Use the Codex provider only when all of the following are true:**

- The target repo is a disposable clone (VM, container, throwaway directory).
- The host has no production credentials, SSH keys, or cloud tokens reachable
  from the working user.
- You explicitly want to evaluate Codex behavior; otherwise leave the
  default (`executor_provider: claude`).

Session resumption is not yet implemented for Codex. The runner persists a
Codex-emitted thread id as `executor_session_id` when one is present, but it
does **not** pass that value back to `CodexExecutor.run()` because the adapter
does not support native resume yet. Passing `resume_session_id` directly to
`CodexExecutor.run()` still raises `NotImplementedError` as a guardrail.

Until native Codex resume exists, continuity is prompt-based: each planner
request includes recent iteration summaries, compact validation summaries,
current repo context, project memory, working memory, and any saved execution
note from an interrupted iteration. The next executor prompt is expected to be
self-contained and derived from that context rather than from a resumed Codex
session.

### Workspace strategy (provider-agnostic)

The orchestrator supports an optional **workspace isolation** mode that runs
the executor inside a disposable git worktree, captures the resulting
unified diff, and leaves the source repo untouched until a human applies the
patch by hand. The settings are provider-agnostic — they live under the
`executor_*` namespace and apply to any provider whose adapter supports
worktree mode (today, only Codex).

```yaml
# config.yaml
executor_provider: claude                         # claude | codex
executor_workspace_strategy: inplace              # inplace | worktree
executor_worktree_base_dir: /tmp/ai-orchestrator-executor-worktrees
executor_apply_policy: manual                     # manual | discard  (auto: not implemented)
```

**Why worktree mode exists.** The Codex CLI runs with
`--dangerously-bypass-approvals-and-sandbox`, which lets it execute
arbitrary shell commands without per-action approval. Worktree mode confines
those writes to a throwaway git worktree so the source repo can't be
modified accidentally, and produces a reviewable diff artifact instead of
in-place mutations.

**Worktree mode is opt-in.** The default (`inplace`) preserves the prior
behaviour for both providers — Claude continues to run in the target repo
exactly as before. Switching strategies is a config-level decision, not a
provider-level one.

#### Provider support matrix

| Provider | `inplace` | `worktree` |
|---|---|---|
| `claude` (default) | ✅ Stable — runs `claude --print` in the target repo. | ❌ Not yet supported. `make_executor` raises `ValueError` so the misconfiguration is visible at startup. |
| `codex` | ✅ Direct mode — Codex runs in the target repo. Use only when the repo is itself disposable. | ✅ Codex runs in a detached worktree under `executor_worktree_base_dir`; the diff is persisted as an artifact and the worktree is removed afterwards. |
| Future providers | Same model — adapters opt into worktree by accepting the workspace kwargs. | Same model. |

#### What `worktree` does, step by step (Codex today)

1. Creates a **detached git worktree** under `executor_worktree_base_dir`
   from the source repo's current `HEAD`.
2. Runs the executor (`codex exec`) with the worktree as `cwd` — the source
   repo is never the working directory.
3. Captures the unified diff (including new and deleted files) by staging
   everything in the worktree and running `git diff --cached HEAD`.
4. Persists the diff as `iterations/<n>/codex_workspace.diff` and the
   workspace path as `iterations/<n>/codex_workspace_path.txt`.
5. Removes the worktree (success **and** failure) so disk usage stays bounded.
6. Surfaces the patch to the human reviewer with the exact apply command:
   `git -C <repo> apply <path/to/codex_workspace.diff>`.

#### Safety guarantees

- The source repo's `HEAD` and working tree are unchanged after a worktree
  iteration; nothing is auto-applied.
- All worktrees land under `executor_worktree_base_dir` — never inside the
  source repo, the user's home, `~/.orchestrator/runs/`, or any other
  artifact directory. `tests/test_executor_provider.py::TestCodexWorktreeMode`
  and `TestProviderMatrix` verify this.
- Cleanup runs in a `finally` block, so a crash or timeout still removes the
  worktree (the path stays on `ExecutionResult.workspace_path` for forensics).
- `executor_apply_policy: auto` is intentionally rejected with
  `NotImplementedError` — automatic apply is out of scope for this sprint.

#### Backward compatibility with legacy `codex_*` keys

Pre-existing config files that use the older Codex-specific names keep
working without edits:

- `codex_workspace_strategy` → `executor_workspace_strategy`
- `codex_worktree_base_dir`  → `executor_worktree_base_dir`
- `codex_apply_policy`       → `executor_apply_policy`

`Config.load()` mirrors the two forms: setting only the legacy key
populates the generic field, and vice versa. **When both forms are written,
the generic `executor_*` form wins.** New configurations should prefer the
generic names.

### Smoke-testing real Codex

A standalone script exercises the real `codex` binary against a fresh
disposable repo and prints the resulting `ExecutionResult`:

```bash
# Direct (inplace) mode — codex edits the throwaway repo directly
CODEX_SMOKE_TEST_OK=1 .venv/bin/python scripts/smoke_test_codex.py

# Worktree isolation mode — codex edits a disposable worktree, the
# throwaway repo stays clean, and the diff is captured + asserted
CODEX_SMOKE_TEST_OK=1 .venv/bin/python scripts/smoke_test_codex.py --worktree
```

The `--worktree` invocation prompts Codex to create `HELLO.txt`, then
verifies that the source repo's HEAD/working tree is unchanged, the file is
absent from the source repo, the worktree was cleaned up, the worktree
landed under the configured base dir, and the captured diff contains the
new file. It exits non-zero if any of those isolation invariants are
violated.

The smoke script refuses to run unless `CODEX_SMOKE_TEST_OK=1` is set, to
make the "this will run dangerous Codex" step explicit. The unit tests in
`tests/test_executor_provider.py` mock subprocess and do **not** require
Codex to be installed.

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
