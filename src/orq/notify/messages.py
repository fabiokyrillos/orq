"""WhatsApp message formats and reply parsing (SPEC 9.3). Pure functions, no I/O.

Phase 6.1: the owner reads these on a phone, so they are in Brazilian Portuguese (CLAUDE.md exception) and laid out in
short sections with blank lines, bold labels and emojis. Machine keywords (APPROVE, DENY, STATUS, IDs) stay as they are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from orq.core.models import Decision, RunRecord, RunState

_DECISION_ID = re.compile(r"^D[A-Z0-9]{3,5}$")  # real IDs are D plus 4 chars; the SPEC example D7K2 is shorter
_RUN_ID = re.compile(r"^R[A-Z0-9]{4,6}$")
_COMMANDS = {"STATUS", "PAUSE", "RESUME", "ABORT"}

HINT = ("Responda começando pelo ID da decisão: `D7K2 1`, `D7K2 <texto>`, `APPROVE D7K2` ou `DENY D7K2`.\n"
        "Comandos: `STATUS`, `STATUS <run>`, `PAUSE <run>`, `RESUME <run>`, `ABORT <run>`.")
DESTRUCTIVE_HINT = "{id} é uma aprovação destrutiva: responda exatamente `APPROVE {id}` ou `DENY {id}`. Número não vale."

MESSAGE_CAP = 2000  # past this a decision stops being readable on a phone; the context is cut first
_TYPES = {"business": ("🟡", "Decisão de negócio"), "ambiguity": ("🔵", "Ambiguidade"), "risk": ("🔴", "Risco"),
          "blocked": ("⛔", "Bloqueio")}
_SOURCES = {"planner": "do planejador", "reviewer": "do revisor", "implementer": "do implementador", "guard": "do guard",
            "orq": "do orq"}
_STAKES = {"low": "baixa", "medium": "média", "high": "alta"}
_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
_VIA = {"dashboard": "no dashboard", "cli": "na linha de comando", "terminal": "no terminal", "whatsapp": "pelo WhatsApp"}
_STATE_EMOJI = {RunState.DONE: "✅", RunState.FAILED: "❌", RunState.ABORTED: "🛑", RunState.QUEUED: "🕒", RunState.PAUSED: "⏸️",
                RunState.AWAITING_HUMAN: "⏳", RunState.AWAITING_PLAN_APPROVAL: "⏳", RunState.PAUSED_RATE_LIMIT: "⏸️"}
_RUN_STATES = {RunState.DONE: "Concluída", RunState.FAILED: "Falhou", RunState.ABORTED: "Abortada"}


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


def _repo(run: RunRecord) -> str:
    return run.repo.split("/")[-1]


def _clean(text: str) -> str:
    """Keep the author's line breaks, drop trailing spaces and runs of blank lines."""
    lines = [line.rstrip() for line in str(text).strip().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def format_decision(decision: Decision, run: RunRecord, *, milestone: str | None = None, auto_minutes: int | None = None) -> str:
    """Sections: header, context, question, options with consequences, recommendation, reply line.

    `auto_minutes` is set when the automatic-answer policy would answer this decision; the owner is told when.
    Capped at MESSAGE_CAP characters: the context is cut first; the dashboard always shows everything.
    """
    emoji, label = _TYPES.get(decision.decision_type, ("❔", decision.decision_type))
    where = [f"📦 {_repo(run)}", f"iteração {run.iteration}"] + ([f"milestone {milestone}"] if milestone else [])
    where.append(f"pergunta {_SOURCES.get(decision.source, decision.source)}")
    head = [f"{emoji} *{label}* · {decision.decision_id}", " · ".join(where)]

    body = ["", "❓ *Pergunta*", _clean(decision.question)]
    if decision.options:
        body += ["", "*Opções*"]
        for index, option in enumerate(decision.options):
            number = _NUMBERS[index] if index < len(_NUMBERS) else f"{index + 1}."
            star = " ⭐" if decision.recommendation == index else ""
            body.append(f"{number} *{option}*{star}")
            detail = decision.option_details[index].strip() if index < len(decision.option_details) else ""
            if detail:
                body.append(f"      ↳ {detail}")
    rec = decision.recommendation
    footer = []
    if decision.recommendation_reason.strip() and rec is not None and 0 <= rec < len(decision.options):
        footer.append(f"💡 *Recomendo a {rec + 1}:* {decision.recommendation_reason.strip()}")
    if auto_minutes is not None:
        footer.append(f"⚙️ Complexidade {_STAKES.get(decision.stakes, decision.stakes)} · se você não responder, "
                      f"orq escolhe a recomendada em {auto_minutes} min")
    if decision.destructive:
        footer.append(f"↩️ Responda `APPROVE {decision.decision_id}` ou `DENY {decision.decision_id}` (número não vale)")
    elif decision.options:
        footer.append(f"↩️ Responda `{decision.decision_id} <número>` ou `{decision.decision_id} <texto>`")
    else:
        footer.append(f"↩️ Responda `{decision.decision_id} <texto>`")
    tail = body + [""] + footer

    context = _clean(decision.context)
    if context:
        room = MESSAGE_CAP - len("\n".join(head + tail)) - len("\n\n*Contexto*\n")
        suffix = "… _(continua no dashboard)_"
        if len(context) > room:
            context = context[: max(0, room - len(suffix))].rstrip() + suffix
        head += ["", "*Contexto*", context]
    return "\n".join(head + tail)


def format_answered_elsewhere(decision: Decision, minutes: int | None = None) -> str:
    """The question is still on the phone: say who settled it, so the owner neither answers again nor wonders."""
    via = _VIA.get(decision.answered_via or "", decision.answered_via or "")
    if decision.answered_by == "auto":
        waited = f" ({minutes} min sem resposta)" if minutes is not None else ""
        line = f"🤖 *{decision.decision_id}* respondida automaticamente pelo orq{waited}"
    elif decision.answered_by == "claude":
        line = f"🧑‍💻 *{decision.decision_id}* respondida pelo Claude {via}".rstrip()
    else:
        line = f"✅ *{decision.decision_id}* respondida por você {via}".rstrip()
    return f"{line}\n➡️ {decision.answer}\n_Nada a fazer aqui._"


def format_run_state(run: RunRecord, *, pr_url: str | None = None, reason: str | None = None) -> str:
    emoji = _STATE_EMOJI.get(run.state, "ℹ️")
    label = _RUN_STATES.get(run.state, run.state.value)
    lines = [f"{emoji} *{label}* · {run.run_id} · {_repo(run)}", run.task_title]
    if pr_url:
        lines.append(f"🔗 {pr_url}")
    if reason:
        lines.append(f"📝 Motivo: {reason}")
    return "\n".join(lines)


def format_status(runs: list[RunRecord], pending: dict[str, list[Decision]], *, slots: tuple[int, int] | None = None,
                  queued: int = 0) -> str:
    """Up to 10 runs (newest first), grouped by project; slots used/limit and the queue length in the header."""
    header = "📋 *Status*"
    if slots is not None:
        header += f" · vagas {slots[0]}/{slots[1]} · {queued} na fila"
    if not runs:
        return header + "\nnenhuma run"
    groups: dict[str, list[RunRecord]] = {}
    for run in runs[:10]:
        groups.setdefault(run.repo, []).append(run)
    lines = [header]
    for repo, members in groups.items():
        lines += ["", f"*{repo.split('/')[-1]}*"]
        for run in members:
            lines.append(f"{_STATE_EMOJI.get(run.state, '🔨')} {run.run_id} {run.state.value} · iter {run.iteration} · {run.task_title}")
            for decision in pending.get(run.run_id, []):
                lines.append(f"      ⏳ {decision.decision_id}: {decision.question.strip()[:80]}")
    return "\n".join(lines)
