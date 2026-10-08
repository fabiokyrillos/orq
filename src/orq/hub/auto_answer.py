"""Hub task (Phase 6.1): apply the automatic-answer policy to pending decisions of live runs.

The answer goes through `record_answer(via="auto", by="auto")`, so a waiting run continues, a run without a process is
resumed by the dispatcher, and WhatsApp announces it like any answer given elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from orq.config import Config
from orq.core.answers import AnswerError, record_answer
from orq.core.auto_answer import check
from orq.core.models import RunState
from orq.core.settings import resolve
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir

log = logging.getLogger("orq.hub.auto_answer")
FINISHED = {RunState.DONE, RunState.FAILED, RunState.ABORTED}


class AutoAnswerer:
    def __init__(self, store: Store, paths: OrqPaths, config: Config, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.store, self.paths, self.config = store, paths, config
        self._clock, self._sleep = clock, sleep

    async def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad decision row must not stop the hub
                log.exception("automatic answers failed")
            await self._sleep(self.config.queue.poll_seconds)

    def tick(self) -> list[str]:
        answered = []
        now = self._clock()
        overrides = self.store.get_settings()
        for decision in self.store.all_pending_decisions():
            run = self.store.get_run(decision.run_id)
            if run is None or run.state in FINISHED:
                continue
            project = self.store.get_project(run.repo)
            eff = resolve(self.config, overrides, project.settings if project else {}, {})
            sent = self.store.notified_at("decision", decision.decision_id, "whatsapp")
            started = sent if sent is not None else datetime.fromisoformat(decision.created_at or "1970-01-01T00:00:00+00:00").timestamp()
            eligible, reason = check(decision, enabled=bool(eff["notify.auto_answer"]), minutes=eff["notify.auto_answer_minutes"],
                                     max_stakes=eff["notify.auto_answer_max_stakes"], started_at=started, now=now)
            if not eligible:
                continue
            try:
                done = record_answer(self.store, self.paths, decision.decision_id, str(decision.recommendation), via="auto", by="auto")
            except AnswerError:
                continue  # answered by someone else in the meantime
            RunDir(self.paths.run_dir(run.run_id)).event("auto_answered", decision_id=decision.decision_id, answer=done.answer,
                                                          reason=reason, minutes=round((now - started) / 60))
            answered.append(decision.decision_id)
        return answered
