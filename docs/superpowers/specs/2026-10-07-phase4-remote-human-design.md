# Phase 4 design: remote human

Date: 2026-10-07. Source of truth remains `docs/SPEC.md` (sections 5, 9, 13, 14, 15). This document fixes the open design points for Phase 4.

Phase 4 scope (SPEC section 15): dashboard with both live streams, run list and decision controls; Windows toast; n8n + Evolution API outbound and inbound (pull) with WhatsApp commands.

Exit criterion: the owner answers a business decision and a destructive approval from WhatsApp and the run continues.

Decisions taken with the owner (2026-10-07):

* One long-lived process, `orq dashboard` (the hub), owns the WhatsApp channel: outbound notifications, inbound polling, acks. Runs never talk to n8n; they wait on SQLite.
* The owner already runs n8n and Evolution API with a dedicated sender number; orq ships the importable workflow and the table SQL.
* `orq run` waits for answers from any channel by default; `--no-prompt` keeps the "exit at the first decision" behaviour.

## 1. Waiting mode in the run process

`Runner` gets `wait_for_answers: bool` (default true for `orq run` and `orq resume`, false with `--no-prompt`) and the await phase becomes asynchronous:

* A pending decision is checked in SQLite every `[notify].answer_poll_seconds` (default 3). The terminal keeps working: the CLI starts a daemon thread reading stdin that stores the answer through the same path as `orq answer` (`answered_via="terminal"`). The run loop sees it on the next poll.
* `PAUSED_RATE_LIMIT` waits already exist; the pause flag is honoured between polls so `orq pause` still works while waiting.
* The run process notifies locally only (toast, section 2). Reminders and WhatsApp are the hub's job.

## 2. Notifier (`src/orq/notify/`)

* `toast.py`: Windows toast through `winotify` (pure Python over PowerShell) on a new decision, `AWAITING_PLAN_APPROVAL`, `DONE`, `FAILED`, `ABORTED`. Failures to show a toast are logged (`toast_failed` event) and never stop the run. `[notify].toast = false` disables it.
* `whatsapp.py`: `WhatsAppClient(base_url, token, http)` with `send(text) -> None` (`POST {base}/orq/notify`, JSON `{"text": ...}`, header `Authorization: Bearer <token>`), `replies(since) -> list[Reply]` (`GET {base}/orq/replies?since=<cursor>`), `ack(ids)` (`POST {base}/orq/replies/ack`). The token comes from the environment variable named by `[notify].n8n_token_env`; orq never stores the owner's phone number, n8n does.
* `messages.py`: pure formatting (SPEC 9.3) and parsing. `format_decision(decision, run) -> str`; `format_run_state(run) -> str`; `parse_reply(text) -> Reply command`:
  * `D7K2 1`, `D7K2 2` -> numbered answer (rejected with a hint for destructive decisions)
  * `D7K2 <free text>` -> text answer
  * `APPROVE D7K2` / `DENY D7K2` (also `D7K2 APPROVE`) -> destructive answer
  * `STATUS`, `STATUS <run>`, `PAUSE <run>`, `RESUME <run>`, `ABORT <run>` -> commands
  * anything else -> `unknown`, answered with a one-line hint.

## 3. Hub: `orq dashboard`

FastAPI app bound to `127.0.0.1:[dashboard].port` (default 8765), started with uvicorn, no auth (loopback only).

HTTP API:

* `GET /` single-page UI (vanilla HTML/JS served from the package).
* `GET /api/runs` run list (state, iteration, milestone, pending decision count, pr url).
* `GET /api/runs/{id}` run detail incl. plan, pending decisions, checkpoint phase.
* `GET /api/runs/{id}/events` SSE of `events.jsonl` (replay then follow).
* `GET /api/runs/{id}/stream/{role}` SSE tail of the current iteration's `implementer.stream.jsonl` or `reviewer.stream.jsonl`, condensed to text lines (assistant text, tool names, result).
* `POST /api/decisions/{id}/answer` body `{"answer": "..."}` -> store + `DECISIONS.md` + event (`answered_via="dashboard"`).
* `POST /api/runs/{id}/pause` (flag file), `/abort` (same logic as the CLI; refuses a live pid), `/resume` (spawns `orq resume <id>` detached with the venv python; refuses a live pid).

Background tasks (asyncio, in the app lifespan):

* **Outbound**: every `[notify].outbox_poll_seconds` (default 5) look for pending decisions and terminal run states not yet in the `notifications` table, send them by WhatsApp, record them. Reminders: a pending decision older than `[notify].reminder_hours` since its last notification is sent again.
* **Inbound**: every `[notify].poll_seconds` (default 20) fetch replies since the stored cursor (`kv` table, key `whatsapp_cursor`), apply each one, reply with a confirmation or a hint, ack the batch, advance the cursor. Applying an answer uses one shared function with `orq answer` (`core/answers.py: record_answer(store, paths, decision_id, text, via)`), so terminal, CLI, dashboard and WhatsApp converge.
* Both tasks keep running when n8n is unreachable (log and retry); the dashboard stays usable.

Store additions: `notifications(kind, key, channel, sent_at)` and `kv(key, value)`; `Store.all_pending_decisions()`, `Store.runs_in_states(...)`.

## 4. Configuration

```toml
[notify]
toast = true
n8n_base_url = "https://<vps>/webhook"
n8n_token_env = "ORQ_N8N_TOKEN"
poll_seconds = 20
outbox_poll_seconds = 5
answer_poll_seconds = 3
reminder_hours = 3

[dashboard]
port = 8765
```

WhatsApp is enabled when `n8n_base_url` is set and the token variable exists; otherwise the hub runs dashboard-only and logs it once.

## 5. n8n side (delivered as files, `docs/n8n/`)

* `orq_messages.sql`: `id serial, decision_id text, direction text, from_number text, text text, received_at timestamptz, consumed_at timestamptz`.
* `orq-workflow.json`: three webhooks under one workflow, all checking the `Authorization` header against an n8n credential: `POST orq/notify` -> Evolution `sendText` to the owner's number; `GET orq/replies?since=` -> Postgres select of unconsumed inbound rows with `id > since`; `POST orq/replies/ack` -> set `consumed_at`. A fourth webhook receives Evolution's `messages.upsert`, keeps only the owner's number, and inserts the row.
* `docs/n8n-setup.md`: import, credentials, test commands with `curl`.

## 6. Testing

* `tests/test_messages.py`: formatting and every reply shape.
* `tests/test_whatsapp.py`: client against `httpx.MockTransport`; cursor and ack; token header; disabled when unconfigured.
* `tests/test_hub.py`: FastAPI `TestClient`: run list, detail, answer endpoint records through the shared function, pause writes the flag, abort refuses a live pid, SSE replays events; outbound task sends a pending decision once and reminds after `reminder_hours`; inbound task applies `D1 1`, rejects `D1 1` for a destructive decision with a hint, applies `APPROVE D1`, handles `STATUS`/`PAUSE`, acks and advances the cursor.
* `tests/test_loop.py`: waiting mode polls the store and continues when the answer appears; `tests/test_cli.py`: `orq run` default waits (fake answer written by a thread), `--no-prompt` still exits.
* Real exit-criterion runs against the owner's VPS with the imported workflow.

## 7. Out of scope

Queue and concurrency (Phase 5), run replay in the dashboard (Phase 5), authentication on the dashboard (loopback only).
