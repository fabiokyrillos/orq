# Phase 6 design: refinement before real projects

Date: 2026-10-07. Source of truth remains `docs/SPEC.md`. This document fixes the design of Phase 6, which the SPEC did not plan. The owner asked for it after Phase 5 (2026-10-07):

* change models whenever he wants (Codex: `gpt-6.1-sol`);
* questions with enough content to decide from the phone;
* progress summaries during long tasks;
* every improvement listed after Phase 5.

Milestone 0 (probes) is recorded in `docs/phase6-findings.md` section 0: Codex CLI 0.161.0 accepts `gpt-6.1-sol` on the Plus plan; its read-only sandbox only works with `windows.sandbox = "unelevated"`.

Decisions taken with the owner (2026-10-07):

* **D1** Models and reasoning efforts are editable in the dashboard at any time, in layers. A run resolves them before every agent call, so a change applies at the next call, even mid-run.
* **D2** Codex CLI 0.161.0 (updated by the owner). Default Codex model `gpt-6.1-sol`.
* **D3** Decisions carry context, a consequence per option and the reason for the recommendation. For model decisions these are fields of the output contracts; orq's own decisions build them from evidence. WhatsApp messages use fixed sections, about 1500 characters. A decision answered elsewhere is announced. FAILED and ABORTED carry their reason.
* **D4** Progress digests: the reviewer writes a short owner update every review (a new contract field, no extra call). The hub sends a digest when a milestone ends and every `[notify].progress_minutes`; `STATUS <run>` returns one on demand.
* **D5** No dashboard authentication beyond the loopback token. The queue becomes editable: edit, reorder, cancel.
* **D6** On a rebase conflict, the implementer gets one bounded attempt to resolve it before the owner is asked.

Exit criterion:

* the owner changes the reviewer model in the dashboard while a run is in progress, and the next review uses it;
* a business decision reaches WhatsApp with enough content to decide without opening anything else (owner judges);
* a task with 3 or more milestones sends progress digests;
* a small task finishes in one iteration;
* a provoked rebase conflict is resolved by the implementer and merged;
* a decision answered while its run has no process resumes on its own;
* after a reboot the hub starts by itself and runs the queue.

## 1. Settings in layers (milestone 1)

### 1.1 Layers and keys

Effective value = the first one set, from most to least specific:

1. the task: optional `## Models` section in TASK.md, `key: value` lines;
2. the project: a `settings` JSON column on `projects`;
3. global overrides: a new `settings` table, written by the dashboard;
4. `config.toml`;
5. code defaults.

| key | used for | live |
|---|---|---|
| `implementer.default_model` | implementer on `hard` milestones and when there is no plan | next call |
| `implementer.mechanical_model` | implementer on `mechanical` milestones | next call |
| `reviewer.codex_model` | planner and reviewer when Codex | next call |
| `reviewer.claude_model` | reviewer when Claude (primary or fallback); default: `implementer.default_model` | next call |
| `reviewer.routine_effort` | milestone reviews | next call |
| `reviewer.final_effort` | planning and the final review | next call |
| `git.protected_paths` | guard hook and diff rules | run start or resume |
| `guard.max_net_deleted_lines`, `guard.source_globs` | diff rules | run start or resume |

Task-level keys are short aliases: `implementer`, `mechanical`, `reviewer`, `claude_reviewer`, `routine_effort`, `final_effort`. Guard keys exist only at the global and project levels.

### 1.2 Resolution and the adapters

* `src/orq/core/settings.py: resolve(config, store, repo, task) -> Effective` is pure over its inputs. `Effective.source(key)` names the layer each value came from, for the dashboard.
* The runner calls it before every agent call and passes `model=` and `effort=` explicitly. `CodexReviewer.run` and `ClaudeReviewer.run` accept `model=`; the router forwards it to whichever adapter it uses. Events `planner`, `implementer` and `reviewer` record `model` and `effort`.

### 1.3 Validation and the dashboard

* **Codex models:** the list comes from `~/.codex/models_cache.json` (`visibility == "list"`, with `supported_reasoning_levels`). An effort a model does not support is refused when saving.
* **Claude models:** free text, with suggestions `opus`, `sonnet`, `haiku`.
* **"Test model" button:** runs the milestone 0 probe (one minimal call, low effort) in a worker thread and shows OK or the CLI error.
* **Pages:** a global Settings page and the project Settings tab. Both show the effective value and its source, with a "reset to inherited" control. New task gets an optional "Models" fold-out that writes `## Models`.

### 1.4 Codex sandbox

`[reviewer].codex_windows_sandbox = "unelevated"` (finding 0.2); the adapter passes `-c windows.sandbox="<value>"`. An empty value omits the flag.

## 2. Decisions with content (milestone 2)

### 2.1 Contract changes

* The `human` object of the review and plan contracts and the implementer's `orq-decision` marker gain three fields, all required in the strict schemas:
  * `context`: what the run was doing and why it needs the owner;
  * `option_details`: one consequence per option, same order as `options`;
  * `recommendation_reason`.
* Prompts describe what a good context contains:
  * the current milestone;
  * the evidence: file, command, error or acceptance criterion;
  * what is blocked until the answer;
  * no repo-internal jargon without a one-line explanation.
* The parser tolerates the old marker shape (fields missing → empty).

### 2.2 Storage

`Decision` and the `decisions` table gain `context`, `option_details` (JSON) and `recommendation_reason`. The columns are added with `ALTER TABLE` when missing.

### 2.3 orq's own decisions

| kind | context and consequences |
|---|---|
| `plan_approval` | planner summary; each milestone with goal, done-when and difficulty; what approve and revise do |
| `guard_pre` | the exact command or path; the guard rule and why it is dangerous; approve = token for this exact action once; deny = the implementer proceeds without it |
| `guard_diff` | each violation with file and line counts; approve = keep and continue to the check; deny = reset to the last iteration commit |
| `secret` | rule, file and line from gitleaks (values redacted); what rescan and abort do |
| `progress` | the stall rule, the last summaries and what each of continue, rollback to n and abort discards |
| `error` | role, error kind, the error text (trimmed); retry and abort |
| `rebase_conflict` | conflicted files, the implementer's attempt (section 6), what retry and abort do |
| `ci_none`, `ci_timeout`, `gate_rounds` | PR number, checks seen and their states, the last failure reason |

### 2.4 Rendering

* **WhatsApp** (`format_decision`), in this order:
  * header line `*[orq] D… · repo · iteration n · milestone k/m*`;
  * `Context:`, `Question:`;
  * the numbered options, each with its consequence;
  * `Recommended: n, because …`;
  * the reply line.
* The message is capped at 1500 characters; the context is cut first, with `(more in the dashboard)`.
* **Dashboard:** the decision card shows everything, uncut.

### 2.5 Answered elsewhere and failure reasons

* When a decision that was sent on WhatsApp is answered on another channel, the hub sends `[orq] D… answered on <channel>: <answer>` once (notification kind `decision_answered`).
* `FAILED` and `ABORTED` messages carry the `reason` of the final state event.

## 3. Progress digests (milestone 3)

* **New contract field:** the review contract gains `owner_update`, 2 to 4 plain sentences (what this iteration achieved, what comes next). It is required; an empty string is allowed when nothing changed. The runner stores it (`state.json` `updates[iteration]`) and logs an `owner_update` event.
* **Digest builder:** `src/orq/core/digest.py: build_digest(run_dir) -> str` is pure over `events.jsonl`, `state.json` and the plan. It produces:
  * a header: run, repo, title, state, elapsed time (owner and queue time excluded);
  * `Done:` the milestones done, with the update that closed each one;
  * `Now:` the current milestone and phase, the last owner update, and any pending decision;
  * numbers: iteration, checks passed/run, files changed so far.

  It is capped at 1500 characters, oldest milestones trimmed first.
* **When the hub sends it** (notification kind `progress`):
  * on every `milestone_done` that does not end the run;
  * every `[notify].progress_minutes` (default 30) for a run that is not waiting for the owner and started more than that long ago;
  * `0` disables both.
* **Elsewhere:** `STATUS <run>` returns the digest. The dashboard's Live tab shows it as a card above the streams.

## 4. Fewer wasted iterations (milestone 4)

* **Task-level completion:** the review contract gains `task_complete` (boolean). A clean `done` (no blocker or major issue) with `task_complete: true` and every acceptance criterion met goes straight to `finalize`, whatever milestones remain. The event `milestones_skipped` lists them.
* **Smaller plans:** the planner prompt asks for one milestone when the task is small (few files, roughly under 150 changed lines), and for at most as many milestones as there are independent deliverables.

## 5. A hub that runs on its own (milestone 5)

* **Autostart:**
  * `orq hub autostart on|off|status` registers or removes a Windows scheduled task `orq-hub` at logon (`schtasks /Create /SC ONLOGON /RL LIMITED`). It runs the venv's `pythonw.exe -m orq dashboard --log-file %USERPROFILE%\.orq\hub.log`.
  * The command builder is pure and tested; the owner runs the command, because it changes persistent Windows configuration.
  * `orq dashboard --log-file` sends uvicorn and hub logs to that file.
* **Auto-resume:** the dispatcher also spawns runs that sit in `AWAITING_HUMAN` or `AWAITING_PLAN_APPROVAL` with no live process once their pending decision is answered. Like queued runs, they count toward the limits, and they rank as "started". `PAUSED` runs are never resumed automatically.

## 6. Rebase conflicts (milestone 6)

When `origin/<base>` moved and the rebase conflicts:

1. orq records `pre_rebase = HEAD` and runs `git rebase origin/<base>` without aborting on conflict.
2. While the rebase stops on conflicts, at most `[merge].max_conflict_rounds` (default 3) rounds, one per rebased commit:
   * the implementer gets the conflicted files, both sides' commit subjects and the rule: resolve keeping both intents, remove every marker, `git add`; never run `git rebase --continue/--abort/--skip`, `git commit` or `git push`;
   * the guard stays on;
   * orq checks that no conflicted paths remain (`git diff --name-only --diff-filter=U`) and no conflict markers remain in the touched files, then runs `git -c core.editor=true rebase --continue` itself.
3. After the rebase completes, orq runs the secret scan of the range and the check command. On success, the gate continues as before: force-with-lease push, CI.
4. Any failure resets to `pre_rebase` (`git rebase --abort` if still in progress, else `git reset --hard pre_rebase`) and asks the owner, as in Phase 3. The decision context lists the files and what the implementer tried.

Events: `rebase_conflict`, `conflict_resolved`, `conflict_resolution_failed`.

## 7. Editable queue (milestone 7)

* Runs gain `queue_order REAL` (default: the creation time). The dispatcher and the Queue tab order queued runs by it.
* `POST /api/runs/{id}/move` with `{to: "top" | "up" | "down" | "bottom"}`.
* `PUT /api/runs/{id}/task` with `{markdown}`, only for `QUEUED` runs at phase `setup` with no process. It validates with `parse_task` (same repo), rewrites `TASK.md` and updates the title, branch and worktree path in the checkpoint and the `runs` row.
* Cancel stays `abort`.

## 8. Testing

* `test_settings.py`: layer resolution and sources; task `## Models` parsing; models cache filtering; effort validation.
* `test_loop.py`: a setting changed between two calls is used by the second; models and efforts recorded in events; `task_complete` skips milestones; conflict resolution success and both failure paths with real git and a fake implementer.
* `test_schema.py` (in `test_adapters.py`): new contract fields; old marker shape tolerated.
* `test_messages.py`: decision sections and the 1500-character cap; answered-elsewhere and failure-reason formats.
* `test_digest.py`: digest from recorded events; cap.
* `test_hub.py`: settings endpoints, test-model endpoint (fake runner), progress notifications (milestone and timer), answered-elsewhere notice, STATUS digest, queue move and edit, auto-resume by the dispatcher.
* `test_cli.py`: `orq hub autostart` argv builder; `--log-file`.
* `test_adapters.py`: `-c windows.sandbox="unelevated"` and `model=` override in the Codex argv.
* Real runs for the exit criterion on both sandboxes.

## 9. Out of scope

Dashboard access from outside the PC, LLM-written summaries beyond the reviewer's `owner_update`, real projects (next, after this phase).
