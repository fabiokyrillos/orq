from pathlib import Path

import pytest

from orq.core.answers import AnswerError, record_answer, resolve_answer
from orq.core.models import Decision, RunRecord
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir


@pytest.fixture
def env(tmp_path: Path):
    paths = OrqPaths(tmp_path / "home")
    store = Store(paths.db)
    store.create_run(RunRecord(run_id="R1", repo="o/r", task_title="t", branch="b"))
    RunDir(paths.run_dir("R1")).create("# Task: t\n")
    store.add_decision(Decision(decision_id="D1", run_id="R1", source="reviewer", decision_type="business",
                                question="Tax?", options=["before", "after"], recommendation=0))
    return store, paths


def test_resolve_answer_maps_index() -> None:
    d = Decision(decision_id="D", run_id="R", source="x", decision_type="risk", question="q", options=["a", "b"])
    assert resolve_answer(d, " 1 ") == "b" and resolve_answer(d, "7") == "7" and resolve_answer(d, "free") == "free"


def test_record_answer_stores_appends_and_logs(env) -> None:
    store, paths = env
    answered = record_answer(store, paths, "D1", "1", via="whatsapp")
    assert answered.answer == "after" and answered.answered_via == "whatsapp" and answered.status == "answered"
    assert "after" in (paths.run_dir("R1") / "DECISIONS.md").read_text(encoding="utf-8")
    assert '"via": "whatsapp"' in (paths.run_dir("R1") / "events.jsonl").read_text(encoding="utf-8")


def test_record_answer_errors(env) -> None:
    store, paths = env
    with pytest.raises(AnswerError, match="no decision"):
        record_answer(store, paths, "DX", "x", via="cli")
    with pytest.raises(AnswerError, match="empty"):
        record_answer(store, paths, "D1", "   ", via="cli")
    record_answer(store, paths, "D1", "before", via="cli")
    with pytest.raises(AnswerError, match="already answered"):
        record_answer(store, paths, "D1", "after", via="cli")
