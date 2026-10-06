"""SQLite store for runs and decisions (SPEC section 11)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from orq.core.models import Decision, RunRecord, RunState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    task_title TEXT NOT NULL,
    branch TEXT NOT NULL,
    state TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    worktree TEXT,
    implementer_session TEXT,
    reviewer_session TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    source TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    recommendation INTEGER,
    destructive INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    answer TEXT,
    answered_via TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    # runs

    def create_run(self, run: RunRecord) -> None:
        now = _now()
        self._conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run.run_id, run.repo, run.task_title, run.branch, run.state.value, run.iteration,
             run.worktree, run.implementer_session, run.reviewer_session, now, now),
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_from_row(row) if row else None

    def list_runs(self) -> list[RunRecord]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC").fetchall()
        return [_run_from_row(r) for r in rows]

    def set_state(self, run_id: str, state: RunState) -> None:
        self._update_run(run_id, state=state.value)

    def set_iteration(self, run_id: str, iteration: int) -> None:
        self._update_run(run_id, iteration=iteration)

    def set_branch(self, run_id: str, branch: str) -> None:
        self._update_run(run_id, branch=branch)

    def set_worktree(self, run_id: str, worktree: str) -> None:
        self._update_run(run_id, worktree=worktree)

    def set_sessions(self, run_id: str, implementer_session: str | None = None, reviewer_session: str | None = None) -> None:
        fields = {k: v for k, v in (("implementer_session", implementer_session), ("reviewer_session", reviewer_session)) if v is not None}
        self._update_run(run_id, **fields)

    def _update_run(self, run_id: str, **fields: object) -> None:
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._conn.execute(f"UPDATE runs SET {assignments} WHERE run_id = ?", (*fields.values(), run_id))
        self._conn.commit()

    # decisions

    def add_decision(self, decision: Decision) -> None:
        self._conn.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (decision.decision_id, decision.run_id, decision.source, decision.decision_type, decision.question,
             json.dumps(decision.options), decision.recommendation, int(decision.destructive), decision.status,
             decision.answer, decision.answered_via, _now(), decision.answered_at),
        )
        self._conn.commit()

    def get_decision(self, decision_id: str) -> Decision | None:
        row = self._conn.execute("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
        return _decision_from_row(row) if row else None

    def pending_decisions(self, run_id: str) -> list[Decision]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE run_id = ? AND status = 'pending' ORDER BY created_at", (run_id,)
        ).fetchall()
        return [_decision_from_row(r) for r in rows]

    def answer_decision(self, decision_id: str, answer: str, answered_via: str) -> None:
        self._conn.execute(
            "UPDATE decisions SET status = 'answered', answer = ?, answered_via = ?, answered_at = ? WHERE decision_id = ?",
            (answer, answered_via, _now(), decision_id),
        )
        self._conn.commit()


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    data = dict(row)
    data["state"] = RunState(data["state"])
    return RunRecord(**data)


def _decision_from_row(row: sqlite3.Row) -> Decision:
    data = dict(row)
    data["options"] = json.loads(data["options"])
    data["destructive"] = bool(data["destructive"])
    return Decision(**data)
