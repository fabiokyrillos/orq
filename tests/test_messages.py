from orq.core.models import Decision, RunRecord, RunState
from orq.notify.messages import (DESTRUCTIVE_HINT, HINT, MESSAGE_CAP, format_answered_elsewhere, format_decision, format_run_state,
                                 format_status, parse_reply)


def decision(**overrides) -> Decision:
    base = dict(decision_id="D7K2", run_id="RAB2CD", source="reviewer", decision_type="business",
                question="O desconto entra antes ou depois do imposto?", options=["Antes", "Depois"], recommendation=0)
    base.update(overrides)
    return Decision(**base)


def run(**overrides) -> RunRecord:
    base = dict(run_id="RAB2CD", repo="owner/repo-x", task_title="Descontos", branch="orq/discounts", iteration=6)
    base.update(overrides)
    return RunRecord(**base)


def test_decision_without_details_keeps_the_sections() -> None:
    assert format_decision(decision(), run()).splitlines() == [
        "🟡 *Decisão de negócio* · D7K2",
        "📦 repo-x · iteração 6 · pergunta do revisor",
        "",
        "❓ *Pergunta*",
        "O desconto entra antes ou depois do imposto?",
        "",
        "*Opções*",
        "1️⃣ *Antes* ⭐",
        "2️⃣ *Depois*",
        "",
        "↩️ Responda `D7K2 <número>` ou `D7K2 <texto>`",
    ]


def test_decision_with_context_consequences_reason_and_automatic_answer() -> None:
    d = decision(decision_type="ambiguity", stakes="low",
                 context="O milestone 2 calcula o total.\nA task não diz quando aplicar o desconto.",
                 option_details=["total = (preço - desconto) * 1.1", "total = preço * 1.1 - desconto"],
                 recommendation_reason="as notas do repo já descontam o preço líquido")

    lines = format_decision(d, run(), milestone="2/3", auto_minutes=30).splitlines()

    assert lines[:8] == ["🔵 *Ambiguidade* · D7K2", "📦 repo-x · iteração 6 · milestone 2/3 · pergunta do revisor", "",
                         "*Contexto*", "O milestone 2 calcula o total.", "A task não diz quando aplicar o desconto.", "", "❓ *Pergunta*"]
    assert "1️⃣ *Antes* ⭐" in lines and "      ↳ total = (preço - desconto) * 1.1" in lines
    assert "💡 *Recomendo a 1:* as notas do repo já descontam o preço líquido" in lines
    assert "⚙️ Complexidade baixa · se você não responder, orq escolhe a recomendada em 30 min" in lines


def test_long_context_is_cut_first_to_fit_the_cap() -> None:
    text = format_decision(decision(context="palavra " * 1000, option_details=["a", "b"], recommendation_reason="r"), run())

    assert len(text) <= MESSAGE_CAP and "(continua no dashboard)" in text
    assert "O desconto entra antes" in text and text.endswith("↩️ Responda `D7K2 <número>` ou `D7K2 <texto>`")


def test_destructive_decision_asks_for_approve_or_deny() -> None:
    text = format_decision(decision(source="guard", decision_type="risk", destructive=True, options=["approve", "deny"], recommendation=1,
                                    question="Permite esta ação uma vez: Bash: rm -rf build?"), run())
    assert text.startswith("🔴 *Risco* · D7K2") and "pergunta do guard" in text
    assert text.endswith("↩️ Responda `APPROVE D7K2` ou `DENY D7K2` (número não vale)")


def test_answered_elsewhere_says_who_answered() -> None:
    owner = format_answered_elsewhere(decision(status="answered", answer="Antes", answered_via="dashboard", answered_by="owner"))
    claude = format_answered_elsewhere(decision(status="answered", answer="Antes", answered_via="dashboard", answered_by="claude"))
    auto = format_answered_elsewhere(decision(status="answered", answer="Antes", answered_via="auto", answered_by="auto"), minutes=31)

    assert owner == "✅ *D7K2* respondida por você no dashboard\n➡️ Antes\n_Nada a fazer aqui._"
    assert claude.startswith("🧑‍💻 *D7K2* respondida pelo Claude no dashboard")
    assert auto.startswith("🤖 *D7K2* respondida automaticamente pelo orq (31 min sem resposta)")


def test_run_states() -> None:
    assert format_run_state(run(state=RunState.DONE), pr_url="https://x/pull/4").splitlines() == [
        "✅ *Concluída* · RAB2CD · repo-x", "Descontos", "🔗 https://x/pull/4"]
    failed = format_run_state(run(state=RunState.FAILED), reason="max iterations (3) reached")
    assert failed.startswith("❌ *Falhou*") and failed.endswith("📝 Motivo: max iterations (3) reached")
    assert format_run_state(run(state=RunState.ABORTED)).startswith("🛑 *Abortada*")


def test_status_groups_by_project_and_shows_slots() -> None:
    runs = [run(run_id="RA1", repo="owner/a", state=RunState.IMPLEMENTING), run(run_id="RB1", repo="owner/b", state=RunState.QUEUED),
            run(run_id="RA2", repo="owner/a", state=RunState.DONE)]

    lines = format_status(runs, {"RA1": [decision()]}, slots=(1, 2), queued=1).splitlines()

    assert lines[0] == "📋 *Status* · vagas 1/2 · 1 na fila"
    assert lines[1:4] == ["", "*a*", "🔨 RA1 IMPLEMENTING · iter 6 · Descontos"] and lines[4].startswith("      ⏳ D7K2: O desconto")
    assert lines[5].startswith("✅ RA2 DONE") and lines[6:8] == ["", "*b*"] and lines[8].startswith("🕒 RB1 QUEUED")
    assert format_status([], {}) == "📋 *Status*\nnenhuma run"


def test_parse_answers() -> None:
    assert parse_reply("D7K2 1") == parse_reply("d7k2 1")
    r = parse_reply("D7K2 2")
    assert r.kind == "answer_index" and r.decision_id == "D7K2" and r.index == 2
    r = parse_reply("D7K2 after taxes, always")
    assert r.kind == "answer_text" and r.text == "after taxes, always"
    assert parse_reply("APPROVE D7K2").kind == "approve" and parse_reply("deny d7k2").kind == "deny"
    assert parse_reply("D7K2 approve").kind == "approve" and parse_reply("D7K2 DENY").decision_id == "D7K2"


def test_parse_commands() -> None:
    assert parse_reply("STATUS").kind == "status" and parse_reply("STATUS").run_id is None
    assert parse_reply("status rab2cd") == parse_reply("STATUS RAB2CD")
    assert parse_reply("STATUS RAB2CD").run_id == "RAB2CD"
    for word in ("PAUSE", "RESUME", "ABORT"):
        r = parse_reply(f"{word} RAB2CD")
        assert r.kind == word.lower() and r.run_id == "RAB2CD"
    assert parse_reply("PAUSE").kind == "unknown" and parse_reply("ABORT nope").kind == "unknown"


def test_parse_unknown_shapes() -> None:
    assert parse_reply("").kind == "unknown"
    assert parse_reply("yes").kind == "unknown"
    assert parse_reply("1").kind == "unknown"
    assert parse_reply("D7K2").kind == "unknown" and parse_reply("D7K2").decision_id == "D7K2"
    assert parse_reply("APPROVE").kind == "unknown"
    assert "D7K2 1" in HINT and "APPROVE D7K2" in DESTRUCTIVE_HINT.format(id="D7K2")
