"""No-progress detection (SPEC 10.2). History is a list of plain dicts so it serializes into state.json."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

PROMPT_SIMILARITY = 0.9


@dataclass(frozen=True)
class ProgressRule:
    rule: str          # same_diff | same_failure | same_prompt
    rollback_to: int   # last iteration before the streak began (0 means the base commit)
    detail: str


class ProgressTracker:
    def __init__(self, history: list[dict] | None = None) -> None:
        self.history: list[dict] = history if history is not None else []

    def record(self, iteration: int, *, diff_hash: str, failure_signature: str | None, next_prompt: str | None) -> ProgressRule | None:
        self.history.append({"iteration": iteration, "diff_hash": diff_hash, "failure_signature": failure_signature,
                             "next_prompt": next_prompt or ""})
        h = self.history
        if len(h) >= 2 and h[-1]["diff_hash"] == h[-2]["diff_hash"]:
            return ProgressRule("same_diff", h[-2]["iteration"] - 1, "the last two iterations produced the same diff")
        if len(h) >= 3 and h[-1]["failure_signature"] and h[-1]["failure_signature"] == h[-2]["failure_signature"] == h[-3]["failure_signature"]:
            return ProgressRule("same_failure", h[-3]["iteration"] - 1, "the check command failed the same way three times in a row")
        if len(h) >= 2 and h[-1]["next_prompt"] and h[-2]["next_prompt"]:
            ratio = SequenceMatcher(None, h[-1]["next_prompt"], h[-2]["next_prompt"]).ratio()
            if ratio >= PROMPT_SIMILARITY:
                return ProgressRule("same_prompt", h[-2]["iteration"] - 1, f"the reviewer repeated its instructions (similarity {ratio:.2f})")
        return None

    def reset(self) -> None:
        self.history.clear()

    def discard_after(self, iteration: int) -> None:
        """Drop history past a rollback target so the discarded iterations do not count as a streak."""
        self.history[:] = [h for h in self.history if h["iteration"] <= iteration]
