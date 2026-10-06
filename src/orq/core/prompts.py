"""Prompt builders for both sides of the loop (SPEC sections 7, 8.2, 8.3, 12)."""

from __future__ import annotations

from orq.core.task import Task
from orq.verify.checks import CheckResult

IMPLEMENTER_RULES = """You are the implementer in an automated loop. A separate reviewer reads your work after every turn.
Standing rules:
- Work only inside the current directory (the git worktree). Do not commit, push, create branches or touch git history; the orchestrator commits for you.
- Do not remove features, files, tests or behaviour that the task does not ask you to remove.
- Keep the check command passing. Add or update tests for what you change.
- Never add secrets, tokens or credentials to the repo.
- A tool call may be denied by the orq guard. Follow the denial text exactly: never work around a denied action with another command or tool.
- Finish your turn with a short plain-text report: what you changed, what is left, anything the reviewer should look at.
- On a business decision or real ambiguity, do not guess and do not edit files. Stop and end your final message with a fenced block tagged orq-decision containing one JSON object with keys decision_type ("business" | "ambiguity" | "risk" | "blocked"), question, options (array of strings) and recommendation (index into options). Nothing may follow the block."""

REVIEWER_RULES = """You are the reviewer in an automated implementer/reviewer loop. You have read-only access to the repository in the current directory; inspect it directly.
Standing rules:
- Any removal of a feature, file, test or behaviour not required by the task -> status needs_human with decision_type risk.
- Never guess business rules. If the task is ambiguous, ask: status needs_human with decision_type business or ambiguity.
- status done only when every acceptance criterion is met, the check command passes and there is no blocker or major issue left. If you list a blocker or major issue, status must be continue with a next_prompt that fixes it.
- Generated or build artifacts (caches, compiled files, editor files) committed to the repo are a major issue.
- Otherwise status continue, with next_prompt: concrete, self-contained instructions for the implementer's next turn. Mention file names.
- Keep summary to a few sentences. List real problems in issues with a severity.
- human must be null unless status is needs_human."""


def _task_block(task: Task) -> str:
    criteria = "\n".join(f"- [ ] {c}" for c in task.acceptance_criteria)
    out = "\n".join(f"- {c}" for c in task.out_of_scope) or "- (none)"
    constraints = "\n".join(f"- {c}" for c in task.constraints) or "- (none)"
    return (
        f"# Task: {task.title}\n\n## Goal\n{task.goal}\n\n## Acceptance criteria\n{criteria}\n\n"
        f"## Out of scope\n{out}\n\n## Constraints\n{constraints}\n\n## Check command\n`{task.check_command}`"
    )


def build_implementer_prompt(task: Task, *, iteration: int, milestone: str | None, next_prompt: str | None,
                             decisions: str, previous_check: CheckResult | None) -> str:
    parts = [_task_block(task), f"\n## Iteration {iteration}"]
    if milestone:
        parts.append(f"Current milestone: {milestone}")
    if decisions.strip():
        parts.append("## Owner decisions so far\n" + decisions.strip())
    if previous_check is not None and not previous_check.ok:
        parts.append("## Last check run failed\n```\n" + previous_check.output.strip()[-4000:] + "\n```")
    if next_prompt:
        parts.append("## Reviewer instructions for this turn\n" + next_prompt.strip())
    elif iteration == 1:
        parts.append("## Instructions\nStart implementing the task. Read the repository first.")
    return "\n\n".join(parts) + "\n"


def build_reviewer_prompt(task: Task, *, iteration: int, milestone: str | None, diff_stat: str, diff_path: str,
                          diff_excerpt: str | None, check: CheckResult, implementer_report: str, decisions: str) -> str:
    check_block = (
        f"exit code: {check.exit_code}, ok: {check.ok}, timed out: {check.timed_out}\n```\n{check.output.strip()[-4000:]}\n```"
    )
    parts = [_task_block(task), f"\n## Iteration {iteration}"]
    if milestone:
        parts.append(f"Current milestone: {milestone}")
    if decisions.strip():
        parts.append("## Owner decisions so far\n" + decisions.strip())
    parts.append(f"## Diff of this iteration (stat)\n```\n{diff_stat.strip() or '(no changes)'}\n```\nFull patch: {diff_path}")
    if diff_excerpt:
        parts.append("## Diff of this iteration (patch)\n```diff\n" + diff_excerpt + "\n```")
    parts.append("## Check command result\n" + check_block)
    parts.append("## Implementer's report\n" + (implementer_report.strip() or "(empty)"))
    parts.append("## Your job\nReview the repository state against the task. Answer in the required JSON shape.")
    return "\n\n".join(parts) + "\n"
