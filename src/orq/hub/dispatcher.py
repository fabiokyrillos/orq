"""Hub dispatcher (Phase 5): start queued runs while the global and per-project limits leave room.

Phase 6: it also resumes runs whose decision was answered while they had no process, so a WhatsApp answer no longer
needs a RESUME. PAUSED runs are never resumed automatically.

The run process takes its own slot (`Store.try_acquire_slot`); the dispatcher only avoids spawning processes that would
sit waiting. A run spawned less than SPAWN_GRACE_SECONDS ago that has not registered yet counts as taking a slot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable

from orq.config import Config
from orq.core.checkpoint import Checkpoint
from orq.core.control import ControlError, spawn_resume
from orq.core.models import RunState
from orq.core.procs import pid_alive
from orq.paths import OrqPaths
from orq.core.settings import slot_limits
from orq.store.db import Store

log = logging.getLogger("orq.hub.dispatcher")
SPAWN_GRACE_SECONDS = 60
# Queued runs, and (Phase 6) runs parked at a decision that has been answered while no process waits for it.
DISPATCHABLE = {RunState.QUEUED, RunState.AWAITING_HUMAN, RunState.AWAITING_PLAN_APPROVAL}


class Dispatcher:
    def __init__(self, store: Store, paths: OrqPaths, config: Config, *, spawn: Callable[[OrqPaths, str], int] = spawn_resume,
                 clock: Callable[[], float] = time.monotonic, alive: Callable[[int], bool] = pid_alive,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.store, self.paths, self.config = store, paths, config
        self._spawn, self._clock, self._alive, self._sleep = spawn, clock, alive, sleep
        self._spawned: dict[str, float] = {}

    async def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep dispatching after a bad run dir
                log.exception("dispatch failed")
            await self._sleep(self.config.queue.poll_seconds)

    def _cap(self, repo: str, default: int) -> int:
        project = self.store.get_project(repo)
        if project is not None and project.max_concurrent:
            return project.max_concurrent
        return default

    def _answered(self, cp: Checkpoint) -> bool:
        """A run parked at a decision whose process is gone (--no-prompt, crash, reboot) and whose answer has arrived."""
        pending = cp.pending_decision or {}
        decision = self.store.get_decision(pending.get("decision_id", "")) if pending else None
        return cp.phase == "await" and decision is not None and decision.status == "answered"

    def tick(self) -> list[str]:
        """Spawn what fits now; return the run ids spawned."""
        now = self._clock()
        self._spawned = {r: t for r, t in self._spawned.items() if now - t < SPAWN_GRACE_SECONDS}
        registered = {row["run_id"]: row for row in self.store.slot_usage() if self._alive(row["pid"])}
        for run_id in registered:
            self._spawned.pop(run_id, None)  # it registered: from now on the slot table speaks for it (Phase 6 finding)
        demand = Counter(row["repo"] for row in registered.values())  # held or waiting: both will use a slot
        total = len(registered)
        queued = []
        for run in sorted(self.store.list_runs(), key=lambda r: r.queue_order):  # queue order (oldest first unless moved)
            if run.state not in DISPATCHABLE or run.run_id in registered:
                continue
            if run.run_id in self._spawned:
                demand[run.repo] += 1
                total += 1
                continue
            cp = Checkpoint.try_load(self.paths.run_dir(run.run_id) / "state.json")
            if cp is None or self._alive(cp.pid):
                continue
            if run.state is not RunState.QUEUED and not self._answered(cp):
                continue  # still waiting for the owner, or paused on purpose
            queued.append((cp.phase == "setup", run))
        queued.sort(key=lambda item: item[0])  # runs that already started first; stable keeps the age order
        global_limit, per_project = slot_limits(self.config, self.store)  # the dashboard may change them (Phase 7.1)
        spawned = []
        for _, run in queued:
            if total >= global_limit:
                break
            if demand[run.repo] >= self._cap(run.repo, per_project):
                continue
            try:
                self._spawn(self.paths, run.run_id)
            except ControlError as exc:
                log.warning("not spawned %s: %s", run.run_id, exc)
                continue
            self._spawned[run.run_id] = now
            demand[run.repo] += 1
            total += 1
            spawned.append(run.run_id)
        return spawned
