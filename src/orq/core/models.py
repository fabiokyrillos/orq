"""Run and decision records (SPEC sections 6 and 8.4)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import Enum

# No 0/1/O/I: the owner types these IDs by hand on WhatsApp.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _short_id(prefix: str, length: int) -> str:
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_run_id() -> str:
    return _short_id("R", 5)


def new_decision_id() -> str:
    return _short_id("D", 4)


class RunState(str, Enum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    FINALIZING = "FINALIZING"
    DONE = "DONE"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    PAUSED_RATE_LIMIT = "PAUSED_RATE_LIMIT"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


@dataclass
class RunRecord:
    run_id: str
    repo: str
    task_title: str
    branch: str
    state: RunState = RunState.QUEUED
    iteration: int = 0
    worktree: str | None = None
    implementer_session: str | None = None
    reviewer_session: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class Project:
    """A GitHub repo the hub works on (Phase 5). Runs join on `repo`."""

    repo: str  # owner/repo
    name: str
    base_branch: str = "main"
    check_command: str = ""
    max_concurrent: int | None = None  # None: [queue].project_concurrency
    local_path: str | None = None  # the folder it was added from, informational only
    created_at: str | None = None
    settings: dict = field(default_factory=dict)  # Phase 6 overrides (orq.core.settings keys)


@dataclass
class Decision:
    decision_id: str
    run_id: str
    source: str  # implementer | reviewer | guard
    decision_type: str  # business | ambiguity | risk | blocked
    question: str
    options: list[str] = field(default_factory=list)
    recommendation: int | None = None
    destructive: bool = False
    status: str = "pending"  # pending | answered | expired
    answer: str | None = None
    answered_via: str | None = None  # whatsapp | dashboard | cli
    created_at: str | None = None
    answered_at: str | None = None
