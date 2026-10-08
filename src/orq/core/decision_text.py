"""Context, consequences and recommendation reasons for the decisions orq raises itself (Phase 6).

Each builder returns the keyword arguments `Decision` takes for these fields. Pure functions over the evidence the
loop already has; the owner reads the result on a phone, so it says what happened, why it matters, and what each
answer will do.
"""

from __future__ import annotations

GUARD_RULES = {
    "recursive_delete": "deletes files or folders recursively; a wrong path loses work",
    "git_reset_hard": "throws away uncommitted changes in the worktree",
    "git_force_push": "rewrites the history of the remote branch",
    "git_branch_delete": "deletes a git branch",
    "git_clean": "deletes untracked files in the worktree",
    "dependency_removal": "removes a dependency the project may still use",
    "sql_destructive": "drops or truncates database objects",
    "write_outside_worktree": "writes outside the run's worktree, elsewhere on your PC",
    "protected_path": "changes a path you marked as protected",
}
DIFF_RULES = {
    "deleted_file": "a file was deleted",
    "removed_test": "a test was removed",
    "removed_export": "an exported function or class disappeared",
    "removed_route": "an HTTP route disappeared",
    "protected_path": "a protected path changed",
    "dependency_removed": "a dependency was removed from a manifest",
    "negative_balance": "much more source was deleted than added",
}


def _milestone(milestone: dict | None) -> str:
    return f"While working on milestone \"{milestone['title']}\", " if milestone and milestone.get("title") else ""


def from_model(human: dict) -> dict:
    """The fields a reviewer, planner or implementer filled; tolerant of the shape before Phase 6."""
    details = human.get("option_details") or []
    stakes = str(human.get("stakes") or "").strip().lower()
    return {"context": str(human.get("context") or "").strip(),
            "option_details": [str(d) for d in details] if isinstance(details, list) else [],
            "recommendation_reason": str(human.get("recommendation_reason") or "").strip(),
            "stakes": stakes if stakes in ("low", "medium", "high") else ""}


def plan_approval(plan: dict) -> dict:
    lines = [f"{i + 1}. {m['title']} [{m['difficulty']}]: {m['goal']} Done when: {m['done_when']}"
             for i, m in enumerate(plan.get("milestones") or [])]
    summary = str(plan.get("summary") or "").strip()
    return {"context": (summary + "\n" if summary else "") + "\n".join(lines),
            "option_details": ["the implementer starts on milestone 1 right away",
                               "the planner plans again; answer with free text saying what to change"],
            "recommendation_reason": "the plan stays inside the task's scope; approve unless a milestone is missing or not wanted", "stakes": "high"}


def guard_pre(description: str, rule: str | None, milestone: dict | None) -> dict:
    why = GUARD_RULES.get(rule or "", "is on the guard's list of destructive actions")
    text = f"{_milestone(milestone)}the implementer tried to run `{description}`. The guard blocked it because it {why}."
    return {"context": text[0].upper() + text[1:],
            "option_details": ["this exact action runs once on the implementer's next turn; anything else stays blocked",
                               "the implementer is told to finish the work without it"],
            "recommendation_reason": "orq never approves a destructive action blind; approve only if the task needs exactly this", "stakes": "high"}


def guard_diff(violations: list, numstat: list[tuple[int, int, str]], report: str = "") -> dict:
    counts = {path: (added, deleted) for added, deleted, path in numstat}
    lines = []
    for v in violations:
        added, deleted = counts.get(v.path, (0, 0))
        lines.append(f"- {v.path}: {DIFF_RULES.get(v.rule, v.rule)} [{v.rule}] ({v.detail}; +{added}/-{deleted} lines)")
    said = " ".join(report.split())[:400]
    return {"context": "This iteration's changes tripped the diff guard:\n" + "\n".join(lines) +
                       (f"\nThe implementer's report: {said}" if said else ""),
            "option_details": ["the changes stay; the check runs and the iteration is committed",
                               "this turn's changes are thrown away (reset to the last iteration) and the implementer continues without them"],
            "recommendation_reason": "removals the task did not ask for are usually mistakes; approve if the task requires them", "stakes": "high"}


def secret(findings: list[dict]) -> dict:
    lines = [f"- {f.get('RuleID')} in {f.get('File')} line {f.get('StartLine')}" for f in findings]
    return {"context": "gitleaks found what looks like secrets in the staged changes (values hidden):\n" + "\n".join(lines),
            "option_details": ["after you remove the secret in the worktree, orq scans again and continues",
                               "the run stops (ABORTED); nothing is committed or pushed"],
            "recommendation_reason": "the repos are public; a pushed secret must be rotated, so nothing is committed until the scan is clean", "stakes": "high"}


def no_progress(rule: str, detail: str, target: str, summaries: list[str]) -> dict:
    recent = "\n".join(f"- {s}" for s in summaries[-3:] if s)
    return {"context": f"The run is not making progress ({rule}: {detail})." + (f" Last reviews:\n{recent}" if recent else ""),
            "option_details": ["keep going as is; the stall counter starts over",
                               f"{target}: later work is discarded and both agents are told what was dropped",
                               "the run stops (ABORTED)"],
            "recommendation_reason": "going back to the last good state usually breaks a loop faster than another attempt", "stakes": "medium"}


def agent_error(role: str, kind: str, error: str) -> dict:
    return {"context": f"The {role} call failed ({kind}). Error: {error.strip()[:400]}",
            "option_details": ["the same step runs again", "the run stops (ABORTED)"],
            "recommendation_reason": "most failures are transient (network, CLI restart); retry once before giving up", "stakes": "low"}


def ci_none(pr_number: int | None) -> dict:
    return {"context": f"PR #{pr_number} has no GitHub checks after the grace period, so CI cannot confirm the change.",
            "option_details": ["the final review runs and the PR merges without CI", "the run stops (ABORTED); the PR stays open"],
            "recommendation_reason": "without CI only the local check backs the merge; abort if the repo should have CI", "stakes": "high"}


def ci_timeout(pr_number: int | None, checks: list[dict]) -> dict:
    states = ", ".join(f"{c.get('name')}: {c.get('bucket')}" for c in checks) or "none reported"
    return {"context": f"GitHub checks on PR #{pr_number} did not finish in time ({states}).",
            "option_details": ["orq waits for CI again", "the run stops (ABORTED); the PR stays open"],
            "recommendation_reason": "free runners are sometimes slow; waiting once more is cheap", "stakes": "low"}


def rebase_conflict(files: list[str], reason: str) -> dict:
    return {"context": f"The base branch moved and rebasing hit conflicts in {', '.join(files) or 'some files'}. "
                       f"The implementer tried to resolve them and orq undid the attempt: {reason.strip()[:400]}",
            "option_details": ["after you resolve the conflict by hand in the worktree, the gate starts again (rebase, CI, review)",
                               "the run stops (ABORTED); the PR stays open"],
            "recommendation_reason": "a conflict the implementer cannot resolve usually needs a choice between two changes; only you can make it", "stakes": "high"}


def gate_rounds(rounds: int, limit: int, reason: str) -> dict:
    return {"context": f"The merge gate failed {rounds} times (limit {limit}). Last reason: {reason.strip()[:400]}",
            "option_details": ["the implementer gets another round with the last failure; the counter starts over",
                               "the run stops (ABORTED); the PR stays open"],
            "recommendation_reason": "repeated gate failures usually mean the task needs your input; abort unless the last reason looks easy", "stakes": "high"}
