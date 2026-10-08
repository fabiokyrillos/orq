"""Dashboard API and WhatsApp hub tasks, with a fake n8n client and seeded run dirs."""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orq.config import Config
from orq.core.checkpoint import Checkpoint
from orq.core.models import Decision, RunRecord, RunState
from orq.git.manager import GitError, GitManager
from orq.hub.app import condense_stream_line, create_app
from orq.hub.whatsapp_tasks import WhatsAppTasks
from orq.notify.whatsapp import InboundMessage
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.inbox: list[InboundMessage] = []
        self.acked: list[list[int]] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def replies(self, since: int) -> list[InboundMessage]:
        return [m for m in self.inbox if m.id > since]

    def ack(self, ids: list[int]) -> None:
        self.acked.append(list(ids))


@pytest.fixture
def home(tmp_path: Path) -> OrqPaths:
    paths = OrqPaths(tmp_path / "home")
    paths.root.mkdir()
    return paths


def seed(paths: OrqPaths, run_id: str = "RAAAAA", state: RunState = RunState.AWAITING_HUMAN, phase: str = "await", pid: int = 999999,
         decision: Decision | None = None, plan: bool = True) -> Store:
    store = Store(paths.db)
    store.create_run(RunRecord(run_id=run_id, repo="owner/sandbox", task_title="Add greeting", branch="orq/add-greeting", iteration=2))
    store.set_state(run_id, state)
    store.set_iteration(run_id, 2)
    rundir = RunDir(paths.run_dir(run_id))
    rundir.create("# Task: Add greeting\n")
    rundir.event("state", state="IMPLEMENTING", iteration=1)
    rundir.event("state", state=state.value)
    itdir = rundir.iteration(2)
    (itdir / "implementer.stream.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}) + "\n"
        + json.dumps({"type": "result", "result": "Done with greeting"}) + "\n", encoding="utf-8")
    (itdir / "reviewer.stream.jsonl").write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Looks fine"}}) + "\n", encoding="utf-8")
    cp = Checkpoint(run_id=run_id, state=state.value, phase=phase, branch="orq/add-greeting", worktree=str(paths.root / "wt"), iteration=2)
    if plan:
        cp.plan = {"summary": "s", "milestones": [{"title": "write it", "goal": "g", "done_when": "d", "difficulty": "hard"}]}
    cp.pr_url = "https://github.com/owner/sandbox/pull/9"
    cp.save(rundir.path / "state.json")
    data = json.loads((rundir.path / "state.json").read_text(encoding="utf-8"))
    data["pid"] = pid
    (rundir.path / "state.json").write_text(json.dumps(data), encoding="utf-8")
    if decision:
        store.add_decision(decision)
    return store


def business() -> Decision:
    return Decision(decision_id="DBBBB", run_id="RAAAAA", source="reviewer", decision_type="business",
                    question="Before or after tax?", options=["before", "after"], recommendation=1)


def destructive() -> Decision:
    return Decision(decision_id="DGGGG", run_id="RAAAAA", source="guard", decision_type="risk", destructive=True,
                    question="The implementer tried: rm -rf build. Allow it once?", options=["approve", "deny"], recommendation=1)


def fake_git() -> GitManager:
    def gh(args: list[str], cwd: Path) -> str:
        if args[:2] == ["repo", "view"]:
            if args[2] == "owner/missing":
                raise GitError("gh repo view failed (1): Could not resolve to a Repository")
            return json.dumps({"nameWithOwner": args[2], "defaultBranchRef": {"name": "main"}})
        raise AssertionError(args)
    return GitManager(gh=gh)


def client(paths: OrqPaths, whatsapp=None, config: Config | None = None, token: bool = True) -> TestClient:
    app = create_app(paths, config or Config(), whatsapp=whatsapp, start_tasks=False, git=fake_git(), allowed_hosts={"testserver"})
    return TestClient(app, headers={"X-Orq-Token": app.state.token} if token else {})


# API


def test_runs_and_detail(home: OrqPaths) -> None:
    seed(home, decision=business())
    c = client(home)
    runs = c.get("/api/runs").json()
    assert runs[0]["run_id"] == "RAAAAA" and runs[0]["pending"] == 1 and runs[0]["milestone"]["title"] == "write it"
    detail = c.get("/api/runs/RAAAAA").json()
    assert detail["decisions"][0]["decision_id"] == "DBBBB" and detail["phase"] == "await" and detail["pr_url"].endswith("/pull/9")
    assert detail["whatsapp"] is False
    assert c.get("/api/runs/RNOPE1").status_code == 404


def test_index_serves_ui(home: OrqPaths) -> None:
    seed(home)
    c = client(home)
    html = c.get("/").text
    assert "<title>orq</title>" in html and "/static/app.js" in html and "__ORQ_TOKEN__" not in html
    assert "EventSource" in c.get("/static/app.js").text and "--accent" in c.get("/static/style.css").text


def test_answer_endpoint_records_via_dashboard(home: OrqPaths) -> None:
    store = seed(home, decision=business())
    c = client(home)
    r = c.post("/api/decisions/DBBBB/answer", json={"answer": "1"})
    assert r.status_code == 200 and r.json()["answer"] == "after" and r.json()["answered_via"] == "dashboard"
    assert store.get_decision("DBBBB").status == "answered"
    assert c.post("/api/decisions/DBBBB/answer", json={"answer": "x"}).status_code == 400
    assert c.post("/api/decisions/DNOPE/answer", json={"answer": "x"}).status_code == 400


def test_pause_abort_resume_endpoints(home: OrqPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(home)
    c = client(home)
    assert c.post("/api/runs/RAAAAA/pause").json() == {"ok": True, "live": False}
    assert (home.run_dir("RAAAAA") / "pause.requested").exists()
    spawned: list[str] = []
    monkeypatch.setattr("orq.hub.app.spawn_resume", lambda paths, run_id: spawned.append(run_id) or 4242)
    assert c.post("/api/runs/RAAAAA/resume").json() == {"ok": True, "pid": 4242} and spawned == ["RAAAAA"]
    assert c.post("/api/runs/RAAAAA/abort").json()["ok"] is True
    assert Store(home.db).get_run("RAAAAA").state is RunState.ABORTED
    assert c.post("/api/runs/RNOPE1/pause").status_code == 404


def test_abort_refuses_live_run(home: OrqPaths) -> None:
    seed(home, pid=os.getpid())
    r = client(home).post("/api/runs/RAAAAA/abort")
    assert r.status_code == 409 and "still running" in r.json()["error"]


def test_events_and_streams_replay_as_sse(home: OrqPaths) -> None:
    seed(home)
    c = client(home)
    events = c.get("/api/runs/RAAAAA/events?follow=0").text
    assert '"state": "IMPLEMENTING"' in events and events.strip().endswith("data: end")
    impl = c.get("/api/runs/RAAAAA/stream/implementer?follow=0").text
    assert "data: Working on it" in impl and "data: [Bash] ls" in impl and "data: [result] Done with greeting" in impl
    rev = c.get("/api/runs/RAAAAA/stream/reviewer?follow=0").text
    assert "data: Looks fine" in rev
    assert c.get("/api/runs/RAAAAA/stream/planner?follow=0").status_code == 404
    assert c.get("/api/runs/RAAAAA/stream/nope?follow=0").status_code == 404


def test_condense_ignores_noise() -> None:
    assert condense_stream_line("not json") is None
    assert condense_stream_line(json.dumps({"type": "system", "subtype": "init"})) is None
    assert condense_stream_line(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}})) == "[turn done] input 5 output 2"


# WhatsApp tasks


def tasks(paths: OrqPaths, store: Store, fake: FakeClient, clock=lambda: 1000.0, spawn=None) -> WhatsAppTasks:
    config = Config()
    return WhatsAppTasks(store, paths, config, fake, clock=clock, spawn=spawn or (lambda p, r: 777))


def test_outbound_sends_pending_decision_once_then_reminds(home: OrqPaths) -> None:
    store = seed(home, decision=business())
    fake = FakeClient()
    now = [1000.0]
    t = tasks(home, store, fake, clock=lambda: now[0])
    assert t.outbound_once() == 1 and "*[orq] DBBBB · sandbox · iteration 2 · milestone 1/1*" in fake.sent[0]
    assert "2. after (recommended)" in fake.sent[0]
    assert t.outbound_once() == 0
    now[0] += 3 * 3600 + 1
    assert t.outbound_once() == 1 and len(fake.sent) == 2
    events = (home.run_dir("RAAAAA") / "events.jsonl").read_text(encoding="utf-8")
    assert '"reminder": true' in events


def test_outbound_announces_terminal_states_but_not_old_ones(home: OrqPaths) -> None:
    store = seed(home, state=RunState.DONE, phase="done")
    fake = FakeClient()
    t = tasks(home, store, fake)             # DONE before the hub started: baseline, not sent
    assert t.outbound_once() == 0
    store.create_run(RunRecord(run_id="RBBBBB", repo="o/r", task_title="Later", branch="b"))
    RunDir(home.run_dir("RBBBBB")).create("# Task: Later\n")
    store.set_state("RBBBBB", RunState.FAILED)
    assert t.outbound_once() == 1 and "RBBBBB · r · FAILED" in fake.sent[0]
    assert t.outbound_once() == 0


def test_inbound_answers_commands_and_cursor(home: OrqPaths) -> None:
    store = seed(home, decision=business())
    fake = FakeClient()
    fake.inbox = [InboundMessage(1, "DBBBB 2"), InboundMessage(2, "STATUS"), InboundMessage(3, "hello?")]
    t = tasks(home, store, fake)

    assert t.inbound_once() == 3
    assert store.get_decision("DBBBB").answer == "after" and store.get_decision("DBBBB").answered_via == "whatsapp"
    assert fake.sent[0].startswith("[orq] DBBBB answered: after")
    assert fake.sent[1].startswith("*[orq] STATUS*") and "RAAAAA" in fake.sent[1]
    assert "Reply with the decision ID" in fake.sent[2]
    assert fake.acked == [[1, 2, 3]] and store.kv_get("whatsapp_cursor") == "3"
    assert t.inbound_once() == 0  # nothing new past the cursor


def test_inbound_destructive_requires_approve_or_deny(home: OrqPaths) -> None:
    store = seed(home, decision=destructive())
    fake = FakeClient()
    fake.inbox = [InboundMessage(1, "DGGGG 1"), InboundMessage(2, "DGGGG yes please"), InboundMessage(3, "APPROVE DGGGG")]
    t = tasks(home, store, fake)
    t.inbound_once()
    assert "reply exactly `APPROVE DGGGG`" in fake.sent[0] and "reply exactly" in fake.sent[1]
    assert store.get_decision("DGGGG").answer == "approve"
    assert fake.sent[2].startswith("[orq] DGGGG answered: approve")


def test_inbound_index_out_of_range_and_unknown_decision(home: OrqPaths) -> None:
    store = seed(home, decision=business())
    fake = FakeClient()
    t = tasks(home, store, fake)
    assert "pick 1..2: 1=before 2=after" in t.handle("DBBBB 9")
    assert "unknown decision DZZZZ" in t.handle("DZZZZ 1")
    assert "answered: before" in t.handle("DBBBB 1")
    assert "already answered" in t.handle("DBBBB 2")


def test_inbound_run_commands(home: OrqPaths) -> None:
    store = seed(home)
    fake = FakeClient()
    spawned: list[str] = []
    t = tasks(home, store, fake, spawn=lambda p, r: spawned.append(r) or 55)
    assert "pause requested" in t.handle("PAUSE RAAAAA") and (home.run_dir("RAAAAA") / "pause.requested").exists()
    assert t.handle("RESUME RAAAAA") == "RAAAAA: resuming (pid 55)" and spawned == ["RAAAAA"]
    assert t.handle("ABORT RAAAAA") == "RAAAAA: aborted" and store.get_run("RAAAAA").state is RunState.ABORTED
    assert "no run RNOPE1" in t.handle("ABORT RNOPE1")


def test_inbound_errors_do_not_stop_the_loop(home: OrqPaths) -> None:
    store = seed(home)

    class Broken(FakeClient):
        def replies(self, since: int):
            raise RuntimeError("n8n down")

    fake = Broken()
    t = tasks(home, store, fake)
    with pytest.raises(RuntimeError):
        t.inbound_once()  # the loop wrapper swallows this; the unit returns the error for visibility


def test_outbound_skips_decisions_of_finished_runs(home: OrqPaths) -> None:
    store = seed(home, state=RunState.ABORTED, phase="done", decision=business())
    fake = FakeClient()
    t = tasks(home, store, fake)
    assert t.outbound_once() == 0 and fake.sent == []


# Phase 5: hardening, projects, tasks, queue, summary, replay

TASK_MD = """# Task: Add greeting
## Repo
owner/a, base branch main
## Goal
Add greeting.txt.
## Acceptance criteria
- [ ] greeting.txt exists
## Check command
python -c "print('ok')"
## Plan approval
skip
"""

FIELDS = {"title": "Add greeting", "goal": "Add greeting.txt.", "acceptance_criteria": ["greeting.txt exists"],
          "out_of_scope": [], "constraints": [], "check_command": "", "base_branch": "", "plan_approval": "skip"}


def test_posts_need_the_token_and_hosts_are_checked(home: OrqPaths) -> None:
    seed(home)
    app = create_app(home, Config(), start_tasks=False, allowed_hosts={"127.0.0.1:8765"})
    page = TestClient(app, base_url="http://127.0.0.1:8765").get("/").text
    assert app.state.token in page and len(app.state.token) >= 32

    bare = TestClient(app, base_url="http://127.0.0.1:8765")
    assert bare.post("/api/runs/RAAAAA/pause").status_code == 403
    assert bare.post("/api/runs/RAAAAA/pause", headers={"X-Orq-Token": "wrong"}).status_code == 403
    assert bare.get("/api/runs").status_code == 200
    rebound = TestClient(app, base_url="http://evil.example:8765")
    assert rebound.get("/api/runs").status_code == 403


def test_projects_list_add_and_update(home: OrqPaths) -> None:
    seed(home)  # a run of owner/sandbox registers that project
    c = client(home)

    added = c.post("/api/projects", json={"source": "owner/a", "check_command": "uv run pytest -q"})
    assert added.status_code == 200 and added.json()["repo"] == "owner/a"
    assert c.post("/api/projects", json={"source": "owner/missing"}).status_code == 400
    assert c.patch("/api/projects/owner/a", json={"max_concurrent": 2, "name": "A"}).json()["max_concurrent"] == 2

    projects = {p["repo"]: p for p in c.get("/api/projects").json()}
    assert projects["owner/a"]["name"] == "A" and projects["owner/a"]["effective_max_concurrent"] == 2
    assert projects["owner/sandbox"]["counts"]["waiting"] == 1 and projects["owner/sandbox"]["effective_max_concurrent"] == 1
    assert c.patch("/api/projects/owner/nope", json={"name": "x"}).status_code == 404


def test_runs_filter_by_project(home: OrqPaths) -> None:
    seed(home)
    c = client(home)
    assert [r["run_id"] for r in c.get("/api/runs?project=owner/sandbox").json()] == ["RAAAAA"]
    assert c.get("/api/runs?project=owner/other").json() == []


def test_task_preview_and_create_from_fields_and_markdown(home: OrqPaths) -> None:
    c = client(home)
    c.post("/api/projects", json={"source": "owner/a", "check_command": "uv run pytest -q"})

    preview = c.post("/api/tasks/preview", json={"project": "owner/a", "fields": FIELDS}).json()
    assert preview["errors"] == [] and "owner/a, base branch main" in preview["markdown"]
    assert "uv run pytest -q" in preview["markdown"]  # the project's check command fills an empty field
    bad = c.post("/api/tasks/preview", json={"project": "owner/a", "fields": {**FIELDS, "acceptance_criteria": []}}).json()
    assert bad["errors"]

    created = c.post("/api/tasks", json={"project": "owner/a", "fields": FIELDS})
    assert created.status_code == 200
    run_id = created.json()["run_id"]
    assert Store(home.db).get_run(run_id).state is RunState.QUEUED

    from_md = c.post("/api/tasks", json={"project": "owner/a", "markdown": TASK_MD})
    assert from_md.status_code == 200
    c.post("/api/projects", json={"source": "owner/b"})
    wrong_repo = c.post("/api/tasks", json={"project": "owner/b", "markdown": TASK_MD})
    assert wrong_repo.status_code == 400 and "owner/a" in wrong_repo.json()["error"]
    assert c.post("/api/tasks", json={"project": "owner/a", "fields": {**FIELDS, "title": ""}}).status_code == 400


def test_rerun_and_queue_view(home: OrqPaths) -> None:
    seed(home)
    c = client(home)
    assert c.post("/api/runs/RAAAAA/rerun").status_code == 400  # the seeded TASK.md is not a valid task
    task_text = TASK_MD.replace("owner/a", "owner/sandbox")
    (home.run_dir("RAAAAA") / "TASK.md").write_text(task_text, encoding="utf-8")

    rerun = c.post("/api/runs/RAAAAA/rerun")
    assert rerun.status_code == 200
    new_id = rerun.json()["run_id"]
    assert (home.run_dir(new_id) / "TASK.md").read_text(encoding="utf-8") == task_text
    queue = c.get("/api/queue").json()
    assert queue["limit"] == 2 and queue["held"] == 0 and [r["run_id"] for r in queue["queued"]] == [new_id]


def test_summary_replay_and_artifacts(home: OrqPaths) -> None:
    seed(home)
    c = client(home)

    summary = c.get("/api/runs/RAAAAA/summary").json()
    assert summary["run_id"] == "RAAAAA" and summary["iterations"] == 2
    replay = c.get("/api/runs/RAAAAA/replay").json()
    assert [s["label"] for s in replay["steps"]] == ["Plan", "Iteration 1"]
    art = c.get("/api/runs/RAAAAA/artifact", params={"path": "iterations/2/implementer.stream.jsonl"}).json()
    assert "Working on it" in art["text"] and "[Bash] ls" in art["text"]  # condensed like the live pane
    assert c.get("/api/runs/RAAAAA/artifact", params={"path": "TASK.md"}).json()["text"].startswith("# Task")
    assert c.get("/api/runs/RAAAAA/artifact", params={"path": "../../orq.db"}).status_code == 400
    assert c.get("/api/runs/RAAAAA/artifact", params={"path": "nope.txt"}).status_code == 404
    assert c.get("/api/runs/RNOPE1/summary").status_code == 404


def test_reviewer_messages_are_condensed_to_their_summary() -> None:
    message = json.dumps({"status": "continue", "summary": "Tests are missing.", "milestone": "m1", "next_prompt": "add tests",
                          "issues": [], "human": None})
    line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": message}})
    assert condense_stream_line(line) == "[continue] Tests are missing."


def test_finished_runs_show_no_pending_decisions(home: OrqPaths) -> None:
    seed(home, state=RunState.ABORTED, phase="done", decision=business())  # left pending by a run aborted before Phase 5
    c = client(home)
    assert c.get("/api/runs").json()[0]["pending"] == 0
    assert c.get("/api/runs/RAAAAA").json()["decisions"] == []


def test_sse_follow_handles_crlf_files_without_repeating_lines(tmp_path: Path) -> None:
    """Run logs are written in text mode, so on Windows they end lines with CRLF; offsets must count those bytes."""
    import asyncio

    from orq.hub.app import sse_lines

    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"n": 1}\r\n{"n": 2}\r\n{"n": 2.5}\r\n')  # 3 CRLFs: a text-mode offset lands inside a line

    class Request:
        rounds = 0

        async def is_disconnected(self) -> bool:
            Request.rounds += 1
            if Request.rounds == 1:
                with path.open("ab") as fh:
                    fh.write(b'{"n": 3}\r\n{"n": 4')  # the last line is still being written
            if Request.rounds == 2:
                with path.open("ab") as fh:
                    fh.write(b'}\r\n')
            return Request.rounds > 2

    async def collect() -> list[str]:
        return [chunk async for chunk in sse_lines(path, Request(), follow=True, condense=None)]

    chunks = asyncio.run(collect())

    assert chunks == ['data: {"n": 1}\n\n', 'data: {"n": 2}\n\n', 'data: {"n": 2.5}\n\n', 'data: {"n": 3}\n\n', 'data: {"n": 4}\n\n']


# Phase 6: settings and models

def settings_client(home: OrqPaths, tmp_path: Path, probe=None) -> TestClient:
    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps({"models": [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]}]}), encoding="utf-8")
    app = create_app(home, Config(), start_tasks=False, git=fake_git(), allowed_hosts={"testserver"}, models_cache=cache,
                     probe=probe or (lambda kind, model, effort: (True, "OK")))
    return TestClient(app, headers={"X-Orq-Token": app.state.token})


def test_global_settings_read_write_and_validate(home: OrqPaths, tmp_path: Path) -> None:
    c = settings_client(home, tmp_path)

    before = c.get("/api/settings").json()
    assert before["effective"]["reviewer.codex_model"] == "gpt-6.1-sol" and before["sources"]["reviewer.codex_model"] == "config"
    assert [m["slug"] for m in before["codex_models"]] == ["gpt-6.1-sol", "gpt-5.5"]

    after = c.put("/api/settings", json={"reviewer.codex_model": "gpt-5.5", "reviewer.final_effort": "xhigh"}).json()
    assert after["effective"]["reviewer.codex_model"] == "gpt-5.5" and after["sources"]["reviewer.final_effort"] == "global"
    bad = c.put("/api/settings", json={"reviewer.final_effort": "max"})  # gpt-5.5 stops at xhigh
    assert bad.status_code == 400 and "does not support max" in bad.json()["error"]
    cleared = c.put("/api/settings", json={"reviewer.codex_model": None, "reviewer.final_effort": None}).json()
    assert cleared["sources"]["reviewer.codex_model"] == "config"


def test_project_settings_override_and_report_sources(home: OrqPaths, tmp_path: Path) -> None:
    seed(home)
    c = settings_client(home, tmp_path)
    c.put("/api/settings", json={"implementer.default_model": "sonnet"})

    project = c.patch("/api/projects/owner/sandbox", json={"settings": {"git.protected_paths": ["db/**"], "reviewer.codex_model": "gpt-5.5"}}).json()

    assert project["settings"] == {"git.protected_paths": ["db/**"], "reviewer.codex_model": "gpt-5.5"}
    assert project["effective"]["git.protected_paths"] == ["db/**"] and project["sources"]["git.protected_paths"] == "project"
    assert project["effective"]["implementer.default_model"] == "sonnet" and project["sources"]["implementer.default_model"] == "global"
    again = c.patch("/api/projects/owner/sandbox", json={"settings": {"reviewer.codex_model": None}}).json()
    assert again["settings"] == {"git.protected_paths": ["db/**"]}
    assert c.patch("/api/projects/owner/sandbox", json={"settings": {"nope": 1}}).status_code == 400


def test_model_test_endpoint_uses_the_probe(home: OrqPaths, tmp_path: Path) -> None:
    calls: list = []
    c = settings_client(home, tmp_path, probe=lambda kind, model, effort: calls.append((kind, model, effort)) or (False, "not supported"))

    r = c.post("/api/models/test", json={"kind": "codex", "model": "gpt-x"}).json()

    assert r == {"ok": False, "message": "not supported"} and calls == [("codex", "gpt-x", "low")]
    assert c.post("/api/models/test", json={"kind": "other", "model": "x"}).status_code == 400


def test_task_with_models_is_validated(home: OrqPaths, tmp_path: Path) -> None:
    c = settings_client(home, tmp_path)
    c.post("/api/projects", json={"source": "owner/a", "check_command": "pytest"})

    ok = c.post("/api/tasks/preview", json={"project": "owner/a", "fields": {**FIELDS, "models": {"reviewer": "gpt-5.5", "final_effort": "high"}}}).json()
    assert ok["errors"] == [] and "## Models\nreviewer: gpt-5.5\nfinal_effort: high" in ok["markdown"]
    bad = c.post("/api/tasks/preview", json={"project": "owner/a", "fields": {**FIELDS, "models": {"reviewer": "gpt-5.5", "final_effort": "max"}}}).json()
    assert bad["errors"] and "does not support max" in bad["errors"][0]


def test_outbound_tells_when_a_decision_was_answered_on_another_channel(home: OrqPaths) -> None:
    from orq.core.answers import record_answer
    store = seed(home, decision=business())
    fake = FakeClient()
    t = tasks(home, store, fake)
    t.outbound_once()                                        # the question went to WhatsApp
    record_answer(store, home, "DBBBB", "0", via="dashboard")

    assert t.outbound_once() == 1 and fake.sent[-1] == "[orq] DBBBB answered on the dashboard: before. Nothing to do here."
    assert t.outbound_once() == 0


def test_answers_given_on_whatsapp_or_never_sent_are_not_echoed(home: OrqPaths) -> None:
    from orq.core.answers import record_answer
    store = seed(home, decision=business())
    fake = FakeClient()
    t = tasks(home, store, fake)
    record_answer(store, home, "DBBBB", "0", via="dashboard")  # answered before it was ever sent

    assert t.outbound_once() == 0 and fake.sent == []


def test_failed_and_aborted_carry_their_reason(home: OrqPaths) -> None:
    store = seed(home, state=RunState.IMPLEMENTING, phase="implement")
    fake = FakeClient()
    t = tasks(home, store, fake)
    RunDir(home.run_dir("RAAAAA")).event("state", state="FAILED", reason="max iterations (3) reached")
    store.set_state("RAAAAA", RunState.FAILED)

    t.outbound_once()

    assert "RAAAAA · sandbox · FAILED" in fake.sent[-1] and fake.sent[-1].endswith("max iterations (3) reached")


# Phase 6: progress digests

def working_run(home: OrqPaths) -> Store:
    store = seed(home, state=RunState.IMPLEMENTING, phase="implement")
    rundir = RunDir(home.run_dir("RAAAAA"))
    rundir.event("owner_update", iteration=1, milestone=1, text="The greeting file exists.")
    return store


def test_progress_digest_on_milestone_end_and_on_the_timer(home: OrqPaths) -> None:
    store = working_run(home)
    fake = FakeClient()
    now = [time_of_first_event(home) + 60]
    t = tasks(home, store, fake, clock=lambda: now[0])

    assert t.outbound_once() == 0                         # nothing new since the hub started, timer not due
    RunDir(home.run_dir("RAAAAA")).event("milestone_done", iteration=1, milestone=1, of=1)
    assert t.outbound_once() == 1 and fake.sent[-1].startswith("*[orq] RAAAAA · sandbox ·") and "Done:" in fake.sent[-1]
    assert t.outbound_once() == 0
    now[0] += 30 * 60 + 1
    assert t.outbound_once() == 1 and "Latest: The greeting file exists." in fake.sent[-1]


def test_no_timer_digest_while_waiting_for_the_owner(home: OrqPaths) -> None:
    store = seed(home, decision=business())               # AWAITING_HUMAN
    fake = FakeClient()
    now = [time_of_first_event(home) + 3 * 3600]
    t = tasks(home, store, fake, clock=lambda: now[0])
    t.outbound_once()                                      # the decision itself
    assert not any(m.startswith("*[orq] RAAAAA") for m in fake.sent)


def test_status_of_one_run_returns_its_digest(home: OrqPaths) -> None:
    store = working_run(home)
    fake = FakeClient()
    reply = tasks(home, store, fake).handle("STATUS RAAAAA")
    assert reply.startswith("*[orq] RAAAAA · sandbox ·") and "Latest: The greeting file exists." in reply


def test_digest_endpoint(home: OrqPaths) -> None:
    working_run(home)
    assert "The greeting file exists." in client(home).get("/api/runs/RAAAAA/digest").json()["text"]


def time_of_first_event(home: OrqPaths) -> float:
    from datetime import datetime
    first = json.loads((home.run_dir("RAAAAA") / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    return datetime.fromisoformat(first["ts"]).timestamp()


# Phase 6: editable queue

def test_queue_move_and_edit_endpoints(home: OrqPaths) -> None:
    c = client(home)
    c.post("/api/projects", json={"source": "owner/a", "check_command": "pytest"})
    first = c.post("/api/tasks", json={"project": "owner/a", "markdown": TASK_MD}).json()["run_id"]
    second = c.post("/api/tasks", json={"project": "owner/a", "markdown": TASK_MD.replace("Add greeting", "Add farewell")}).json()["run_id"]

    assert c.post(f"/api/runs/{second}/move", json={"to": "top"}).status_code == 200
    assert [r["run_id"] for r in c.get("/api/queue").json()["queued"]] == [second, first]

    edited = TASK_MD.replace("Add greeting", "Add a warm greeting")
    r = c.put(f"/api/runs/{first}/task", json={"markdown": edited})
    assert r.status_code == 200 and r.json()["task_title"] == "Add a warm greeting" and r.json()["branch"] == "orq/add-a-warm-greeting"
    assert (home.run_dir(first) / "TASK.md").read_text(encoding="utf-8") == edited
    assert Checkpoint.load(home.run_dir(first) / "state.json").branch == "orq/add-a-warm-greeting"

    assert c.put(f"/api/runs/{first}/task", json={"markdown": TASK_MD.replace("owner/a", "owner/b")}).status_code == 400
    Store(home.db).set_state(first, RunState.PLANNING)
    assert c.put(f"/api/runs/{first}/task", json={"markdown": edited}).status_code == 409
    assert c.post(f"/api/runs/{first}/move", json={"to": "up"}).status_code == 409


def test_decisions_answered_before_the_hub_started_are_not_echoed(home: OrqPaths) -> None:
    from orq.core.answers import record_answer
    store = seed(home, decision=business())
    store.mark_notified("decision", "DBBBB", "whatsapp")        # sent by an earlier hub
    record_answer(store, home, "DBBBB", "0", via="dashboard")   # answered while no hub ran
    fake = FakeClient()

    assert tasks(home, store, fake).outbound_once() == 0 and fake.sent == []


# Phase 6.1: who answered

def test_answer_endpoint_records_who_answered(home: OrqPaths) -> None:
    store = seed(home, decision=business())
    c = client(home)

    assert c.post("/api/decisions/DBBBB/answer", json={"answer": "1", "by": "nobody"}).status_code == 400
    r = c.post("/api/decisions/DBBBB/answer", json={"answer": "1", "by": "claude"})

    assert r.status_code == 200 and r.json()["answered_by"] == "claude" and store.get_decision("DBBBB").answered_by == "claude"
