"""Deterministic corrections to agent output seen in real runs (Phase 3).

* The planner tends to add milestones for work the orchestrator owns (run the check, commit, open or merge
  the PR). Those are dropped.
* The Codex reviewer runs in a read-only sandbox without python; it reports "python is not recognized" as a
  blocker even though the orchestrator's check result is in its prompt. Such complaints are detected so the
  review can be retried with a correction and, failing that, stripped.
"""

from __future__ import annotations

import re

_ORCHESTRATOR_MILESTONE = re.compile(
    r"\b(?:run|execute|rerun|re-run|invoke)\b[^.]{0,60}\b(?:check command|check|unittest|pytest|test suite|tests?|verification)\b"
    r"|\b(?:open|create|raise|submit|merge)\b[^.]{0,40}\b(?:pr|pull request)\b"
    r"|\bpush(?:ing)?\b[^.]{0,30}\b(?:branch|remote|origin)\b"
    r"|\bcommit(?:ting)?\b[^.]{0,30}\b(?:changes|files|branch|work)\b",
    re.IGNORECASE,
)
_SANDBOX_COMPLAINT = re.compile(
    r"\b(?:python|python3|py|pytest|node|npm|uv|pip|the check command|check command|command)\b[^.]{0,80}"
    r"(?:not (?:found|recognized|recognised|available|installed|on (?:the )?path)|cannot (?:be )?(?:find|found|run|execute)d?"
    r"|could not (?:be )?(?:run|execute|confirm|verify|find)|unable to (?:run|execute|confirm|verify)|is unavailable|missing from (?:the )?path)"
    r"|\b(?:cannot|could not|unable to|can't)\b[^.]{0,40}\b(?:run|execute|confirm|verify)\b[^.]{0,40}\b(?:check|tests?|command)\b",
    re.IGNORECASE,
)


def drop_orchestrator_milestones(milestones: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split milestones into (kept, dropped); dropped ones describe verification, commits, pushes or PRs."""
    kept, dropped = [], []
    for milestone in milestones:
        text = f"{milestone.get('title', '')}. {milestone.get('goal', '')}"
        (dropped if _ORCHESTRATOR_MILESTONE.search(text) else kept).append(milestone)
    if not kept:  # never leave the run without a milestone; the planner's judgement beats the heuristic then
        return milestones, []
    return kept, dropped


def is_sandbox_complaint(text: str) -> bool:
    return bool(_SANDBOX_COMPLAINT.search(text or ""))


def review_complains_about_sandbox(review: dict) -> bool:
    texts = [str(i.get("description", "")) for i in review.get("issues", []) if isinstance(i, dict)]
    human = review.get("human") or {}
    if isinstance(human, dict):
        texts.append(str(human.get("question", "")))
    return any(is_sandbox_complaint(t) for t in texts)


def strip_sandbox_complaints(review: dict) -> tuple[dict, bool]:
    """Remove sandbox-tooling complaints from a review. A needs_human about them becomes continue."""
    issues = [i for i in review.get("issues", []) if isinstance(i, dict)]
    kept = [i for i in issues if not is_sandbox_complaint(str(i.get("description", "")))]
    changed = len(kept) != len(issues)
    out = {**review, "issues": kept}
    human = review.get("human") or {}
    if review.get("status") == "needs_human" and isinstance(human, dict) and is_sandbox_complaint(str(human.get("question", ""))):
        out["status"] = "continue"
        out["human"] = None
        out["next_prompt"] = ("Continue with the current milestone. The orchestrator ran the check command in the real environment "
                              "and it passed; the reviewer's sandbox lacks the tools, which is not an issue.")
        changed = True
    return out, changed
