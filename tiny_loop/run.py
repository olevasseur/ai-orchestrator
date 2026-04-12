#!/usr/bin/env python3
"""
tiny_loop: bounded Claude ↔ OpenAI iteration loop.

Usage:
    python -m tiny_loop.run --repo /path/to/repo --objective "Implement feature X"
    python -m tiny_loop.run --repo . --objective-file task.md --max-iterations 3
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from tiny_loop.claude_runner import run_claude
from tiny_loop.git_helpers import repo_context, diff_summary, has_meaningful_diff
from tiny_loop.prompts import build_initial_prompt, build_continuation_prompt
from tiny_loop.reviewer import build_reviewer_packet, call_reviewer, call_initial_planner
from tiny_loop.state import new_run_state, new_iteration_record, save_state


TERMINAL_DECISIONS = {"pause_for_human", "stop_success", "stop_failure"}

# Keywords used to classify iteration steps by type.
_VALIDATION_KEYWORDS = [
    "validate", "validation", "verify", "confirm", "check that",
    "run the full", "run pytest", "full test suite", "full suite",
    "smoke test", "smoke command", "scope check", "git diff --stat",
    "ensure everything", "ensure all tests",
]
_PACKAGING_KEYWORDS = [
    "package", "packaging", "handoff", "artifact", "summary",
    "capture output", "before/after",
]
_TEST_KEYWORDS = [
    "add test", "add focused test", "add targeted test", "add unit test",
    "write test", "create test", "test coverage", "tests for",
]


def classify_step(step_text: str) -> str:
    """Classify a step as 'implementation', 'tests', 'validation', or 'packaging'.

    Uses simple keyword matching on the step prompt. Checked in order of
    specificity: packaging > validation > tests > implementation (default).
    """
    lower = step_text.lower()
    if any(kw in lower for kw in _PACKAGING_KEYWORDS):
        return "packaging"
    if any(kw in lower for kw in _VALIDATION_KEYWORDS):
        return "validation"
    if any(kw in lower for kw in _TEST_KEYWORDS):
        return "tests"
    return "implementation"


def run(
    repo_path: str,
    objective: str,
    max_iterations: int = 5,
    output_dir: str | None = None,
    openai_api_key: str | None = None,
    openai_model: str = "gpt-4o",
    claude_timeout: int = 600,
) -> dict:
    """Execute the bounded iteration loop. Returns final state dict."""

    api_key = openai_api_key or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("Error: OPENAI_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    repo = str(Path(repo_path).resolve())
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # Output directory for this run — unique per run under /tmp by default
    out = Path(output_dir) if output_dir else Path("/tmp/tiny-loop-runs") / run_id
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"

    state = new_run_state(run_id, repo, objective, max_iterations)
    save_state(state, state_path)

    print(f"Run {run_id} started — max {max_iterations} iterations")
    print(f"Repo: {repo}")
    print(f"Output: {out}")
    print(f"Objective: {objective[:120]}{'...' if len(objective) > 120 else ''}")
    print()

    session_id: str | None = None

    # --- Initial planning: ask OpenAI for a bounded iteration-1 step ---
    ctx = repo_context(repo)
    print("Planning iteration 1...")
    initial_plan = call_initial_planner(api_key, openai_model, objective, ctx, max_iterations)
    state["initial_plan"] = initial_plan.to_dict()

    # Track OpenAI response chain for sprint continuity
    openai_response_id: str | None = initial_plan.response_id or None
    state["openai_thread"] = {
        "conversation_id": initial_plan.conversation_id,
        "planner_response_id": initial_plan.response_id,
        "latest_response_id": initial_plan.response_id,
        "response_ids": [initial_plan.response_id] if initial_plan.response_id else [],
    }

    save_state(state, state_path)
    print(f"  Step: {initial_plan.iteration_1_prompt[:120]}{'...' if len(initial_plan.iteration_1_prompt) > 120 else ''}")
    print(f"  Rationale: {initial_plan.rationale}")
    if initial_plan.response_id:
        print(f"  OpenAI response: {initial_plan.response_id}")
    print()

    for i in range(max_iterations):
        itr_num = i + 1
        state["current_iteration"] = itr_num
        print(f"── Iteration {itr_num}/{max_iterations} ──")

        # 1. Build the Claude prompt
        if itr_num == 1:
            current_step = initial_plan.iteration_1_prompt
            prompt = build_initial_prompt(current_step, ctx)
        else:
            last_decision = state["iterations"][-1]["reviewer_decision"]
            current_step = last_decision.get("next_prompt_for_claude") or objective
            prompt = build_continuation_prompt(
                objective, current_step, state["iterations"]
            )

        # 2. Classify step type
        step_type = classify_step(current_step)
        print(f"  Step type: {step_type}")

        # 3. Run Claude
        print("  Running Claude...")
        result = run_claude(
            prompt, repo, timeout=claude_timeout, resume_session_id=session_id
        )
        session_id = result.session_id or session_id

        abnormal = result.timed_out or result.exit_code != 0
        has_diff = has_meaningful_diff(repo) if abnormal else True

        if result.timed_out:
            print(f"  Claude timed out (>{claude_timeout}s)")
        elif result.exit_code != 0:
            print(f"  Claude exited with code {result.exit_code}")
        else:
            print(f"  Claude completed (exit 0)")

        # 3b. Retry policy — depends on step type
        # Implementation/tests: retry only if abnormal + no diff (existing behavior)
        # Validation/packaging: always retry on abnormal (these steps are low-risk)
        retried = False
        should_retry = False
        if abnormal:
            if step_type in ("validation", "packaging"):
                should_retry = True  # always worth retrying non-code steps
            elif not has_diff:
                should_retry = True  # no code produced — safe to retry

        if should_retry:
            print(f"  {'Validation/packaging' if step_type in ('validation', 'packaging') else 'No meaningful diff'} — retrying same step once...")
            retried = True
            result = run_claude(
                prompt, repo, timeout=claude_timeout, resume_session_id=session_id
            )
            session_id = result.session_id or session_id

            abnormal = result.timed_out or result.exit_code != 0
            has_diff = has_meaningful_diff(repo) if abnormal else True

            if result.timed_out:
                print(f"  Retry timed out (>{claude_timeout}s)")
            elif result.exit_code != 0:
                print(f"  Retry exited with code {result.exit_code}")
            else:
                print(f"  Retry completed (exit 0)")

        # 3. Capture git diff
        diff = diff_summary(repo)

        # 5. Build abnormal execution context if needed
        abnormal_execution = None
        if abnormal:
            abnormal_execution = {
                "timed_out": result.timed_out,
                "exit_code": result.exit_code,
                "timeout_seconds": claude_timeout,
                "has_meaningful_diff": has_diff,
                "was_retried": retried,
                "step_type": step_type,
            }

        # 5. Build reviewer packet and call OpenAI
        print("  Calling reviewer...")
        packet = build_reviewer_packet(
            objective=objective,
            iteration_number=itr_num,
            max_iterations=max_iterations,
            claude_output=result.stdout,
            git_diff=diff,
            previous_summaries=state["iterations"],
            current_step=current_step,
            abnormal_execution=abnormal_execution,
        )

        decision = call_reviewer(
            api_key, openai_model, packet,
            previous_response_id=openai_response_id,
        )

        # Update response chain
        if decision.response_id:
            openai_response_id = decision.response_id
            thread = state["openai_thread"]
            thread["latest_response_id"] = decision.response_id
            thread["response_ids"].append(decision.response_id)
            if decision.conversation_id:
                thread["conversation_id"] = decision.conversation_id

        print(f"  Decision: {decision.decision}")
        print(f"  Rationale: {decision.rationale}")
        if decision.risk_flags:
            print(f"  Risks: {', '.join(decision.risk_flags)}")

        # 6. Record iteration
        record = new_iteration_record(
            iteration=itr_num,
            prompt=prompt,
            claude_output=result.stdout,
            claude_exit_code=result.exit_code,
            claude_timed_out=result.timed_out,
            claude_session_id=result.session_id,
            git_diff=diff,
            reviewer_packet=packet,
            reviewer_decision=decision.to_dict(),
        )
        record["abnormal_execution"] = abnormal_execution
        record["was_retried"] = retried
        record["step_type"] = step_type
        record["openai_response_id"] = decision.response_id
        state["iterations"].append(record)
        save_state(state, state_path)

        # 6. Check stop conditions
        if decision.decision in TERMINAL_DECISIONS:
            print(f"\n  Stopping: {decision.decision}")
            state["status"] = decision.decision
            state["final_outcome"] = decision.completion_assessment
            break

        print()
    else:
        # Exhausted all iterations
        print(f"\n  Hard stop: reached {max_iterations} iterations")
        state["status"] = "max_iterations_reached"
        state["final_outcome"] = (
            state["iterations"][-1]["reviewer_decision"]["completion_assessment"]
            if state["iterations"]
            else "No iterations completed."
        )

    state["ended_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state, state_path)

    # Write human-readable summary
    _write_summary(state, out / "summary.md")

    summary_path = out / "summary.md"

    print(f"\n{'=' * 50}")
    print(f"Run complete.")
    print(f"  Status:  {state['status']}")
    print(f"  Run dir: {out}")
    print(f"  State:   {state_path}")
    print(f"  Summary: {summary_path}")
    print(f"{'=' * 50}")
    return state


def _write_summary(state: dict, path: Path) -> None:
    """Write a markdown summary for post-run human review."""
    lines = [
        f"# Run {state['run_id']}",
        f"",
        f"**Objective:** {state['objective']}",
        f"**Repo:** {state['repo_path']}",
        f"**Status:** {state['status']}",
        f"**Iterations:** {len(state['iterations'])} / {state['max_iterations']}",
        f"**Started:** {state['started_at']}",
        f"**Ended:** {state['ended_at']}",
        f"**Outcome:** {state.get('final_outcome', 'n/a')}",
        f"",
    ]

    plan = state.get("initial_plan")
    if plan:
        lines.extend([
            f"## Initial Plan (from OpenAI)",
            f"",
            f"**Iteration 1 step:** {plan.get('iteration_1_prompt', 'n/a')}",
            f"",
            f"**Rationale:** {plan.get('rationale', 'n/a')}",
            f"",
            f"**Expected remaining steps:** {plan.get('expected_remaining_steps', 'n/a')}",
            f"",
        ])

    for itr in state["iterations"]:
        dec = itr["reviewer_decision"]
        lines.extend([
            f"## Iteration {itr['iteration']}",
            f"",
            f"**Claude exit code:** {itr['claude_exit_code']}",
            f"**Reviewer decision:** {dec['decision']}",
            f"**Rationale:** {dec['rationale']}",
            f"**Assessment:** {dec['completion_assessment']}",
        ])
        if dec.get("risk_flags"):
            lines.append(f"**Risks:** {', '.join(dec['risk_flags'])}")
        lines.extend([
            f"",
            f"<details><summary>Claude output (click to expand)</summary>",
            f"",
            f"```",
            itr["claude_output"][:3000],
            f"```",
            f"</details>",
            f"",
        ])

    path.write_text("\n".join(lines))


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Bounded Claude ↔ OpenAI iteration loop."
    )
    parser.add_argument("--repo", required=True, help="Path to target repository.")
    parser.add_argument("--objective", default=None, help="Task objective (inline).")
    parser.add_argument(
        "--objective-file", default=None, help="Path to file containing the objective."
    )
    parser.add_argument(
        "--max-iterations", type=int, default=5, help="Max iterations (default: 5)."
    )
    parser.add_argument("--output-dir", default=None, help="Override output directory.")
    parser.add_argument("--openai-model", default="gpt-4o", help="OpenAI model for reviewer.")
    parser.add_argument(
        "--claude-timeout", type=int, default=600, help="Claude timeout in seconds."
    )

    args = parser.parse_args()

    if args.objective_file:
        objective = Path(args.objective_file).read_text().strip()
    elif args.objective:
        objective = args.objective
    else:
        parser.error("Provide --objective or --objective-file.")

    run(
        repo_path=args.repo,
        objective=objective,
        max_iterations=args.max_iterations,
        output_dir=args.output_dir,
        openai_model=args.openai_model,
        claude_timeout=args.claude_timeout,
    )


if __name__ == "__main__":
    main()
