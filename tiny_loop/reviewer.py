"""OpenAI reviewer: evaluates Claude's iteration output and decides next step."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
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
) -> str:
    """Build the user message for the reviewer."""
    parts = []

    # Surface abnormal execution prominently at the top
    if abnormal_execution:
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
        has_diff = abnormal_execution.get("has_meaningful_diff", False)
        warning_lines.append(
            f"Meaningful code changes: {'YES — review diff carefully' if has_diff else 'NONE'}"
        )
        if abnormal_execution.get("was_retried"):
            warning_lines.append(
                "This was already retried once with the same step and still failed."
            )
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

    return "\n".join(parts)


def call_reviewer(
    api_key: str,
    model: str,
    reviewer_packet: str,
) -> ReviewerDecision:
    """Call OpenAI to review the iteration and return a structured decision."""
    client = OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
            {"role": "user", "content": reviewer_packet},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)

    return ReviewerDecision(
        decision=data.get("decision", "pause_for_human"),
        rationale=data.get("rationale", ""),
        next_prompt_for_claude=data.get("next_prompt_for_claude"),
        risk_flags=data.get("risk_flags", []),
        completion_assessment=data.get("completion_assessment", ""),
    )


@dataclass
class PlannerResult:
    iteration_1_prompt: str
    rationale: str
    expected_remaining_steps: str

    def to_dict(self) -> dict:
        return asdict(self)


def call_initial_planner(
    api_key: str,
    model: str,
    objective: str,
    repo_ctx: str,
    max_iterations: int,
) -> PlannerResult:
    """Ask OpenAI to decompose the sprint brief into a bounded first step."""
    client = OpenAI(api_key=api_key)

    system = INITIAL_PLANNER_SYSTEM_PROMPT.format(max_iterations=max_iterations)
    user_msg = f"## Sprint brief\n{objective}\n\n## Repository context\n{repo_ctx}"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)

    return PlannerResult(
        iteration_1_prompt=data.get("iteration_1_prompt", objective),
        rationale=data.get("rationale", ""),
        expected_remaining_steps=data.get("expected_remaining_steps", ""),
    )
