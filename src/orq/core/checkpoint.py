"""state.json: everything the runner needs to continue after a pause or a crash (SPEC 6 and 11)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSION = 1


@dataclass
class Checkpoint:
    run_id: str
    state: str
    phase: str                       # setup | implement | verify | review | await | finalize | done
    branch: str
    worktree: str
    repo_path: str | None = None
    base_commit: str | None = None
    last_commit: str | None = None
    iteration: int = 0
    version: int = VERSION
    pid: int = field(default_factory=os.getpid)
    commits: dict[str, str] = field(default_factory=dict)      # iteration -> sha after that iteration
    summaries: dict[str, str] = field(default_factory=dict)    # iteration -> reviewer summary
    implementer_session: str | None = None
    reviewer_session: str | None = None
    outcome: dict = field(default_factory=lambda: {"next_prompt": None, "milestone": None, "done": False})
    previous_check: dict | None = None
    report: str = ""                                           # implementer's final message of this iteration
    diff_hash: str | None = None
    denied_actions: list[dict] = field(default_factory=list)   # guard denials of this iteration, not yet decided
    guard_approved: list[str] = field(default_factory=list)
    guard_denied: list[str] = field(default_factory=list)
    diff_approved: bool = False
    pending_decision: dict | None = None                       # {decision_id, kind, payload}
    progress_history: list[dict] = field(default_factory=list)
    discarded: str | None = None                               # note about rolled-back iterations for the next prompts
    reviewer_fallback_until: float | None = None
    rate_limit_until: float | None = None
    rate_limit_retries: int = 0
    interrupted: bool = False                                  # the implementer turn was cut by a crash
    started_at: str | None = None
    elapsed_seconds: float = 0.0

    def save(self, path: Path) -> None:
        self.pid = os.getpid()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Checkpoint | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != VERSION:
            raise ValueError(f"state.json version {data.get('version')} is not supported (expected {VERSION})")
        return cls(**data)
