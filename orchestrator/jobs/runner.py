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
from orchestrator.memory.manager import MemoryManager
from orchestrator.planner.openai_planner import OpenAIPlanner
from orchestrator.storage.store import RunStore
from orchestrator.ui import review as ui
from orchestrator.utils import git as git_utils
from orchestrator.utils.config import Config
from orchestrator.utils.safety import check_command
from orchestrator.utils.validation import run_validation_command, ValidationResult

# Labels and colours per classification, for terminal display
_CLASS_STYLE = {
    "passed":                 ("[green]✓  passed[/green]",              False),
    "implementation_failure": ("[red]✗  implementation failure[/red] [dim](fix code)[/dim]",  True),
    "missing_tool":           ("[yellow]⚠  environment / dependency issue[/yellow] [dim](missing tool or module — fix env setup, not code)[/dim]", True),
    "timeout":                ("[yellow]⏱  timed out[/yellow]",         True),
}


class OrchestratorRunner:
    def __init__(
        self,
        store: RunStore,
        planner: OpenAIPlanner,
        executor: BaseExecutor,
        config: Config,
        yes: bool = False,
        review_fn=None,
        status_fn=None,
        post_iter_fn=None,
    ) -> None:
        self.store = store
        self.planner = planner
        self.executor = executor
        self.config = config
        self.yes = yes  # skip all Confirm prompts when True
        # Optional callbacks for non-terminal drivers (e.g. web UI).
        # review_fn(plan_dict, iteration, ask_planner) -> {"decision": ..., "prompt": ...}
        self.review_fn = review_fn
        # status_fn(status_str, iteration_n) — called at key phase transitions
        self.status_fn = status_fn
        # post_iter_fn(itr_state, run_state) -> "continue" | "stopped"
        # Called after each completed iteration; blocks until user decides to proceed.
        self.post_iter_fn = post_iter_fn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop: plan → review → execute → validate → update memory → repeat."""
        run_state = self._load_run_state()

        # Memory lives in <target-repo>/.orchestrator/
        self.memory = MemoryManager(run_state.repo_path)
        self.memory.init()

        while True:
            itr_n = run_state.current_iteration
            itr_state = self._load_or_create_iteration(itr_n)

            ui.console.print(f"\n[bold cyan]━━ Iteration {itr_n} ━━[/bold cyan]")

            # --- Planning ---
            if itr_state.status == Status.QUEUED:
                self._notify("planning", itr_n)
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
                    ui.console.print("[green bold]Planner signals task complete![/green bold]")
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

                if self.review_fn is not None:
                    review = self.review_fn(plan_dict, itr_n, ask_planner)
                else:
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
                itr_state.proposed_prompt = approved_prompt
                itr_state.status = Status.RUNNING
                self._save_iteration(itr_state, run_state)

            # --- Pre-execution safety: warn on dirty working tree ---
            if itr_state.status == Status.RUNNING and not itr_state.executor_exit_code:
                self._warn_if_dirty(run_state.repo_path)

            # --- Execution ---
            if itr_state.status == Status.RUNNING and not itr_state.executor_exit_code:
                self._notify("executing", itr_n)
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

                diff = git_utils.diff_summary(run_state.repo_path)
                self.store.write_git_diff(itr_n, diff)

                # Checkpoint execution findings before validation so they survive
                # if validation is interrupted (see clear_exec_note at end of loop).
                if result.exit_code == 0:
                    self.memory.save_exec_note(
                        itr_n=itr_n,
                        objective=itr_state.objective,
                        executor_stdout=result.stdout,
                    )

                if result.timed_out:
                    itr_state.status = Status.TIMED_OUT
                    self._save_iteration(itr_state, run_state)
                    run_state.status = Status.TIMED_OUT
                    self._save_run_state(run_state)
                    ui.console.print("[yellow]Executor timed out.[/yellow]")
                    return

            # --- Validation ---
            self._notify("validating", itr_n)
            self._run_validation(itr_state, run_state)

            # --- Memory update (deterministic, no LLM) ---
            self.memory.update_working_memory(itr_state)
            self.memory.clear_exec_note()  # full iteration done; note is now in working memory
            sat = self.memory.saturation_status()
            ui.show_memory_saturation(sat)

            # --- Auto-refresh if threshold reached ---
            self._maybe_refresh_memory(run_state, itr_state.number, sat)

            # --- Advance ---
            itr_state.status = Status.SUCCEEDED
            itr_state.finished_at = datetime.utcnow().isoformat()
            self._save_iteration(itr_state, run_state)

            run_state.current_iteration += 1
            run_state.touch()
            self._save_run_state(run_state)

            # --- Post-iteration pause (web UI only) ---
            # Blocks here until the user clicks Continue or Stop.
            # The terminal CLI path leaves post_iter_fn=None and loops immediately.
            if self.post_iter_fn is not None:
                decision = self.post_iter_fn(itr_state, run_state)
                if decision == "stopped":
                    run_state.status = Status.STOPPED
                    self._save_run_state(run_state)
                    return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _warn_if_dirty(self, repo_path: str) -> None:
        """Warn the user if the repo has uncommitted changes before execution."""
        try:
            result = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            dirty = result.stdout.strip()
        except Exception:
            dirty = ""

        if dirty:
            ui.console.print(
                "\n[yellow bold]⚠  Uncommitted changes detected in target repo:[/yellow bold]"
            )
            for line in dirty.splitlines()[:10]:
                ui.console.print(f"  [dim]{line}[/dim]")
            if len(dirty.splitlines()) > 10:
                ui.console.print("  [dim]...[/dim]")
            if self.yes:
                ui.console.print("[dim]Proceeding (--yes).[/dim]")
                return
            from rich.prompt import Confirm
            if not Confirm.ask("Proceed with execution anyway?", default=True):
                raise RuntimeError("User aborted: uncommitted changes in repo.")

    def _call_planner(self, run_state: RunState, itr_n: int) -> dict:
        task = self.store.read_task()
        repo_ctx = git_utils.repo_context(run_state.repo_path)

        # Last 3 iterations only — older context lives in working_memory.md
        all_prev_ns = [n for n in self.store.list_iterations() if n < itr_n]
        recent_raw = [self.store.read_iteration_state(n) for n in all_prev_ns[-3:]]
        recent_iterations = []
        for itr_raw in recent_raw:
            summary = dict(itr_raw)
            if summary.get("validation_results"):
                summary["validation_summary"] = self._build_validation_summary(
                    summary.pop("validation_results")
                )
            else:
                summary.pop("validation_results", None)
            # Truncate proposed_prompt — planner doesn't need to re-read its own output;
            # working_memory already summarises what happened each iteration.
            if len(summary.get("proposed_prompt", "")) > 300:
                summary["proposed_prompt"] = summary["proposed_prompt"][:300] + "… [truncated]"
            recent_iterations.append(summary)

        project_memory = self.memory.load_project_memory()
        working_memory = self.memory.load_working_memory()

        # If a prior execution succeeded but validation was interrupted, surface
        # its findings to the planner so context isn't silently lost.
        exec_note = self.memory.load_exec_note()
        if exec_note:
            working_memory = working_memory + "\n\n---\n\n" + exec_note

        request_data = {
            "task": task,
            "project_memory": project_memory,
            "working_memory": working_memory,
            "repo_context": repo_ctx,
            "recent_iterations": recent_iterations,
        }
        self.store.write_planner_request(itr_n, request_data)

        plan = self.planner.plan(
            task, repo_ctx, recent_iterations,
            project_memory=project_memory,
            working_memory=working_memory,
        )
        self.store.write_planner_response(itr_n, plan)
        return plan

    def _run_validation(self, itr_state: IterationState, run_state: RunState) -> None:
        itr_n = itr_state.number
        val_output_lines: list[str] = []
        exit_codes: list[int] = []
        val_results: list[dict] = []

        ui.console.print("\n[bold]Validation:[/bold]")

        for cmd in itr_state.validation_commands:
            safety = check_command(
                cmd,
                self.config.command_allowlist,
                self.config.command_denylist,
            )
            if safety == "denied":
                ui.console.print(f"  [red]DENIED:[/red] {cmd}")
                exit_codes.append(-2)
                val_results.append({"cmd": cmd, "exit_code": -2,
                                    "classification": "denied", "timed_out": False})
                continue
            if safety == "needs_confirmation":
                if self.yes:
                    # Command was part of the approved plan — skip re-confirmation.
                    ui.console.print(f"  [dim]  (auto-approved via --yes)[/dim]")
                else:
                    from rich.prompt import Confirm
                    if not Confirm.ask(f"  Run unrecognised command [yellow]{cmd}[/yellow]?"):
                        exit_codes.append(-3)
                        val_results.append({"cmd": cmd, "exit_code": -3,
                                            "classification": "skipped", "timed_out": False})
                        continue

            ui.console.print(f"  [dim]$ {cmd}[/dim]")
            vr = run_validation_command(cmd, run_state.repo_path, self.config.validation_timeout)

            label, show_output = _CLASS_STYLE.get(
                vr.classification, ("[dim]unknown[/dim]", True)
            )
            ui.console.print(f"  {label}")

            if show_output and (vr.stdout.strip() or vr.stderr.strip()):
                combined = (vr.stdout + vr.stderr).strip()
                # Show last 20 lines to avoid flooding the terminal
                lines = combined.splitlines()[-20:]
                ui.console.print(
                    "  [dim]" + "\n  ".join(lines) + "[/dim]"
                )

            val_output_lines.append(f"$ {cmd}\n{vr.stdout}")
            exit_codes.append(vr.exit_code)
            val_results.append(vr.to_dict())

        itr_state.validation_exit_codes = exit_codes
        itr_state.validation_results = val_results
        self.store.write_validation_output(itr_n, "\n".join(val_output_lines), "")

    def _build_validation_summary(self, val_results: list[dict]) -> list[dict]:
        """
        Produce a compact structured summary for the planner.
        Keeps cmd + classification + exit_code; omits bulky stdout/stderr.
        """
        return [
            {
                "cmd": r.get("cmd", ""),
                "classification": r.get("classification", ""),
                "exit_code": r.get("exit_code"),
                "timed_out": r.get("timed_out", False),
            }
            for r in val_results
        ]

    def _maybe_refresh_memory(self, run_state: RunState, itr_n: int, sat: dict) -> None:
        """Auto-refresh memory if saturation or interval threshold is hit."""
        interval = getattr(self.config, "memory_refresh_interval", 5)
        should_refresh = (
            sat["recommendation"] == "refresh now"
            or ((itr_n + 1) % interval == 0)
        )
        if not should_refresh:
            return
        # Skip if no real API key (demo mode)
        if not self.config.openai_api_key or self.config.openai_api_key == "demo":
            ui.console.print("[dim]Memory refresh skipped (no API key).[/dim]")
            return
        before_chars = sat["char_count"]
        ui.console.print("[dim]Auto-refreshing memory...[/dim]")
        snapshot = self.memory.refresh(self.planner.compress_memory)
        after_chars = len(self.memory.load_working_memory())
        ui.console.print(
            f"[dim]Memory archived → {snapshot.name} "
            f"({before_chars} → {after_chars} chars)[/dim]"
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

    def _notify(self, status: str, iteration: int = 0) -> None:
        """Signal a phase transition to an external driver (e.g. web UI)."""
        if self.status_fn is not None:
            self.status_fn(status, iteration)
