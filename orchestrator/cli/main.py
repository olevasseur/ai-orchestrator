"""
CLI entry point.

Commands:
  orchestrator start  --repo PATH --task TEXT [--task-file PATH] [--demo]
  orchestrator review
  orchestrator status
  orchestrator resume
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from orchestrator.executor.cli_executor import make_executor
from orchestrator.jobs.models import RunState, Status
from orchestrator.jobs.runner import OrchestratorRunner
from orchestrator.planner.openai_planner import OpenAIPlanner
from orchestrator.storage.store import RunStore
from orchestrator.ui import review as ui
from orchestrator.utils.config import Config

app = typer.Typer(help="Human-in-the-loop coding orchestrator.", no_args_is_help=True)
console = Console()


def _load_config() -> Config:
    return Config.load()


def _make_planner(cfg: Config) -> OpenAIPlanner:
    if not cfg.openai_api_key:
        console.print("[red]OPENAI_API_KEY not set. Add it to .env or environment.[/red]")
        raise typer.Exit(1)
    return OpenAIPlanner(api_key=cfg.openai_api_key, model=cfg.openai_model)


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

@app.command()
def start(
    repo: str = typer.Option(..., help="Path to the target repository."),
    task: Optional[str] = typer.Option(None, help="Task description (inline text)."),
    task_file: Optional[Path] = typer.Option(
        None, "--task-file", help="Path to a markdown file with the task."
    ),
    demo: bool = typer.Option(False, "--demo", help="Demo mode: skip real Claude Code calls."),
    config_file: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml."),
) -> None:
    """Start a new orchestration run."""
    cfg = Config.load(config_file)

    # Resolve task text
    if task_file:
        task_text = task_file.read_text()
    elif task:
        task_text = task
    else:
        console.print("[red]Provide --task or --task-file.[/red]")
        raise typer.Exit(1)

    # Resolve executor mode
    mode = "demo" if demo else cfg.executor_mode
    executor = make_executor(mode, cfg.claude_cli_path)

    # Resolve planner (skip in demo mode if no key)
    if mode == "demo" and not cfg.openai_api_key:
        from orchestrator.planner.openai_planner import OpenAIPlanner as _P

        class _DemoPlanner(_P):
            def plan(self, task, repo_context, prev):  # type: ignore[override]
                return {
                    "objective": "[DEMO] Implement the task.",
                    "proposed_prompt": task[:1000],
                    "validation_commands": ["echo 'No real validation in demo mode'"],
                    "risks": "Demo mode — no real planning.",
                    "next_step_framing": "Next: add real OpenAI key.",
                    "done": False,
                }

            def ask(self, question, ctx):  # type: ignore[override]
                return "[DEMO] Planner not available without OPENAI_API_KEY."

        planner = _DemoPlanner(api_key="demo", model=cfg.openai_model)
    else:
        planner = _make_planner(cfg)

    # Create run
    store = RunStore.create(cfg.log_dir, repo)
    store.write_task(task_text)

    run_state = RunState(
        run_id=store.run_id,
        repo_path=str(Path(repo).resolve()),
        status=Status.QUEUED,
    )
    store.write_state(run_state.to_dict())

    console.print(f"[green]Run created:[/green] {store.run_id}")
    console.print(f"[dim]Artifacts: {store.run_dir}[/dim]")

    runner = OrchestratorRunner(store, planner, executor, cfg)
    runner.run()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status(
    run_id: Optional[str] = typer.Option(None, help="Run ID (default: most recent)."),
    config_file: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Show the status of the current (or specified) run."""
    cfg = Config.load(config_file)

    if run_id:
        store = RunStore.from_run_id(cfg.log_dir, run_id)
    else:
        store = RunStore.latest(cfg.log_dir)
        if store is None:
            console.print("[red]No runs found.[/red]")
            raise typer.Exit(1)

    run_state = store.read_state()
    iterations = [store.read_iteration_state(n) for n in store.list_iterations()]
    ui.show_status(run_state, iterations)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

@app.command()
def resume(
    run_id: Optional[str] = typer.Option(None, help="Run ID (default: most recent)."),
    demo: bool = typer.Option(False, "--demo"),
    config_file: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Resume an interrupted run from where it left off."""
    cfg = Config.load(config_file)

    if run_id:
        store = RunStore.from_run_id(cfg.log_dir, run_id)
    else:
        store = RunStore.latest(cfg.log_dir)
        if store is None:
            console.print("[red]No runs found.[/red]")
            raise typer.Exit(1)

    raw = store.read_state()
    if not raw:
        console.print(f"[red]No state found for run {store.run_id}.[/red]")
        raise typer.Exit(1)

    run_state = RunState.from_dict(raw)
    if run_state.status in (Status.SUCCEEDED, Status.STOPPED):
        console.print(f"[yellow]Run {store.run_id} already finished: {run_state.status}[/yellow]")
        raise typer.Exit(0)

    mode = "demo" if demo else cfg.executor_mode
    executor = make_executor(mode, cfg.claude_cli_path)
    planner = _make_planner(cfg)

    console.print(f"[green]Resuming run:[/green] {store.run_id}")
    runner = OrchestratorRunner(store, planner, executor, cfg)
    runner.run()


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@app.command()
def review(
    run_id: Optional[str] = typer.Option(None),
    config_file: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """
    Show the pending review for the current (or specified) run and process it.

    Useful when the run was interrupted at the awaiting_review step.
    """
    cfg = Config.load(config_file)

    if run_id:
        store = RunStore.from_run_id(cfg.log_dir, run_id)
    else:
        store = RunStore.latest(cfg.log_dir)
        if store is None:
            console.print("[red]No runs found.[/red]")
            raise typer.Exit(1)

    raw = store.read_state()
    run_state = RunState.from_dict(raw)
    itr_n = run_state.current_iteration
    itr_raw = store.read_iteration_state(itr_n)
    if not itr_raw or itr_raw.get("status") != Status.AWAITING_REVIEW:
        console.print(
            f"[yellow]No pending review for run {store.run_id} "
            f"(iteration {itr_n} status: {itr_raw.get('status', 'unknown')}).[/yellow]"
        )
        raise typer.Exit(0)

    mode = cfg.executor_mode
    executor = make_executor(mode, cfg.claude_cli_path)
    planner = _make_planner(cfg)

    console.print(f"[green]Reviewing run:[/green] {store.run_id}")
    runner = OrchestratorRunner(store, planner, executor, cfg)
    runner.run()
