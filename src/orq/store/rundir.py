"""Per-run directory: TASK.md, DECISIONS.md, state.json, events.jsonl, iterations (SPEC section 11)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class RunDir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def create(self, task_text: str) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "allow_tokens").mkdir(exist_ok=True)
        (self.path / "iterations").mkdir(exist_ok=True)
        (self.path / "TASK.md").write_text(task_text, encoding="utf-8")

    def iteration(self, n: int) -> Path:
        path = self.path / "iterations" / str(n)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def event(self, event_type: str, **data: object) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "type": event_type, **data}
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_state(self, state: dict) -> None:
        tmp = self.path / "state.json.tmp"
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.path / "state.json")

    def read_state(self) -> dict | None:
        path = self.path / "state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def append_decision(self, decision_id: str, question: str, answer: str) -> None:
        with (self.path / "DECISIONS.md").open("a", encoding="utf-8") as handle:
            handle.write(f"## {decision_id}\n**Question:** {question}\n**Answer:** {answer}\n\n")

    def decisions_text(self) -> str:
        path = self.path / "DECISIONS.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""
