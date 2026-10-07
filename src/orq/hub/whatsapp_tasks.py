"""Hub-side WhatsApp loops (SPEC 9.3): outbound decisions and run states with reminders, inbound replies and commands."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from collections.abc import Awaitable, Callable

from orq.config import Config
from orq.core.answers import AnswerError, record_answer
from orq.core.checkpoint import Checkpoint
from orq.core.control import ControlError, abort_run, request_pause, spawn_resume
from orq.core.models import Decision, RunState
from orq.core.digest import build_digest
from orq.core.summary import read_events
from orq.notify.messages import (DESTRUCTIVE_HINT, HINT, format_answered_elsewhere, format_decision, format_run_state, format_status,
                                 parse_reply)
from orq.notify.whatsapp import WhatsAppClient
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir

log = logging.getLogger("orq.hub.whatsapp")
CHANNEL = "whatsapp"
CURSOR_KEY = "whatsapp_cursor"
TERMINAL = {RunState.DONE, RunState.FAILED, RunState.ABORTED}
# A timer digest only while the run works; waiting for the owner or a slot is not news (Phase 6).
WORKING = {RunState.PLANNING, RunState.IMPLEMENTING, RunState.VERIFYING, RunState.REVIEWING, RunState.FINALIZING,
           RunState.PAUSED_RATE_LIMIT}


class WhatsAppTasks:
    def __init__(self, store: Store, paths: OrqPaths, config: Config, client: WhatsAppClient, *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 spawn: Callable[[OrqPaths, str], int] = spawn_resume) -> None:
        self.store, self.paths, self.config, self.client = store, paths, config, client
        self._clock, self._sleep, self._spawn = clock, sleep, spawn
        self._baseline()

    def _baseline(self) -> None:
        """Runs that ended and answers given before the hub started are old news: mark them without sending."""
        for decision in self.store.answered_elsewhere(CHANNEL):
            self.store.mark_notified("decision_answered", decision.decision_id, CHANNEL)
        for run in self.store.list_runs():
            if run.state in TERMINAL and self.store.notified_at("run_state", f"{run.run_id}:{run.state.value}", CHANNEL) is None:
                self.store.mark_notified("run_state", f"{run.run_id}:{run.state.value}", CHANNEL)
            if run.state not in TERMINAL:
                # Milestones that ended before the hub started are not announced again; the timer starts now.
                for key in self._milestone_keys(run.run_id):
                    if self.store.notified_at("progress", key, CHANNEL) is None:
                        self.store.mark_notified("progress", key, CHANNEL, at=self._clock())
                if self.store.notified_at("progress", f"{run.run_id}:timer", CHANNEL) is None:
                    self.store.mark_notified("progress", f"{run.run_id}:timer", CHANNEL, at=self._clock())

    def _run_is_live(self, run_id: str) -> bool:
        run = self.store.get_run(run_id)
        return run is not None and run.state not in TERMINAL

    # outbound

    async def outbound_loop(self) -> None:
        while True:
            try:
                self.outbound_once()
            except Exception:  # noqa: BLE001 - n8n may be down; keep the dashboard alive and retry
                log.exception("outbound failed")
            await self._sleep(self.config.notify.outbox_poll_seconds)

    def outbound_once(self) -> int:
        sent = 0
        reminder_seconds = self.config.notify.reminder_hours * 3600
        for decision in self.store.all_pending_decisions():
            if not self._run_is_live(decision.run_id):
                continue  # left behind by an aborted or failed run; nobody can act on it
            last = self.store.notified_at("decision", decision.decision_id, CHANNEL)
            if last is not None and self._clock() - last < reminder_seconds:
                continue
            run = self.store.get_run(decision.run_id)
            if run is None:
                continue
            self.client.send(format_decision(decision, run, milestone=self._milestone(run.run_id)))
            self.store.mark_notified("decision", decision.decision_id, CHANNEL, at=self._clock())
            RunDir(self.paths.run_dir(run.run_id)).event("notified", channel=CHANNEL, decision_id=decision.decision_id, reminder=last is not None)
            sent += 1
        sent += self._progress_once()
        for decision in self.store.answered_elsewhere(CHANNEL):
            # The phone still shows the question; say it is settled so the owner does not answer it again.
            self.client.send(format_answered_elsewhere(decision))
            self.store.mark_notified("decision_answered", decision.decision_id, CHANNEL, at=self._clock())
            sent += 1
        for run in self.store.list_runs():
            if run.state not in TERMINAL:
                continue
            key = f"{run.run_id}:{run.state.value}"
            if self.store.notified_at("run_state", key, CHANNEL) is not None:
                continue
            cp = Checkpoint.try_load(self.paths.run_dir(run.run_id) / "state.json")
            self.client.send(format_run_state(run, pr_url=cp.pr_url if cp else None, reason=self._final_reason(run.run_id)))
            self.store.mark_notified("run_state", key, CHANNEL, at=self._clock())
            RunDir(self.paths.run_dir(run.run_id)).event("notified", channel=CHANNEL, state=run.state.value)
            sent += 1
        return sent

    def _milestone_keys(self, run_id: str) -> list[str]:
        return [f"{run_id}:m{e.get('milestone')}:i{e.get('iteration')}" for e in read_events(self.paths.run_dir(run_id))
                if e.get("type") == "milestone_done"]

    def _progress_once(self) -> int:
        """Digest when a milestone ends, and every [notify].progress_minutes while a run works (Phase 6)."""
        minutes = self.config.notify.progress_minutes
        if minutes <= 0:
            return 0
        sent = 0
        now = self._clock()
        for run in self.store.list_runs():
            if run.state in TERMINAL:
                continue
            run_dir = self.paths.run_dir(run.run_id)
            fresh = [k for k in self._milestone_keys(run.run_id) if self.store.notified_at("progress", k, CHANNEL) is None]
            last = self.store.notified_at("progress", f"{run.run_id}:timer", CHANNEL)
            events = read_events(run_dir)
            started = datetime.fromisoformat(events[0]["ts"]).timestamp() if events else now
            due = run.state in WORKING and now - started >= minutes * 60 and (last is None or now - last >= minutes * 60)
            if not fresh and not due:
                continue
            self.client.send(build_digest(run_dir, repo=run.repo))
            for key in fresh:
                self.store.mark_notified("progress", key, CHANNEL, at=now)
            self.store.mark_notified("progress", f"{run.run_id}:timer", CHANNEL, at=now)
            RunDir(run_dir).event("notified", channel=CHANNEL, kind="progress", milestones=len(fresh))
            sent += 1
        return sent

    def _milestone(self, run_id: str) -> str | None:
        cp = Checkpoint.try_load(self.paths.run_dir(run_id) / "state.json")
        milestones = ((cp.plan or {}).get("milestones") or []) if cp else []
        return f"{min(cp.milestone_index, len(milestones) - 1) + 1}/{len(milestones)}" if cp and milestones else None

    def _final_reason(self, run_id: str) -> str | None:
        """Why a run ended: the reason of its last state event (FAILED and ABORTED carry one)."""
        for event in reversed(read_events(self.paths.run_dir(run_id))):
            if event.get("type") == "state":
                return event.get("reason")
        return None

    # inbound

    async def inbound_loop(self) -> None:
        while True:
            try:
                self.inbound_once()
            except Exception:  # noqa: BLE001
                log.exception("inbound failed")
            await self._sleep(self.config.notify.poll_seconds)

    def inbound_once(self) -> int:
        since = int(self.store.kv_get(CURSOR_KEY, "0") or 0)
        messages = self.client.replies(since)
        if not messages:
            return 0
        for message in messages:
            reply = self.handle(message.text)
            if reply:
                self.client.send(reply)
        self.client.ack([m.id for m in messages])
        self.store.kv_set(CURSOR_KEY, str(max(m.id for m in messages)))
        return len(messages)

    def handle(self, text: str) -> str | None:
        """Apply one owner message and return the confirmation or hint to send back."""
        reply = parse_reply(text)
        try:
            if reply.kind == "status" and reply.run_id:
                if self.store.get_run(reply.run_id) is None:
                    return f"[orq] no run {reply.run_id}"
                return build_digest(self.paths.run_dir(reply.run_id), repo=self.store.get_run(reply.run_id).repo)  # type: ignore[union-attr]
            if reply.kind == "status":
                every = self.store.list_runs()
                runs = [r for r in every if reply.run_id is None or r.run_id == reply.run_id]
                held = self.store.held_slots()
                queued = sum(1 for r in every if r.state is RunState.QUEUED)
                return format_status(runs, {r.run_id: self.store.pending_decisions(r.run_id) for r in runs},
                                     slots=(held, self.config.limits.max_concurrent_runs), queued=queued)
            if reply.kind == "pause":
                live = request_pause(self.paths, reply.run_id or "")
                return f"{reply.run_id}: pause requested" + ("" if live else " (no live process; it stays paused until RESUME)")
            if reply.kind == "resume":
                pid = self._spawn(self.paths, reply.run_id or "")
                return f"{reply.run_id}: resuming (pid {pid})"
            if reply.kind == "abort":
                warning = abort_run(self.paths, reply.run_id or "")
                return f"{reply.run_id}: aborted" + (f" ({warning})" if warning else "")
            if reply.kind in ("answer_index", "answer_text", "approve", "deny"):
                return self._answer(reply.kind, reply.decision_id or "", reply.index, reply.text)
        except (ControlError, AnswerError) as exc:
            return f"[orq] {exc}"
        return "[orq] " + HINT

    def _answer(self, kind: str, decision_id: str, index: int | None, text: str) -> str:
        decision = self.store.get_decision(decision_id)
        if decision is None:
            return f"[orq] unknown decision {decision_id}"
        if decision.destructive and kind not in ("approve", "deny"):
            return "[orq] " + DESTRUCTIVE_HINT.format(id=decision_id)
        if kind == "answer_index":
            if index is None or not 1 <= index <= len(decision.options):
                options = " ".join(f"{i + 1}={o}" for i, o in enumerate(decision.options)) or "(no numbered options; reply with text)"
                return f"[orq] {decision_id}: pick 1..{len(decision.options)}: {options}"
            value = decision.options[index - 1]
        elif kind in ("approve", "deny"):
            value = kind
        else:
            value = text
        answered = record_answer(self.store, self.paths, decision_id, value, via=CHANNEL)
        return f"[orq] {decision_id} answered: {answered.answer}. The run continues."


def pending_for(store: Store, run_id: str) -> list[Decision]:
    return store.pending_decisions(run_id)
