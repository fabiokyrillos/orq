# Phase 4 findings

**Date:** 2026-10-07
**Status:** COMPLETE. Exit criterion met on 2026-10-07: in run `RHGBG6` the owner answered a reviewer question (`DRF2C 1`) and two destructive approvals (`APPROVE DQ4K9`, `APPROVE DYN7V`) from WhatsApp, the run continued each time and ended with PR #6 merged, announced back on WhatsApp.

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
* Deliverables for the VPS: `docs/n8n/orq-workflow.json` (hand-written against n8n 1.x node schemas, Data table node 1.1; review each node after import), `docs/n8n-setup.md` with PowerShell checks for the three endpoints. Storage is an n8n Data table, not Postgres: the owner has no Postgres at hand.

## 4. Other findings

* Fixes needed on the imported workflow: the owner replaced the HTTP Request node with the community Evolution node and typed `=$json.body.text` (literal text; it must be `{{ $json.body.text }}`); the two Data table conditions on `id` lost their column after the node UI reloaded the schema and had to be re-selected; the `OWNER_NUMBER` placeholder in the `If` node had to be replaced. All visible in the pasted JSON before the first test.
* `[Environment]::SetEnvironmentVariable(..., "User")` writes `HKCU\Environment`; processes started earlier (the shell driving orq) never see it, so the hub first reported `WhatsApp: off`. `read_secret` now falls back to the user's persistent environment on Windows.
* Decisions left pending by aborted runs (three of them from Phases 2 and 3) would have been sent to the phone at hub start; the outbound loop now skips decisions whose run is finished.

* Decision IDs are `D` plus 4 characters (`DQMKR`); the SPEC example `D7K2` has 3. The reply parser accepts 3 to 5.
* SQLite connections now use `check_same_thread=False`: the hub answers requests from worker threads and the CLI's terminal thread has its own `Store` anyway.
* The answer can land before the waiting run's first poll (the terminal thread or the dashboard are fast); the run then takes the `answer_applied` path without printing "answered via". Harmless; the event log has the channel.
* `pytest` wall time grew to about 4 minutes because the CLI tests drive full fake runs through the gate and the new waiting mode; still under the integration marker threshold.

## 5. Exit-criterion run `RHGBG6` (owner on the phone, hub on the PC)

Setup done by the owner in about an hour from the guide: Data table `orq_messages`, two Header Auth credentials, workflow imported, Evolution webhook. Three fixes were needed after import (section 4 below). Endpoint checks from the PC before the run: `notify` 200, `replies` returned the owner's `hello` (id 1), `ack` consumed it, a request without the token got 403.

```
16:33:16 QUEUED           orq run ~/.orq/tasks/phase2-guard.md (default wait mode, no terminal thread: stdin was not a tty)
16:33:18 PLANNING         2 milestones
16:34:12 IMPLEMENTING 1   hook: deny recursive_delete `rm -rf probe.txt`; CHANGELOG.md written
16:34:57 AWAITING_HUMAN   DRF2C (reviewer, blocked) -> toast at :57, WhatsApp at 16:35:12 (hub outbound poll)
18:04:45 answer           "DRF2C 1" from the phone -> applied at 18:04:48 (hub inbound poll 20 s, run poll 3 s)
18:04:48 AWAITING_HUMAN   DQ4K9 (guard, destructive) -> WhatsApp
18:05:29 answer           "APPROVE DQ4K9" -> token written, implementer ran the command at 18:05:37 (allow-by-token)
18:05:51 AWAITING_HUMAN   DYN7V (diff guard, deleted_file) -> WhatsApp
18:06:31 answer           "APPROVE DYN7V" -> applied, check ok, commit, review
18:06:57 IMPLEMENTING 3   reviewer asked for one more pass (milestone 2), done
18:07:39 FINALIZING       PR #6; CI passed after 21 s; final review done at 18:08:29
18:08:37 DONE             squash merge, worktree removed; toast and WhatsApp "RHGBG6 · orq-phase0-sandbox · DONE" at 18:08:42
```

The 90-minute gap between the first notification and the first answer was the owner being away; the hub kept the dashboard up and would have sent a reminder at 3 h. Each WhatsApp hop (phone to applied answer) took 3 to 5 s on top of the 20 s inbound poll.

Confirmed: numbered answers for ordinary decisions, `APPROVE <id>` for destructive ones, confirmations sent back after each, cursor advanced to 4, no stale decision from earlier aborted runs was announced.
## 6. Carried into Phase 5

* Dashboard authentication if it ever leaves loopback.
* Run replay and summaries in the dashboard (SPEC Phase 5).
* Reviewer pane shows Codex's schema-shaped progress notes as JSON; a prettier condensation can parse the `summary` field.
