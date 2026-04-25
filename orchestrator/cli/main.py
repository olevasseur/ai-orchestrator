"""
CLI entry point.

Commands:
  orchestrator start  --repo PATH --task TEXT [--task-file PATH] [--demo]
  orchestrator review
  orchestrator status
  orchestrator resume
  orchestrator memory init    --repo PATH
  orchestrator memory status  --repo PATH
  orchestrator memory refresh --repo PATH
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from orchestrator.executor.cli_executor import make_executor
from orchestrator.jobs.models import RunState, Status
from orchestrator.jobs.runner import OrchestratorRunner
from orchestrator.memory.manager import MemoryManager
from orchestrator.planner.openai_planner import OpenAIPlanner
from orchestrator.storage.store import RunStore
from orchestrator.ui import review as ui
from orchestrator.utils.config import Config

app = typer.Typer(help="Human-in-the-loop coding orchestrator.", no_args_is_help=True)
console = Console()


def _load_config(config_file: Optional[Path] = None) -> Config:
    return Config.load(config_file)


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
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Auto-approve validation prompts and dirty-tree warnings (no interactive confirmation).",
    ),
) -> None:
    """Start a new orchestration run."""
    cfg = Config.load(config_file)

    if task_file:
        task_text = task_file.read_text()
    elif task:
        task_text = task
    else:
        console.print("[red]Provide --task or --task-file.[/red]")
        raise typer.Exit(1)

    mode = "demo" if demo else cfg.executor_mode
    executor = make_executor(
        mode,
        cfg.claude_cli_path,
        provider=cfg.executor_provider,
        codex_cli_path=cfg.codex_cli_path,
        codex_workspace_strategy=cfg.codex_workspace_strategy,
        codex_worktree_base_dir=cfg.codex_worktree_base_dir,
        codex_apply_policy=cfg.codex_apply_policy,
    )

    # Resolve planner (stub in demo mode if no key)
    if mode == "demo" and not cfg.openai_api_key:
        from orchestrator.planner.openai_planner import OpenAIPlanner as _P

        class _DemoPlanner(_P):
            def plan(self, task, repo_context, recent_iterations, **kwargs):  # type: ignore[override]
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

            def compress_memory(self, working, project):  # type: ignore[override]
                return working, project  # no-op in demo mode

        planner = _DemoPlanner(api_key="demo", model=cfg.openai_model)
    else:
        planner = _make_planner(cfg)

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

    runner = OrchestratorRunner(store, planner, executor, cfg, yes=yes)
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
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Auto-approve validation prompts and dirty-tree warnings.",
    ),
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
    executor = make_executor(
        mode,
        cfg.claude_cli_path,
        provider=cfg.executor_provider,
        codex_cli_path=cfg.codex_cli_path,
        codex_workspace_strategy=cfg.codex_workspace_strategy,
        codex_worktree_base_dir=cfg.codex_worktree_base_dir,
        codex_apply_policy=cfg.codex_apply_policy,
    )
    planner = _make_planner(cfg)

    console.print(f"[green]Resuming run:[/green] {store.run_id}")
    runner = OrchestratorRunner(store, planner, executor, cfg, yes=yes)
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
    executor = make_executor(
        mode,
        cfg.claude_cli_path,
        provider=cfg.executor_provider,
        codex_cli_path=cfg.codex_cli_path,
        codex_workspace_strategy=cfg.codex_workspace_strategy,
        codex_worktree_base_dir=cfg.codex_worktree_base_dir,
        codex_apply_policy=cfg.codex_apply_policy,
    )
    planner = _make_planner(cfg)

    console.print(f"[green]Reviewing run:[/green] {store.run_id}")
    runner = OrchestratorRunner(store, planner, executor, cfg)
    runner.run()


# ---------------------------------------------------------------------------
# memory subcommand group
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------

@app.command()
def web(
    host: str = typer.Option("0.0.0.0", help="Host to bind (0.0.0.0 = all interfaces)."),
    port: int = typer.Option(7999, help="Port to listen on."),
) -> None:
    """Start the mobile-friendly web UI (requires: pip install 'orchestrator[web]')."""
    try:
        from orchestrator.web.server import app as _web_app  # noqa: F401
        import uvicorn
    except ImportError as exc:
        console.print(f"[red]Missing web dependency: {exc}[/red]")
        console.print("Install with:  pip install 'ai-orchestrator[web]'")
        raise typer.Exit(1)

    console.print(f"[green]Starting web UI on http://{host}:{port}[/green]")
    console.print("[dim]Open that address in your browser (or use your Tailscale IP).[/dim]")
    uvicorn.run("orchestrator.web.server:app", host=host, port=port, reload=False)


# ---------------------------------------------------------------------------
# memory subcommand group
# ---------------------------------------------------------------------------

memory_app = typer.Typer(help="Manage orchestrator memory for a target repo.")
app.add_typer(memory_app, name="memory")


@memory_app.command("init")
def memory_init(
    repo: str = typer.Option(".", help="Target repository path."),
) -> None:
    """Create .orchestrator/ memory files in the target repo (idempotent)."""
    mem = MemoryManager(repo)
    mem.init()
    console.print(f"[green]Memory initialised:[/green] {mem.root}")
    console.print(f"  {mem.project_memory_path}")
    console.print(f"  {mem.working_memory_path}")
    console.print(f"  {mem.snapshots_dir}/")


@memory_app.command("status")
def memory_status(
    repo: str = typer.Option(".", help="Target repository path."),
) -> None:
    """Show memory saturation and snapshot history for the target repo."""
    mem = MemoryManager(repo)
    if not mem.working_memory_path.exists():
        console.print("[yellow]No memory files found. Run `orchestrator memory init --repo PATH`.[/yellow]")
        raise typer.Exit(0)

    sat = mem.saturation_status()
    rec = sat["recommendation"]
    colour = {"healthy": "green", "monitor": "yellow",
              "refresh soon": "yellow", "refresh now": "red"}.get(rec, "white")
    _hints = {
        "healthy":      "",
        "monitor":      " (growing — keep an eye on it)",
        "refresh soon": " — consider: orchestrator memory refresh --repo .",
        "refresh now":  " — run: orchestrator memory refresh --repo .",
    }
    hint = _hints.get(rec, "")
    stale_label = (
        "yes (consecutive Next: lines look repetitive — refresh recommended)"
        if sat["stale_items_detected"]
        else "no"
    )
    working_chars = sat["char_count"]
    project_chars = sat["project_char_count"]
    total_chars = working_chars + project_chars

    console.print(f"\n[bold]Memory status:[/bold] {mem.root}")
    console.print(f"  Project memory    : {project_chars} chars  [dim](stable — not compressed by refresh)[/dim]")
    console.print(f"  Working memory    : {working_chars} chars · {sat['iterations_in_memory']} iteration(s)")
    console.print(f"  Total payload     : ~{total_chars} chars  [dim](sent to planner each iteration)[/dim]")
    console.print(f"  Open questions    : {sat['open_questions']}")
    console.print(f"  Stale detection   : {stale_label}")
    console.print(f"  Recommendation    : [{colour}]{rec}[/{colour}]{hint}  [dim](based on working memory)[/dim]")

    snapshots = mem.list_snapshots()
    if snapshots:
        console.print(f"\n  Snapshots ({len(snapshots)}):")
        for s in snapshots[:5]:
            console.print(f"    {s.name}")
        if len(snapshots) > 5:
            console.print(f"    … and {len(snapshots) - 5} more")
    else:
        console.print("\n  No snapshots yet.")


@memory_app.command("refresh")
def memory_refresh(
    repo: str = typer.Option(".", help="Target repository path."),
    config_file: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Compress and archive working memory (requires OPENAI_API_KEY)."""
    cfg = Config.load(config_file)
    planner = _make_planner(cfg)
    mem = MemoryManager(repo)
    mem.init()

    console.print("[dim]Compressing memory via LLM...[/dim]")
    snapshot = mem.refresh(planner.compress_memory)
    console.print(f"[green]Memory refreshed.[/green]")
    console.print(f"  Snapshot : {snapshot}")
    console.print(f"  Working  : {mem.working_memory_path}")
    console.print(f"  Project  : {mem.project_memory_path}")
