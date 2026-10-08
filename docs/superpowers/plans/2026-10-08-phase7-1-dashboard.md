# Phase 7.1 Implementation Plan: dashboard for daily use

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** pin, archive and remove projects; editable slots; a usage view; a read-only chat per project; a fixed rail, sectioned settings, bulk add; the dashboard in Portuguese.

**Architecture:** project `status`/`pinned` columns with domain functions in `core/projects.py`; `settings.slot_limits`; `core/usage.py` reading run dirs; `core/chat.py` with a `ClaudeChat` agent in a fixed chat worktree; dashboard changes in `app.js`/`style.css`.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, vanilla JS. Design: `docs/superpowers/specs/2026-10-08-phase7-1-dashboard-design.md`.

---

### Task 1: projects and slots

- [ ] Store: `status`, `pinned` columns (migration); tests.
- [ ] `set_pinned`, `archive_project`, `restore_project`, `remove_project`; `add_project` reactivates; tests (refusals while a run is live, clone and worktrees deleted, owner folder untouched).
- [ ] Endpoints and task refusal for inactive projects; hub tests.
- [ ] `limits.max_concurrent_runs`, `queue.project_concurrency` as global-only settings with a 1 to 6 range; `slot_limits` used by dispatcher, loop, queue API, project data, CLI status; tests.
- [ ] Commit `feat(projects): pin, archive and remove projects; slots in settings`.

### Task 2: usage

- [ ] `core/usage.py` with tests (totals by day/project/role/model, chat calls, compactions, latest limits).
- [ ] `GET /api/usage`; hub test.
- [ ] Commit `feat(usage): token usage overall and per project, plan limits, compactions`.

### Task 3: chat

- [ ] `ClaudeChat` agent (argv test with the fake claude); `core/chat.py` store and `ask()` (chat worktree refreshed to origin/base, session resumed); tests.
- [ ] Endpoints; hub tests.
- [ ] Commit `feat(chat): read-only conversation with Claude about a project`.

### Task 4: dashboard

- [ ] Fixed rail; pinned/archived groups; project actions (pin, archive, restore, remove).
- [ ] Settings sections; slots in Execução.
- [ ] Bulk add.
- [ ] Usage pages; chat tab.
- [ ] Portuguese texts; `CLAUDE.md` exception.
- [ ] Commit `feat(dashboard): Portuguese, fixed rail, sectioned settings, usage and chat views`.

### Task 5: real use, findings, spec

- [ ] Exit criteria in the design, on the owner's hub; a sandbox run.
- [ ] `docs/phase7-findings.md` section 9; SPEC; push.
