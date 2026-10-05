# orq: Local Claude Code ↔ Codex Orchestrator

**Spec version:** 1.0
**Date:** 2026-10-05
**Owner:** Binho (Fábio Kyrillos)
**Status:** Phase 0 complete (see `docs/phase0-findings.md`). Ready for Phase 1.

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
| Codex CLI | 0.139.0 |
| Claude plan | Max 5x |
| ChatGPT plan | Plus (tight limits, treat as scarce) |
| Target repos | Public on GitHub (free Actions), created by the owner |
| GitHub review | None. All gating happens inside orq before merge |
| Messaging | Evolution API + n8n, both on a VPS |
| Live view | Required |

**CLI invocation rules (Phase 0 findings):**

* `claude` and `codex` on PATH are npm `.cmd` shims. Adapters resolve and launch the real targets (`claude.exe`; `node codex.js`) with an argument list, never through a shell.
* Prompts go in on stdin. stdin is always closed or fed; `codex exec` waits on an open stdin.
* Adapters strip every `CLAUDE*` and `ANTHROPIC*` variable from the child environment, so orq behaves the same when started from inside a Claude session.
* The standalone `claude` CLI has its own login, separate from the desktop app. `claude auth status` must report `loggedIn: true` before a run starts.
* The Codex model is always passed explicitly (`-m`). `~/.codex/config.toml` is shared with the desktop app and may name a model the CLI cannot use on a ChatGPT plan.
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
* **Notifier:** Windows toast plus WhatsApp via n8n.
* **Store:** SQLite for runs, queue, decisions; files for raw logs.
* **Dashboard (Phase 4):** local web page with both live streams and decision controls.

Rejected alternatives (for the record):

* *tmux/pty driving interactive TUIs:* fragile screen parsing, no tmux on Windows.
* *Loop inside the Claude Code `Stop` hook:* little code, but poor control over commits, pauses and limits.

## 6. Run lifecycle

```
QUEUED → PLANNING → AWAITING_PLAN_APPROVAL (optional) → IMPLEMENTING
       → VERIFYING → REVIEWING → (back to IMPLEMENTING, or)
       → FINALIZING (PR, CI, rebase, merge) → DONE

Side states: AWAITING_HUMAN, PAUSED_RATE_LIMIT, FAILED, ABORTED
```

Every transition is written to `events.jsonl` and to SQLite before it takes effect, so a crash can resume from the last state.

## 7. One iteration

1. Build the implementer prompt: task, current milestone, reviewer's `next_prompt`, owner decisions so far (`DECISIONS.md` from the run dir), standing rules.
2. Run the implementer (resume its session), stream events to the log and dashboard.
3. If the implementer emitted a decision marker (section 8.3) → `AWAITING_HUMAN`.
4. If the guard denied a tool call → `AWAITING_HUMAN` (destructive approval).
5. Collect `git diff` against the last iteration commit.
6. Post execution guard: diff rules and secret scan. Violation → `AWAITING_HUMAN`.
7. Run the local check command.
8. Commit the iteration on the run branch.
9. Run the reviewer with: task, milestone, diff summary, check results, implementer's final message. The reviewer reads the repo itself.
10. Act on reviewer status: `continue` → next iteration, `needs_human` → `AWAITING_HUMAN`, `done` → next milestone or `FINALIZING`.
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

Reviewer standing rules (in its prompt):

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

### 8.4 Decision object (stored in SQLite)

`decision_id` (short, e.g. `D7K2`), `run_id`, `source` (implementer | reviewer | guard), `decision_type`, `question`, `options`, `recommendation`, `destructive` (bool), `status` (pending | answered | expired), `answer`, `answered_via` (whatsapp | dashboard | cli), timestamps.

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

**Note:** Evolution API uses an unofficial WhatsApp Web session. Use a dedicated sender number, not the owner's personal one.

### 9.4 Desktop

Windows toast on every new decision and on run completion or failure.

## 10. Safety

### 10.1 Limits

* Max iterations per run (default 15) and max wall time (default 6 h).
* Max concurrent runs (default 2), because subscription limits are shared.

### 10.2 No progress detection

Pause and ask the owner when any of these fire:

* Same diff hash in 2 consecutive iterations.
* Same failing check signature in 3 consecutive iterations.
* `next_prompt` near identical to the previous one (simple text similarity).

### 10.3 Guard: pre execution (`PreToolUse` hook, Python)

Denies and records: `git push --force`, `git reset --hard`, branch deletion, recursive deletes, `DROP`/`TRUNCATE`, dependency removal, writes outside the worktree. orq turns the denial into a destructive decision. On `APPROVE`, orq writes a one time allow token for that exact action into the run dir; the hook consumes it on retry.

Confirmed in Phase 0:

* The hook is attached with `--settings <file>` kept in the run dir, never inside the worktree. It fires under `--dangerously-skip-permissions` and overrides `--allowedTools`.
* Deny format: exit 0 with `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}` on stdout. The reason reaches the model verbatim. (Exit code 2 also works but prefixes the reason with the hook command line.)
* The hook command is `"<absolute python.exe>" "<absolute guard.py>"` with forward slashes; it runs through Git Bash. orq passes `ORQ_RUN_DIR` in the environment so the hook finds the token directory and its log.
* Every denial also appears in `result.permission_denials` with the full `tool_input`.
* `.claude/**` in the worktree is a protected path: `--setting-sources project,local` still loads the repo's own settings and hooks.

### 10.4 Guard: post execution (diff rules)

Deleted files, removed tests, removed exported functions or routes, protected paths (configurable per repo), large negative line balance. On `DENY`, the worktree is reset to the last iteration commit and the implementer is told to proceed without that change.

### 10.5 Secrets (repos are public)

* `gitleaks` scan before every commit and every push. A hit blocks and raises a `risk` decision.
  * Before commit: `gitleaks git --pre-commit --staged --redact --no-banner --report-format json --report-path <file> <worktree>`.
  * Before push: `gitleaks git --log-opts=origin/<base>..HEAD --redact --no-banner <worktree>`.
  * Exit code 1 means leaks found, 0 means clean.
* Standard `.gitignore` applied at repo creation (`.env`, credentials, data dumps).

### 10.6 Git strategy

* One worktree per run, outside the repo: `<worktree_root>\<repo>\<run_id>`, default root `%USERPROFILE%\.orq\worktrees`. Set `core.longpaths=true` in the repo config; without it, checkout fails past 260 characters.
* Worktree config also sets `core.autocrlf=false`; the owner's global `autocrlf=true` would otherwise rewrite line endings in public repos.
* `core.longpaths` only fixes git. Python, PowerShell 5.1 (which Codex uses to read files) and other tools still fail past 260 characters unless Windows `LongPathsEnabled=1`. Prerequisite: the owner enables it, or sets a short `worktree_root` such as `C:\orq-wt`.
* Branch `orq/<task-slug>`.
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

## 11. Storage and logs

Never inside the repo (repos are public):

```
%USERPROFILE%\.orq\
  orq.db                     # SQLite: runs, queue, decisions
  config.toml                # global config (secrets via env vars)
  worktrees\<repo>\<run_id>\
  runs\<run_id>\
    TASK.md
    DECISIONS.md
    state.json               # crash recovery
    events.jsonl             # every event, timestamped
    iterations\<n>\
      implementer.prompt.md
      implementer.stream.jsonl
      reviewer.prompt.md
      reviewer.output.json
      diff.patch
      checks.txt
    allow_tokens\
```

## 12. Rate limits and model routing

* Detect usage limit errors from both CLIs and distinguish them from real failures.
  * Claude (real occurrences): message `You've hit your session limit · resets 10pm (America/Cayenne)` with `error: "rate_limit"`. The reset is a local clock time plus zone, with no date. A failed `claude -p` call reports `is_error: true` and exit code 1 while `subtype` still says `success`, so adapters test `is_error`. Every `stream-json` run also emits a `rate_limit_event` (`rate_limit_info.status`, `resetsAt`, `rateLimitType`) before `result`; the adapter records it for proactive backoff.
  * Codex: failures arrive as an `error` event followed by `turn.failed` and exit code 1. The session file (`~/.codex/sessions/.../rollout-*-<thread_id>.jsonl`) carries a `rate_limits` snapshot on every `token_count` event (`primary.used_percent`, `primary.resets_at`, `secondary.*`). The adapter reads it after each call and switches to the fallback before the limit is reached (default 90 percent), then back after `resets_at`.
  * An unrecognized error is never retried blindly; it pauses the run for the owner.
* Codex limit → switch reviewer to the Claude fallback, record it, retry Codex after reset.
* Claude limit → `PAUSED_RATE_LIMIT`, backoff, notify owner.
* Keep reviewer prompts lean: the reviewer reads the repo; send only task, milestone, diff summary, check results, implementer final message.
* Reviewer effort: low for routine iterations, high for planning and the final merge gate.
* Implementer model per step: planner may tag milestones as `hard` (Opus) or `mechanical` (Sonnet).

## 13. Configuration (example)

```toml
[limits]
max_iterations = 15
max_wall_hours = 6
max_concurrent_runs = 2

[implementer]
default_model = "opus"
mechanical_model = "sonnet"

[reviewer]
primary = "codex"
codex_model = "gpt-5.5"
fallback = "claude"
switch_at_used_percent = 90
routine_effort = "low"
final_effort = "high"

[git]
merge_strategy = "squash"
worktree_root = "~/.orq/worktrees"
protected_paths = [".github/**", "migrations/**", "**/.env*"]

[notify]
toast = true
n8n_base_url = "https://<vps>/webhook"
n8n_token_env = "ORQ_N8N_TOKEN"
poll_seconds = 20
reminder_hours = 3
```

## 14. CLI surface

```
orq run <TASK.md> [--repo <path|owner/repo>] [--plan-approval required|skip]
orq status [<run_id>]
orq answer <decision_id> "<text>" | --approve | --deny
orq pause | resume | abort <run_id>
orq rollback <run_id> --to <n>
orq logs <run_id> [--follow]
orq dashboard
```

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

### Phase 2: safety

* `PreToolUse` guard with one time approval tokens.
* Post execution diff rules.
* No progress detection, rollback.
* `state.json` and crash resume.
* Rate limit handling and Claude reviewer fallback.

**Exit criteria:** a forced destructive action pauses and resumes correctly after approve and after deny; a killed process resumes the run.

### Phase 3: autonomous close

* Planning step with optional approval.
* Merge gate (section 10.7) including rebase and CI re run.
* Model routing per milestone.

**Exit criteria:** a task goes from TASK.md to merged PR with zero owner input when no decision is needed.

### Phase 4: remote human

* Dashboard with both live streams, run list, decision controls.
* Windows toast.
* n8n + Evolution API outbound and inbound (pull), WhatsApp commands.

**Exit criteria:** owner answers a business decision and a destructive approval from WhatsApp and the run continues.

### Phase 5: scale

* Queue with concurrent runs and global concurrency limit.
* Run summaries and replay in the dashboard.

## 16. Non goals

* Using any paid model API.
* Human code review on GitHub.
* Running on WSL or Linux (may come later, not designed for now).
