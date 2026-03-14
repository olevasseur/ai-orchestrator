Build a local human-in-the-loop coding orchestrator for iterative software development.

Primary use case:
I am building on existing repositories and I want to reduce copy-paste friction between an LLM planner, a coding agent, terminal commands, and my own human review.

High-level workflow:
1. I start a run for a given repo and a task prompt (often a long markdown prompt, not a one-line goal).
2. The tool sends the task, repo context, and previous iteration context to an OpenAI planner.
3. The planner returns:
   - a concise objective summary
   - a proposed implementation prompt for Claude Code
   - suggested validation commands
   - risks / assumptions
   - suggested next-step framing
4. The tool shows me the planner output in a terminal review step.
5. I can:
   - approve
   - edit the prompt
   - reject
   - ask a follow-up question to the planner before continuing
6. Once approved, the tool executes the implementation step with Claude Code.
7. After Claude Code finishes, the tool runs validation commands.
8. The tool stores logs, git diff summaries, exit codes, and iteration state.
9. The tool sends results back to the OpenAI planner.
10. The planner proposes the next increment.
11. The loop repeats until I stop.

Important product requirements:
- v1 should be local-only and terminal-first
- no phone/mobile UI yet
- architecture should make it easy to add phone approval later
- optimize for resumability after interruption
- optimize for reducing copy-paste and context loss
- support long-running commands
- support existing repositories
- task input can be either:
  - --task "text"
  - --task-file path/to/task.md

Technical requirements:
- Python project
- clean modular architecture
- pyproject.toml preferred
- use OpenAI API as planner
- use Claude Code as executor
- use JSON or YAML for persisted state
- save each run under a run directory
- store stdout/stderr logs in files
- store planner request/response artifacts
- store approved prompt artifacts
- store git diff summary after executor step
- command/job statuses:
  - queued
  - running
  - succeeded
  - failed
  - timed_out
  - awaiting_review

CLI commands to implement:
- orchestrator start --repo /path --task "...”
- orchestrator start --repo /path --task-file task.md
- orchestrator review
- orchestrator status
- orchestrator resume

Suggested project structure:
- planner/
- executor/
- jobs/
- storage/
- cli/
- ui/
- utils/

Behavior details:
- Read config from:
  - .env for secrets
  - config.yaml for tool settings
- Config should include:
  - OpenAI model name
  - repo path defaults
  - allowed validation commands
  - default timeouts
  - log directory
  - executor mode
- Safety:
  - destructive commands must require explicit confirmation
  - keep a simple allowlist / denylist mechanism for commands
- Review UX:
  - show planner summary
  - show proposed Claude prompt
  - show validation commands
  - allow approve/edit/ask/stop
- Long-running command support:
  - persist PID
  - persist start/end times
  - persist log file path
  - persist exit code
  - handle resume after interruption
- Keep the implementation simple and robust
- Avoid premature complexity
- Leave clean extension points for later Slack/Telegram/web approval

Executor integration:
Implement the executor behind an abstraction so that v1 can support either:
1. Claude Agent SDK
2. Claude Code CLI subprocess wrapper

Default to the CLI wrapper if the SDK path is not straightforward.

Deliverables:
- full scaffolded code
- pyproject.toml
- README with setup steps
- example .env.example
- example config.yaml
- example task.md
- example run directory with sample artifacts
- clear notes on where to plug in mobile approval later

Also include:
- a minimal demo mode
- a sample run against a toy repo
- comments explaining key architectural decisions
