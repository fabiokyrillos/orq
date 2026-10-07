"""One way to record an owner's answer, shared by the terminal, `orq answer`, the dashboard and WhatsApp (SPEC 9.2)."""

from __future__ import annotations

from orq.core.models import Decision
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir


class AnswerError(ValueError):
    pass


def resolve_answer(decision: Decision, text: str) -> str:
    """An option index becomes the option text; anything else is kept verbatim."""
    value = text.strip()
    if value.isdigit() and decision.options and 0 <= int(value) < len(decision.options):
        return decision.options[int(value)]
    return value


def record_answer(store: Store, paths: OrqPaths, decision_id: str, text: str, *, via: str) -> Decision:
    """Store the answer, append it to the run's DECISIONS.md and log the event. Raises AnswerError when it cannot."""
    decision = store.get_decision(decision_id)
    if decision is None:
        raise AnswerError(f"no decision {decision_id}")
    if decision.status != "pending":
        raise AnswerError(f"{decision_id} already answered: {decision.answer}")
    value = resolve_answer(decision, text)
    if not value:
        raise AnswerError("empty answer")
    store.answer_decision(decision_id, answer=value, answered_via=via)
    rundir = RunDir(paths.run_dir(decision.run_id))
    rundir.append_decision(decision_id, decision.question, value)
    rundir.event("answer", decision_id=decision_id, answer=value, via=via)
    answered = store.get_decision(decision_id)
    assert answered is not None
    return answered
