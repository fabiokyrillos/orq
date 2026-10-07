"""Prompt builders for both sides of the loop (SPEC sections 7, 8.2, 8.3, 12)."""

from __future__ import annotations

from orq.core.task import Task
from orq.verify.checks import CheckResult

# Phase 6: the owner answers from a phone, without the repository at hand; a bare question cannot be decided.
HUMAN_GUIDE = """How to ask the owner (fill every field; the owner reads it on a phone and cannot open the repository):
- context: 2 to 4 sentences. What the run was doing (milestone, file, behaviour), what you found, quoting the evidence (file and function, error text, the acceptance criterion in question), and what stays blocked until the answer.
- question: one sentence that the owner can answer by picking an option.
- options: 2 to 4 short labels.
- option_details: one sentence per option, in the same order: what will happen in the code and for the user of the software if the owner picks it.
- recommendation: index of the option you recommend; recommendation_reason: one sentence explaining why.
- Plain language. Explain any repository-specific name in a few words."""

IMPLEMENTER_RULES = """You are the implementer in an automated loop. A separate reviewer reads your work after every turn.
Standing rules:
- Work only inside the current directory (the git worktree). Do not commit, push, create branches or touch git history; the orchestrator commits for you.
- Do not remove features, files, tests or behaviour that the task does not ask you to remove.
- Keep the check command passing. Add or update tests for what you change.
- Never add secrets, tokens or credentials to the repo.
- A tool call may be denied by the orq guard. Follow the denial text exactly: never work around a denied action with another command or tool.
- Finish your turn with a short plain-text report: what you changed, what is left, anything the reviewer should look at.
- On a business decision or real ambiguity, do not guess and do not edit files. Stop and end your final message with a fenced block tagged orq-decision containing one JSON object with keys decision_type ("business" | "ambiguity" | "risk" | "blocked"), context, question, options (array of strings), option_details (array of strings, one per option), recommendation (index into options) and recommendation_reason. Nothing may follow the block.
""" + HUMAN_GUIDE

REVIEWER_RULES = """You are the reviewer in an automated implementer/reviewer loop. You have read-only access to the repository in the current directory; inspect it directly.
Standing rules:
- Any removal of a feature, file, test or behaviour not required by the task -> status needs_human with decision_type risk.
- Never guess business rules. If the task is ambiguous, ask: status needs_human with decision_type business or ambiguity.
- The task is split into milestones (see the plan). status done means the CURRENT milestone's "done when" holds, the check command passes and there is no blocker or major issue left. On the last milestone, done also requires every acceptance criterion of the task. If you list a blocker or major issue, status must be continue with a next_prompt that fixes it.
- Generated or build artifacts (caches, compiled files, editor files) committed to the repo are a major issue.
- The orchestrator commits, pushes, opens the pull request, waits for CI and merges. Never ask the implementer to do any of that, and never count a missing PR or merge as an issue.
- The check command result below was produced by the orchestrator in the real environment and is authoritative. Do not re-run it, and never report tool availability in your sandbox as an issue.
- Otherwise status continue, with next_prompt: concrete, self-contained instructions for the implementer's next turn. Mention file names.
- Keep summary to a few sentences. List real problems in issues with a severity.
- human must be null unless status is needs_human.
""" + HUMAN_GUIDE


PLANNER_RULES = """You are the planner for an automated implementer/reviewer loop. You have read-only access to the repository in the current directory; read it before planning.
Standing rules:
- Split the task into 1 to 8 ordered milestones. Each milestone is one coherent change a single implementer turn can finish and a reviewer can verify from the repository. Give each a concrete done_when.
- Milestones are code, test and documentation changes only. The orchestrator runs the check command after every turn, commits, pushes, opens the pull request, waits for CI and merges. Never plan a milestone for verification, committing, pushing, PR creation or merging.
- Tag difficulty: "hard" for design, debugging or anything touching behaviour that is easy to get wrong; "mechanical" for boilerplate, tests that mirror existing ones, docs, renames.
- Stay inside the task's scope and constraints. Never plan removals of features, files or tests the task does not ask for.
- On a business decision or real ambiguity that changes the plan, do not guess: status needs_human with the question and options. Otherwise status plan.
- human must be null unless status is needs_human; milestones must be empty when status is needs_human.
""" + HUMAN_GUIDE

FINAL_REVIEW_NOTE = ("## Final review before merge\nThis is the merge gate. All milestones are reported done. Answer done only when every "
                     "acceptance criterion of the task holds in the repository and the check and CI results below pass. "
                     "Otherwise status continue with a next_prompt that fixes what is missing.")

INTERRUPTED_NOTE = ("## Interrupted turn\nYour previous turn was interrupted before it finished. "
                    "The worktree holds your uncommitted work; continue from there.")


def guard_outcome_lines(approved: list[str], denied: list[str]) -> str:
    lines = [f"- The owner approved this action; run exactly: `{a}`" for a in approved]
    lines += [f"- The owner denied this action: `{d}`. Proceed without it." for d in denied]
    return "## Guard decisions\n" + "\n".join(lines) if lines else ""


def plan_block(plan: dict | None, index: int) -> str:
    """The plan and the current milestone, shared by both prompts. Empty when there is no plan."""
    if not plan or not plan.get("milestones"):
        return ""
    milestones = plan["milestones"]
    lines = [f"{i + 1}. [{m.get('difficulty', '?')}] {m.get('title', '')}: {m.get('goal', '')} (done when: {m.get('done_when', '')})"
             for i, m in enumerate(milestones)]
    current = milestones[min(index, len(milestones) - 1)]
    return (f"## Plan ({len(milestones)} milestones)\n" + "\n".join(lines) +
            f"\n\n## Current milestone ({min(index, len(milestones) - 1) + 1}/{len(milestones)}): {current.get('title', '')}\n"
            f"Goal: {current.get('goal', '')}\nDone when: {current.get('done_when', '')}")


def plan_markdown(plan: dict) -> str:
    lines = [f"# Plan\n\n{plan.get('summary', '').strip()}\n"]
    for i, m in enumerate(plan.get("milestones", [])):
        lines.append(f"## {i + 1}. {m.get('title', '')} [{m.get('difficulty', '?')}]\n\n{m.get('goal', '')}\n\n**Done when:** {m.get('done_when', '')}\n")
    return "\n".join(lines)


def build_planner_prompt(task: Task, *, decisions: str, feedback: str | None) -> str:
    # The rules go first: until Phase 6 they were defined but never sent (the CLIs get no other system prompt).
    parts = [PLANNER_RULES, _task_block(task)]
    if decisions.strip():
        parts.append("## Owner decisions so far\n" + decisions.strip())
    if feedback:
        parts.append("## The owner asked for a revised plan\n" + feedback.strip())
    parts.append("## Your job\nRead the repository, then produce the plan in the required JSON shape.")
    return "\n\n".join(parts) + "\n"


def _task_block(task: Task) -> str:
    criteria = "\n".join(f"- [ ] {c}" for c in task.acceptance_criteria)
    out = "\n".join(f"- {c}" for c in task.out_of_scope) or "- (none)"
    constraints = "\n".join(f"- {c}" for c in task.constraints) or "- (none)"
    return (
        f"# Task: {task.title}\n\n## Goal\n{task.goal}\n\n## Acceptance criteria\n{criteria}\n\n"
        f"## Out of scope\n{out}\n\n## Constraints\n{constraints}\n\n## Check command\n`{task.check_command}`"
    )


def build_implementer_prompt(task: Task, *, iteration: int, milestone: str | None, next_prompt: str | None,
                             decisions: str, previous_check: CheckResult | None, discarded: str | None = None,
                             interrupted: bool = False, plan: dict | None = None, milestone_index: int = 0) -> str:
    parts = [_task_block(task)]
    block = plan_block(plan, milestone_index)
    if block:
        parts.append(block)
    parts.append(f"\n## Iteration {iteration}")
    if milestone and not block:
        parts.append(f"Current milestone: {milestone}")
    if decisions.strip():
        parts.append("## Owner decisions so far\n" + decisions.strip())
    if discarded:
        parts.append("## Discarded iterations\n" + discarded.strip())
    if interrupted:
        parts.append(INTERRUPTED_NOTE)
    if previous_check is not None and not previous_check.ok:
        parts.append("## Last check run failed\n```\n" + previous_check.output.strip()[-4000:] + "\n```")
    if next_prompt:
        parts.append("## Reviewer instructions for this turn\n" + next_prompt.strip())
    elif iteration == 1:
        parts.append("## Instructions\nStart implementing the task. Read the repository first.")
    return "\n\n".join(parts) + "\n"


def build_reviewer_prompt(task: Task, *, iteration: int, milestone: str | None, diff_stat: str, diff_path: str,
                          diff_excerpt: str | None, check: CheckResult, implementer_report: str, decisions: str,
                          discarded: str | None = None, plan: dict | None = None, milestone_index: int = 0,
                          final: bool = False, ci_result: str | None = None) -> str:
    check_block = (
        f"exit code: {check.exit_code}, ok: {check.ok}, timed out: {check.timed_out}\n```\n{check.output.strip()[-4000:]}\n```"
    )
    parts = [REVIEWER_RULES, _task_block(task)]
    block = plan_block(plan, milestone_index)
    if block:
        parts.append(block)
    if final:
        parts.append(FINAL_REVIEW_NOTE)
    parts.append(f"\n## Iteration {iteration}")
    if milestone and not block:
        parts.append(f"Current milestone: {milestone}")
    if decisions.strip():
        parts.append("## Owner decisions so far\n" + decisions.strip())
    if discarded:
        parts.append("## Discarded iterations\n" + discarded.strip())
    parts.append(f"## Diff of this iteration (stat)\n```\n{diff_stat.strip() or '(no changes)'}\n```\nFull patch: {diff_path}")
    if diff_excerpt:
        parts.append("## Diff of this iteration (patch)\n```diff\n" + diff_excerpt + "\n```")
    parts.append("## Check command result (run by the orchestrator in the real environment; authoritative, do not run it yourself)\n" + check_block)
    if ci_result:
        parts.append("## GitHub Actions result\n" + ci_result.strip())
    parts.append("## Implementer's report\n" + (implementer_report.strip() or "(empty)"))
    parts.append("## Your job\nReview the repository state against the task. Answer in the required JSON shape.")
    return "\n\n".join(parts) + "\n"
