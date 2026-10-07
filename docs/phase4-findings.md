# Phase 4 findings

**Date:** 2026-10-07
**Status:** IMPLEMENTATION COMPLETE, exit criterion pending. Everything is tested against a fake n8n; the real test (a business decision and a destructive approval answered from WhatsApp) needs the owner to import `docs/n8n/orq-workflow.json` into the VPS n8n and set `ORQ_N8N_TOKEN` plus `[notify].n8n_base_url`.

Design: `docs/superpowers/specs/2026-10-07-phase4-remote-human-design.md`. Plan: `docs/superpowers/plans/2026-10-07-phase4-remote-human.md`.

## 1. What changed in how a run behaves

* `orq run` and `orq resume` no longer prompt with `typer.prompt`. When a decision is raised the run prints it and polls SQLite every `[notify].answer_poll_seconds` until any channel answers: a daemon thread reading the terminal, `orq answer` in another window, the dashboard, or WhatsApp through the hub. `--no-prompt` keeps the Phase 2 behaviour (exit at the decision).
* One function records answers for every channel (`core/answers.py: record_answer`): option index mapping, pending check, `DECISIONS.md`, `answer` event with `via`. Previous duplicates in the CLI and the loop are gone.
* The run shows a Windows toast on new decisions and on `DONE`/`FAILED`/`ABORTED` (`notified` or `toast_failed` events). `winotify` drives PowerShell's toast API, no native dependency.

## 2. Hub (`orq dashboard`)

* FastAPI on `127.0.0.1:8765`, single HTML page. Run list refreshes every 5 s; the run page streams `events.jsonl` and the two agent logs over SSE, condensed to readable lines (assistant text, `[Tool] detail`, `[result]`, Codex messages). Decision cards offer the options as buttons, a free-text box, and approve/deny for destructive ones. Pause, abort and resume buttons call the same functions as the CLI; resume spawns a detached `orq resume` that waits for answers with no terminal.
* Smoke test against the real `~/.orq` (7 runs from Phases 1 to 3): list, detail, events (39 SSE lines for `RSPZNW`), both streams. A Phase 1 run still has the pre-Phase-2 `state.json` (no `version`), which crashed the first version of the run list; read-only views now use `Checkpoint.try_load` and show such runs without checkpoint details. `resume`/`control` keep the strict loader.
* WhatsApp loops run inside the hub only when `[notify].n8n_base_url` and the token variable are set; otherwise the hub says `WhatsApp: off` and serves the dashboard alone.

## 3. WhatsApp

* Outbound: pending decisions (SPEC 9.3 format, 1-based option numbers) and run completions, each once; reminders after `reminder_hours`. Runs that were already finished when the hub starts are not announced.
* Inbound: cursor in the `kv` table, batch ack, one confirmation or hint per message. Destructive decisions accept only `APPROVE <id>`/`DENY <id>`; a number gets the destructive hint. Commands `STATUS`, `PAUSE`, `RESUME` (spawns `orq resume`), `ABORT` (refuses live runs).
* Both loops swallow and log n8n errors so the dashboard keeps working when the VPS is unreachable.
* Deliverables for the VPS: `docs/n8n/orq_messages.sql`, `docs/n8n/orq-workflow.json` (hand-written against n8n 1.x node schemas; review each node after import), `docs/n8n-setup.md` with `curl` checks for the three endpoints.

## 4. Other findings

* Decision IDs are `D` plus 4 characters (`DQMKR`); the SPEC example `D7K2` has 3. The reply parser accepts 3 to 5.
* SQLite connections now use `check_same_thread=False`: the hub answers requests from worker threads and the CLI's terminal thread has its own `Store` anyway.
* The answer can land before the waiting run's first poll (the terminal thread or the dashboard are fast); the run then takes the `answer_applied` path without printing "answered via". Harmless; the event log has the channel.
* `pytest` wall time grew to about 4 minutes because the CLI tests drive full fake runs through the gate and the new waiting mode; still under the integration marker threshold.

## 5. Exit-criterion run: pending

Steps once the n8n workflow is live (section 5 of `docs/n8n-setup.md`):

1. `orq dashboard` in one terminal (`WhatsApp: on`).
2. `orq run ~/.orq/tasks/phase2-guard.md` in another: the hook denies `rm -rf probe.txt`; the phone receives `*[orq] D.... · orq-phase0-sandbox · iteration 1*` with `Reply: APPROVE D.... or DENY D....`; reply `APPROVE D....`; the run continues to the diff-rule decision, approve that one too, and the PR merges.
3. A task with an ambiguous requirement for the business decision (for example "add a discount helper; whether it applies before or after tax is not specified") answered with `D.... 1` from the phone.

Record the timings and the exact messages here, then mark Phase 4 complete in `docs/SPEC.md` section 15.

## 6. Carried into Phase 5

* Dashboard authentication if it ever leaves loopback.
* Run replay and summaries in the dashboard (SPEC Phase 5).
* Reviewer pane shows Codex's schema-shaped progress notes as JSON; a prettier condensation can parse the `summary` field.
