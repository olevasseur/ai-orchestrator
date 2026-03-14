"""
OpenAI planner: takes task + repo context + previous iteration results and
returns a structured plan for the next implementation increment.

The planner always returns a JSON object with these keys:
  - objective        : concise summary of what this iteration should achieve
  - proposed_prompt  : the full Claude Code implementation prompt
  - validation_commands : list of shell commands to verify the change
  - risks            : assumptions, risks, things to watch out for
  - next_step_framing: how to frame the *following* iteration (lookahead)
  - done             : bool — planner thinks the task is complete

Extension point: swap OpenAI for any LLM by implementing the same interface.
"""

from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

SYSTEM_PROMPT = """You are a senior software architect acting as a planning agent
in a human-in-the-loop coding orchestrator. Your job is to break a large task
into small, safe, reviewable increments and produce a precise implementation
prompt for Claude Code to execute.

Always respond with a single JSON object — no markdown fences, no prose outside
the JSON — with exactly these keys:
{
  "objective": "...",
  "proposed_prompt": "...",
  "validation_commands": ["...", "..."],
  "risks": "...",
  "next_step_framing": "...",
  "done": false
}

Rules:
- Keep each iteration small and independently verifiable.
- The proposed_prompt must be self-contained: include all necessary context.
- validation_commands should be runnable shell commands (pytest, etc.).
- Set done=true only when the whole task is complete and verified.
"""


class OpenAIPlanner:
    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def plan(
        self,
        task: str,
        repo_context: str,
        previous_iterations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Call the planner and return parsed JSON."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        # Build user message
        user_parts = [f"## Task\n{task}", f"## Repo context\n{repo_context}"]
        if previous_iterations:
            itr_text = json.dumps(previous_iterations, indent=2)
            user_parts.append(f"## Previous iterations\n{itr_text}")

        messages.append({"role": "user", "content": "\n\n".join(user_parts)})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
        )

        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)

        # Normalise
        data.setdefault("objective", "")
        data.setdefault("proposed_prompt", "")
        data.setdefault("validation_commands", [])
        data.setdefault("risks", "")
        data.setdefault("next_step_framing", "")
        data.setdefault("done", False)

        return data

    def ask(self, question: str, context: str) -> str:
        """Ad-hoc follow-up question to the planner (during review step)."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"## Context\n{context}\n\n## Question\n{question}",
            },
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.3,
        )
        return response.choices[0].message.content or ""
