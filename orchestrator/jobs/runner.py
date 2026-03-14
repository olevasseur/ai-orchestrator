"""
Core orchestrator loop.

Responsibilities:
1. Call the planner to get the next increment plan.
2. Run the human review step (terminal UI or future webhook).
3. Execute the approved prompt with Claude Code.
4. Run validation commands.
5. Persist all artifacts.
6. Repeat until done or stopped.

This module has no CLI concerns — it's driven by the cli/ layer.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from orchestrator.executor.base import BaseExecutor
from orchestrator.jobs.models import IterationState, RunState, Status
from orchestrator.planner.openai_planner import OpenAIPlanner
from orchestrator.storage.store import RunStore
from orchestrator.ui import review as ui
from orchestrator.utils import git as git_utils
from orchestrator.utils.config import Config
from orchestrator.utils.safety import check_command


class OrchestratorRunner:
    def __init__(
        self,
        store: RunStore,
        planner: OpenAIPlanner,
        executor: BaseExecutor,
        config: Config,
    ) -> None:
        self.store = store
        self.planner = planner
        self.executor = executor
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop: plan → review → execute → validate → repeat."""
        run_state = self._load_run_state()

        while True:
            itr_n = run_state.current_iteration
            itr_state = self._load_or_create_iteration(itr_n)

            ui.console.print(
                f"\n[bold cyan]━━ Iteration {itr_n} ━━[/bold cyan]"
            )

            # --- Planning ---
            if itr_state.status == Status.QUEUED:
                itr_state.status = Status.RUNNING
                itr_state.started_at = datetime.utcnow().isoformat()
                self._save_iteration(itr_state, run_state)

                plan = self._call_planner(run_state, itr_n)
                itr_state.objective = plan.get("objective", "")
                itr_state.proposed_prompt = plan.get("proposed_prompt", "")
                itr_state.validation_commands = plan.get("validation_commands", [])
                itr_state.risks = plan.get("risks", "")
                itr_state.next_step_framing = plan.get("next_step_framing", "")
                itr_state.status = Status.AWAITING_REVIEW
                self._save_iteration(itr_state, run_state)

                if plan.get("done"):
                    ui.console.print(
                        "[green bold]Planner signals task complete![/green bold]"
                    )
                    run_state.status = Status.SUCCEEDED
                    self._save_run_state(run_state)
                    return

            # --- Review ---
            if itr_state.status == Status.AWAITING_REVIEW:
                plan_dict = {
                    "objective": itr_state.objective,
                    "proposed_prompt": itr_state.proposed_prompt,
                    "validation_commands": itr_state.validation_commands,
                    "risks": itr_state.risks,
                    "next_step_framing": itr_state.next_step_framing,
                }

                def ask_planner(question: str) -> str:
                    ctx = f"Task:\n{self.store.read_task()}\n\nPlan:\n{plan_dict}"
                    return self.planner.ask(question, ctx)

                review = ui.run_review(plan_dict, itr_n, ask_planner)
                itr_state.human_decision = review["decision"]
                approved_prompt = review["prompt"]

                if review["decision"] == "stopped":
                    run_state.status = Status.STOPPED
                    itr_state.status = Status.STOPPED
                    self._save_iteration(itr_state, run_state)
                    self._save_run_state(run_state)
                    return

                self.store.write_approved_prompt(itr_n, approved_prompt)
                itr_state.proposed_prompt = approved_prompt  # may have been edited
                itr_state.status = Status.RUNNING
                self._save_iteration(itr_state, run_state)

            # --- Execution ---
            if itr_state.status == Status.RUNNING and not itr_state.executor_exit_code:
                itr_dir = self.store.iteration_dir(itr_n)
                result = self.executor.run(
                    prompt=itr_state.proposed_prompt,
                    repo_path=run_state.repo_path,
                    timeout=self.config.executor_timeout,
                    log_stdout_path=str(itr_dir / "executor_stdout.log"),
                    log_stderr_path=str(itr_dir / "executor_stderr.log"),
                )

                ui.show_execution_result(result, itr_n)

                itr_state.executor_exit_code = result.exit_code
                self.store.write_executor_output(
                    itr_n, result.stdout, result.stderr, result.exit_code
                )

                # Git diff
                diff = git_utils.diff_summary(run_state.repo_path)
                self.store.write_git_diff(itr_n, diff)

                if result.timed_out:
                    itr_state.status = Status.TIMED_OUT
                    self._save_iteration(itr_state, run_state)
                    run_state.status = Status.TIMED_OUT
                    self._save_run_state(run_state)
                    ui.console.print("[yellow]Executor timed out.[/yellow]")
                    return

            # --- Validation ---
            self._run_validation(itr_state, run_state)

            # --- Advance ---
            itr_state.status = Status.SUCCEEDED
            itr_state.finished_at = datetime.utcnow().isoformat()
            self._save_iteration(itr_state, run_state)

            run_state.current_iteration += 1
            run_state.touch()
            self._save_run_state(run_state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_planner(self, run_state: RunState, itr_n: int) -> dict:
        task = self.store.read_task()
        repo_ctx = git_utils.repo_context(run_state.repo_path)
        prev_iterations = [
            self.store.read_iteration_state(n)
            for n in self.store.list_iterations()
            if n < itr_n
        ]

        request_data = {
            "task": task,
            "repo_context": repo_ctx,
            "previous_iterations": prev_iterations,
        }
        self.store.write_planner_request(itr_n, request_data)

        plan = self.planner.plan(task, repo_ctx, prev_iterations)
        self.store.write_planner_response(itr_n, plan)
        return plan

    def _run_validation(self, itr_state: IterationState, run_state: RunState) -> None:
        itr_n = itr_state.number
        val_stdout_lines = []
        exit_codes = []

        for cmd in itr_state.validation_commands:
            safety = check_command(
                cmd,
                self.config.command_allowlist,
                self.config.command_denylist,
            )
            if safety == "denied":
                ui.console.print(f"[red]DENIED command:[/red] {cmd}")
                exit_codes.append(-2)
                continue
            if safety == "needs_confirmation":
                from rich.prompt import Confirm
                if not Confirm.ask(f"Run unknown command [yellow]{cmd}[/yellow]?"):
                    exit_codes.append(-3)
                    continue

            ui.console.print(f"[dim]$ {cmd}[/dim]")
            try:
                r = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=run_state.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=self.config.validation_timeout,
                )
                val_stdout_lines.append(f"$ {cmd}\n{r.stdout}")
                exit_codes.append(r.returncode)
                if r.returncode == 0:
                    ui.console.print(f"  [green]✓[/green] {cmd}")
                else:
                    ui.console.print(f"  [red]✗[/red] {cmd} (exit {r.returncode})")
            except subprocess.TimeoutExpired:
                ui.console.print(f"  [yellow]timed out[/yellow]: {cmd}")
                exit_codes.append(-1)

        itr_state.validation_exit_codes = exit_codes
        self.store.write_validation_output(
            itr_n, "\n".join(val_stdout_lines), ""
        )

    def _load_run_state(self) -> RunState:
        d = self.store.read_state()
        if d:
            return RunState.from_dict(d)
        raise RuntimeError(f"No state found at {self.store.state_path}")

    def _load_or_create_iteration(self, n: int) -> IterationState:
        d = self.store.read_iteration_state(n)
        if d:
            return IterationState.from_dict(d)
        return IterationState(number=n)

    def _save_iteration(self, itr: IterationState, run: RunState) -> None:
        self.store.write_iteration_state(itr.number, itr.to_dict())

    def _save_run_state(self, run: RunState) -> None:
        run.touch()
        self.store.write_state(run.to_dict())
