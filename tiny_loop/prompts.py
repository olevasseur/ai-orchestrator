"""Prompt construction for Claude iterations."""

from __future__ import annotations


def build_initial_prompt(step: str, repo_context: str) -> str:
    """First iteration: give Claude a specific bounded step from the planner."""
    return f"""\
You are implementing one bounded step in this repository.
An external reviewer will evaluate your work and tell you what to do next.

## Your ONE task for this iteration
{step}

## Repository context
{repo_context}

## Instructions
1. Implement ONLY the step described above — make real code changes.
2. Run relevant tests or validations to confirm this step works.
3. STOP. Do not continue to additional steps even if you can see what comes next.

## Summary format
- What you changed and why (1-3 sentences)
- What tests/validations you ran and their results
- What files were modified
- What remains to be done

Do not expand scope beyond the step above. The reviewer decides what happens next.
"""


def build_continuation_prompt(
    objective: str,
    next_step: str,
    previous_summaries: list[dict],
) -> str:
    """Subsequent iterations: next step from reviewer + prior context."""
    history = "\n".join(
        f"- Iteration {s['iteration']}: {s.get('reviewer_decision', {}).get('completion_assessment', 'n/a')}"
        for s in previous_summaries
    )

    return f"""\
You are continuing an implementation task one step at a time.
An external reviewer evaluates your work after each step.

## Overall objective (for context only)
{objective}

## Previous iterations
{history}

## Your ONE task for this iteration
{next_step}

## Instructions
1. Implement ONLY the step described above — make real code changes.
2. Run relevant tests or validations to confirm this step works.
3. STOP. Do not continue to additional steps even if you can see what comes next.

## Summary format
- What you changed and why (1-3 sentences)
- What tests/validations you ran and their results
- What files were modified
- What remains to be done

Do not expand scope beyond the step above. The reviewer decides what happens next.
"""
