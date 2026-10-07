# Phase 6 Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before real projects, make orq comfortable to run unattended:
* models changeable at any time;
* decisions with enough content to answer from the phone;
* progress digests;
* fewer wasted iterations;
* a hub that starts and resumes on its own;
* rebase conflicts resolved by the implementer;
* an editable queue.

**Architecture:** layered settings resolved before every agent call; richer output contracts plus orq-built evidence for its own decisions; pure digest builder used by the hub; dispatcher extended for answered runs; bounded conflict loop inside `gate_ci`.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, vanilla JS. Design: `docs/superpowers/specs/2026-10-07-phase6-refinement-design.md`.

---

### Task 0: probes (done)

- [x] Codex CLI 0.161.0: every listed model answers on Plus; the read-only sandbox only works with `windows.sandbox="unelevated"` (`docs/phase6-findings.md` section 0).

### Task 1: settings in layers, live models, Codex sandbox flag

Files: `src/orq/core/settings.py` (new), `src/orq/store/db.py` (`settings` table, `projects.settings`), `src/orq/config.py`, `src/orq/adapters/codex.py`, `src/orq/adapters/claude.py`, `src/orq/adapters/router.py`, `src/orq/core/loop.py`, `src/orq/core/task.py` (`## Models`), `src/orq/hub/app.py`, `src/orq/hub/static/app.js`; tests `test_settings.py`, `test_adapters.py`, `test_router.py`, `test_loop.py`, `test_task.py`, `test_hub.py`.

- [ ] `resolve()` with sources; task `## Models`; models cache reader; effort validation.
- [ ] Adapters: `model=` per call; Codex `-c windows.sandbox=...`; the router forwards the model.
- [ ] Runner resolves before every call; events record model and effort; guard settings from the project at start or resume.
- [ ] Hub: `GET/PUT /api/settings`, project settings in `PATCH /api/projects`, `GET /api/models`, `POST /api/models/test`; UI: global Settings page, project overrides with sources, Models fold-out in New task.
- [ ] Default `codex_model = "gpt-6.1-sol"`.
- [ ] Commit `feat(settings): layered settings with live model changes; Codex unelevated sandbox`.

### Task 2: decisions with content

Files: `src/orq/adapters/schema.py`, `src/orq/adapters/claude.py` (marker), `src/orq/core/prompts.py`, `src/orq/core/models.py`, `src/orq/store/db.py`, `src/orq/core/loop.py`, `src/orq/notify/messages.py`, `src/orq/hub/whatsapp_tasks.py`, `src/orq/hub/static/app.js`; tests `test_adapters.py`, `test_loop.py`, `test_messages.py`, `test_hub.py`.

- [ ] Contracts and marker: `context`, `option_details`, `recommendation_reason`; prompts explain them.
- [ ] Decision fields and columns; orq-built context for every orq decision kind (design table 2.3).
- [ ] WhatsApp sections with the 1500-character cap; dashboard card.
- [ ] Answered-elsewhere notice; FAILED and ABORTED with reason.
- [ ] Commit `feat(decisions): context, option consequences and recommendation reason on every decision`.

### Task 3: progress digests

Files: `src/orq/adapters/schema.py` (`owner_update`), `src/orq/core/prompts.py`, `src/orq/core/checkpoint.py`, `src/orq/core/loop.py`, `src/orq/core/digest.py` (new), `src/orq/config.py` (`progress_minutes`), `src/orq/hub/whatsapp_tasks.py`, `src/orq/hub/app.py`, `static/app.js`; tests `test_digest.py`, `test_hub.py`, `test_loop.py`.

- [ ] `owner_update` stored and logged; `build_digest`; hub sends on milestone and timer; `STATUS <run>`; dashboard card.
- [ ] Commit `feat(progress): owner updates from the reviewer and progress digests on WhatsApp and the dashboard`.

### Task 4: fewer wasted iterations

Files: `src/orq/adapters/schema.py` (`task_complete`), `src/orq/core/prompts.py`, `src/orq/core/loop.py`; tests `test_loop.py`.

- [ ] Clean done with `task_complete` goes to finalize (`milestones_skipped`); planner guidance on small tasks.
- [ ] Commit `feat(loop): the reviewer can close the whole task; planner prefers one milestone for small tasks`.

### Task 5: hub autostart and auto-resume

Files: `src/orq/cli.py` (`hub autostart`, `dashboard --log-file`), `src/orq/hub/dispatcher.py`; tests `test_cli.py`, `test_queue.py`.

- [ ] Scheduled-task argv builder, on/off/status; log file.
- [ ] Dispatcher spawns answered runs without a process, ranked as started.
- [ ] Commit `feat(hub): autostart at logon and automatic resume of answered runs`.

### Task 6: rebase conflicts by the implementer

Files: `src/orq/git/manager.py`, `src/orq/core/loop.py`, `src/orq/core/prompts.py`, `src/orq/config.py` (`max_conflict_rounds`); tests `test_git.py`, `test_loop.py`.

- [ ] `start_rebase`, `conflicted_files`, `continue_rebase`, `has_conflict_markers`; bounded loop; reset and ask on failure.
- [ ] Commit `feat(gate): the implementer resolves rebase conflicts before the owner is asked`.

### Task 7: editable queue

Files: `src/orq/store/db.py` (`queue_order`), `src/orq/hub/dispatcher.py`, `src/orq/hub/app.py`, `static/app.js`; tests `test_queue.py`, `test_hub.py`.

- [ ] Move and edit endpoints; Queue tab controls.
- [ ] Commit `feat(queue): reorder and edit queued tasks`.

### Task 8: real runs, findings, spec

- [ ] Exit-criterion runs on both sandboxes.
- [ ] `docs/phase6-findings.md`; SPEC (environment, sections 4, 5, 8, 9, 10, 12, 13, 14, 15 with a Phase 6 entry); push.
