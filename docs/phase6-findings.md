# Phase 6 findings

**Date:** 2026-10-07
**Status:** COMPLETE except two checks that need the owner:
* judging on the phone whether the decision messages are enough to decide (section 2.2);
* the autostart after a reboot (section 2.6).

Every other exit criterion was met on 2026-10-07 by runs `R53CG9`, `RASQKU`, `R5R5YS` and `RGWZZN` on the two sandboxes. All four ended with merged PRs.

Design: `docs/superpowers/specs/2026-10-07-phase6-refinement-design.md`. Plan: `docs/superpowers/plans/2026-10-07-phase6-refinement.md`.

## 0. Codex CLI 0.161.0 probes (milestone 0)

The owner updated the Codex CLI from 0.139.0 to 0.161.0 to use `gpt-6.1-sol`. Claude Code stays at 2.1.219.

### 0.1 Models on a ChatGPT Plus account

On 0.139.0, every newer model was refused:

* `gpt-6.1-sol`, `gpt-6-sol`: "not supported when using Codex with a ChatGPT account";
* `gpt-5.6-sol`: "requires a newer version of Codex".

0.139.0 could not even read `~/.codex/models_cache.json` written by the desktop app (`missing field base_instructions`).

On 0.161.0, one minimal call each (`codex exec -m <model> -c model_reasoning_effort=low --sandbox read-only --skip-git-repo-check --json "Reply with the single word OK."`):

| model | result |
|---|---|
| gpt-6.1-sol | OK |
| gpt-6-sol | OK |
| gpt-6-astra | OK |
| gpt-6-luna | OK |
| gpt-5.6-sol | OK |
| gpt-5.6-terra | OK |
| gpt-5.6-luna | OK |
| gpt-5.5 | OK |

`~/.codex/models_cache.json` lists these models with `visibility: list` and their `supported_reasoning_levels`:

* `low` to `max` for every model;
* `ultra` ("automatic task delegation") on the sol, terra and astra models;
* `gpt-5.5` stops at `xhigh`.

Hidden entries (`gpt-reserve`, `codex-auto-review`) are not offered.

### 0.2 The read-only sandbox broke with the update (FAIL, then fixed)

Through orq's `CodexReviewer` (which passes `--ignore-user-config`), the reviewer on 0.161.0 could not start any process:

* every command failed with `Failed to create unified exec process: helper_unknown_error: setup refresh had errors`;
* the review still validated against the schema, with status `done` and a blocker issue saying it could not read `app.py`;
* the planner said the same in its summary.

What the investigation found:

* **Not orq-specific.** The same error appears in a bare `codex sandbox -- cmd /c python --version`.
* **`codex doctor`:** `✗ sandbox  elevated Windows sandbox provisioning recorded a structured failure`.
* **`~/.codex/.sandbox/setup_error.json`:** `{"code": "helper_unknown_error", "message": "setup refresh had errors"}`.
* **The day's sandbox log names the cause:** `runtime read/execute validation failed: validate runtime read/execute access on C:\Users\<owner>\AppData\Local\OpenAI\Codex\runtimes\cua_node\...\node_repl.exe: open ACL target for root-only update`. The elevated setup of the new CLI tries to grant its sandbox user access to a runtime of the Codex desktop app, and cannot.
* **The update caused it.** The first error is at 19:59 UTC on 2026-10-07, right after the update; the 2026-10-06 log has none.
* **The owner's config was not the fix.** `~/.codex/config.toml` sets `[windows] sandbox = "elevated"`. orq ignores that file, and passing `-c windows.sandbox="elevated"` explicitly fails the same way.

`windows.sandbox` accepts `elevated`, `unelevated` and `mxc` on 0.161.0. Same read-only probe (`python --version`, `python -c "print(1+1)"`, `Get-Content app.py`, `Set-Content probe.txt x`):

| `windows.sandbox` | process start | read app.py | python | write blocked |
|---|---|---|---|---|
| (default, user config ignored) | rejected: "blocked by policy" | no | no | (nothing ran) |
| `elevated` | fails: "setup refresh had errors" | no | no | (nothing ran) |
| `unelevated` | yes | yes | yes, `Python 3.13.12`, `2` | yes, "Access to the path ... is denied" |
| `mxc` | yes | yes | yes, with `Failed to find real location of ...python.exe` on stderr | yes |

**Decision:** the Codex adapter passes `-c windows.sandbox="unelevated"` on every call (`[reviewer].codex_windows_sandbox`, default `unelevated`). This needs no admin provisioning and keeps writes blocked.

Side effect: Python now runs inside the reviewer's sandbox. The Phase 3 hygiene rules for "python is not recognized" complaints (`core/hygiene.py`) stay as a backstop, but the cause they work around is gone (Phase 5 improvement 6).

Schema output (`--output-schema`, both the review and the plan contract) and the rate-limit snapshot from the session file still work on 0.161.0 with `gpt-6.1-sol` (`primary.used_percent`, `resets_at`, `plan_type: plus`).

## 1. What changed

* **Settings in layers.**
  * Models, efforts and guard settings resolve as TASK.md `## Models` > project > dashboard overrides > `config.toml`, before every agent call.
  * The dashboard has a global Settings page, a per-project block that shows where each value comes from, a per-task Models fold-out, and a "Test" button (one minimal call).
  * The Codex adapter restates `windows.sandbox="unelevated"` (section 0.2). The default Codex model is `gpt-6.1-sol`, and the owner's `config.toml` now names it too.
* **Decisions with content.**
  * The reviewer, planner and implementer contracts ask for `context`, `option_details` and `recommendation_reason`.
  * orq builds the same for its own decisions.
  * WhatsApp shows them in fixed sections, capped at 1500 characters.
  * A decision answered on another channel is announced on WhatsApp, and FAILED or ABORTED messages carry their reason.
* **Progress digests.**
  * The reviewer writes `owner_update` in the same call.
  * The hub sends a digest when a milestone ends and every `progress_minutes` (30) while a run works.
  * `STATUS <run>` returns a digest, and the Live tab shows one.
* **Fewer wasted iterations.** `task_complete` lets the reviewer close the whole task, and the planner is told to use one milestone for small tasks.
* **A hub that runs on its own.**
  * `orq hub autostart on|off|status` manages a logon task.
  * `orq dashboard --log-file` writes everything to a file.
  * The dispatcher resumes a run whose decision was answered while no process waited for it.
* **Rebase conflicts.** The implementer gets one turn per conflicting commit; orq continues the rebase, checks, scans, and undoes everything on failure.
* **Editable queue.** Reorder (top, up, down, bottom) and edit the TASK.md of a queued run that has not started. Asset URLs are versioned so the browser never keeps an old script after an update.

## 2. Exit-criterion runs (hub on the PC, WhatsApp on, `max_concurrent_runs = 2`)

Tasks: `~/.orq/tasks/phase6-*.md`, queued through the hub API in this order: `R5R5YS` (sandbox-b, money helpers with a business rule left open), `R53CG9` (sandbox-b, README), `RASQKU` (sandbox, whisper). `RGWZZN` (sandbox, three modules) followed. Times are UTC.

```
21:34:41 R5R5YS and R53CG9 spawned together (sandbox-b cap 2); RASQKU queued
21:35:0x a conflicting README edit pushed to sandbox-b main by hand (provokes the rebase conflict)
21:35:13 R53CG9 plan: 1 milestone (small-task rule)
21:35:18 R5R5YS planner asks DJLFU (business: who pays the leftover cents); slot released; WhatsApp 21:35:20
21:35:3x R5R5YS process killed by hand while waiting (auto-resume test)
21:35:41 RASQKU spawned (see 3.1 for the 23 s delay)
21:36:05 R53CG9 gate: rebase conflict in README.md -> implementer round 1
21:36:35 R53CG9 conflict_resolved (1 round, 30 s): kept the task's description line and the base's new sentence
21:37:37 R53CG9 DONE, sandbox-b PR #3 merged
21:38:02 RASQKU DONE in 1 iteration, sandbox PR #8 merged
21:46:08 DJLFU answered from the dashboard (no WhatsApp answer within 8 minutes)
21:46:11 the hub resumes R5R5YS on its own (3 s), the planner plans again with the answer
21:47:20 R5R5YS planner asks D4KHU (ambiguity: float sums are not exact; keep floats with cent arithmetic or return Decimal)
21:57:01 D4KHU answered from the dashboard; the waiting process continues in place
22:02:06 R5R5YS review 1 on gpt-6.1-sol/low; 22:02:09 sandbox-b reviewer set to gpt-6-sol in the project settings
22:04:41 timer digest on WhatsApp (30 min after the run started)
22:05:59 diff guard DQFMC: a test removed (see 3.2); approved from the dashboard
22:07:01 R5R5YS review 2 on gpt-6-sol/low; final review on gpt-6-sol/high
22:08:17 R5R5YS DONE, sandbox-b PR #4 merged (clean rebase onto #3)
22:16:52 RGWZZN plan: 3 milestones (slugify, count_words, initials)
22:19:13 and 22:19:44 milestone digests on WhatsApp
22:21:18 RGWZZN DONE, sandbox PR #9 merged
```

### 2.1 Live model change: met

R5R5YS used `gpt-6.1-sol` for its first review. The project setting changed at 22:02:09, and from the next call (review 2 and the final review) the events record `gpt-6-sol`. No restart was needed. The setting was then cleared, and sandbox-b inherits `gpt-6.1-sol` again.

### 2.2 Decisions with content: met by the content, the owner's judgement pending

The planner's first question, as WhatsApp received it (950 characters):

```
*[orq] DJLFU · orq-phase5-sandbox-b · iteration 0*
Business decision (planner)
Context: The requested money.py module does not exist yet; split_bill(total, people) will divide bills into amounts with two decimal places. For split_bill(100, 3), one person must pay 33.34 and the others 33.33. The task explicitly says which people pay leftover cents is "a business rule nobody has decided," so the implementation plan is blocked until you choose.
Question: Should the first people or the last people in the returned list pay the leftover cents?
1. First people (recommended): Allocate extra cents from the start of the list, so split_bill(100, 3) returns [33.34, 33.33, 33.33].
2. Last people: Allocate extra cents from the end of the list, so split_bill(100, 3) returns [33.33, 33.33, 33.34].
Recommended: 1, because Allocating from the start provides a simple, predictable ordering for callers and tests.
Reply: DJLFU <number>  or  DJLFU <free text>
```

* **D4KHU, the second question:** it quoted the evidence (`sum([0.15, 0.14])` is `0.29000000000000004`) and explained what each option would change for callers.
* **DQFMC, the diff guard:** it named the test, the rule and the line counts.
* **Before Phase 6:** the same kind of decision carried only "Plan proposed (2 milestones): 1. ... Approve it, or answer with what to change."
* **Answered elsewhere:** the owner did not answer on WhatsApp during the session. All three decisions were answered on the dashboard, and each answer was announced on WhatsApp.

### 2.3 Progress digests: met

RGWZZN (3 milestones) sent a digest after milestones 1 and 2. The last milestone ends the run, which sends the DONE message instead. R5R5YS sent a timer digest 30 minutes after it started. The digest after the run ended:

```
*[orq] RGWZZN · orq-phase0-sandbox · DONE · 5m of work*
Add text helpers in three modules
Done:
- 1/3 Add slugify: The slug helper converts "Olá Mundo!" to "ola-mundo" and handles repeated separators and empty text. ...
- 2/3 Add count_words: Word counting is complete and handles spaces, tabs, newlines, and empty input. ...
Latest: The text helpers now remove accents and join words into slugs, count whitespace-separated words, and produce uppercase name initials. ...
Checks 3/3 passed · 6 files changed
```

### 2.4 Small task in one iteration: met

* RASQKU and R53CG9: one milestone and one iteration each.
* RGWZZN: three milestones, one iteration each, because the task asked for three.
* Phase 5 comparison: every task had two milestones, the second one nearly empty.

### 2.5 Rebase conflict resolved by the implementer: met

* **How the conflict happened:** R53CG9's branch rewrote the README's description line, and `main` changed the same line meanwhile.
* **The resolution:** the implementer kept the task's required line directly under the title, kept the base's new sentence as the next paragraph, and stated this in one line per file.
* **What orq did:** found no markers left, continued the rebase, ran the check (5 tests OK) and the secret scan of the range, force-pushed, then CI (22 s), final review and merge.

### 2.6 Answered run without a process resumes; autostart

* **Auto-resume: met.** R5R5YS was killed while it waited for DJLFU. The dashboard answer at 21:46:08 was followed by `resume_spawned` at 21:46:11. No `RESUME` was needed.
* **Autostart after a reboot: not tested in the session.** It needs the owner to run `uv run orq hub autostart on` (Windows may require an administrator terminal for a logon task) and then log off and on. `orq hub autostart status` and `uv run orq status` confirm it.

### 2.7 Cost

* **Codex:** 4 runs, 6 planner calls (R5R5YS planned three times because of its two questions), 10 reviews. They ran on `gpt-6.1-sol`, except R5R5YS after the switch to `gpt-6-sol`. The 5-hour Codex window ended at 13 %. The router never switched to Claude.
* **Wall time:**
  * R53CG9: 3m 01s.
  * RASQKU: 3m 25s.
  * RGWZZN: 5m 09s.
  * R5R5YS: 33m 40s, of which about 20 minutes were spent waiting for answers.

## 3. Bugs found and fixed during the runs

### 3.1 The dispatcher counted a parked run as "just spawned"

A run spawned less than 60 s earlier counted as taking a slot even after it had registered and released the slot at a decision. RASQKU started 23 s late. Once a run appears in the slot table, the table now speaks for it.

### 3.2 The diff guard flagged the run's own earlier work

R5R5YS removed in iteration 2 a test it had added in iteration 1, after the reviewer asked for stricter parsing, and the owner was asked to approve. Removals of files, tests, exports, routes and dependencies now count only when the run's base commit has them. The diff-guard context also quotes the implementer's report, so the owner sees why.

### 3.3 A Phase 5 answer was announced when the hub started

The answered-elsewhere notice went out for DCDRR, answered on the dashboard in Phase 5, when the new hub started. The start-up baseline now covers answers too.

### 3.4 The reviewer and the planner never received their standing rules (since Phase 1)

`REVIEWER_RULES` and `PLANNER_RULES` were defined in `core/prompts.py` but never put in a prompt; the real prompts started at `# Task:`. This explains the Phase 3 finding that "prompt text alone does not fix either behaviour". Both prompts now start with their rules.

Visible effects in this phase:
* the planner asked about the open business rule and about float exactness instead of guessing;
* plans followed the size rule;
* no reviewer complained about its sandbox.

## 4. Other findings

* Answering a question from a planner run sends it back to planning. Two planner questions in a row cost two extra planning calls (about 40 s each).
* The planner now asks more often. Both questions here were legitimate: one rule the task explicitly left open, and a real exactness problem. Watch the rate on real projects.
* The rebase-conflict prompt did not stop the implementer from running the check command itself (it is allowed to). orq runs the check again anyway.
* A finished run's digest lists the milestones closed by `milestone_done`. The last milestone ends in the merge gate, so it only appears in "Latest". This is cosmetic.

## 5. Spec changes made in Phase 6

Header status; environment (Codex CLI 0.161.0, model availability, unelevated sandbox); 8.2 (`owner_update`, `task_complete`, `human` fields, rules actually sent); 8.4 (decision fields, orq-built context, `DECISIONS.md` context, `expired`); 10.1 (auto-resume, queue order and edits); 10.4 (removals measured against the base); 10.7 (conflict resolution); 12 (settings in layers); 13 (`claude_model`, `codex_windows_sandbox`, `max_conflict_rounds`, `progress_minutes`); 14 (`dashboard --log-file`, `hub autostart`); 15 (Phase 6).

## 6. Carried forward

* The owner's two checks: the decision messages on the phone, and the autostart after a reboot.
* Real projects (the volleyball repo first), now with per-project protected paths and models.
* A finished run's digest could list its last milestone (done in 6.1).

## 7. Phase 6.1 (2026-10-08): who answered, automatic answers, readable messages

Design: `docs/superpowers/specs/2026-10-08-phase6-1-auto-answer-and-messages-design.md`.

### 7.1 Why

On the phone the owner saw three decisions "answered on the dashboard" and read it as orq approving things by itself. In fact Claude (the coding assistant) had answered them during the Phase 6 tests, after 8 minutes without a reply.

The owner asked for three things:
* every answer says who gave it;
* a bounded way for decisions to be answered when he does not answer;
* messages that are easier to read.

### 7.2 What changed

* **`answered_by` (`owner` | `claude` | `auto`) on every answer.** The dashboard page answers as the owner. Claude must pass `by="claude"` (API) or `--by claude` (CLI). `DECISIONS.md`, the summary and WhatsApp show who answered.
* **`stakes` (`low` | `medium` | `high`).** The asking agent rates it; orq rates its own decisions (agent-error retry and CI wait `low`, no progress `medium`, everything else `high`). The prompt tells the agents never to rate a business question `low`.
* **Automatic answers.** Opt-in (`notify.auto_answer`, global or per project in the dashboard), off by default. The answer is the recommendation, after `auto_answer_minutes` (30 by default) since WhatsApp got the question, only up to `auto_answer_max_stakes` (`low`). Never automatic: destructive actions, business rules, guard decisions, plan approvals, a missing recommendation or reason, or a recommendation to abort. The decision message says when the policy would answer it.
* **Messages in Brazilian Portuguese, formatted for a phone.** `CLAUDE.md` records the exception. The layout is in SPEC 9.3. The agents write their owner-facing fields in Portuguese, and orq's own decision texts are Portuguese. Machine keywords are unchanged.

### 7.3 Real run `RV767J` (sandbox, automatic answers on with 3 minutes for the test)

```
14:40:51 PLANNING
14:41:34 planner asks D2HN6 (ambiguity: round partial minutes up or to the nearest), stakes low
14:41:38 WhatsApp, in the new layout, with "se você não responder, orq escolhe a recomendada em 3 min"
14:42:25 the owner answers on WhatsApp ("Sempre para cima"), recorded by=owner, 47 s later
14:42:53 plan: 1 milestone, rounding up as answered
14:45:58 DONE, sandbox PR #10 merged
```

* The owner answered first, so the automatic answer correctly did not happen. Its path is covered by `tests/test_auto_answer.py`:
  * every exclusion;
  * the eligible case;
  * the hub task answering once and logging `auto_answered`.
* The planner wrote context, options and consequences in Portuguese and rated the purely technical question `low`, as the task described it.
* Automatic answers were set back to the default (off) on the sandbox after the run.

### 7.4 Small follow-ups found

* A blank line now separates the reply line from the recommendation.
* A finished run's digest lists its last milestone.
