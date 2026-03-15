"""
Terminal review UI using Rich.

Shows the planner output and prompts the user to:
  [a] approve
  [e] edit the proposed prompt
  [q] ask the planner a follow-up question
  [s] stop the run

Extension point: replace `run_review()` with a webhook call (Slack, Telegram,
web) that returns the same decision dict — the orchestrator loop doesn't care
how the approval arrives.
"""

from __future__ import annotations

from typing import Callable

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table

console = Console()


def show_plan(plan: dict, iteration: int) -> None:
    """Render the planner output in the terminal."""
    console.print(Rule(f"[bold cyan]Planner Output — Iteration {iteration}[/bold cyan]"))

    console.print(Panel(plan.get("objective", ""), title="Objective", border_style="green"))

    console.print("\n[bold]Proposed Claude Code Prompt:[/bold]")
    console.print(
        Panel(plan.get("proposed_prompt", ""), border_style="blue", padding=(1, 2))
    )

    cmds = plan.get("validation_commands", [])
    if cmds:
        console.print("\n[bold]Validation commands:[/bold]")
        for cmd in cmds:
            console.print(f"  [yellow]$[/yellow] {cmd}")

    if plan.get("risks"):
        console.print(Panel(plan["risks"], title="Risks / Assumptions", border_style="red"))

    if plan.get("next_step_framing"):
        console.print(
            Panel(
                plan["next_step_framing"],
                title="Next-step framing (preview)",
                border_style="dim",
            )
        )


def run_review(
    plan: dict,
    iteration: int,
    ask_planner: Callable[[str], str],
) -> dict:
    """
    Interactive review step.

    Returns a dict with:
      decision: "approved" | "edited" | "stopped"
      prompt: str   (approved or edited prompt)
    """
    show_plan(plan, iteration)

    proposed = plan.get("proposed_prompt", "")

    while True:
        console.print("\n")
        choice = Prompt.ask(
            "[bold]Review[/bold]",
            choices=["approve", "edit", "question", "stop"],
            default="approve",
            show_choices=True,
            show_default=True,
        ).lower()

        if choice == "approve":
            console.print("[green]Approved.[/green]")
            return {"decision": "approved", "prompt": proposed}

        elif choice == "edit":
            console.print(
                "[dim]Opening editor... paste your edited prompt below.\n"
                "End with a line containing only '---END---'[/dim]"
            )
            lines: list[str] = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if line.strip() == "---END---":
                    break
                lines.append(line)
            proposed = "\n".join(lines)
            console.print("[green]Prompt updated.[/green]")
            show_plan({**plan, "proposed_prompt": proposed}, iteration)

        elif choice == "question":
            question = Prompt.ask("Your question to the planner")
            console.print("[dim]Asking planner...[/dim]")
            answer = ask_planner(question)
            console.print(Panel(answer, title="Planner answer", border_style="cyan"))

        elif choice == "stop":
            console.print("[red]Run stopped by user.[/red]")
            return {"decision": "stopped", "prompt": proposed}


def show_execution_result(result, iteration: int) -> None:
    """Show a summary of the executor output."""
    console.print(Rule(f"[bold]Executor result — Iteration {iteration}[/bold]"))
    status = (
        "[green]SUCCESS[/green]"
        if result.exit_code == 0
        else "[red]FAILED[/red]"
        if not result.timed_out
        else "[yellow]TIMED OUT[/yellow]"
    )
    console.print(f"Status: {status}  (exit code: {result.exit_code})")
    if result.stdout:
        console.print(
            Panel(result.stdout[-3000:], title="stdout (last 3000 chars)", border_style="dim")
        )
    if result.stderr:
        console.print(
            Panel(result.stderr[-1000:], title="stderr (last 1000 chars)", border_style="red")
        )


def show_status(run_state: dict, iterations: list[dict]) -> None:
    """Show a summary table of the current run."""
    console.print(Rule("[bold cyan]Orchestrator Status[/bold cyan]"))
    console.print(f"Run ID   : [bold]{run_state.get('run_id', '?')}[/bold]")
    console.print(f"Repo     : {run_state.get('repo_path', '?')}")
    console.print(f"Status   : {run_state.get('status', '?')}")
    console.print(f"Iteration: {run_state.get('current_iteration', 0)}")

    if iterations:
        table = Table(title="Iterations")
        table.add_column("#", style="cyan")
        table.add_column("Status")
        table.add_column("Objective")
        table.add_column("Decision")
        for itr in iterations:
            table.add_row(
                str(itr.get("number", "?")),
                itr.get("status", ""),
                (itr.get("objective") or "")[:60],
                itr.get("human_decision", ""),
            )
        console.print(table)
