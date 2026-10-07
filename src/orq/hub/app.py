"""The hub: local dashboard (FastAPI + SSE) and owner of the WhatsApp channel (SPEC 5, 9, 14).

Loopback only, no auth. Runs are separate processes; the hub reads their run dirs and SQLite and writes
answers and control requests through the same functions the CLI uses.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from orq.config import Config
from orq.core.answers import AnswerError, record_answer
from orq.core.checkpoint import Checkpoint
from orq.core.control import ControlError, abort_run, request_pause, spawn_resume
from orq.core.models import Decision, RunRecord
from orq.hub.whatsapp_tasks import WhatsAppTasks
from orq.notify.whatsapp import WhatsAppClient
from orq.paths import OrqPaths
from orq.store.db import Store

STATIC = Path(__file__).with_name("static")
STREAM_FILES = {"implementer": "implementer.stream.jsonl", "reviewer": "reviewer.stream.jsonl"}


class AnswerBody(BaseModel):
    answer: str


def create_app(paths: OrqPaths, config: Config, *, whatsapp: WhatsAppClient | None = None, start_tasks: bool = True) -> FastAPI:
    store = Store(paths.db)
    tasks = WhatsAppTasks(store, paths, config, whatsapp) if whatsapp is not None else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        handles = []
        if tasks is not None and start_tasks:
            handles = [asyncio.create_task(tasks.outbound_loop()), asyncio.create_task(tasks.inbound_loop())]
        try:
            yield
        finally:
            for handle in handles:
                handle.cancel()

    app = FastAPI(title="orq", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.whatsapp = tasks

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/runs")
    async def runs() -> list[dict]:
        return [run_summary(paths, store, run) for run in store.list_runs()]

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str) -> dict:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        cp = Checkpoint.try_load(paths.run_dir(run_id) / "state.json")
        detail = run_summary(paths, store, run)
        detail["plan"] = cp.plan if cp else None
        detail["phase"] = cp.phase if cp else None
        detail["decisions"] = [decision_dict(d) for d in store.pending_decisions(run_id)]
        detail["whatsapp"] = tasks is not None
        return detail

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, follow: int = 1) -> StreamingResponse:
        path = paths.run_dir(run_id) / "events.jsonl"
        if not path.parent.exists():
            raise HTTPException(404, f"no run {run_id}")
        return StreamingResponse(sse_lines(path, request, follow=bool(follow), condense=None), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/stream/{role}")
    async def run_stream(run_id: str, role: str, request: Request, follow: int = 1) -> StreamingResponse:
        if role not in STREAM_FILES and role != "planner":
            raise HTTPException(404, f"unknown role {role}")
        path = latest_stream_file(paths, run_id, role)
        if path is None:
            raise HTTPException(404, f"no {role} stream yet for {run_id}")
        return StreamingResponse(sse_lines(path, request, follow=bool(follow), condense=condense_stream_line), media_type="text/event-stream")

    @app.post("/api/decisions/{decision_id}/answer")
    async def answer(decision_id: str, body: AnswerBody) -> dict:
        try:
            decision = record_answer(store, paths, decision_id, body.answer, via="dashboard")
        except AnswerError as exc:
            raise HTTPException(400, str(exc))
        return decision_dict(decision)

    @app.post("/api/runs/{run_id}/pause")
    async def pause(run_id: str) -> dict:
        try:
            live = request_pause(paths, run_id)
        except ControlError as exc:
            raise HTTPException(404, str(exc))
        return {"ok": True, "live": live}

    @app.post("/api/runs/{run_id}/abort")
    async def abort(run_id: str) -> dict:
        try:
            warning = abort_run(paths, run_id)
        except ControlError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True, "warning": warning}

    @app.post("/api/runs/{run_id}/resume")
    async def resume(run_id: str) -> dict:
        try:
            pid = spawn_resume(paths, run_id)
        except ControlError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True, "pid": pid}

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


# helpers


def run_summary(paths: OrqPaths, store: Store, run: RunRecord) -> dict:
    cp = Checkpoint.try_load(paths.run_dir(run.run_id) / "state.json")
    milestone = None
    if cp and cp.plan and cp.plan.get("milestones"):
        milestones = cp.plan["milestones"]
        index = min(cp.milestone_index, len(milestones) - 1)
        milestone = {"index": index + 1, "of": len(milestones), "title": milestones[index]["title"], "difficulty": milestones[index]["difficulty"]}
    return {
        "run_id": run.run_id, "repo": run.repo, "task_title": run.task_title, "branch": run.branch, "state": run.state.value,
        "iteration": run.iteration, "updated_at": run.updated_at, "created_at": run.created_at,
        "pending": len(store.pending_decisions(run.run_id)), "milestone": milestone,
        "pr_url": cp.pr_url if cp else None, "phase": cp.phase if cp else None,
    }


def decision_dict(decision: Decision) -> dict:
    return asdict(decision)


def latest_stream_file(paths: OrqPaths, run_id: str, role: str) -> Path | None:
    run_dir = paths.run_dir(run_id)
    if role == "planner":
        path = run_dir / "planner.stream.jsonl"
        return path if path.exists() else None
    cp = Checkpoint.try_load(run_dir / "state.json")
    iteration = cp.iteration if cp else 0
    for it in range(iteration, 0, -1):
        itdir = run_dir / "iterations" / str(it)
        if role == "reviewer" and (itdir / "final_review.stream.jsonl").exists() and cp and cp.phase in ("gate_review", "gate_merge", "done"):
            return itdir / "final_review.stream.jsonl"
        path = itdir / STREAM_FILES[role]
        if path.exists():
            return path
    return None


def condense_stream_line(line: str) -> str | None:
    """One readable line per interesting Claude or Codex stream event; None for noise."""
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    kind = event.get("type")
    if kind == "assistant":  # Claude
        parts = []
        for block in (event.get("message") or {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                tool_input = block.get("input") or {}
                detail = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("pattern") or ""
                parts.append(f"[{block.get('name')}] {str(detail)[:160]}")
        return "\n".join(parts) or None
    if kind == "result":
        return f"[result] {(event.get('result') or '')[:2000]}"
    if kind == "item.completed":  # Codex
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            return item.get("text", "")[:2000]
        if item.get("type") == "command_execution":
            return f"[exec] {item.get('command', '')[:160]}"
        if item.get("type") == "reasoning":
            return f"[reasoning] {str(item.get('text', ''))[:300]}"
        return None
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        return f"[turn done] input {usage.get('input_tokens')} output {usage.get('output_tokens')}"
    return None


async def sse_lines(path: Path, request: Request, *, follow: bool, condense) -> AsyncIterator[str]:
    """Replay a line file as SSE, then follow it until the client goes away (or return when follow is off)."""
    position = 0
    while True:
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                for line in handle:
                    if not line.endswith("\n"):
                        break  # partial write; read it next round
                    position += len(line.encode("utf-8"))
                    text = condense(line) if condense else line.rstrip("\n")
                    if text:
                        payload = "\n".join(f"data: {part}" for part in text.splitlines())
                        yield payload + "\n\n"
        if not follow:
            yield "event: end\ndata: end\n\n"
            return
        if await request.is_disconnected():
            return
        await asyncio.sleep(0.5)
