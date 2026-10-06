import json
import re
from pathlib import Path

import pytest

from orq.core.models import Decision, RunRecord, RunState, new_decision_id, new_run_id
from orq.store.db import Store
from orq.store.rundir import RunDir


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "orq.db")


def make_run(run_id: str = "R1") -> RunRecord:
    return RunRecord(run_id=run_id, repo="owner/sandbox", task_title="Add thing", branch="orq/add-thing")


def test_ids_are_short_and_upper_case() -> None:
    assert re.fullmatch(r"R[A-Z2-9]{5}", new_run_id())
    assert re.fullmatch(r"D[A-Z2-9]{4}", new_decision_id())
    assert new_run_id() != new_run_id()


def test_create_and_get_run(store: Store) -> None:
    store.create_run(make_run())

    run = store.get_run("R1")

    assert run is not None
    assert run.repo == "owner/sandbox"
    assert run.state is RunState.QUEUED
    assert run.iteration == 0


def test_get_missing_run_returns_none(store: Store) -> None:
    assert store.get_run("nope") is None


def test_set_state_and_iteration(store: Store) -> None:
    store.create_run(make_run())

    store.set_state("R1", RunState.IMPLEMENTING)
    store.set_iteration("R1", 3)
    store.set_sessions("R1", implementer_session="abc")

    run = store.get_run("R1")
    assert run is not None
    assert run.state is RunState.IMPLEMENTING
    assert run.iteration == 3
    assert run.implementer_session == "abc"


def test_list_runs_newest_first(store: Store) -> None:
    store.create_run(make_run("R1"))
    store.create_run(make_run("R2"))

    assert [r.run_id for r in store.list_runs()] == ["R2", "R1"]


def test_decision_lifecycle(store: Store) -> None:
    store.create_run(make_run())
    decision = Decision(
        decision_id="D7K2", run_id="R1", source="reviewer", decision_type="business",
        question="Before or after tax?", options=["Before", "After"], recommendation=0,
    )
    store.add_decision(decision)

    pending = store.pending_decisions("R1")
    assert [d.decision_id for d in pending] == ["D7K2"]
    assert pending[0].options == ["Before", "After"]
    assert pending[0].status == "pending"

    store.answer_decision("D7K2", answer="1", answered_via="cli")

    assert store.pending_decisions("R1") == []
    answered = store.get_decision("D7K2")
    assert answered is not None
    assert answered.answer == "1"
    assert answered.answered_via == "cli"
    assert answered.answered_at is not None


def test_rundir_layout_and_events(tmp_path: Path) -> None:
    rundir = RunDir(tmp_path / "runs" / "R1")
    rundir.create("# Task: x\n")

    assert (rundir.path / "TASK.md").read_text(encoding="utf-8") == "# Task: x\n"
    assert (rundir.path / "allow_tokens").is_dir()
    assert rundir.iteration(2) == rundir.path / "iterations" / "2"
    assert rundir.iteration(2).is_dir()

    rundir.event("state", state="PLANNING")
    rundir.event("note", text="café")

    lines = (rundir.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == ["state", "note"]
    assert events[0]["state"] == "PLANNING"
    assert events[1]["text"] == "café"
    assert all("ts" in e for e in events)


def test_rundir_decisions_md(tmp_path: Path) -> None:
    rundir = RunDir(tmp_path / "runs" / "R1")
    rundir.create("# Task: x\n")

    rundir.append_decision("D7K2", "Before or after tax?", "Before")
    text = rundir.decisions_text()
    assert "D7K2" in text and "Before or after tax?" in text and "Before" in text
