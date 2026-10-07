"""Per-run directory: TASK.md, DECISIONS.md, events.jsonl, iterations (SPEC section 11). state.json belongs to Checkpoint."""

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

    def archive_iteration(self, n: int) -> None:
        """Keep the logs of a discarded iteration out of the way of its replacement."""
        path = self.path / "iterations" / str(n)
        if path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            path.rename(self.path / "iterations" / f"{n}.discarded-{stamp}")

    def event(self, event_type: str, **data: object) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "type": event_type, **data}
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_decision(self, decision_id: str, question: str, answer: str, context: str = "") -> None:
        # Both agents read this file: the context carries the evidence (the flagged diff, the denied command).
        block = f"## {decision_id}\n" + (f"**Context:** {context}\n" if context else "") + f"**Question:** {question}\n**Answer:** {answer}\n\n"
        with (self.path / "DECISIONS.md").open("a", encoding="utf-8") as handle:
            handle.write(block)

    def decisions_text(self) -> str:
        path = self.path / "DECISIONS.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""
