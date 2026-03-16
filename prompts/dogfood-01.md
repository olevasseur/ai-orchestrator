Improve the review/output wording for validation results so environment or dependency problems are clearer.

Goals:
- Make missing-tool or dependency failures easier to distinguish from implementation failures in terminal output.
- Keep the change small and focused on wording/display only.
- Do not change the validation classification logic unless absolutely necessary for this wording improvement.
- Keep planner/executor flow unchanged.

Constraints:
- Minimal diff
- No unrelated refactors
- Update tests only if needed

Before coding:
1. summarize the exact files you plan to touch
2. explain the smallest safe implementation plan
3. give me the validation commands I should run locally after the change

After coding:
1. summarize what changed
2. repeat the validation commands
3. mention any caveats
