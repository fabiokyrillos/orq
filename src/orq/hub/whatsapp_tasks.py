"""Hub-side WhatsApp loops (SPEC 9.3): outbound decisions and run states with reminders, inbound replies and commands."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from orq.config import Config
from orq.core.answers import AnswerError, record_answer
from orq.core.checkpoint import Checkpoint
from orq.core.control import ControlError, abort_run, request_pause, spawn_resume
from orq.core.models import Decision, RunState
from orq.notify.messages import DESTRUCTIVE_HINT, HINT, format_decision, format_run_state, format_status, parse_reply
from orq.notify.whatsapp import WhatsAppClient
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir

log = logging.getLogger("orq.hub.whatsapp")
CHANNEL = "whatsapp"
CURSOR_KEY = "whatsapp_cursor"
TERMINAL = {RunState.DONE, RunState.FAILED, RunState.ABORTED}


class WhatsAppTasks:
    def __init__(self, store: Store, paths: OrqPaths, config: Config, client: WhatsAppClient, *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 spawn: Callable[[OrqPaths, str], int] = spawn_resume) -> None:
        self.store, self.paths, self.config, self.client = store, paths, config, client
        self._clock, self._sleep, self._spawn = clock, sleep, spawn
        self._baseline()

    def _baseline(self) -> None:
        """Runs that ended before the hub started are old news: mark them without sending."""
        for run in self.store.list_runs():
            if run.state in TERMINAL and self.store.notified_at("run_state", f"{run.run_id}:{run.state.value}", CHANNEL) is None:
                self.store.mark_notified("run_state", f"{run.run_id}:{run.state.value}", CHANNEL)

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
            self.client.send(format_decision(decision, run))
            self.store.mark_notified("decision", decision.decision_id, CHANNEL, at=self._clock())
            RunDir(self.paths.run_dir(run.run_id)).event("notified", channel=CHANNEL, decision_id=decision.decision_id, reminder=last is not None)
            sent += 1
        for run in self.store.list_runs():
            if run.state not in TERMINAL:
                continue
            key = f"{run.run_id}:{run.state.value}"
            if self.store.notified_at("run_state", key, CHANNEL) is not None:
                continue
            cp = Checkpoint.try_load(self.paths.run_dir(run.run_id) / "state.json")
            self.client.send(format_run_state(run, pr_url=cp.pr_url if cp else None))
            self.store.mark_notified("run_state", key, CHANNEL, at=self._clock())
            RunDir(self.paths.run_dir(run.run_id)).event("notified", channel=CHANNEL, state=run.state.value)
            sent += 1
        return sent

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
            if reply.kind == "status":
                runs = [r for r in self.store.list_runs() if reply.run_id is None or r.run_id == reply.run_id]
                return format_status(runs, {r.run_id: self.store.pending_decisions(r.run_id) for r in runs})
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
