"""Layout of the runtime directory (SPEC section 11). Never inside a target repo."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OrqPaths:
    root: Path

    @classmethod
    def from_env(cls) -> OrqPaths:
        override = os.environ.get("ORQ_HOME")
        return cls(Path(override) if override else Path.home() / ".orq")

    @property
    def db(self) -> Path:
        return self.root / "orq.db"

    @property
    def config(self) -> Path:
        return self.root / "config.toml"

    @property
    def repos(self) -> Path:
        return self.root / "repos"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def chats(self) -> Path:
        return self.root / "chats"  # Phase 7.1: <owner>__<repo>/<chat_id>/

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def iteration_dir(self, run_id: str, iteration: int) -> Path:
        return self.run_dir(run_id) / "iterations" / str(iteration)

    def allow_tokens(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "allow_tokens"
