"""The hub: local dashboard (FastAPI + SSE), queue dispatcher and owner of the WhatsApp channel (SPEC 5, 9, 14).

Loopback only. Runs are separate processes; the hub reads their run dirs and SQLite and writes answers, control
requests and queued tasks through the same functions the CLI uses. Since the hub can start agents (Phase 5), every
non-GET request needs the per-start token embedded in the page, and requests for any other Host are refused
(DNS rebinding).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from orq.config import Config
from orq.core.answers import AnswerError, record_answer
from orq.core.checkpoint import Checkpoint
from orq.core.control import ControlError, abort_run, request_pause, spawn_resume
from orq.core.models import Decision, Project, RunRecord, RunState
from orq.core.projects import ProjectError, add_project
from orq.core.settings import EFFORTS, KEYS, LIVE_KEYS, TASK_ALIASES, SettingsError, codex_models, resolve, validate_overrides
from orq.core.queue import QueueError, enqueue
from orq.core.digest import build_digest
from orq.core.summary import build_replay, load_summary
from orq.core.task import TaskError, parse_task
from orq.core.taskfile import TaskFields, render_task
from orq.adapters.probe import probe_model
from orq.git.manager import GitManager
from orq.hub.auto_answer import AutoAnswerer
from orq.hub.dispatcher import Dispatcher
from orq.hub.whatsapp_tasks import WhatsAppTasks
from orq.notify.whatsapp import WhatsAppClient
from orq.paths import OrqPaths
from orq.store.db import Store

STATIC = Path(__file__).with_name("static")
STREAM_FILES = {"implementer": "implementer.stream.jsonl", "reviewer": "reviewer.stream.jsonl"}
TOKEN_HEADER = "X-Orq-Token"
TERMINAL = {RunState.DONE, RunState.FAILED, RunState.ABORTED}
ARTIFACT_LIMIT = 400_000  # characters; longer files are cut to their tail
STATE_GROUPS = {
    "running": {RunState.PLANNING, RunState.IMPLEMENTING, RunState.VERIFYING, RunState.REVIEWING, RunState.FINALIZING,
                RunState.PAUSED_RATE_LIMIT},
    "waiting": {RunState.AWAITING_HUMAN, RunState.AWAITING_PLAN_APPROVAL, RunState.PAUSED},
    "queued": {RunState.QUEUED},
    "done": {RunState.DONE},
    "failed": {RunState.FAILED, RunState.ABORTED},
}


class AnswerBody(BaseModel):
    answer: str
    by: str = "owner"  # the page answers for the owner; Claude passes "claude" when it answers during tests


class ProjectBody(BaseModel):
    source: str
    name: str | None = None
    base_branch: str | None = None
    check_command: str | None = None
    max_concurrent: int | None = None


class ProjectPatch(BaseModel):
    name: str | None = None
    base_branch: str | None = None
    check_command: str | None = None
    max_concurrent: int | None = None
    settings: dict | None = None  # Phase 6 overrides; a None value clears that key


class MoveBody(BaseModel):
    to: str


class TaskTextBody(BaseModel):
    markdown: str


class ModelTestBody(BaseModel):
    kind: str
    model: str
    effort: str = "low"


class FieldsBody(BaseModel):
    title: str = ""
    goal: str = ""
    acceptance_criteria: list[str] = []
    out_of_scope: list[str] = []
    constraints: list[str] = []
    check_command: str = ""
    base_branch: str = ""
    plan_approval: str = "required"
    models: dict[str, str] = {}


class TaskBody(BaseModel):
    project: str
    fields: FieldsBody | None = None
    markdown: str | None = None


def create_app(paths: OrqPaths, config: Config, *, whatsapp: WhatsAppClient | None = None, start_tasks: bool = True,
               git: GitManager | None = None, allowed_hosts: set[str] | None = None, token: str | None = None,
               models_cache: Path | None = None, probe=None) -> FastAPI:
    store = Store(paths.db)
    git = git or GitManager()
    tasks = WhatsAppTasks(store, paths, config, whatsapp) if whatsapp is not None else None
    dispatcher = Dispatcher(store, paths, config)
    port = config.dashboard.port
    hosts = allowed_hosts or {f"127.0.0.1:{port}", f"localhost:{port}"}
    secret = token or secrets.token_urlsafe(32)
    probe = probe or (lambda kind, model, effort: probe_model(kind, model, effort=effort,
                                                              windows_sandbox=config.reviewer.codex_windows_sandbox))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        handles = []
        if start_tasks:
            handles.append(asyncio.create_task(dispatcher.loop()))
            handles.append(asyncio.create_task(AutoAnswerer(store, paths, config).loop()))
        if tasks is not None and start_tasks:
            handles += [asyncio.create_task(tasks.outbound_loop()), asyncio.create_task(tasks.inbound_loop())]
        try:
            yield
        finally:
            for handle in handles:
                handle.cancel()

    app = FastAPI(title="orq", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.whatsapp = tasks
    app.state.token = secret
    app.state.dispatcher = dispatcher

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in hosts:
            return JSONResponse({"error": "unknown host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), secret):
            return JSONResponse({"error": "missing or wrong request token; reload the page"}, status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        # Versioned asset URLs: after an orq update the browser must not keep the old script from its cache.
        version = int(max((STATIC / name).stat().st_mtime for name in ("app.js", "style.css")))
        page = (STATIC / "index.html").read_text(encoding="utf-8").replace("__ORQ_TOKEN__", secret)
        return page.replace("/static/app.js", f"/static/app.js?v={version}").replace("/static/style.css", f"/static/style.css?v={version}")

    @app.get("/api/runs")
    async def runs(project: str | None = None) -> list[dict]:
        return [run_summary(paths, store, run) for run in store.list_runs() if project is None or run.repo == project]

    # projects

    def project_dict(project: Project, counts: Counter | None = None) -> dict:
        data = asdict(project)
        data["effective_max_concurrent"] = project.max_concurrent or config.queue.project_concurrency
        data["counts"] = {group: (counts or Counter())[group] for group in STATE_GROUPS}
        eff = resolve(config, store.get_settings(), project.settings, {})
        data["effective"], data["sources"] = eff.values, eff.sources
        return data

    def validated(overrides: dict, base: dict) -> dict:
        """Validate a change against the layer it lands in; efforts are checked on the model that will run them."""
        merged = {**base, **overrides}
        codex_model = merged.get("reviewer.codex_model") or resolve(config, store.get_settings(), {}, {})["reviewer.codex_model"]
        try:
            validate_overrides(overrides, cache_path=models_cache, codex_model=codex_model)
            clean = validate_overrides({k: v for k, v in merged.items() if v is not None}, cache_path=models_cache, codex_model=codex_model)
        except SettingsError as exc:
            raise HTTPException(400, str(exc))
        return {k: v for k, v in clean.items() if v is not None}

    def settings_payload() -> dict:
        eff = resolve(config, store.get_settings(), {}, {})
        return {"overrides": store.get_settings(), "effective": eff.values, "sources": eff.sources, "keys": list(KEYS),
                "live_keys": list(LIVE_KEYS), "efforts": list(EFFORTS), "codex_models": codex_models(models_cache),
                "claude_models": ["opus", "sonnet", "haiku"], "task_aliases": TASK_ALIASES}

    @app.get("/api/settings")
    async def get_settings() -> dict:
        return settings_payload()

    @app.put("/api/settings")
    async def put_settings(body: dict) -> dict:
        current = store.get_settings()
        keep = validated(body, current)
        store.set_settings({**{k: None for k in current if k not in keep}, **keep})
        return settings_payload()

    @app.get("/api/models")
    async def models() -> dict:
        return {"codex": codex_models(models_cache), "claude": ["opus", "sonnet", "haiku"], "efforts": list(EFFORTS)}

    @app.post("/api/models/test")
    async def test_model(body: ModelTestBody) -> dict:
        if body.kind not in ("codex", "claude"):
            raise HTTPException(400, f"unknown model kind {body.kind}")
        ok, message = await asyncio.to_thread(probe, body.kind, body.model, body.effort)
        return {"ok": ok, "message": message}

    @app.get("/api/projects")
    async def projects() -> list[dict]:
        counts: dict[str, Counter] = {}
        for run in store.list_runs():
            group = next((g for g, members in STATE_GROUPS.items() if run.state in members), None)
            if group:
                counts.setdefault(run.repo, Counter())[group] += 1
        return [project_dict(p, counts.get(p.repo)) for p in store.list_projects()]

    @app.post("/api/projects")
    async def create_project(body: ProjectBody) -> dict:
        try:
            project = await asyncio.to_thread(add_project, store, git, body.source, name=body.name, base_branch=body.base_branch,
                                              check_command=body.check_command, max_concurrent=body.max_concurrent)
        except ProjectError as exc:
            raise HTTPException(400, str(exc))
        return project_dict(project)

    @app.patch("/api/projects/{owner}/{repo}")
    async def patch_project(owner: str, repo: str, body: ProjectPatch) -> dict:
        slug = f"{owner}/{repo}"
        if store.get_project(slug) is None:
            raise HTTPException(404, f"no project {slug}")
        changes = body.model_dump(exclude_unset=True)
        if "settings" in changes:
            changes["settings"] = validated(changes["settings"] or {}, store.get_project(slug).settings)  # type: ignore[union-attr]
        store.update_project(slug, **changes)
        return project_dict(store.get_project(slug))  # type: ignore[arg-type]

    # tasks and queue

    def task_text(body: TaskBody) -> str:
        project = store.get_project(body.project)
        if project is None:
            raise HTTPException(400, f"no project {body.project}")
        if body.markdown is not None:
            return body.markdown
        if body.fields is None:
            raise HTTPException(400, "give fields or markdown")
        f = body.fields
        return render_task(TaskFields(
            title=f.title, repo=project.repo, goal=f.goal, check_command=f.check_command or project.check_command,
            base_branch=f.base_branch or project.base_branch, acceptance_criteria=f.acceptance_criteria,
            out_of_scope=f.out_of_scope, constraints=f.constraints, plan_approval=f.plan_approval, models=f.models))

    def validate(body: TaskBody, text: str) -> list[str]:
        try:
            task = parse_task(text)
        except TaskError as exc:
            return [str(exc)]
        if task.repo != body.project:
            return [f"the task names {task.repo} but the project is {body.project}"]
        if task.models:
            project = store.get_project(body.project)
            base = resolve(config, store.get_settings(), project.settings if project else {}, {})
            overrides = {TASK_ALIASES[k]: v for k, v in task.models.items()}
            try:
                validate_overrides(overrides, cache_path=models_cache,
                                   codex_model=overrides.get("reviewer.codex_model") or base["reviewer.codex_model"])
            except SettingsError as exc:
                return [f"Models: {exc}"]
        return []

    @app.post("/api/tasks/preview")
    async def preview_task(body: TaskBody) -> dict:
        text = task_text(body)
        return {"markdown": text, "errors": validate(body, text)}

    @app.post("/api/tasks")
    async def create_task(body: TaskBody) -> dict:
        text = task_text(body)
        errors = validate(body, text)
        if errors:
            raise HTTPException(400, "; ".join(errors))
        try:
            run_id = enqueue(paths, store, config, text)
        except (TaskError, QueueError) as exc:
            raise HTTPException(400, str(exc))
        return {"run_id": run_id}

    @app.post("/api/runs/{run_id}/rerun")
    async def rerun(run_id: str) -> dict:
        path = paths.run_dir(run_id) / "TASK.md"
        if not path.exists():
            raise HTTPException(404, f"no run {run_id}")
        try:
            new_id = enqueue(paths, store, config, path.read_text(encoding="utf-8"))
        except (TaskError, QueueError) as exc:
            raise HTTPException(400, f"cannot queue {run_id} again: {exc}")
        return {"run_id": new_id}

    @app.get("/api/queue")
    async def queue() -> dict:
        rows = store.slot_usage(alive=dispatcher._alive)
        queued = store.queued_runs()
        return {"limit": config.limits.max_concurrent_runs, "held": sum(1 for r in rows if r["held"]),
                "slots": rows, "queued": [run_summary(paths, store, r) for r in queued]}

    def queued_or_409(run_id: str) -> RunRecord:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        cp = Checkpoint.try_load(paths.run_dir(run_id) / "state.json")
        if run.state is not RunState.QUEUED or cp is None or cp.phase != "setup" or dispatcher._alive(cp.pid):
            raise HTTPException(409, f"{run_id} has started; only a queued run that has not started can change")
        return run

    @app.post("/api/runs/{run_id}/move")
    async def move(run_id: str, body: MoveBody) -> dict:
        queued_or_409(run_id)
        try:
            store.move_in_queue(run_id, body.to)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.put("/api/runs/{run_id}/task")
    async def edit_task(run_id: str, body: TaskTextBody) -> dict:
        run = queued_or_409(run_id)
        try:
            task = parse_task(body.markdown)
        except TaskError as exc:
            raise HTTPException(400, str(exc))
        if task.repo != run.repo:
            raise HTTPException(400, f"the task names {task.repo} but the run belongs to {run.repo}")
        (paths.run_dir(run_id) / "TASK.md").write_text(body.markdown, encoding="utf-8")
        cp = Checkpoint.load(paths.run_dir(run_id) / "state.json")
        cp.branch = f"orq/{task.slug}"  # type: ignore[union-attr]
        cp.save(paths.run_dir(run_id) / "state.json", pid=0)  # type: ignore[union-attr]
        store.set_task(run_id, task.title, cp.branch)  # type: ignore[union-attr]
        return run_summary(paths, store, store.get_run(run_id))  # type: ignore[arg-type]

    # summary and replay

    def run_dir_or_404(run_id: str) -> Path:
        run_dir = paths.run_dir(run_id)
        if not run_dir.exists():
            raise HTTPException(404, f"no run {run_id}")
        return run_dir

    @app.get("/api/runs/{run_id}/summary")
    async def summary(run_id: str) -> dict:
        return load_summary(run_dir_or_404(run_id))

    @app.get("/api/runs/{run_id}/digest")
    async def digest(run_id: str) -> dict:
        run = store.get_run(run_id)
        return {"text": build_digest(run_dir_or_404(run_id), repo=run.repo if run else None)}

    @app.get("/api/runs/{run_id}/replay")
    async def replay(run_id: str) -> dict:
        return build_replay(run_dir_or_404(run_id))

    @app.get("/api/runs/{run_id}/artifact")
    async def artifact(run_id: str, path: str) -> dict:
        run_dir = run_dir_or_404(run_id).resolve()
        target = (run_dir / path).resolve()
        if not target.is_relative_to(run_dir):
            raise HTTPException(400, "path outside the run directory")
        if not target.is_file():
            raise HTTPException(404, f"no {path} in {run_id}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if target.name.endswith(".stream.jsonl"):
            text = "\n".join(line for line in map(condense_stream_line, text.splitlines(keepends=True)) if line)
        truncated = len(text) > ARTIFACT_LIMIT
        return {"path": path, "text": text[-ARTIFACT_LIMIT:], "truncated": truncated}

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str) -> dict:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        cp = Checkpoint.try_load(paths.run_dir(run_id) / "state.json")
        detail = run_summary(paths, store, run)
        detail["plan"] = cp.plan if cp else None
        detail["phase"] = cp.phase if cp else None
        detail["decisions"] = [decision_dict(d) for d in store.pending_decisions(run_id)] if run.state not in TERMINAL else []
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
            if body.by not in ("owner", "claude"):
                raise AnswerError("by must be owner or claude")
            decision = record_answer(store, paths, decision_id, body.answer, via="dashboard", by=body.by)
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
        "pending": 0 if run.state in TERMINAL else len(store.pending_decisions(run.run_id)), "milestone": milestone,
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
            return _schema_message(item.get("text", ""))
        if item.get("type") == "command_execution":
            return f"[exec] {item.get('command', '')[:160]}"
        if item.get("type") == "reasoning":
            return f"[reasoning] {str(item.get('text', ''))[:300]}"
        return None
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        return f"[turn done] input {usage.get('input_tokens')} output {usage.get('output_tokens')}"
    return None


def _schema_message(text: str) -> str:
    """Codex forces every message into the reviewer or planner schema; show its status and summary, not the JSON."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:2000]
    if not isinstance(data, dict) or "summary" not in data:
        return text[:2000]
    line = f"[{data.get('status', '')}] {data['summary']}".strip()
    question = (data.get("human") or {}).get("question")
    return (line + (f"\nquestion: {question}" if question else ""))[:2000]


async def sse_lines(path: Path, request: Request, *, follow: bool, condense) -> AsyncIterator[str]:
    """Replay a line file as SSE, then follow it until the client goes away (or return when follow is off)."""
    position = 0
    while True:
        if path.exists():
            # Bytes, not text: run logs are written in text mode, so on Windows lines end with CRLF and a text-mode
            # offset would drift by one byte per line and land inside an earlier line on the next round.
            with path.open("rb") as handle:
                handle.seek(position)
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        break  # partial write; read it next round
                    position += len(raw)
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n") + "\n"
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
