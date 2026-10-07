# Phase 5 design: scale (queue, concurrency, projects, summaries, replay)

Date: 2026-10-07. Source of truth remains `docs/SPEC.md` (sections 5, 6, 10.1, 11, 13, 14, 15). This document fixes the open design points for Phase 5.

Phase 5 scope (SPEC section 15): queue with concurrent runs and a global concurrency limit; run summaries and replay in the dashboard. Added by the owner on 2026-10-07: one dashboard for several projects (distinct GitHub repos), with a project selector, per-project run lists, tasks created and queued from the dashboard, and projects added from `owner/repo` or a local folder. The WhatsApp hub stays single for all projects.

Exit criterion (new; the SPEC had none for Phase 5): with `max_concurrent_runs = 2`, three tasks are created and queued from the dashboard across two projects; two run at once and the third starts when a slot frees; a run parked on a decision does not block the queue; all three end with a merged PR; every finished run shows its summary and can be replayed iteration by iteration.

Decisions taken with the owner (2026-10-07):

* **D1** A project is identified by `owner/repo` on GitHub. orq keeps its own clone under `~/.orq/repos/<owner>/<repo>` and never creates worktrees in the owner's working copy. "Add from a local folder" only reads that folder's `origin` remote. Projects live in SQLite (the dashboard writes them).
* **D2** The hub dispatches queued runs, one detached process per run (the existing `orq resume` path). `orq run` in a terminal still works and obeys the same slots.
* **D3** A run waiting for the owner (`AWAITING_HUMAN`, `AWAITING_PLAN_APPROVAL`) releases its slot; after the answer it asks again and goes ahead of runs that never started. `PAUSED_RATE_LIMIT` keeps its slot.
* **D4** Per-project cap, default 1 active run per project, on top of the global `[limits].max_concurrent_runs`.
* **D5** Deterministic summary (no LLM) from `events.jsonl`, cached in `summary.json`; replay is an iteration-by-iteration navigator over the recorded artifacts.
* **D6** Real runs use the existing sandbox plus a second sandbox repo; real projects (volleyball) come after Phase 5.

## 1. Projects

Table `projects`: `repo TEXT PRIMARY KEY` (`owner/repo`), `name`, `base_branch` (default `main`), `check_command` (default empty), `max_concurrent INTEGER` (default `[queue].project_concurrency`, 1), `local_path` (informational), `created_at`.

* Runs keep their existing `repo` column; that is the join key. On store open, every `repo` found in `runs` without a project row gets one (old runs group themselves). `orq run`/`orq queue` on an unknown repo registers it too.
* `orq project add <owner/repo | folder> [--name] [--base] [--check] [--max-concurrent]` and `orq project list`. A folder is resolved with `git -C <folder> remote get-url origin`, accepting `https://github.com/o/r(.git)` and `git@github.com:o/r(.git)`. The repo is validated with `gh repo view <o/r> --json nameWithOwner,defaultBranchRef`; its default branch becomes `base_branch` unless given.
* Shared logic in `src/orq/core/projects.py` (`resolve_source`, `add_project`), used by the CLI and the hub.

## 2. Git under concurrency

* Clone path becomes `repos/<owner>/<repo>`. Legacy clones (`repos/<repo>`) are left where they are: finished and aborted runs reference them through `state.json` (`repo_path`); new runs clone afresh.
* `src/orq/core/locks.py: file_lock(path)` is a cross-process lock (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere; released by the OS when a process dies). `GitManager` takes a per-repo lock (`<clone>.orq.lock` next to the clone; from a worktree the clone is found once through `git rev-parse --git-common-dir`) around commands that write shared refs or the worktree list: `clone`, `fetch`, `worktree add/remove/prune`, `branch -D`, `push`, and the fetch inside `base_moved`/`rebase_onto_base`.

## 3. Queue and slots

States: `QUEUED` now means "waiting for a slot". A queued run has a run dir, `TASK.md`, a checkpoint at phase `setup` with `pid = 0`, and a `runs` row; no process and no worktree.

* `src/orq/core/queue.py: enqueue(paths, store, config, task_text, clone_url=None) -> run_id` parses the task, registers the project, creates the run exactly as `Runner` would (shared helper `new_run_checkpoint`), and logs `queued`.
* Table `slots(run_id PRIMARY KEY, repo, pid, held INTEGER, fresh INTEGER, since TEXT)`. `Store.try_acquire_slot(run_id, repo, pid, fresh, global_limit, project_limit) -> bool` runs in one `BEGIN IMMEDIATE` transaction: drop rows whose pid is dead, upsert the caller as a waiter, then grant when the global and project counts of held slots are under their limits and no older waiter of a higher or equal class (started runs before fresh ones) could take that slot first. `release_slot(run_id)` deletes the row. `slot_usage()` feeds the dashboard and `STATUS`.
* `Runner` acquires a slot at the top of the main loop whenever it does not hold one and the phase is not `await`; while it waits it is in state `QUEUED` (event `waiting_for_slot` once, with usage), polling every `[queue].poll_seconds` and honouring the pause flag. It releases the slot when it raises a decision (`await`) and when `execute()` returns. `fresh` is true until the run leaves `setup`.
* Dispatcher (hub task, every `[queue].poll_seconds`): for `QUEUED` runs at phase `setup` with no live pid, oldest first, while `slot_usage` leaves room for that repo, spawn `orq resume <id>` (detached, the existing `spawn_resume`). A spawned run is not spawned again for 60 s; runs spawned in this tick count as taking a slot. The run itself still acquires the slot, so a race with a terminal `orq run` only makes one of them wait.
* Without the hub, queued runs wait; `orq queue` says so and `orq resume <id>` starts one by hand.
* Cancelling a queued run is `abort` (no worktree yet); a spawned run waiting for its slot is paused first, like any live run.

## 4. Hub API and UI

New and changed endpoints (JSON, loopback):

* `GET /api/projects` (with counts per state group: running, waiting, queued, done, failed); `POST /api/projects` `{source, name?, base_branch?, check_command?, max_concurrent?}`; `PATCH /api/projects/{owner}/{repo}`.
* `GET /api/runs?project=<owner/repo>`; `GET /api/queue` (slots used and limits, queued runs in order).
* `POST /api/tasks/preview` `{project, fields | markdown}` -> `{markdown, errors}`; `POST /api/tasks` same body -> `{run_id}`. Fields: `title, goal, acceptance_criteria[], out_of_scope[], constraints[], check_command, base_branch, plan_approval`. The repo always comes from the project; a pasted markdown whose `## Repo` names another repo is rejected. `src/orq/core/taskfile.py: render_task(fields) -> str` is the inverse of `parse_task` (round-trip tested).
* `POST /api/runs/{id}/rerun` queues a copy of the run's `TASK.md`.
* `GET /api/runs/{id}/summary`, `GET /api/runs/{id}/replay`, `GET /api/runs/{id}/artifact?path=<relative>` (section 5).

Hardening (the hub can now start agents): every non-GET request needs header `X-Orq-Token`, a random token generated when the hub starts and embedded in the served page; a middleware rejects requests whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding). No CORS headers are sent.

UI (vanilla HTML/JS, no build; `static/index.html`, `static/app.js`, `static/style.css`): left sidebar with "All projects" and each project with counters, plus "Add project"; per project the views Runs, Queue and New task (form with live TASK.md preview, or paste markdown); run page as in Phase 4 plus Summary and Replay tabs and a "Run again" button. The reviewer pane condenses Codex's schema-shaped messages to their `summary` field.

## 5. Summaries and replay

`src/orq/core/summary.py: build_summary(run_dir) -> dict`, pure over `events.jsonl`, `state.json` and the iteration directories:

* state, task title, repo, started/ended timestamps, wall time, time spent per state (gaps between consecutive `state` events), iterations, milestones (planned, done), PR url and whether it merged;
* decisions: id, source, type, question (first line), answer, channel, seconds waited;
* agent calls per role (count, token usage summed from the role events), implementer models used, reviewer switches;
* checks run and failed, CI wait and re-runs, guard denials and diff-rule hits, files changed (from the iteration `diff.patch` headers).

The runner writes `summary.json` when the run ends (`DONE`, `FAILED`, `ABORTED`; also `abort_run`); the hub computes it on the fly for runs without one.

Replay: `GET /api/runs/{id}/replay` returns the planner step and one entry per iteration (including `n.discarded-<ts>` ones) with its events (by `iteration` field and position between `IMPLEMENTING` transitions) and the artifacts present (`implementer.prompt.md`, `implementer.stream.jsonl` condensed, `diff.patch`, `checks.txt`, `reviewer.prompt.md`, `reviewer.output.json`, `final_review.*`). `artifact?path=` serves one file from the run dir, path-checked to stay inside it; `.stream.jsonl` files are condensed like the live panes.

## 6. WhatsApp

Unchanged channel. `STATUS` groups runs by project and shows `slots used/limit` and the queued count. Queued runs are not announced (noise); decisions and completions already carry the repo name.

## 7. Configuration

```toml
[limits]
max_concurrent_runs = 2      # global; runs waiting for the owner do not count

[queue]
poll_seconds = 5             # dispatcher tick and slot wait poll
project_concurrency = 1      # default cap for new projects
```

## 8. Testing

* `test_projects.py`: source resolution (both URL shapes, folder), validation through a fake gh, auto-registration from runs.
* `test_locks.py`: a second process cannot take a held lock; released on exit.
* `test_store.py`: slot grant under global and project limits, dead pid reclaimed, started runs ahead of fresh ones, FIFO within a class.
* `test_queue.py`: `enqueue` builds a resumable run with `pid = 0`; dispatcher spawns within limits, skips recently spawned and live runs (fake spawner).
* `test_loop.py`: a runner waits in `QUEUED` while the slot is taken and continues when freed; releases the slot on a decision and re-acquires after the answer; releases on exit.
* `test_taskfile.py`: `render_task` round-trips through `parse_task`.
* `test_summary.py`: summary from a recorded `events.jsonl` fixture.
* `test_hub.py`: projects, tasks preview/create, rerun, queue, summary, replay, artifact path escape refused, token and Host checks.
* `test_cli.py`: `orq queue` then `orq resume` with the fake CLIs reaches `DONE`; `orq project add/list`.
* Real runs: the exit criterion above on `orq-phase0-sandbox` and the second sandbox repo.

## 9. Out of scope

Dashboard authentication beyond the loopback token, queue reordering, task templates library, multi-milestone `done` (Phase 3 note), the volleyball and other real projects.
