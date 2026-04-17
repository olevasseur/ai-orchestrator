"""OpenAI reviewer: evaluates Claude's iteration output and decides next step.

Uses the OpenAI Responses API with previous_response_id chaining so that
a single sprint maintains conversational continuity across the initial
planner call and all subsequent reviewer calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from typing import Any

from openai import OpenAI

INITIAL_PLANNER_SYSTEM_PROMPT = """\
You are an iteration planner for a bounded automation loop (max {max_iterations} iterations).

You will receive a sprint brief (the high-level objective and constraints) and
repository context. Your job is to produce ONLY the first bounded implementation
step for a coding agent (Claude) to execute.

Rules:
- The first step must be the smallest coherent change that moves toward the objective.
- "Smallest coherent step" means: one logical change that can be implemented and
  validated independently. Examples: add a single function, wire one dispatch entry,
  add one test class. NOT the entire feature at once.
- Do NOT repeat the full sprint brief back as the step. Decompose it.
- The step must be concrete and actionable — not "assess the codebase" or "plan the work".
- Include specific file paths, function names, or test cases when possible.
- Remember there are up to {max_iterations} iterations total, so scope the first step accordingly.

Respond with a JSON object with exactly these keys:
{{
  "iteration_1_prompt": "concrete, bounded instruction for Claude's first step",
  "rationale": "1-2 sentences on why this is the right first step",
  "expected_remaining_steps": "brief outline of what later iterations would cover"
}}
"""


REVIEWER_SYSTEM_PROMPT = """\
You are a code reviewer and iteration planner in a bounded automation loop.

A coding agent (Claude) is implementing a sprint objective iteratively. Each
iteration has a specific bounded step. You receive the step that was assigned,
Claude's output, a git diff, and the overall sprint objective.

Your job: decide what happens next.

Decision policy (follow in order):

1. stop_failure — Claude crashed, timed out, produced no useful changes, or is
   stuck repeating the same failed approach.

2. stop_success — The ENTIRE sprint objective is satisfied and validated. Every
   part of the objective has been implemented and tested. Do not stop with
   success if clearly scoped sub-parts remain unimplemented.

3. continue — The current iteration's step was completed (even partially), the
   overall sprint still has clearly in-scope work remaining, and you can identify
   a concrete next step. THIS IS THE DEFAULT when progress was made and the
   sprint is not yet fully done. A successful bounded step with remaining work
   is a normal continue, not a reason to pause.

4. pause_for_human — Reserve this for genuinely ambiguous, risky, or blocked
   situations:
   - Test failures or regressions that are not obviously fixable
   - Contradictory requirements discovered
   - Scope ambiguity that cannot be resolved from the sprint brief
   - The next step requires changes outside the stated file/scope boundaries
   Do NOT pause simply because the sprint is partially complete, the diff was
   truncated, or you are uncertain whether unrelated code was affected. Partial
   completion with clear remaining work is a continue, not a pause.

When continuing, provide ONLY the next narrow implementation step — not a broad
plan. Never expand scope beyond the original objective. The loop has a hard cap
on iterations, so be efficient.

Respond with a JSON object with exactly these keys:
{
  "decision": "continue" | "pause_for_human" | "stop_success" | "stop_failure",
  "rationale": "1-2 sentence explanation",
  "next_prompt_for_claude": "next narrow step for Claude (null if stopping/pausing)",
  "risk_flags": ["short string", ...],
  "completion_assessment": "short assessment of overall progress toward the objective"
}
"""


@dataclass
class ReviewerDecision:
    decision: str  # continue | pause_for_human | stop_success | stop_failure
    rationale: str
    next_prompt_for_claude: str | None
    risk_flags: list[str]
    completion_assessment: str
    response_id: str = ""
    conversation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def build_reviewer_packet(
    objective: str,
    iteration_number: int,
    max_iterations: int,
    claude_output: str,
    git_diff: str,
    previous_summaries: list[dict],
    current_step: str = "",
    abnormal_execution: dict | None = None,
    *,
    json_mode: bool = False,
) -> str:
    """Build the user message for the reviewer.

    Args:
        json_mode: When True, appends a reminder that the response must be a
            json object.  Required by the OpenAI API when using
            ``text={"format": {"type": "json_object"}}`` — the input message
            must contain the word 'json' or the API raises an error.
    """
    parts = []

    # Surface abnormal execution prominently at the top
    if abnormal_execution:
        step_type = abnormal_execution.get("step_type", "implementation")
        warning_lines = ["\n## ⚠ ABNORMAL EXECUTION WARNING"]
        if abnormal_execution.get("timed_out"):
            warning_lines.append(
                f"Claude TIMED OUT after {abnormal_execution.get('timeout_seconds', '?')} seconds."
            )
        elif abnormal_execution.get("exit_code", 0) != 0:
            warning_lines.append(
                f"Claude exited with NON-ZERO exit code {abnormal_execution['exit_code']}."
            )
        warning_lines.append(
            f"Output is likely INCOMPLETE or PARTIAL."
        )
        warning_lines.append(
            f"Step type: {step_type}"
        )
        has_diff = abnormal_execution.get("has_meaningful_diff", False)
        warning_lines.append(
            f"Meaningful code changes: {'YES — review diff carefully' if has_diff else 'NONE'}"
        )
        if abnormal_execution.get("was_retried"):
            warning_lines.append(
                "This was already retried once with the same step and still failed."
            )

        # Give step-type-aware guidance
        if step_type in ("validation", "packaging"):
            warning_lines.append(
                "\nIMPORTANT: This was a VALIDATION/PACKAGING step, not an implementation step."
            )
            warning_lines.append(
                "If prior iterations successfully implemented and tested the feature, "
                "this failure likely does NOT mean the sprint failed."
            )
            warning_lines.append(
                "Review prior iteration assessments. If implementation and tests are "
                "complete, prefer stop_success over pause_for_human. Only pause if "
                "you genuinely cannot determine whether the sprint objective was met."
            )
        else:
            warning_lines.append(
                "Consider whether this step should be simplified, "
                "or whether this is a stop_failure / pause_for_human situation."
            )
        parts.append("\n".join(warning_lines))

    parts.append(f"\n## Sprint objective\n{objective}")
    parts.append(f"\n## Iteration {iteration_number} of {max_iterations}")

    if current_step:
        parts.append(f"\n## This iteration's assigned step\n{current_step}")

    if previous_summaries:
        summary_text = "\n".join(
            f"- Iteration {s['iteration']}: {s.get('reviewer_decision', {}).get('rationale', 'n/a')}"
            for s in previous_summaries
        )
        parts.append(f"\n## Previous iterations\n{summary_text}")

    # Truncate Claude output to keep reviewer context manageable
    truncated = claude_output[:8000]
    if len(claude_output) > 8000:
        truncated += "\n... [truncated]"
    parts.append(f"\n## Claude output\n{truncated}")

    # Truncate diff similarly
    diff_truncated = git_diff[:4000]
    if len(git_diff) > 4000:
        diff_truncated += "\n... [truncated]"
    parts.append(f"\n## Git diff\n{diff_truncated}")

    if json_mode:
        parts.append(
            "\nRespond with a json object using exactly the keys specified above."
        )

    return "\n".join(parts)


def _extract_json_text(response) -> str:
    """Extract the text content from a Responses API response."""
    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if content.type == "output_text":
                    return content.text
    return "{}"


def call_reviewer(
    api_key: str,
    model: str,
    reviewer_packet: str,
    previous_response_id: str | None = None,
) -> ReviewerDecision:
    """Call OpenAI to review the iteration and return a structured decision.

    Uses the Responses API with previous_response_id for sprint continuity.
    """
    client = OpenAI(api_key=api_key)

    response = client.responses.create(
        model=model,
        instructions=REVIEWER_SYSTEM_PROMPT,
        input=reviewer_packet,
        previous_response_id=previous_response_id,
        text={"format": {"type": "json_object"}},
        temperature=0.2,
        store=True,
    )

    raw = _extract_json_text(response)
    data = json.loads(raw)

    return ReviewerDecision(
        decision=data.get("decision", "pause_for_human"),
        rationale=data.get("rationale", ""),
        next_prompt_for_claude=data.get("next_prompt_for_claude"),
        risk_flags=data.get("risk_flags", []),
        completion_assessment=data.get("completion_assessment", ""),
        response_id=response.id,
        conversation_id=response.conversation.id if response.conversation else "",
    )


@dataclass
class PlannerResult:
    iteration_1_prompt: str
    rationale: str
    expected_remaining_steps: str
    response_id: str = ""
    conversation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def call_initial_planner(
    api_key: str,
    model: str,
    objective: str,
    repo_ctx: str,
    max_iterations: int,
) -> PlannerResult:
    """Ask OpenAI to decompose the sprint brief into a bounded first step.

    This is the first call in the sprint's response chain. Its response_id
    is used as the root for subsequent reviewer calls via previous_response_id.
    """
    client = OpenAI(api_key=api_key)

    instructions = INITIAL_PLANNER_SYSTEM_PROMPT.format(max_iterations=max_iterations)
    user_msg = (
        f"## Sprint brief\n{objective}\n\n## Repository context\n{repo_ctx}"
        f"\n\nRespond with a json object using exactly the keys specified above."
    )

    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=user_msg,
        text={"format": {"type": "json_object"}},
        temperature=0.2,
        store=True,
    )

    raw = _extract_json_text(response)
    data = json.loads(raw)

    return PlannerResult(
        iteration_1_prompt=data.get("iteration_1_prompt", objective),
        rationale=data.get("rationale", ""),
        expected_remaining_steps=data.get("expected_remaining_steps", ""),
        response_id=response.id,
        conversation_id=response.conversation.id if response.conversation else "",
    )
