"""
DirectPlanner: bypass the AI planner and use the submitted text directly as
the executor prompt. Produces a minimal compatible plan dict so the rest of
the review/run pipeline continues unchanged.
"""

from __future__ import annotations


class DirectPlanner:
    """Thin compatibility shim that wraps raw user text as a plan object.

    Implements the same interface as OpenAIPlanner so the runner, review UI,
    and storage layer require no changes.
    """

    def plan(
        self,
        task: str,
        repo_context: str,
        recent_iterations: list,
        *,
        project_memory: str = "",
        working_memory: str = "",
    ) -> dict:
        return {
            "objective": task,
            "proposed_prompt": task,
            "validation_commands": [],
            "risks": "",
            "next_step_framing": "",
            "done": False,
        }

    def ask(self, question: str, context: str) -> str:
        return (
            "[Planner bypassed — no AI assistant available for questions "
            "in direct-prompt mode.]"
        )
