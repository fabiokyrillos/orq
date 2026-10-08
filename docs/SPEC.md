# orq: Local Claude Code ↔ Codex Orchestrator

**Spec version:** 1.0
**Date:** 2026-10-05
**Owner:** Binho (Fábio Kyrillos)
**Status:** Phases 0 to 6 complete (see `docs/phase*-findings.md`).

---

## 1. Purpose

Automate the loop the owner runs by hand today:

1. Claude Code implements a step in a GitHub repo.
2. The owner copies the output to Codex.
3. Codex reviews and writes the next prompt.
4. The owner pastes it back into Claude Code.

`orq` removes the owner from the middle. It runs the loop on its own until the task is merged, and only calls the owner for business decisions, ambiguity, risk, or destructive actions.

## 2. Hard constraints

* **No Anthropic API, no OpenAI API.** Only the locally installed, already authenticated CLIs (`claude`, `codex`) running on the owner's subscriptions.
* **Runs locally** on the owner's PC, orchestrated from the terminal.
* **Windows native.** No tmux, no WSL dependency. Hooks and scripts are Python, not bash.
* **Everything in English:** code, identifiers, comments, docs, commit messages.
* **Destructive actions always require explicit owner approval.**

## 3. Environment

| Item | Value |
|---|---|
| OS | Windows (native) |
| Claude Code | 2.1.219 |
| Codex CLI | 0.161.0 (Phase 6; 0.139.0 before) |
| Claude plan | Max 5x |
| ChatGPT plan | Plus (tight limits, treat as scarce) |
| Target repos | On GitHub, created by the owner. Public or private (Phase 7); GitHub Actions on a private repo spends the plan's minutes (2,000 a month on Free) |
| GitHub review | None. All gating happens inside orq before merge |
| Messaging | Evolution API + n8n, both on a VPS |
| Live view | Required |

**CLI invocation rules (Phase 0 findings):**

* `claude` and `codex` on PATH are npm `.cmd` shims. Adapters resolve and launch the real targets (`claude.exe`; `node codex.js`) with an argument list, never through a shell.
* Prompts go in on stdin. stdin is always closed or fed; `codex exec` waits on an open stdin.
* Adapters strip every `CLAUDE*` and `ANTHROPIC*` variable from the child environment, so orq behaves the same when started from inside a Claude session.
* The standalone `claude` CLI has its own login, separate from the desktop app. `claude auth status` must report `loggedIn: true` before a run starts.
* The Codex model is always passed explicitly (`-m`). `~/.codex/config.toml` is shared with the desktop app and may name a model the CLI cannot use on a ChatGPT plan.
* Phase 6: on a ChatGPT Plus account, CLI 0.139.0 refuses every model newer than `gpt-5.5`; 0.161.0 accepts `gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-astra`, `gpt-6-luna`, `gpt-5.6-*` and `gpt-5.5`. Because orq ignores the user config (`--ignore-user-config`), it restates `-c windows.sandbox="unelevated"`: on 0.161.0 the elevated Windows sandbox fails to provision on this PC (`setup refresh had errors`), while unelevated reads files, runs python and blocks writes.
* Every `claude -p` call uses `--setting-sources project,local --strict-mcp-config`, which keeps the owner's global plugins, hooks and MCP servers out of orq runs (146 tools and 58k context tokens down to 32 tools and 9k) while `--settings` hooks still load. `--safe-mode` and `--bare` are never used: the first disables orq's own guard hook, the second disables OAuth.
* A Claude session can only be resumed from the directory it was created in, so every call for a run uses the worktree as cwd. `--append-system-prompt` is not stored in the session and is passed on every call.
* The implementer runs with `--dangerously-skip-permissions`; the guard hook (section 10.3) is the control. A failed call reports `is_error: true` and exit code 1 while `subtype` still says `success`.

**Implementation stack (proposed):** Python 3.12+, `uv`, `typer` (CLI), `sqlite3` (queue and status), `asyncio` subprocesses, `httpx` (n8n calls), `FastAPI` + Server Sent Events (dashboard, Phase 4), `pytest`.

## 4. Roles

| Role | Default | Fallback | Permissions |
|---|---|---|---|
| Implementer | `claude` (Opus for hard steps, Sonnet for mechanical ones) | none | Full edit and exec inside the worktree, guarded by hooks |
| Reviewer | `codex exec --sandbox read-only` (on `exec resume`: `-c sandbox_mode="read-only"`, there is no `--sandbox` flag) | `claude -p --model opus --tools "Read,Grep,Glob" --json-schema <schema>` | Read only |
| Planner | Same agent as Reviewer, high reasoning effort | Same fallback | Read only |

The reviewer adapter must be swappable at runtime: when Codex hits its usage limit, switch to the Claude reviewer and switch back after the limit resets. All adapters share one interface.

## 5. Architecture

Headless pipeline. The orchestrator calls both CLIs in non interactive mode, parses structured output, and owns all decisions about flow, git, and safety.

Components:

* **Core:** per run state machine and the main loop.
* **Adapters:** `ClaudeImplementer`, `CodexReviewer`, `ClaudeReviewer`. Same interface: `run(prompt, session_id) -> AgentResult` with streamed events.
* **Guard:** Claude Code `PreToolUse` hook (pre execution) plus diff rules (post execution) plus secret scan.
* **GitManager:** worktrees, branches, commits, rebase, PR and merge via `gh`.
* **Verifier:** runs the task's local check command and reads GitHub Actions status.
* **Notifier:** Windows toast plus WhatsApp via n8n. Phase 4: the toast is shown by the run process (`notify/toast.py`); WhatsApp belongs to the hub.
* **Store:** SQLite for runs, decisions, projects and concurrency slots; files for raw logs.
* **Hub / Dashboard (Phase 4, `orq dashboard`):** one long-lived FastAPI process on 127.0.0.1 that serves the dashboard (run list, both live streams over SSE, decision controls, pause/abort/resume) and owns the WhatsApp channel: it sends new decisions and run completions, reminds after `reminder_hours`, polls n8n for replies, applies them and acks. Runs never talk to n8n.
* **Projects, queue and dispatcher (Phase 5):** a project is one GitHub repo (`owner/repo`); runs join it on their `repo`. Queued runs have no process until the hub's dispatcher spawns them (`orq resume`) within the global and per-project limits (section 10.1). The hub also creates projects and queued tasks, and serves run summaries and replay. Every non-GET request needs a token generated when the hub starts and embedded in the page, and requests for any Host other than `127.0.0.1:<port>`/`localhost:<port>` are refused.

Rejected alternatives (for the record):

* *tmux/pty driving interactive TUIs:* fragile screen parsing, no tmux on Windows.
* *Loop inside the Claude Code `Stop` hook:* little code, but poor control over commits, pauses and limits.

## 6. Run lifecycle

```
QUEUED → PLANNING → AWAITING_PLAN_APPROVAL (optional) → IMPLEMENTING
       → VERIFYING → REVIEWING → (back to IMPLEMENTING, or)
       → FINALIZING (PR, CI, rebase, merge) → DONE

Side states: AWAITING_HUMAN, PAUSED_RATE_LIMIT, PAUSED, FAILED, ABORTED
```

Every transition is written to `events.jsonl` and to SQLite before it takes effect. `state.json` in the run dir is the checkpoint: it records the phase inside the iteration (`setup`, `plan`, `implement`, `verify`, `review`, `await`, `finalize`, `gate_ci`, `gate_review`, `gate_merge`, `done`), the pending decision and everything the next step needs. `orq resume <run_id>` rebuilds the run from it and continues through the same code path as a live run (Phase 2):

* A crash during an implementer turn keeps the uncommitted work; the same session is re-invoked with an "interrupted turn" note. The orphaned CLI child (pid kept in `child.pid`) is killed first.
* `orq resume` refuses a run whose process is still alive, a `DONE`/`ABORTED` run, and a worktree whose HEAD moved away from the last recorded commit (the one exception is the iteration commit itself, when the crash landed between the commit and the next checkpoint write).
* `orq pause` drops a `pause.requested` flag; the loop stops between phases with state `PAUSED`.
* Decisions are answered either in the terminal of the live process or, after a crash or kill, with `orq answer` followed by `orq resume`.
* Phase 5: `QUEUED` means "waiting for a slot". A queued run has its run dir, `TASK.md` and a checkpoint at phase `setup` with `pid = 0`, but no process and no worktree. A run that answered a decision and finds no free slot also waits in `QUEUED`. When a run ends (`DONE`, `FAILED`, `ABORTED`) it writes `summary.json`.

## 7. One iteration

1. Build the implementer prompt: task, current milestone, reviewer's `next_prompt`, owner decisions so far (`DECISIONS.md` from the run dir), standing rules.
2. Run the implementer (resume its session), stream events to the log and dashboard.
3. If the implementer emitted a decision marker (section 8.3) → `AWAITING_HUMAN`.
4. If the guard denied a tool call → `AWAITING_HUMAN` (destructive approval).
5. Stage everything the implementer produced and collect `git diff` against the last iteration commit.
6. Post execution guard on the staged content: diff rules and secret scan. Violation → `AWAITING_HUMAN`.
7. Run the local check command.
8. Commit the iteration on the run branch, committing only the index from step 5. Files the check command generates (caches, build output) never enter the commit, and the commit is exactly what was scanned.
9. Run the reviewer with: task, milestone, diff summary, check results, implementer's final message. The reviewer reads the repo itself.
10. Act on reviewer status: `continue` → next iteration, `needs_human` → `AWAITING_HUMAN`, `done` → next milestone or `FINALIZING`. Phase 3: `done` means the current milestone's `done_when` holds; orq advances `milestone_index` and the next prompt opens the next milestone. `done` on the last milestone enters the merge gate. A clean `done` never feeds the no-progress rules (advancing a milestone with an empty diff is progress). A `done` that still lists a `blocker` or `major` issue is treated as `continue`, with the issues as the next prompt (deterministic, seen in Phase 1).
11. Check limits and no progress rules (section 10).

## 8. Contracts

### 8.1 TASK.md (input, lives in the run dir, not in the repo)

```markdown
# Task: <title>
## Repo
<owner/repo>, base branch <main>
## Goal
<what and why>
## Acceptance criteria
- [ ] ...
## Out of scope
- ...
## Constraints
- ...
## Check command
<e.g. uv run pytest -q>
## Plan approval
required | skip
```

### 8.2 Reviewer output (enforced with `--output-schema` on Codex and `--json-schema` on the Claude fallback, which returns it in `result.structured_output`)

```json
{
  "status": "continue | done | needs_human",
  "summary": "string",
  "milestone": "string",
  "next_prompt": "string or null",
  "issues": [
    { "severity": "blocker | major | minor", "description": "string" }
  ],
  "human": {
    "decision_type": "business | ambiguity | risk | blocked",
    "question": "string",
    "options": ["string"],
    "recommendation": 0
  }
}
```

`human` is null unless `status` is `needs_human`.

Confirmed in Phase 0: Codex rejects a schema unless every object sets `additionalProperties: false` and lists every property in `required`. Optional values are therefore nullable, not omitted: `next_prompt` is `["string","null"]` and `human` is `["object","null"]`. Codex forces every agent message, including intermediate progress notes, into this shape, so the adapter reads only the final message (`-o <file>`).

Phase 6 additions, all required by the strict schema:
* `owner_update` (string): 2 to 4 plain sentences for the owner's progress digests (section 9.3).
* `task_complete` (boolean): with a clean `done`, the whole task already holds and the remaining milestones are skipped (event `milestones_skipped`).
* `human` gains `context`, `option_details` (one consequence per option) and `recommendation_reason`, so the owner can decide from a phone. The same three fields are part of the planner's `human` (8.5) and the implementer's marker (8.3). The validator tolerates their absence in older answers.

Reviewer standing rules (sent at the top of every reviewer prompt; until Phase 6 they were defined but never sent):

* Any removal of a feature, file, test or behavior not required by TASK.md → `needs_human` with `decision_type: risk`.
* Never guess business rules. Ask.
* `done` only when every acceptance criterion is met and checks pass.

### 8.3 Implementer decision marker

Implementer standing rule (via `--append-system-prompt`): on a business decision or real ambiguity, do not guess. Stop and end the final message with:

````
```orq-decision
{"decision_type": "business", "question": "...", "options": ["...", "..."], "recommendation": 0}
```
````

### 8.5 Planner output (Phase 3; same strict-schema rules as 8.2)

```json
{
  "status": "plan | needs_human",
  "summary": "string",
  "milestones": [
    { "title": "string", "goal": "string", "done_when": "string", "difficulty": "hard | mechanical" }
  ],
  "human": { "decision_type": "...", "question": "...", "options": ["..."], "recommendation": 0 }
}
```

One to eight ordered milestones; `milestones` is empty and `human` set when `status` is `needs_human`. The planner is the reviewer agent at `[reviewer].final_effort`, read only. The plan is written to `<run_dir>/PLAN.md` and kept in `state.json`; both prompts carry the full plan and the current milestone. `## Plan approval: required` raises `AWAITING_PLAN_APPROVAL` with options `approve` and `revise`; free text is a revision request and the planner runs again with it.

### 8.4 Decision object (stored in SQLite)

`decision_id` (short, e.g. `D7K2`), `run_id`, `source` (implementer | reviewer | guard), `decision_type`, `question`, `options`, `recommendation`, `destructive` (bool), `status` (pending | answered | expired), `answer`, `answered_via` (whatsapp | dashboard | cli), timestamps.

Phase 6: `context`, `option_details` and `recommendation_reason`. Decisions from the models carry what the models wrote. orq builds them from evidence for its own decisions (`src/orq/core/decision_text.py`):
* the guard rule and the current milestone;
* the flagged files with line counts and the implementer's report;
* gitleaks hits;
* the stall rule and the last reviews;
* the agent error;
* CI state;
* the rebase conflict and what the implementer tried.

`DECISIONS.md` records the context with the answer, so both agents keep the evidence. `abort` marks a run's pending decisions `expired`.

Phase 6.1: `answered_by` (`owner` | `claude` | `auto`) records who answered, besides the channel (`answered_via`, plus `auto`). The dashboard API and `orq answer` take `by`; `owner` is the default. `stakes` (`low` | `medium` | `high`) is rated by the asking agent (a field of the `human` object and the marker) or by orq for its own decisions. With the opt-in policy (`[notify].auto_answer`, off by default; per project in the dashboard), the hub answers a pending decision with its recommendation when all of these hold:
* the owner has not answered for `auto_answer_minutes` since WhatsApp got it;
* the stakes are at or under `auto_answer_max_stakes` (default `low`);
* it is not destructive, not `business`, not from the guard, and not a plan approval;
* there is a recommendation with a reason, and the recommendation is not `abort`.

Event `auto_answered`.

## 9. Human in the loop

### 9.1 Detection layers (most reliable first)

1. **Deterministic rules** (no LLM): protected paths touched, dependency added or removed, files deleted, tests removed, large negative line balance in source, secret found, guard denial.
2. **Reviewer** `needs_human`.
3. **Implementer** decision marker.
4. **Deadlock:** reviewer rejects the same issue 3 times, or no progress rules fire.

### 9.2 Answer flow

* Answer is appended to the run's `DECISIONS.md` (run dir, never the repo) and injected into every later prompt on both sides.
* The run resumes from the stored session IDs.
* No answer → the run stays paused, reminder every N hours, other runs keep going.
* Phase 4: every channel (terminal thread, `orq answer`, dashboard, WhatsApp) records the answer through one function (`core/answers.py: record_answer`), and a waiting run polls SQLite every `[notify].answer_poll_seconds` until it sees it. `orq run --no-prompt` exits at the decision instead; `orq resume` (also spawned by the dashboard or a WhatsApp `RESUME`) picks it up later.

### 9.3 WhatsApp via n8n + Evolution API (VPS)

**Outbound:** orq → `POST` n8n webhook (HTTPS, auth header) → Evolution API `sendText` to the owner's number.

**Inbound (pull model, no open port on the PC):**

* Evolution API incoming message webhook → n8n.
* n8n accepts only the owner's number, stores the message in a table (Postgres on the VPS).
* orq polls an n8n endpoint `GET /orq/replies?since=<cursor>` and acks consumed ones with `POST /orq/replies/ack`. orq never touches the DB directly.

Suggested table `orq_messages`: `id`, `decision_id`, `direction`, `from_number`, `text`, `received_at`, `consumed_at`.

**Message format:**

```
*[orq] D7K2 · repo-x · iteration 6*
Business decision: is the discount applied before or after tax?
1. Before (reviewer recommends)
2. After
Reply: D7K2 1  or  D7K2 <free text>
```

**Reply rules:**

* Only the owner's number is accepted.
* The decision ID is required. Loose replies are ignored with a hint.
* Destructive approvals require `APPROVE <id>` or `DENY <id>`. A numbered reply never approves a destructive action.

**Commands:** `STATUS`, `PAUSE <run>`, `RESUME <run>`, `ABORT <run>`.

Phase 4 implementation: `docs/n8n/orq-workflow.json` (webhooks `orq/notify`, `orq/replies`, `orq/replies/ack`, plus `orq/evolution` for Evolution's `MESSAGES_UPSERT`), `docs/n8n-setup.md`. Numbered replies are 1-based as printed in the message. `RESUME` spawns a detached `orq resume` on the PC; `ABORT` refuses a live run. Unknown messages get a one-line hint. Reminders are sent by the hub every `[notify].reminder_hours` while a decision is pending; run completions (`DONE`, `FAILED`, `ABORTED`) are announced once.

Phase 6.1: messages are in Brazilian Portuguese (owner's decision, recorded in `CLAUDE.md`) and laid out for a phone:
* a header with an emoji per decision type;
* `*Contexto*`, `❓ *Pergunta*`, then `*Opções*` with one `↳` consequence per option;
* `💡 *Recomendo a n:*`, plus the time of an automatic answer when the policy would take it;
* the reply line.

Blank lines separate the sections, the context keeps its line breaks, and the cap is 2000 characters. An answer given elsewhere says who gave it (`por você`, `pelo Claude`, `automaticamente pelo orq (n min sem resposta)`). Machine keywords (`APPROVE`, `DENY`, `STATUS`, IDs) are unchanged. The section 9.3 example above shows the Phase 4 layout.

**Note:** Evolution API uses an unofficial WhatsApp Web session. Use a dedicated sender number, not the owner's personal one.

### 9.4 Desktop

Windows toast on every new decision and on run completion or failure. Phase 4: `winotify` from the run process; a failed toast is an event (`toast_failed`), never an error. `[notify].toast = false` disables it.

## 10. Safety

### 10.1 Limits

* Max iterations per run (default 15) and max wall time (default 6 h).
* Max concurrent runs (default 2), because subscription limits are shared.
* Phase 6: the dispatcher also spawns runs parked at a decision that was answered while no process waited for it (they rank with the runs that already started); `PAUSED` runs are never resumed automatically. Queued runs follow `queue_order`, which the dashboard can change (top, up, down, bottom); a queued run that has not started can have its TASK.md edited.
* Phase 5: a run takes a slot (SQLite table `slots`, one `BEGIN IMMEDIATE` transaction) before agent work and gives it back when it raises a decision and when it ends; waiting for the owner costs no subscription time. `PAUSED_RATE_LIMIT` keeps its slot. A slot is granted when the global count is under `[limits].max_concurrent_runs`, the project's count is under its own cap (`max_concurrent`, default `[queue].project_concurrency` = 1) and no better waiter could take it first: runs that already started go before runs that never ran, then oldest first. Rows of dead processes are dropped on the next acquire. Time spent waiting for a slot or for the owner does not count toward `max_wall_hours`.

### 10.2 No progress detection

Pause and ask the owner when any of these fire:

* Same diff hash in 2 consecutive iterations.
* Same failing check signature in 3 consecutive iterations.
* `next_prompt` near identical to the previous one (`difflib` ratio at or above 0.9).

The decision (`source orq`, type `blocked`) offers `continue` (the streak counter resets), `rollback to iteration <n>` (the last iteration before the streak; `n = 0` means the base commit) and `abort`. A rollback resets the worktree to that iteration's commit, archives the discarded iteration directories, tells both agents what was discarded in their next prompts, and sets the iteration counter back so discarded iterations do not count toward `max_iterations`.

### 10.3 Guard: pre execution (`PreToolUse` hook, Python)

Denies and records: `git push --force`, `git reset --hard`, branch deletion, `git clean`, recursive deletes (`rm -r`, `Remove-Item -Recurse`, `rmdir /s`, `del /s`), `DROP TABLE|DATABASE|SCHEMA`/`TRUNCATE TABLE`, dependency removal (`uv remove`, `pip uninstall`, `npm uninstall`, ...), writes outside the worktree (file tools by path, Bash redirections and `tee` to absolute paths), and file tools on `protected_paths`. orq turns the denial into a destructive decision. On `APPROVE`, orq writes a one time allow token for that exact action into the run dir; the hook consumes it on retry.

Implemented in Phase 2 (`src/orq/guard/`):

* The rules are pure functions (`rules.py`); the hook (`hook.py`) only reads stdin, checks the token and logs. A hook crash allows the call and logs the error: the diff rules and the reviewer are the next layers.
* The action key is `sha256(tool_name + canonical JSON of tool_input)[:24]`; the token is `<run_dir>/allow_tokens/<key>`. The owner approves one exact action; the next prompt quotes the command verbatim so the implementer retries it unchanged.
* The runner writes `claude-settings.json` (the hook attachment) and `guard.json` (worktree path, protected paths) into the run dir; the implementer adapter adds `--settings` when the file exists. Every hook call appends a line to `<run_dir>/guard.jsonl` (`allow`, `deny`, `allow-by-token`).
* The deny reason tells the model not to work around the denial and to end its turn stating the exact command it needs. After the turn, orq reads `result.permission_denials`, asks one decision per unique action (`approve`/`deny`), and when the turn produced no changes it skips the check and the review and goes straight to those decisions.

Confirmed in Phase 0:

* The hook is attached with `--settings <file>` kept in the run dir, never inside the worktree. It fires under `--dangerously-skip-permissions` and overrides `--allowedTools`.
* Deny format: exit 0 with `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}` on stdout. The reason reaches the model verbatim. (Exit code 2 also works but prefixes the reason with the hook command line.)
* The hook command is `"<absolute python.exe>" "<absolute guard.py>"` with forward slashes; it runs through Git Bash. orq passes `ORQ_RUN_DIR` in the environment so the hook finds the token directory and its log.
* Every denial also appears in `result.permission_denials` with the full `tool_input`.
* `.claude/**` in the worktree is a protected path: `--setting-sources project,local` still loads the repo's own settings and hooks.

### 10.4 Guard: post execution (diff rules)

Deleted files, removed tests, removed exported functions or routes, protected paths (configurable per repo), large negative line balance. Phase 6: a removal counts only when the run's base commit has the removed file, test, export, route or dependency; reworking what the run itself added in an earlier iteration never asks the owner. On `DENY`, the worktree is reset to the last iteration commit and the implementer is told to proceed without that change.

Implemented in Phase 2 (`src/orq/guard/diff_rules.py`), evaluated on the staged index right after the secret scan and before the check command. Rule ids: `deleted_file`, `removed_test`, `removed_export`, `removed_route`, `protected_path`, `dependency_removed` (manifest lines in `pyproject.toml`, `requirements*.txt`, `package.json`, `Cargo.toml`, `go.mod`), `negative_balance` (net deleted source lines above `[guard].max_net_deleted_lines`, default 300, over `[guard].source_globs`). The name-based rules are heuristics: a removed name that reappears anywhere in the added lines (rename, move) does not fire. One decision per iteration lists every violation; the approved list lands in `DECISIONS.md` so the reviewer sees it. On deny the iteration ends without a commit or a review.

### 10.5 Secrets (repos are public or may become public)

* `gitleaks` scan before every commit and every push. A hit blocks and raises a `risk` decision.
  * Before commit: `gitleaks git --pre-commit --staged --redact --no-banner --report-format json --report-path <file> <worktree>`.
  * Before push: `gitleaks git --log-opts=origin/<base>..HEAD --redact --no-banner <worktree>`.
  * Exit code 1 means leaks found, 0 means clean.
* Standard `.gitignore` applied at repo creation (`.env`, credentials, data dumps).

### 10.6 Git strategy

* One worktree per run, outside the repo: `<worktree_root>\<repo>\<run_id>`, default root `%USERPROFILE%\.orq\worktrees`. Phase 5: the clone lives at `repos\<owner>\<repo>` (older clones at `repos\<repo>` stay for the runs that reference them), and concurrent runs share it: git commands that write shared refs or the worktree list (clone, fetch, worktree add/remove, branch delete, push, `gh pr merge`) take a cross-process lock `<clone>.orq.lock`, which the OS releases when a process dies. Set `core.longpaths=true` in the repo config; without it, checkout fails past 260 characters.
* Phase 7: when the project has a local folder (the owner's own checkout), a new clone is seeded from it: `git clone --reference <the folder's git dir> --dissociate <GitHub URL>`. Objects come from the folder, `--dissociate` copies them so the clone does not depend on the folder, and `origin` stays GitHub. A linked worktree seeds from its main repo; a folder that cannot seed (shallow, broken, gone) falls back to a plain clone. The run logs `repo_cloned` with `local:<git dir>` or `github`. orq never creates worktrees, branches, config, hooks or lock files in the owner's folder: `create_worktree` writes `core.autocrlf` and `core.longpaths` with `git config`, which would land in the shared repo config.
* Worktree config also sets `core.autocrlf=false`; the owner's global `autocrlf=true` would otherwise rewrite line endings in public repos.
* `core.longpaths` only fixes git. Python, PowerShell 5.1 (which Codex uses to read files) and other tools still fail past 260 characters unless Windows `LongPathsEnabled=1`. Prerequisite: the owner enables it, or sets a short `worktree_root` such as `C:\orq-wt`.
* Branch `orq/<task-slug>`; if it already exists locally or on origin, `orq/<task-slug>-<run_id>`.
* One commit per iteration: `orq(<run_id>) iter <n>: <summary>`.
* `orq rollback <run_id> --to <n>` resets the worktree to that commit and tells the reviewer what was discarded.

### 10.7 Merge gate (no human review on GitHub)

All must hold:

1. GitHub Actions green.
2. Final reviewer pass at high reasoning effort returns `done`.
3. Guard clean, no secrets.
4. No pending decisions.
5. Branch up to date with base. If base moved, rebase, re run CI, re run step 2.

Then `gh pr merge --squash --delete-branch`, remove the worktree, mark `DONE`. Squash is the default, configurable.

Confirmed in Phase 0:

* `gh pr merge` does not wait for checks; it merged a PR whose CI was still queued. Step 1 is orq's job and must complete before the merge call.
* `gh pr checks <n> --json name,state,bucket` exits 1 with `no checks reported` for about a minute after PR creation; treat that as pending. Free runners can sit in `QUEUED` for many minutes, so the CI wait has a long timeout (default 60 min) and polls every 20 to 30 s.
* Merging from inside the worktree works. `--delete-branch` removes the remote branch and skips the local delete with a warning (exit 0); orq removes the worktree and local branch itself.
Implemented in Phase 3 as resumable phases after `finalize` (push, PR):

* `gate_ci`: when `origin/<base>` moved, `git rebase` and `git push --force-with-lease` (orq's push, never the implementer's). Phase 6: on a conflict, the implementer gets one turn per conflicting commit (at most `[merge].max_conflict_rounds`) to resolve the files. orq checks that no conflicted paths and no markers are left, continues the rebase itself, then runs the check command and a secret scan of the range. Any failure aborts the rebase, resets the branch, and asks the owner `retry`/`abort` with the files and the reason; then `CiWatcher` polls `gh pr checks --json name,state,bucket,link` every `[merge].poll_seconds` for up to `[merge].ci_timeout_minutes`. "No checks reported" is pending during `[merge].ci_grace_minutes`, then the owner is asked (`merge without CI`/`abort`). A failing check whose run has no job steps is re-run (`gh run rerun`, at most `[merge].max_ci_reruns`); a real failure sends the run back to the implementer with the tail of `gh run view --log-failed`.
* `gate_review`: final reviewer pass at `[reviewer].final_effort` against the whole task with the PR diff stat and the CI result; `done` with no blocker/major issue proceeds, anything else sends the run back to the implementer. Gate restarts are bounded by `[merge].max_gate_rounds`.
* `gate_merge`: no pending decisions, base re-checked (back to `gate_ci` if it moved), secret scan of the range, `gh pr merge <n> --<strategy> --delete-branch`, `gh pr view --json state` must say `MERGED`, worktree and local branch removed (`[git].keep_worktree` keeps them for debugging).

* A run can fail without ever running: after 15 minutes GitHub cancels a job no hosted runner picked up (`conclusion: failure`, job `cancelled`, no steps, annotation `The job was not acquired by Runner of type hosted even after multiple attempts`). The verifier treats that as an infrastructure failure and re-runs it (`gh run rerun <id>`, default 3 attempts) instead of starting a new implementer iteration.

## 11. Storage and logs

Never inside the repo (repos are public):

```
%USERPROFILE%\.orq\
  orq.db                     # SQLite: runs, decisions, projects, slots, notifications
  config.toml                # global config (secrets via env vars)
  repos\<owner>\<repo>\        # orq's own clone per project (never the owner's checkout; seeded from it, Phase 7)
  tasks\                     # the owner's TASK.md files (optional)
  worktrees\<repo>\<run_id>\
  runs\<run_id>\
    TASK.md
    DECISIONS.md
    state.json               # checkpoint: phase, pending decision, sessions, commits (crash recovery)
    summary.json             # written when the run ends (Phase 5)
    events.jsonl             # every event, timestamped
    claude-settings.json     # attaches the guard hook (--settings)
    guard.json               # worktree path and protected paths for the hook
    guard.jsonl              # every hook decision
    child.pid                # pid of the running CLI child, removed when it exits
    pause.requested          # present while an `orq pause` is pending
    iterations\<n>\
      implementer.prompt.md
      implementer.stream.jsonl
      reviewer.prompt.md
      reviewer.output.json
      diff.patch
      checks.txt
    iterations\<n>.discarded-<ts>\   # archived by a rollback
    allow_tokens\
```

## 12. Rate limits and model routing

* Detect usage limit errors from both CLIs and distinguish them from real failures.
  * Claude (real occurrences): message `You've hit your session limit · resets 10pm (America/Cayenne)` with `error: "rate_limit"`. The reset is a local clock time plus zone, with no date. A failed `claude -p` call reports `is_error: true` and exit code 1 while `subtype` still says `success`, so adapters test `is_error`. Every `stream-json` run also emits a `rate_limit_event` (`rate_limit_info.status`, `resetsAt`, `rateLimitType`) before `result`; the adapter records it for proactive backoff.
  * Codex: failures arrive as an `error` event followed by `turn.failed` and exit code 1. The session file (`~/.codex/sessions/.../rollout-*-<thread_id>.jsonl`) carries a `rate_limits` snapshot on every `token_count` event (`primary.used_percent`, `primary.resets_at`, `secondary.*`). The adapter reads it after each call and switches to the fallback before the limit is reached (default 90 percent), then back after `resets_at`.
  * An unrecognized error is never retried blindly; it pauses the run for the owner.
* Codex limit → switch reviewer to the Claude fallback, record it, retry Codex after reset. Phase 2: `ReviewerRouter` wraps both adapters behind the `Agent` interface. The Codex adapter reads the `rate_limits` snapshot from the session file after every call; at or past `switch_at_used_percent` the router routes later reviews to the fallback until `primary.resets_at` (proactive). A Codex failure classified as a limit (snapshot `rate_limit_reached_type` set, or a message matching usage limit / rate limit / quota / 429) switches at once and the same review is retried on the fallback (reactive). Events `reviewer_switched` and `reviewer_restored`; the choice survives a resume through `state.json`.
* Claude limit → `PAUSED_RATE_LIMIT`, backoff, notify owner. Phase 2: the wait is until the reset time (`resetsAt` from the stream event when it lies in the future, else the `resets <time> (<zone>)` text, else 15 minutes) plus 60 s, then the same phase is retried. After `[limits].rate_limit_retries` (default 3) waits inside one iteration the owner is asked (`retry`/`abort`). The owner may kill the process during the wait; `orq resume` honours the recorded reset time.
* Any other agent failure (`error`, `auth`) is never retried blindly and no longer ends the run as `FAILED`: it raises a `blocked` decision with `retry` and `abort`. `FAILED` is reserved for invariants (invalid reviewer output after a retry, limits exceeded).
* Keep reviewer prompts lean: the reviewer reads the repo; send only task, milestone, diff summary, check results, implementer final message.
* Reviewer effort: low for routine iterations, high for planning and the final merge gate.
* Phase 6: models and efforts are settings in layers, resolved before every agent call:
  1. TASK.md `## Models`;
  2. the project;
  3. global dashboard overrides;
  4. `config.toml`;
  5. defaults.

  A change in the dashboard applies from the next call of a running run. The role events record `model` and `effort`. The router sends the Codex model to Codex and `reviewer.claude_model` to the Claude fallback. Default Codex model: `gpt-6.1-sol`.
* Implementer model per step: planner may tag milestones as `hard` (Opus) or `mechanical` (Sonnet). Phase 3: the model is passed on every implementer call (`--model` on a resumed session keeps the context, Phase 0) and recorded as an `implementer_model` event; reviewer effort is `routine_effort` for milestone reviews and `final_effort` for planning and the final review.

## 13. Configuration (example)

```toml
[limits]
max_iterations = 15
max_wall_hours = 6
max_concurrent_runs = 2      # global; runs waiting for the owner do not count
rate_limit_retries = 3

[implementer]
default_model = "opus"
mechanical_model = "sonnet"

[reviewer]
primary = "codex"
codex_model = "gpt-6.1-sol"
claude_model = ""                           # Claude reviewer; empty follows implementer.default_model
codex_windows_sandbox = "unelevated"        # restated because the user config is ignored (Phase 6)
fallback = "claude"
switch_at_used_percent = 90
routine_effort = "low"
final_effort = "high"

[git]
merge_strategy = "squash"
worktree_root = "~/.orq/worktrees"
protected_paths = [".github/**", "migrations/**", "**/.env*"]
sandbox_repos = []          # optional allowlist; empty allows any repo
keep_worktree = false       # true keeps the worktree after the merge (debugging)

[merge]
poll_seconds = 20
ci_timeout_minutes = 60
ci_grace_minutes = 5
max_ci_reruns = 3
max_gate_rounds = 3
max_conflict_rounds = 3                     # implementer turns on a rebase conflict before the owner is asked

[guard]
max_net_deleted_lines = 300
source_globs = ["**/*.py", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.jsx", "**/*.rs", "**/*.go", "**/*.java", "**/*.cs"]

[notify]
toast = true
n8n_base_url = "https://<vps>/webhook"   # empty disables WhatsApp
n8n_token_env = "ORQ_N8N_TOKEN"
poll_seconds = 20                         # inbound replies (hub)
outbox_poll_seconds = 5                   # new decisions and run states (hub)
answer_poll_seconds = 3                   # a waiting run re-reads SQLite this often
reminder_hours = 3
progress_minutes = 30                     # digest of a working run this often and when a milestone ends; 0 disables
auto_answer = false                       # Phase 6.1: answer low-stakes decisions with the recommendation when the owner does not
auto_answer_minutes = 30
auto_answer_max_stakes = "low"

[dashboard]
port = 8765                               # 127.0.0.1 only

[queue]
poll_seconds = 5                          # hub dispatcher tick and a run's wait for a slot
project_concurrency = 1                   # active runs per project unless the project sets its own

[projects]
scan_roots = []                           # Phase 7: folders with the owner's checkouts, e.g. ['D:\Projetos\GitHub']; global only
scan_depth = 3                            # folder levels below each root
```

## 14. CLI surface

```
orq run <TASK.md> [--no-prompt]
orq queue <TASK.md>
orq project add <owner/repo|folder> [--name] [--base] [--check] [--max-concurrent]
orq project candidates
orq project list
orq status [<run_id>]
orq answer <decision_id> "<text>" | <option index> | --approve | --deny
orq pause | abort <run_id>
orq resume <run_id> [--no-prompt]
orq rollback <run_id> --to <n>
orq logs <run_id> [--follow]
orq dashboard [--port <n>] [--log-file <path>]
orq hub autostart on|off|status
```

`orq queue` only queues; the hub starts the run when a slot is free (or `orq resume <run_id>` by hand). `orq status` shows slot usage and the queue length. A project added from a folder reads that folder's `origin` and seeds orq's clone from it (10.6); orq always works in its own clone. `orq project candidates` (and the dashboard's Add project page) lists the checkouts under `[projects].scan_roots` first, then the owner's and the owner's organizations' GitHub repos that are not on this PC (archived repos left out). Scanning only reads the folders: `git remote`, `git branch` and `git --no-optional-locks status`, one checkout at a time.

`orq hub autostart on` registers a scheduled task that runs the hub under `pythonw` at logon. With no console, the hub starts console children (git, gh, taskkill, model probes) with `CREATE_NO_WINDOW`, and runs are spawned with a hidden console of their own (`CREATE_NO_WINDOW`, not `DETACHED_PROCESS`) that their children inherit, so no window flashes (Phase 7).

`orq run` and `orq resume` wait for decisions to be answered from any channel (type in the terminal, use the dashboard, reply on WhatsApp, or `orq answer` elsewhere); `--no-prompt` exits at the decision instead. `orq dashboard` is the hub (section 5); WhatsApp works only while it runs.

`--no-prompt` runs headless: the process exits at the first decision (`AWAITING_HUMAN`) and `orq answer` plus `orq resume` continue it. Without it, decisions are asked in the terminal.

## 15. Phases

### Phase 0: environment validation (half a day)

Write findings to `docs/phase0-findings.md`. For each item record the exact command, output sample, and pass/fail.

* `claude -p` with `--output-format json` and `stream-json`; session ID capture; `--resume`; `--append-system-prompt`; `--model` switch on a resumed session; permission flags for unattended runs.
* `PreToolUse` hook calling a Python script on Windows; deny output format; how to attach hooks per run.
* `codex exec` with `--output-schema`, JSON event output, session resume, `--sandbox read-only` on Windows native, reasoning effort control.
* Claude used as read only reviewer (`claude -p` restricted to read tools).
* Usage limit error formats for both CLIs (capture from real occurrences or docs).
* `gh` auth, PR create, checks status, squash merge.
* `gitleaks` install via `winget` and a scan run.
* Worktree creation with long paths.

**Exit criteria:** every Phase 1 dependency confirmed or a documented workaround.

### Phase 1: MVP

* Single run, terminal output only.
* TASK.md parsing, worktree and branch, commit per iteration.
* Implementer ↔ Codex loop with the reviewer schema.
* Max iterations, `events.jsonl`, raw logs outside the repo.
* `gitleaks` before commit and push.
* Pauses answered in the same terminal.
* Ends by opening the PR. Merge is manual in this phase.

**Exit criteria:** a small real task in a test repo goes from TASK.md to an open PR with no copy and paste.

Status: complete on 2026-10-06 (see `docs/phase1-findings.md`).

### Phase 2: safety

* `PreToolUse` guard with one time approval tokens.
* Post execution diff rules.
* No progress detection, rollback.
* `state.json` and crash resume.
* Rate limit handling and Claude reviewer fallback.

**Exit criteria:** a forced destructive action pauses and resumes correctly after approve and after deny; a killed process resumes the run.

Status: complete on 2026-10-06 (see `docs/phase2-findings.md`). Also delivered: headless mode (`orq run --no-prompt`, `orq resume --no-prompt`), `orq answer`, `orq pause`, `orq abort`, `orq rollback`.

### Phase 3: autonomous close

* Planning step with optional approval.
* Merge gate (section 10.7) including rebase and CI re run.
* Model routing per milestone.

**Exit criteria:** a task goes from TASK.md to merged PR with zero owner input when no decision is needed.

Status: complete on 2026-10-06 (see `docs/phase3-findings.md`).

### Phase 4: remote human

* Dashboard with both live streams, run list, decision controls.
* Windows toast.
* n8n + Evolution API outbound and inbound (pull), WhatsApp commands.

**Exit criteria:** owner answers a business decision and a destructive approval from WhatsApp and the run continues.

Status: complete on 2026-10-07 (see `docs/phase4-findings.md`): run `RHGBG6` was driven to a merged PR with every decision answered from WhatsApp.

### Phase 5: scale

* Queue with concurrent runs and global concurrency limit.
* Run summaries and replay in the dashboard.
* Added by the owner: several projects in one dashboard, tasks created and queued from the dashboard, projects added by `owner/repo` or local folder.

**Exit criteria:** with `max_concurrent_runs = 2`, three tasks created and queued from the dashboard across two projects; two run at once and the third starts when a slot frees; a run parked on a decision does not block the queue; all three end with a merged PR; every finished run shows its summary and can be replayed iteration by iteration.

Status: complete on 2026-10-07 (see `docs/phase5-findings.md`).

### Phase 6: refinement before real projects (added after Phase 5 at the owner's request)

* Models and efforts changeable at any time, in layers (section 12), and per project guard settings.
* Decisions with context, option consequences and the reason for the recommendation (8.2, 8.4). A decision answered on another channel is announced on WhatsApp, and FAILED or ABORTED carry their reason.
* Progress digests from the reviewer's `owner_update`, sent on WhatsApp when a milestone ends and every `[notify].progress_minutes` while a run works. `STATUS <run>` returns one, and the dashboard shows one.
* `task_complete` and a planner rule for small tasks.
* Hub autostart at logon (`orq hub autostart`) and automatic resume of runs whose decision is answered while no process waits.
* Rebase conflicts resolved by the implementer (10.7).
* Editable queue (reorder, edit queued tasks).

**Exit criteria:**
* the reviewer model changed in the dashboard during a run is used by the next review;
* a business decision on WhatsApp has enough content to decide;
* a task with 3 or more milestones sends digests;
* a small task finishes in one iteration;
* a provoked rebase conflict is resolved by the implementer and merged;
* an answer to a run without a process resumes it;
* after a reboot the hub starts by itself.

Status: see `docs/phase6-findings.md`.

### Phase 6.1: attribution, automatic answers, readable messages (owner's request after reading Phase 6 on the phone)

* Every answer records who gave it.
* Opt-in automatic answers for low-stakes decisions after a timeout (8.4).
* Formatted Portuguese WhatsApp messages (9.3).

Status: complete on 2026-10-08 (see `docs/phase6-findings.md` section 7).

### Phase 7: projects from the owner's local folders (owner's request before the first real project)

* orq's clone is seeded from the owner's checkout (10.6), which orq only reads.
* Adding a project lists the checkouts on this PC first and the GitHub repos that are not on the PC second (section 14).
* Private repos are supported (section 3).

**Exit criteria:** the picker lists the owner's repos, nested ones included, and no linked worktree; a sandbox added from an owner-style checkout (another branch, uncommitted changes) is cloned from that folder and merges a task, and the folder is identical before and after; a sandbox only on GitHub is cloned from GitHub and merges a task.

Status: complete on 2026-10-08 (see `docs/phase7-findings.md`): run `R59GV9` cloned from the owner-style checkout and merged with the folder unchanged; run `RJ5DDN` cloned from GitHub and merged.

## 16. Non goals

* Using any paid model API.
* Human code review on GitHub.
* Running on WSL or Linux (may come later, not designed for now).
