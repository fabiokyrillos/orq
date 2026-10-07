# Phase 5 Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Several projects in one hub, a queue that runs tasks concurrently under global and per-project limits, tasks created and queued from the dashboard, and deterministic run summaries with an iteration-by-iteration replay.

**Architecture:** Projects and slots live in SQLite. Every run stays one process; it takes a slot before agent work, gives it back while it waits for the owner, and the hub's dispatcher spawns queued runs through the existing `orq resume` path. Git commands that touch shared refs are serialised per repo with a cross-process file lock. Summary and replay are pure readers of the run dir.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, msvcrt/fcntl locks, vanilla JS. Design: `docs/superpowers/specs/2026-10-07-phase5-scale-design.md`.

---

### Task 1: projects and concurrency-safe git

Files: `src/orq/core/locks.py`, `src/orq/core/projects.py` (new); `src/orq/store/db.py` (`projects`), `src/orq/git/manager.py`, `src/orq/config.py` (`[queue]`), `src/orq/cli.py` (`project add/list`), `tests/fakes/fake_gh.py` (`repo view`); tests `tests/test_locks.py`, `tests/test_projects.py`, `tests/test_git.py`, `tests/test_config.py`, `tests/test_cli.py`.

- [ ] `file_lock(path, timeout)` context manager; test with a child python process holding the lock.
- [ ] Store: `projects` table, `upsert_project`, `get_project`, `list_projects`, `update_project`; auto-register repos found in `runs` on open.
- [ ] `projects.parse_remote(url)`, `resolve_source(source)` (owner/repo or folder), `add_project(store, git, source, ...)` validating with `gh repo view`.
- [ ] `GitManager.ensure_repo` clones to `repos/<owner>/<repo>`; lock around clone/fetch/worktree/branch/push/rebase fetch.
- [ ] `orq project add`, `orq project list`.
- [ ] Commit `feat(projects): project registry and per-repo git lock`.

### Task 2: slots, queue, dispatcher

Files: `src/orq/core/queue.py` (new), `src/orq/store/db.py` (`slots`), `src/orq/core/loop.py`, `src/orq/core/control.py`, `src/orq/hub/dispatcher.py` (new), `src/orq/cli.py` (`queue`, status), `src/orq/notify/messages.py` (STATUS); tests `tests/test_store.py`, `tests/test_queue.py`, `tests/test_loop.py`, `tests/test_cli.py`, `tests/test_messages.py`.

- [ ] Store: `slots` table, `try_acquire_slot`, `release_slot`, `slot_usage` with the grant rules of design section 3.
- [ ] `new_run_checkpoint` shared by `Runner` and `enqueue`; queued checkpoint has `pid = 0`.
- [ ] Runner: acquire before non-`await` phases (state `QUEUED` while waiting, pause honoured), release on decision and on exit.
- [ ] `Dispatcher.tick()` spawns queued runs within limits; hub lifespan runs it every `[queue].poll_seconds` (also without WhatsApp).
- [ ] `orq queue <TASK.md>`; `orq status` shows slots; WhatsApp `STATUS` grouped by project with slot usage.
- [ ] Commit `feat(queue): slots with global and per-project limits, queued runs dispatched by the hub`.

### Task 3: summaries and replay (backend)

Files: `src/orq/core/summary.py` (new), `src/orq/core/loop.py`, `src/orq/core/control.py`; tests `tests/test_summary.py`.

- [ ] `build_summary(run_dir)` per design section 5; `write_summary` on terminal states and in `abort_run`.
- [ ] `build_replay(run_dir)`: planner step plus iterations with events and artifacts.
- [ ] Commit `feat(summary): deterministic run summary and replay model`.

### Task 4: hub API (projects, tasks, queue, summary, replay, hardening)

Files: `src/orq/core/taskfile.py` (new), `src/orq/hub/app.py`; tests `tests/test_taskfile.py`, `tests/test_hub.py`.

- [ ] `render_task(fields)` round-trips with `parse_task`.
- [ ] Endpoints of design section 4; artifact path confined to the run dir.
- [ ] Token header on non-GET requests and Host check; token injected into the page.
- [ ] Reviewer stream condensation shows `summary` of schema-shaped Codex messages.
- [ ] Commit `feat(hub): projects, task creation, queue, summary and replay endpoints; request token and host check`.

### Task 5: dashboard UI

Files: `src/orq/hub/static/index.html`, `app.js`, `style.css`; `src/orq/hub/app.py` (static mount).

- [ ] Sidebar with projects and counters, Add project dialog; per-project Runs, Queue, New task (form, preview, paste markdown); run page with Summary and Replay tabs and Run again.
- [ ] Manual check in the browser against a seeded `ORQ_HOME`.
- [ ] Commit `feat(dashboard): multi-project UI with queue, task form, summary and replay`.

### Task 6: real runs, findings, spec

- [ ] Owner creates the second sandbox repo with CI; register both projects from the dashboard; `max_concurrent_runs = 2`.
- [ ] Exit criterion runs (design header), one of them with a plan approval answered late to show the slot release.
- [ ] `docs/phase5-findings.md`; SPEC sections 5, 6, 10.1, 11, 13, 14, 15.
- [ ] Commit `docs: Phase 5 findings and spec updates`; push.
