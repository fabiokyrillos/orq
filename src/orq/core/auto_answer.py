"""Automatic answers (Phase 6.1): when the owner allows it, a low-stakes decision left unanswered is answered with the
recommended option. Off by default; the owner sets the timeout and the highest stakes it may take.

Never automatic: destructive actions, business rules, the guard, plan approvals, a missing recommendation or reason,
and a recommendation to abort.
"""

from __future__ import annotations

from orq.core.models import Decision

STAKES = ("low", "medium", "high")


def check(decision: Decision, *, enabled: bool, minutes: float, max_stakes: str, started_at: float, now: float) -> tuple[bool, str]:
    """(eligible, reason). `started_at` is when the owner was told (WhatsApp send time, else creation)."""
    if not enabled:
        return False, "automatic answers are off"
    if decision.status != "pending":
        return False, "already answered"
    if decision.destructive:
        return False, "destructive"
    if decision.decision_type == "business":
        return False, "business decision"
    if decision.source == "guard":
        return False, "guard decision"
    if decision.source == "planner" and decision.options[:2] == ["approve", "revise"]:
        return False, "plan approval"
    rec = decision.recommendation
    if rec is None or not 0 <= rec < len(decision.options):
        return False, "no recommendation"
    if not decision.recommendation_reason.strip():
        return False, "no reason for the recommendation"
    if decision.options[rec].strip().lower() == "abort":
        return False, "the recommendation is to abort"
    if decision.stakes not in STAKES:
        return False, "stakes unknown"
    if STAKES.index(decision.stakes) > STAKES.index(max_stakes if max_stakes in STAKES else "low"):
        return False, f"stakes {decision.stakes} above {max_stakes}"
    waited = (now - started_at) / 60
    if waited < minutes:
        return False, f"waiting: {int(waited)} of {minutes:g} min"
    return True, f"{decision.stakes} stakes, {int(waited)} min without an answer"
