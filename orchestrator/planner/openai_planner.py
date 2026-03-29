"""
OpenAI planner: takes task + repo context + memory + recent iterations and
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

You will receive:
- The original task
- Stable project memory (architecture facts, constraints)
- Rolling working memory (recent progress, open questions, decisions)
- The last few iteration summaries (recent history only)
- Current repo context

Use the memory to maintain continuity across iterations without being given
the full history. Trust working memory as an accurate summary of prior work.

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
- The proposed_prompt must NOT ask the executor to run, report on, or summarise
  validation. Validation is performed externally by the orchestrator using the
  validation_commands list. Asking the executor to validate produces prose that
  is not grounded in observed command output and cannot be trusted.
- validation_commands must each be a single, simple, self-contained shell command
  that directly proves one condition: e.g. `grep -q 'string' file.py`,
  `pytest -q tests/test_foo.py`, `test -f path/to/file`, `python -c "import mod"`.
  Do NOT use compound commands, background processes, curl to local servers, sleep,
  or anything that requires a service to be already running. Prefer grep/awk on
  files over runtime HTTP checks.
- Set done=true only when the whole task is complete and verified.
- If a previous validation showed missing_tool, address environment setup first.
"""

_ASK_SYSTEM = """You are an assistant helping a developer understand the current state of a software project managed by an AI coding orchestrator.

You will be given context about the current task, recent iteration history, working memory, and/or a proposed plan. Answer the user's question directly in clear prose.

Rules:
- Start with a direct answer to the question — no preamble.
- Ground your answer in the context provided; do not speculate beyond it.
- Write for a developer who wants to understand what is happening and why.
- If helpful, use short labelled sections (e.g. "Current state:", "Blocker:", "Relevant files:", "Suggested next step:") but only when they add clarity.
- Do NOT produce a JSON planning object.
- Do NOT generate implementation prompts, validation command lists, or plan fields unless the user explicitly asks for a plan.
- Keep the answer concise. Prefer a few clear sentences over exhaustive lists.
"""

_COMPRESS_SYSTEM = """You are compressing an orchestrator's working memory log.

You will receive the current working_memory.md and project_memory.md.
project_memory.md is stable reference material — do not modify it.
Your job is to compress working_memory.md only.

Rules for the compressed working memory:
- Max 1500 characters.
- Keep: open questions, key decisions, latest progress, what matters next.
- Drop: resolved items, redundant entries, superseded assumptions.
- Do not copy content that is already covered in project_memory.md.

Respond with a JSON object with exactly one key:
  "working_memory": "... full markdown content ..."
"""


class OpenAIPlanner:
    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def plan(
        self,
        task: str,
        repo_context: str,
        recent_iterations: list[dict[str, Any]],
        *,
        project_memory: str = "",
        working_memory: str = "",
    ) -> dict[str, Any]:
        """Call the planner and return parsed JSON."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        user_parts = [f"## Task\n{task}"]
        if project_memory.strip():
            user_parts.append(f"## Project memory\n{project_memory}")
        if working_memory.strip():
            user_parts.append(f"## Working memory\n{working_memory}")
        user_parts.append(f"## Repo context\n{repo_context}")
        if recent_iterations:
            itr_text = json.dumps(recent_iterations, indent=2)
            user_parts.append(f"## Recent iterations (last {len(recent_iterations)})\n{itr_text}")

        messages.append({"role": "user", "content": "\n\n".join(user_parts)})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
        )

        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)

        data.setdefault("objective", "")
        data.setdefault("proposed_prompt", "")
        data.setdefault("validation_commands", [])
        data.setdefault("risks", "")
        data.setdefault("next_step_framing", "")
        data.setdefault("done", False)

        return data

    def ask(self, question: str, context: str) -> str:
        """Ad-hoc follow-up question answered in natural language (not a plan)."""
        messages = [
            {"role": "system", "content": _ASK_SYSTEM},
            {"role": "user", "content": f"## Context\n{context}\n\n## Question\n{question}"},
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.3,
        )
        return response.choices[0].message.content or ""

    def compress_memory(
        self, working_memory: str, project_memory: str
    ) -> str:
        """
        Compress working_memory into a fresh concise version.
        project_memory is passed as read-only context so the compressor knows
        what stable facts are already captured there.
        Returns new_working_memory only. project_memory is never modified.
        Called only on memory refresh — not in the per-iteration hot path.
        """
        messages = [
            {"role": "system", "content": _COMPRESS_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"## Working Memory\n{working_memory}\n\n"
                    f"## Project Memory (context only — do not modify)\n{project_memory}"
                ),
            },
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        data = json.loads(response.choices[0].message.content or "{}")
        return data.get("working_memory", working_memory)
