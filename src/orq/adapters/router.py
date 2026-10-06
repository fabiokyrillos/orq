"""Swaps the reviewer between Codex and the Claude fallback around usage limits (SPEC 4 and 12).

Proactive: after a successful primary call whose rate-limit snapshot is at or past the threshold, later
calls go to the fallback until the snapshot's reset time. Reactive: a primary failure classified as a
rate limit switches at once and the same review is retried on the fallback.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from orq.adapters.base import Agent, AgentResult, EventCallback

DEFAULT_FALLBACK_SECONDS = 3600.0
SwitchEvent = tuple[str, str, float | None]  # ("switched" | "restored", reviewer name, until)


class ReviewerRouter:
    def __init__(self, primary: Agent, fallback: Agent, *, switch_at_used_percent: int, clock: Callable[[], float] = time.time,
                 fallback_until: float | None = None, on_switch: Callable[[SwitchEvent], None] | None = None) -> None:
        self.primary, self.fallback = primary, fallback
        self.threshold = switch_at_used_percent
        self.clock = clock
        self.fallback_until = fallback_until
        self.on_switch = on_switch

    @property
    def active(self) -> Agent:
        if self.fallback_until is not None:
            if self.clock() < self.fallback_until:
                return self.fallback
            self.fallback_until = None
            self._notify("restored", self.primary.name, None)
        return self.primary

    @property
    def name(self) -> str:
        return self.active.name

    async def run(self, prompt: str, *, cwd: Path, log_path: Path, session_id: str | None = None,
                  run_dir: Path | None = None, on_event: EventCallback | None = None, model: str | None = None,
                  effort: str | None = None, contract: object | None = None) -> AgentResult:
        common = dict(cwd=cwd, log_path=log_path, run_dir=run_dir, on_event=on_event, effort=effort, contract=contract)
        agent = self.active
        if agent is self.fallback:
            # The fallback cannot resume the primary's session.
            return await self.fallback.run(prompt, session_id=None, **common)
        result = await self.primary.run(prompt, session_id=session_id, **common)
        snapshot = (result.rate_limit or {}).get("primary") or {}
        if result.error_kind == "rate_limit":
            self._switch(snapshot.get("resets_at"))
            return await self.fallback.run(prompt, session_id=None, **common)
        used = snapshot.get("used_percent")
        if isinstance(used, (int, float)) and used >= self.threshold:
            self._switch(snapshot.get("resets_at"))
        return result

    def _switch(self, resets_at: object) -> None:
        self.fallback_until = float(resets_at) if isinstance(resets_at, (int, float)) and resets_at > self.clock() \
            else self.clock() + DEFAULT_FALLBACK_SECONDS
        self._notify("switched", self.fallback.name, self.fallback_until)

    def _notify(self, kind: str, name: str, until: float | None) -> None:
        if self.on_switch:
            self.on_switch((kind, name, until))
