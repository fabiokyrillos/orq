# Phase 4 Remote Human Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner answer decisions and drive runs away from the terminal: Windows toasts, a local dashboard with live streams and controls, and WhatsApp through n8n + Evolution API, while runs wait for answers from any channel.

**Architecture:** Runs wait on SQLite. A single hub process (`orq dashboard`, FastAPI + SSE) serves the UI and owns the WhatsApp channel (outbound, reminders, inbound polling, acks). Answer recording is one shared function used by terminal, CLI, dashboard and WhatsApp.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, winotify, pytest. Design: `docs/superpowers/specs/2026-10-07-phase4-remote-human-design.md`.

---

### Task 1: shared answer recording and waiting mode

Files: `src/orq/core/answers.py` (new), `src/orq/core/loop.py`, `src/orq/cli.py`, `src/orq/config.py` (`[notify]`, `[dashboard]`); tests `tests/test_loop.py`, `tests/test_cli.py`, `tests/test_config.py`.

- [ ] `record_answer(store, paths, decision_id, text, via) -> Decision` maps an option index, refuses non-pending decisions, appends to `DECISIONS.md`, logs the `answer` event. `orq answer` and the runner's terminal path use it.
- [ ] `Runner(wait_for_answers=False)`: when true and no `human` callback, `_await` polls the store every `[notify].answer_poll_seconds` (asyncio sleep) until the decision is answered; pause flag honoured between polls. Test with a fake sleep that writes the answer on the second poll.
- [ ] `orq run` / `orq resume` default: `wait_for_answers=True`, `human=None`, plus a daemon stdin thread (`cli._terminal_answers`) that records whatever the owner types for the current pending decision. `--no-prompt` keeps exiting at the decision.
- [ ] Commit `feat(core): wait for answers from any channel; shared answer recording`.

### Task 2: notifier, messages, WhatsApp client

Files: `src/orq/notify/__init__.py`, `toast.py`, `messages.py`, `whatsapp.py`; tests `tests/test_messages.py`, `tests/test_whatsapp.py`, `tests/test_loop.py` (toast hook).

- [ ] `messages.format_decision`, `format_run_state`, `parse_reply` with every shape from the design; destructive rule enforced by the parser's consumer (`Reply.kind` in `answer_index | answer_text | approve | deny | status | pause | resume | abort | unknown`).
- [ ] `WhatsAppClient` over an injected `httpx.Client`; `from_config(config)` returns `None` when unconfigured.
- [ ] `toast.notify(title, message)` via `winotify`, swallowed failures; `Runner(notifier=...)` calls it on new decisions and terminal states (`notified` / `toast_failed` events).
- [ ] Commit `feat(notify): toast, WhatsApp client and message formats`.

### Task 3: hub API and UI

Files: `src/orq/hub/__init__.py`, `app.py`, `static/index.html`, `src/orq/store/db.py` (`notifications`, `kv`), `src/orq/cli.py` (`dashboard` command); tests `tests/test_hub.py`.

- [ ] Store: `notifications` and `kv` tables, `all_pending_decisions()`, `mark_notified`, `notified_at`, `kv_get/kv_set`.
- [ ] `create_app(paths, config)`: routes from the design; SSE generators read `events.jsonl` and stream logs with tail-follow; `/resume` spawns `[sys.executable, "-m", "orq", "resume", id]` detached (`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` on Windows).
- [ ] UI: runs table, run page with two stream panes and the events feed, decision cards with option buttons, free-text box, approve/deny, pause/abort/resume buttons.
- [ ] `orq dashboard [--port]` runs uvicorn on 127.0.0.1.
- [ ] Commit `feat(hub): local dashboard with live streams and decision controls`.

### Task 4: hub WhatsApp tasks

Files: `src/orq/hub/whatsapp_tasks.py`; tests `tests/test_hub.py`.

- [ ] Outbound loop: pending decisions and terminal states not yet notified -> `client.send(format_...)`, record; reminders after `reminder_hours`.
- [ ] Inbound loop: fetch since cursor, `parse_reply`, apply (`record_answer`, pause flag, spawn resume, abort), reply with confirmation/hint, ack, store cursor. Unknown or wrong-format replies get the hint text; destructive numbered replies are refused.
- [ ] Both loops tolerate n8n errors (log, continue). Unit tests drive one iteration of each loop with a fake client.
- [ ] Commit `feat(hub): WhatsApp outbound, reminders and inbound commands`.

### Task 5: n8n deliverables, real test, docs

- [ ] `docs/n8n/orq_messages.sql`, `docs/n8n/orq-workflow.json`, `docs/n8n-setup.md` (import, credentials, curl tests for the three endpoints).
- [ ] Owner imports the workflow and sets `ORQ_N8N_TOKEN` and `[notify].n8n_base_url`. Then: a sandbox task that raises a business decision (ambiguous requirement) answered from WhatsApp, and the guard task from Phase 2 answered with `APPROVE <id>` from WhatsApp.
- [ ] `docs/phase4-findings.md`; SPEC sections 5, 9, 13, 14, 15.
- [ ] Commit `docs: Phase 4 findings and spec updates`.
