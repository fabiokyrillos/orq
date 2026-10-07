"""SQLite store for runs and decisions (SPEC section 11)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from orq.core.models import Decision, Project, RunRecord, RunState
from orq.core.procs import pid_alive

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
CREATE TABLE IF NOT EXISTS notifications (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    channel TEXT NOT NULL,
    sent_at REAL NOT NULL,
    PRIMARY KEY (kind, key, channel)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    repo TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_branch TEXT NOT NULL DEFAULT 'main',
    check_command TEXT NOT NULL DEFAULT '',
    max_concurrent INTEGER,
    local_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pid INTEGER NOT NULL,
    held INTEGER NOT NULL DEFAULT 0,
    fresh INTEGER NOT NULL DEFAULT 1,
    since REAL NOT NULL
);
"""

# Runs register their repo as a project; databases from before Phase 5 get theirs when opened.
_REGISTER_PROJECTS = """
INSERT OR IGNORE INTO projects (repo, name, created_at)
SELECT repo, substr(repo, instr(repo, '/') + 1), min(created_at) FROM runs {where} GROUP BY repo
"""
_PROJECT_FIELDS = ("name", "base_branch", "check_command", "max_concurrent", "local_path", "settings")
# Columns added after a table first shipped: (table, column, definition).
_MIGRATIONS = (("projects", "settings", "TEXT NOT NULL DEFAULT '{}'"),
               ("decisions", "context", "TEXT NOT NULL DEFAULT ''"),
               ("decisions", "option_details", "TEXT NOT NULL DEFAULT '[]'"),
               ("decisions", "recommendation_reason", "TEXT NOT NULL DEFAULT ''"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)  # the hub serves requests from worker threads
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        for table, column, definition in _MIGRATIONS:
            columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self._conn.execute(_REGISTER_PROJECTS.format(where=""))
        self._conn.commit()

    # runs

    def create_run(self, run: RunRecord) -> None:
        now = _now()
        self._conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run.run_id, run.repo, run.task_title, run.branch, run.state.value, run.iteration,
             run.worktree, run.implementer_session, run.reviewer_session, now, now),
        )
        self._conn.execute(_REGISTER_PROJECTS.format(where="WHERE repo = ?"), (run.repo,))
        self._conn.commit()

    # projects

    def upsert_project(self, project: Project) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO projects (repo, name, base_branch, check_command, max_concurrent, local_path, settings, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, coalesce((SELECT created_at FROM projects WHERE repo = ?), ?))",
            (project.repo, project.name, project.base_branch, project.check_command, project.max_concurrent, project.local_path,
             json.dumps(project.settings), project.repo, project.created_at or _now()),
        )
        self._conn.commit()

    def get_project(self, repo: str) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
        return _project_from_row(row) if row else None

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY lower(name), repo").fetchall()
        return [_project_from_row(r) for r in rows]

    def update_project(self, repo: str, **fields: object) -> None:
        unknown = set(fields) - set(_PROJECT_FIELDS)
        if unknown:
            raise ValueError(f"unknown project fields: {sorted(unknown)}")
        if not fields:
            return
        if "settings" in fields:
            fields["settings"] = json.dumps(fields["settings"] or {})
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._conn.execute(f"UPDATE projects SET {assignments} WHERE repo = ?", (*fields.values(), repo))
        self._conn.commit()

    # global setting overrides (Phase 6; orq.core.settings keys, values as JSON)

    def get_settings(self) -> dict:
        return {row["key"]: json.loads(row["value"]) for row in self._conn.execute("SELECT key, value FROM settings ORDER BY key")}

    def set_settings(self, values: dict) -> None:
        """None clears a key (the value is inherited again)."""
        for key, value in values.items():
            if value is None:
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
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

    # slots (Phase 5): a run holds one while it works; waiting for the owner holds none

    def try_acquire_slot(self, run_id: str, repo: str, pid: int, *, fresh: bool, global_limit: int, default_project_limit: int,
                         alive: Callable[[int], bool] = pid_alive) -> bool:
        """Register as a waiter and take a slot when the limits allow it and no better waiter could take it first.

        Better means: a run that already started (it answered a decision) before one that never ran, then oldest first.
        Rows of dead processes are dropped, so a crash never leaks a slot.
        """
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM slots").fetchall()]
            for row in rows:
                if row["run_id"] != run_id and not alive(row["pid"]):
                    conn.execute("DELETE FROM slots WHERE run_id = ?", (row["run_id"],))
            rows = [r for r in rows if r["run_id"] == run_id or alive(r["pid"])]
            me = next((r for r in rows if r["run_id"] == run_id), None)
            if me is not None and me["held"]:
                conn.execute("UPDATE slots SET pid = ? WHERE run_id = ?", (pid, run_id))
                conn.commit()
                return True
            if me is None:
                me = {"run_id": run_id, "repo": repo, "pid": pid, "held": 0, "fresh": int(fresh), "since": time.time()}
                conn.execute("INSERT INTO slots VALUES (:run_id, :repo, :pid, :held, :fresh, :since)", me)
                rows.append(me)
            else:
                me.update(pid=pid, fresh=int(fresh), repo=repo)
                conn.execute("UPDATE slots SET pid = ?, fresh = ?, repo = ? WHERE run_id = ?", (pid, int(fresh), repo, run_id))
            caps = {r["repo"]: r["max_concurrent"] for r in conn.execute("SELECT repo, max_concurrent FROM projects").fetchall()}
            held = [r for r in rows if r["held"]]

            def has_room(target: str) -> bool:
                cap = caps.get(target) or default_project_limit
                return len(held) < global_limit and sum(1 for r in held if r["repo"] == target) < cap

            rank = (me["fresh"], me["since"])
            granted = has_room(repo) and not any(
                not r["held"] and r["run_id"] != run_id and (r["fresh"], r["since"]) < rank and has_room(r["repo"]) for r in rows)
            if granted:
                conn.execute("UPDATE slots SET held = 1 WHERE run_id = ?", (run_id,))
            conn.commit()
            return granted
        except BaseException:
            conn.rollback()
            raise

    def release_slot(self, run_id: str) -> None:
        self._conn.execute("DELETE FROM slots WHERE run_id = ?", (run_id,))
        self._conn.commit()

    def slot_usage(self, alive: Callable[[int], bool] | None = None) -> list[dict]:
        """Every slot row (held or waiting), oldest first; with `alive`, only rows of live processes."""
        rows = [dict(r) for r in self._conn.execute("SELECT * FROM slots ORDER BY since").fetchall()]
        return [r for r in rows if alive(r["pid"])] if alive else rows

    def held_slots(self) -> int:
        return sum(1 for row in self.slot_usage(alive=pid_alive) if row["held"])

    # decisions

    def add_decision(self, decision: Decision) -> None:
        self._conn.execute(
            "INSERT INTO decisions (decision_id, run_id, source, decision_type, question, options, recommendation, destructive, "
            "status, answer, answered_via, created_at, answered_at, context, option_details, recommendation_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (decision.decision_id, decision.run_id, decision.source, decision.decision_type, decision.question,
             json.dumps(decision.options), decision.recommendation, int(decision.destructive), decision.status,
             decision.answer, decision.answered_via, _now(), decision.answered_at, decision.context,
             json.dumps(decision.option_details), decision.recommendation_reason),
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

    def all_pending_decisions(self) -> list[Decision]:
        rows = self._conn.execute("SELECT * FROM decisions WHERE status = 'pending' ORDER BY created_at").fetchall()
        return [_decision_from_row(r) for r in rows]

    # notifications (hub) and small key/value state

    def notified_at(self, kind: str, key: str, channel: str) -> float | None:
        row = self._conn.execute("SELECT sent_at FROM notifications WHERE kind = ? AND key = ? AND channel = ?", (kind, key, channel)).fetchone()
        return float(row["sent_at"]) if row else None

    def mark_notified(self, kind: str, key: str, channel: str, at: float | None = None) -> None:
        import time as _time

        self._conn.execute("INSERT OR REPLACE INTO notifications VALUES (?, ?, ?, ?)", (kind, key, channel, at if at is not None else _time.time()))
        self._conn.commit()

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self._conn.commit()

    def answered_elsewhere(self, channel: str) -> list[Decision]:
        """Decisions sent on `channel`, then answered on another one, not yet announced there (kind decision_answered)."""
        rows = self._conn.execute(
            "SELECT d.* FROM decisions d JOIN notifications n ON n.kind = 'decision' AND n.key = d.decision_id AND n.channel = ? "
            "WHERE d.status = 'answered' AND coalesce(d.answered_via, '') != ? AND NOT EXISTS (SELECT 1 FROM notifications a "
            "WHERE a.kind = 'decision_answered' AND a.key = d.decision_id AND a.channel = ?) ORDER BY d.answered_at",
            (channel, channel, channel)).fetchall()
        return [_decision_from_row(r) for r in rows]

    def expire_pending(self, run_id: str) -> None:
        """A finished run's open questions can no longer be acted on."""
        self._conn.execute("UPDATE decisions SET status = 'expired' WHERE run_id = ? AND status = 'pending'", (run_id,))
        self._conn.commit()

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


def _project_from_row(row: sqlite3.Row) -> Project:
    data = dict(row)
    data["settings"] = json.loads(data.get("settings") or "{}")
    return Project(**data)


def _decision_from_row(row: sqlite3.Row) -> Decision:
    data = dict(row)
    data["options"] = json.loads(data["options"])
    data["destructive"] = bool(data["destructive"])
    data["option_details"] = json.loads(data.get("option_details") or "[]")
    return Decision(**data)
