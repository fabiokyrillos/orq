# Phase 5 findings

**Date:** 2026-10-07
**Status:** COMPLETE. Exit criterion met on 2026-10-07: three tasks created and queued from the dashboard across two projects (`orq-phase0-sandbox`, `orq-phase5-sandbox-b`) with `max_concurrent_runs = 2`. Two ran at once, and the third started 3 s after a run parked on its plan approval gave its slot back. All three ended with a merged PR, and each shows its summary and an iteration-by-iteration replay in the dashboard. Total wall time for the three tasks: 6 minutes.

Design: `docs/superpowers/specs/2026-10-07-phase5-scale-design.md`. Plan: `docs/superpowers/plans/2026-10-07-phase5-scale.md`.

## 1. What changed

* **Projects.** One GitHub repo each, stored in SQLite, joined to runs on `repo`. Runs from earlier phases grouped themselves into the sandbox project on first open. A project is added with `orq project add` or the dashboard, from `owner/repo` or a local folder (only its `origin` is read), and validated with `gh repo view`.
* **Clones and git under concurrency.** Clones now live at `repos/<owner>/<repo>`; the run `R296LT` cloned `repos/fabiokyrillos/orq-phase0-sandbox` afresh, and the legacy `repos/orq-phase0-sandbox` stays for older runs. Commands that write shared refs take a cross-process lock `<clone>.orq.lock`.
* **Queue and slots.**
  * `QUEUED` now means "waiting for a slot".
  * A run takes a slot before agent work. It gives the slot back when it raises a decision and when it ends.
  * After an answer, a run waits for a slot ahead of runs that never started.
  * The hub's dispatcher spawns queued runs through `orq resume`.
  * `orq queue` and the dashboard's New task queue a task without starting it.
* **Summaries and replay.**
  * `summary.json` is written when a run ends. It records time per state, decisions with channel and wait, agent calls with tokens, checks, CI, guard hits and files changed.
  * The replay groups events and artifacts by step. A resumed turn merges into its step; a rolled-back turn maps to its `n.discarded-<ts>` directory.
  * No LLM is involved in either.
* **Dashboard.**
  * Left rail with a slot meter and one entry per project (running, waiting, queued counters).
  * Per project: Runs, Queue, New task (form with a live TASK.md preview, or pasted markdown) and Settings.
  * Run page: Live, Summary and Replay tabs, plus Run again.
  * Every non-GET request needs the per-start token from the page. Requests for any other Host are refused.

## 2. Exit-criterion runs (hub on the PC, WhatsApp on)

Setup from the dashboard:
* added `fabiokyrillos/orq-phase5-sandbox-b` (created for this phase with a `calc.add` seed and CI) from the Add project page;
* set its cap to 2 in Settings, so its two tasks could overlap while the sandbox kept the default cap of 1;
* pasted three TASK.md files (`~/.orq/tasks/phase5-*.md`) into New task, in this order: A `R296LT`, B `RBKXUK` with plan approval required, B `RPYCZH`.

Times are UTC from the event logs.

```
19:09:35 R296LT queued (A)   19:09:37 spawned by the hub, slot 1/2, PLANNING
19:09:48 RBKXUK queued (B)   19:09:52 spawned, slot 2/2, PLANNING
19:10:02 RPYCZH queued (B)   slots 2/2: stays queued, no process
19:10:34 RBKXUK AWAITING_PLAN_APPROVAL DCDRR -> slot released; toast; WhatsApp at 19:10:38
19:10:37 RPYCZH spawned 3 s after the release, slot 2/2, PLANNING
19:11:17 DCDRR approved from the dashboard (43 s after it was raised)
19:11:20 RBKXUK waiting_for_slot (2/2 held) -> QUEUED
19:12:25 R296LT PR #7, CI passed after 21 s, final review done
19:13:20 R296LT DONE (PR #7 merged), slot released
19:13:26 RBKXUK takes the freed slot (6 s, one poll), IMPLEMENTING 1
19:13:47 RPYCZH DONE (sandbox-b PR #1 merged)
19:14:40 RBKXUK PR #2; 19:14:45 base had moved (PR #1) -> rebased and force-pushed by orq; CI passed after 21 s
19:15:35 RBKXUK DONE (sandbox-b PR #2 merged)
```

Confirmed afterwards on GitHub: sandbox PR #7 and sandbox-b PRs #1 and #2 are `MERGED`; `sandbox-b` main holds `calc.py` (add, subtract, multiply) and `percent.py` with their tests.

| run | project | wall | queued | planning | implementing | reviewing | finalizing | decisions |
|---|---|---|---|---|---|---|---|---|
| R296LT | sandbox | 3m 47s | 6 s | 51 s | 58 s | 47 s | 62 s | none |
| RPYCZH | sandbox-b | 3m 46s | 43 s | 38 s | 40 s | 33 s | 71 s | none |
| RBKXUK | sandbox-b | 5m 48s | 2m 14s | 40 s | 27 s | 38 s | 61 s | DCDRR, 45 s awaiting approval |

* The queue behaved as designed. The release on a decision let RPYCZH run during RBKXUK's plan approval. After the answer, RBKXUK waited 2 minutes for a slot instead of exceeding the limit, then took the first one that freed.
* Two runs on one repo merged one after the other without conflict. The second one went through the Phase 3 rebase path (`rebased`, force push, CI again) because they touched different files.
* Dispatch latency is one dispatcher tick (`[queue].poll_seconds = 5`): 2 to 5 s from queue or release to spawn. A run that waits for a slot polls at the same interval (6 s observed).
* Codex usage (5-hour window, Plus plan) went from 8 % to 13 % for the three runs: 3 planner calls, 6 milestone reviews, 3 final reviews. Two concurrent runs at this size are well inside the limit; the router never switched to the Claude reviewer.
* WhatsApp still worked across projects. DCDRR reached the phone while the dashboard answered it, and the three completions were announced with their repo names.

## 3. Bugs found and fixed

* **Live panes repeated fragments of old lines.** Run logs are written in text mode, so on Windows every line ends with CRLF. The SSE follower counted decoded characters, drifted one byte per line, and once more than two lines had been read it re-sent tails of earlier lines (a lone `}`). This had been there since Phase 4; the Phase 4 tests only used `follow=0`. It now reads bytes (`test_sse_follow_handles_crlf_files_without_repeating_lines`).
* **Wall time counted the owner's time.** Before this phase, waiting for an answer counted toward `max_wall_hours`, so RHGBG6's 91-minute wait counted against the owner's 2-hour limit. Time spent waiting for the owner or for a slot no longer counts.
* **Empty task titles.** `parse_task` accepted an empty `# Task:` heading by taking the next line (`## Repo`) as the title. The title regex is now limited to one line.
* **Stale "needs you" flags.** Decisions left pending by runs aborted in Phases 2 and 3 marked those runs as "needs you" in the dashboard. Finished runs no longer show pending decisions, and `abort` marks them `expired`.
* **Missing queue time in summaries.** Time in the queue before the process started was missing from the summary's QUEUED time, because the first `state` event comes at spawn. The `queued` event now opens it.
* **Settings header after a save.** The Settings page did not re-render after a save, so the header kept the old cap.

## 4. Other findings

* The planner still splits these 20-line tasks into two milestones. Every run needed a second, nearly empty iteration (Phase 3 note).
* Answering on one channel does not tell the other: after DCDRR was answered on the dashboard, the phone still showed the question without a follow-up. Reminders stop, because the decision is no longer pending.
* The run summaries of the older runs match the earlier findings: RV348F 2m 48s (Phase 3), and RHGBG6 5721 s with 91 minutes awaiting the owner (Phase 4).

## 5. Spec changes made in Phase 5

* Header status; section 5 (store, projects, queue and dispatcher, hub hardening); section 6 (`QUEUED`, `summary.json`); section 10.1 (slot rules); section 10.6 (clone path, repo lock); section 11 (layout); section 13 (`[queue]`); section 14 (`orq queue`, `orq project`); section 15 (Phase 5 scope, exit criterion, status).

## 6. Carried forward

* Real projects (the volleyball repo and others), now that the ecosystem is proven on sandboxes.
* Letting the reviewer mark several milestones done at once, or a planner rule against splitting small tasks.
* A WhatsApp note when a decision is answered on another channel.
* Dashboard authentication if the hub ever leaves loopback; queue reordering.
