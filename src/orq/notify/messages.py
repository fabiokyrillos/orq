"""WhatsApp message formats and reply parsing (SPEC 9.3). Pure functions, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass

from orq.core.models import Decision, RunRecord

_DECISION_ID = re.compile(r"^D[A-Z0-9]{3,5}$")  # real IDs are D plus 4 chars; the SPEC example D7K2 is shorter
_RUN_ID = re.compile(r"^R[A-Z0-9]{4,6}$")
_COMMANDS = {"STATUS", "PAUSE", "RESUME", "ABORT"}

HINT = ("Reply with the decision ID first: `D7K2 1`, `D7K2 <text>`, `APPROVE D7K2` or `DENY D7K2`. "
        "Commands: STATUS, STATUS <run>, PAUSE <run>, RESUME <run>, ABORT <run>.")
DESTRUCTIVE_HINT = "{id} is a destructive approval: reply exactly `APPROVE {id}` or `DENY {id}`. A number does not count."

_TYPE_LABELS = {"business": "Business decision", "ambiguity": "Ambiguity", "risk": "Risk", "blocked": "Blocked"}


@dataclass(frozen=True)
class Reply:
    kind: str                    # answer_index | answer_text | approve | deny | status | pause | resume | abort | unknown
    decision_id: str | None = None
    run_id: str | None = None
    index: int | None = None     # 1-based, as printed in the message
    text: str = ""


def parse_reply(raw: str) -> Reply:
    tokens = raw.strip().split()
    if not tokens:
        return Reply("unknown")
    head = tokens[0].upper()
    rest = tokens[1:]
    if head in _COMMANDS:
        if head == "STATUS":
            run_id = rest[0].upper() if rest else None
            if run_id is not None and not _RUN_ID.match(run_id):
                return Reply("unknown", text=raw)
            return Reply("status", run_id=run_id)
        if len(rest) == 1 and _RUN_ID.match(rest[0].upper()):
            return Reply(head.lower(), run_id=rest[0].upper())
        return Reply("unknown", text=raw)
    if head in ("APPROVE", "DENY") and len(rest) == 1 and _DECISION_ID.match(rest[0].upper()):
        return Reply(head.lower(), decision_id=rest[0].upper())
    if _DECISION_ID.match(head):
        decision_id = head
        if len(rest) == 1 and rest[0].upper() in ("APPROVE", "DENY"):
            return Reply(rest[0].lower(), decision_id=decision_id)
        if len(rest) == 1 and rest[0].isdigit():
            return Reply("answer_index", decision_id=decision_id, index=int(rest[0]))
        if rest:
            return Reply("answer_text", decision_id=decision_id, text=" ".join(rest))
        return Reply("unknown", decision_id=decision_id, text=raw)
    return Reply("unknown", text=raw)


def format_decision(decision: Decision, run: RunRecord) -> str:
    repo = run.repo.split("/")[-1]
    label = _TYPE_LABELS.get(decision.decision_type, decision.decision_type)
    lines = [f"*[orq] {decision.decision_id} · {repo} · iteration {run.iteration}*", f"{label} ({decision.source}): {decision.question.strip()}"]
    for index, option in enumerate(decision.options):
        mark = " (recommended)" if decision.recommendation == index else ""
        lines.append(f"{index + 1}. {option}{mark}")
    if decision.destructive:
        lines.append(f"Reply: APPROVE {decision.decision_id}  or  DENY {decision.decision_id}")
    elif decision.options:
        lines.append(f"Reply: {decision.decision_id} <number>  or  {decision.decision_id} <free text>")
    else:
        lines.append(f"Reply: {decision.decision_id} <free text>")
    return "\n".join(lines)


def format_run_state(run: RunRecord, *, pr_url: str | None = None, reason: str | None = None) -> str:
    repo = run.repo.split("/")[-1]
    lines = [f"*[orq] {run.run_id} · {repo} · {run.state.value}*", run.task_title]
    if pr_url:
        lines.append(f"PR: {pr_url}")
    if reason:
        lines.append(reason)
    return "\n".join(lines)


def format_status(runs: list[RunRecord], pending: dict[str, list[Decision]]) -> str:
    if not runs:
        return "*[orq] STATUS*\nno runs"
    lines = ["*[orq] STATUS*"]
    for run in runs[:10]:
        line = f"{run.run_id} {run.state.value} iter {run.iteration} · {run.repo.split('/')[-1]} · {run.task_title}"
        for decision in pending.get(run.run_id, []):
            line += f"\n  pending {decision.decision_id}: {decision.question.strip()[:80]}"
        lines.append(line)
    return "\n".join(lines)
