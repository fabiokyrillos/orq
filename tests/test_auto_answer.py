"""Automatic answers (Phase 6.1): only low-stakes, recommended, non-destructive, non-business decisions, after a timeout."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orq.config import Config
from orq.core.auto_answer import check
from orq.core.models import Decision, RunRecord, RunState
from orq.hub.auto_answer import AutoAnswerer
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc).timestamp()


def decision(**overrides) -> Decision:
    base = dict(decision_id="DAAAA", run_id="R1", source="reviewer", decision_type="ambiguity", question="Float or Decimal?",
                options=["floats", "Decimal"], recommendation=0, recommendation_reason="keeps the signature", stakes="low")
    base.update(overrides)
    return Decision(**base)


def run_check(d: Decision, *, enabled=True, minutes=30, max_stakes="low", waited=31) -> tuple[bool, str]:
    return check(d, enabled=enabled, minutes=minutes, max_stakes=max_stakes, started_at=T0, now=T0 + waited * 60)


def test_eligible_decision_after_the_timeout() -> None:
    assert run_check(decision()) == (True, "low stakes, 31 min without an answer")


@pytest.mark.parametrize("overrides, waited, enabled, max_stakes, reason", [
    ({}, 31, False, "low", "automatic answers are off"),
    ({}, 29, True, "low", "waiting: 29 of 30 min"),
    ({"stakes": "medium"}, 31, True, "low", "stakes medium above low"),
    ({"stakes": ""}, 31, True, "high", "stakes unknown"),
    ({"destructive": True}, 31, True, "high", "destructive"),
    ({"decision_type": "business"}, 31, True, "high", "business decision"),
    ({"source": "guard"}, 31, True, "high", "guard decision"),
    ({"options": ["approve", "revise"], "source": "planner"}, 31, True, "high", "plan approval"),
    ({"recommendation": None}, 31, True, "high", "no recommendation"),
    ({"recommendation_reason": ""}, 31, True, "high", "no reason for the recommendation"),
    ({"options": ["retry", "abort"], "recommendation": 1}, 31, True, "high", "the recommendation is to abort"),
    ({"status": "answered"}, 31, True, "high", "already answered"),
])
def test_exclusions(overrides, waited, enabled, max_stakes, reason) -> None:
    eligible, why = run_check(decision(**overrides), enabled=enabled, max_stakes=max_stakes, waited=waited)
    assert not eligible and why == reason


def test_medium_allowed_when_the_owner_raises_the_limit() -> None:
    assert run_check(decision(stakes="medium"), max_stakes="medium")[0]


@pytest.fixture
def home(tmp_path: Path) -> OrqPaths:
    paths = OrqPaths(tmp_path / "home")
    store = Store(paths.db)
    store.create_run(RunRecord(run_id="R1", repo="o/a", task_title="t", branch="b"))
    store.set_state("R1", RunState.AWAITING_HUMAN)
    RunDir(paths.run_dir("R1")).create("# Task: t\n")
    return paths


def test_hub_task_answers_eligible_decisions_once(home: OrqPaths) -> None:
    store = Store(home.db)
    store.add_decision(decision())
    store.mark_notified("decision", "DAAAA", "whatsapp", at=T0)   # the clock starts when WhatsApp got it
    store.set_settings({"notify.auto_answer": True})
    now = [T0 + 10 * 60]
    auto = AutoAnswerer(store, home, Config(), clock=lambda: now[0])

    assert auto.tick() == []
    now[0] = T0 + 31 * 60
    assert auto.tick() == ["DAAAA"]
    answered = store.get_decision("DAAAA")
    assert (answered.answer, answered.answered_via, answered.answered_by) == ("floats", "auto", "auto")
    events = (home.run_dir("R1") / "events.jsonl").read_text(encoding="utf-8")
    assert '"auto_answered"' in events and "31 min without an answer" in events
    assert auto.tick() == []


def test_hub_task_respects_project_settings_and_finished_runs(home: OrqPaths) -> None:
    store = Store(home.db)
    store.add_decision(decision())
    store.set_settings({"notify.auto_answer": True})
    store.update_project("o/a", settings={"notify.auto_answer": False})
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    auto = AutoAnswerer(store, home, Config(), clock=lambda: later.timestamp())

    assert auto.tick() == []                                       # off for this project
    store.update_project("o/a", settings={})
    store.set_state("R1", RunState.ABORTED)
    assert auto.tick() == []                                       # nobody waits for a finished run
