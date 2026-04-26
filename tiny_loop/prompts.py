"""Prompt construction for tiny_loop executor iterations."""

from __future__ import annotations

_JSON_REMINDER = (
    "\nRespond with a json summary using the format described above."
)


def _artifact_instruction(artifact_dir: str | None) -> str:
    """Return an artifact-directory instruction block, or empty string."""
    if not artifact_dir:
        return ""
    return f"""
## Artifact directory
When you produce smoke-test captures, evidence files, or any output artifacts,
write them to: {artifact_dir}/artifacts/
Create the directory if it does not exist. Do NOT write artifacts to /tmp or
other ad-hoc locations.
"""


def build_initial_prompt(
    step: str,
    repo_context: str,
    *,
    artifact_dir: str | None = None,
    json_mode: bool = False,
) -> str:
    """First iteration: give the executor a specific bounded step.

    Args:
        step: The bounded implementation step for this iteration.
        repo_context: Repository context (recent commits, file tree, etc.).
        artifact_dir: Directory where smoke-test outputs and evidence files
            should be written.  When provided, an instruction block is added.
        json_mode: When True, appends a reminder that ensures the word 'json'
            appears in the prompt text.  Required by the OpenAI API when using
            ``response_format={"type": "json_object"}``.
    """
    suffix = _JSON_REMINDER if json_mode else ""
    artifact_block = _artifact_instruction(artifact_dir)
    return f"""\
You are implementing one bounded step in this repository.
An external reviewer will evaluate your work and tell you what to do next.

## Your ONE task for this iteration
{step}

## Repository context
{repo_context}
{artifact_block}
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
{suffix}"""


def build_continuation_prompt(
    objective: str,
    next_step: str,
    previous_summaries: list[dict],
    *,
    artifact_dir: str | None = None,
    json_mode: bool = False,
) -> str:
    """Subsequent iterations: next step from reviewer + prior context.

    Args:
        objective: Overall sprint objective (for context only).
        next_step: The bounded step assigned for this iteration.
        previous_summaries: List of prior iteration summary dicts.
        artifact_dir: Directory where smoke-test outputs and evidence files
            should be written.  When provided, an instruction block is added.
        json_mode: When True, appends a reminder that ensures the word 'json'
            appears in the prompt text.  Required by the OpenAI API when using
            ``response_format={"type": "json_object"}``.
    """
    history = "\n".join(
        f"- Iteration {s['iteration']}: {s.get('reviewer_decision', {}).get('completion_assessment', 'n/a')}"
        for s in previous_summaries
    )
    suffix = _JSON_REMINDER if json_mode else ""
    artifact_block = _artifact_instruction(artifact_dir)

    return f"""\
You are continuing an implementation task one step at a time.
An external reviewer evaluates your work after each step.

## Overall objective (for context only)
{objective}

## Previous iterations
{history}

## Your ONE task for this iteration
{next_step}
{artifact_block}
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
{suffix}"""
