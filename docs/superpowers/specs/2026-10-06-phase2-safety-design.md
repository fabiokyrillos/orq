# Phase 2 design: safety

Date: 2026-10-06. Source of truth remains `docs/SPEC.md` (sections 10, 11, 12, 15). This document fixes the design decisions for Phase 2 that the spec leaves open and records the module layout. When implementation contradicts the spec, the spec is updated in the same change.

Phase 2 scope (SPEC section 15):

1. `PreToolUse` guard with one time approval tokens.
2. Post execution diff rules.
3. No progress detection and rollback.
4. `state.json` checkpoints and crash resume.
5. Rate limit handling and the Claude reviewer fallback.

Exit criteria: a forced destructive action pauses and resumes correctly after approve and after deny; a killed process resumes the run.

Decisions taken with the owner (2026-10-06):

* A crash in the middle of an implementer turn keeps the uncommitted partial work. On resume the same Claude session is re-invoked with an "interrupted turn" note.
* Once the guard exists, `[git].sandbox_repos` becomes an optional allowlist. An empty list allows any repo.
* The exit-criteria runs against the sandbox repo are driven by orq's author with the real CLIs at the end of the phase.

## 1. Guard: pre execution hook

### Layout

* `src/orq/guard/hook.py`: the script Claude Code executes. Standalone entry point (`if __name__ == "__main__"`), reads the JSON payload on stdin, writes the deny JSON on stdout. Imports only `orq.guard.rules` so start-up stays cheap.
* `src/orq/guard/rules.py`: pure functions. `classify(tool_name, tool_input, worktree) -> Violation | None` and `action_key(tool_name, tool_input) -> str`.
* `src/orq/guard/settings.py`: `write_hook_settings(run_dir) -> Path` generates `<run_dir>/claude-settings.json` with the hook command `"<sys.executable>" "<path to hook.py>"` using forward slashes, matcher `Bash|Write|Edit|MultiEdit|NotebookEdit`.

`ClaudeImplementer.run` receives the settings path through a new constructor argument `settings_path: Path | None` and adds `--settings <path>`. `ORQ_RUN_DIR` and a new `ORQ_WORKTREE` are passed in the environment so the hook finds the token directory, its log and the worktree boundary.

### Rules (SPEC 10.3)

`Bash` command text, case-insensitive, after collapsing whitespace:

| Rule id | Pattern |
|---|---|
| `git_force_push` | `git push` with `--force`, `-f`, `--force-with-lease` |
| `git_reset_hard` | `git reset --hard` |
| `git_branch_delete` | `git branch -D|-d|--delete`, `git push origin --delete|:branch` |
| `git_clean` | `git clean` with `-f` or `-x` |
| `recursive_delete` | `rm -r|-rf|-fr`, `Remove-Item ... -Recurse`, `rmdir /s`, `rd /s`, `del /s` |
| `sql_destructive` | `DROP TABLE|DATABASE|SCHEMA`, `TRUNCATE` |
| `dependency_removal` | `uv remove`, `pip uninstall`, `poetry remove`, `npm uninstall|remove|rm|un`, `yarn remove`, `pnpm remove` |
| `write_outside_worktree` | Bash: redirection (`>`, `>>`) or `tee` to an absolute path outside `ORQ_WORKTREE` |

`Write`, `Edit`, `MultiEdit`, `NotebookEdit`: `write_outside_worktree` when the resolved `file_path` (or `notebook_path`) is not inside `ORQ_WORKTREE`, plus `protected_path` when it matches `[git].protected_paths`.

Everything else is allowed with no output.

### Tokens

* `action_key` is `sha256(tool_name + "\n" + json.dumps(tool_input, sort_keys=True))[:24]`.
* Token file: `<run_dir>/allow_tokens/<action_key>`. The hook unlinks it and allows (`allow-by-token`). One token, one use.
* The owner approves the exact action; the implementer must retry the exact same command. The next prompt quotes it verbatim.

### Hook output

Deny: exit 0 and `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "<reason>"}}`.

Reason text (reaches the model verbatim):

> orq guard: `<short description>` is destructive and needs owner approval (rule `<rule id>`). Do not work around it with another command. Finish any independent work, then end your turn and state the exact command you need and why.

Every hook call appends one JSON line to `<run_dir>/guard.jsonl`: timestamp, decision (`allow`, `deny`, `allow-by-token`), rule, tool name, tool input, action key.

### Runner integration

After an implementer turn, the runner collects `result.permission_denials` (primary signal, has `tool_input`) and the `deny` lines from `guard.jsonl` (secondary). For each unique action key it creates a `Decision(source="guard", decision_type="risk", destructive=True)` with options `["approve", "deny"]`, question built from the rule and the command. Duplicate keys in the same turn produce one decision.

* Approve: write the token, next implementer prompt: "The owner approved this action; run exactly: `<command>`."
* Deny: next implementer prompt: "The owner denied this action: `<command>`. Proceed without it."

The denial does not abort the iteration: stage, scan, check, commit and review still run on whatever the implementer produced, then the decision is asked before the next iteration starts. Simplification: decisions from the same turn are asked in sequence, each appends to `DECISIONS.md`.

The implementer standing rules gain one line: "A tool call may be denied by the orq guard. Follow the denial text; never work around it."

## 2. Guard: post execution diff rules

`src/orq/guard/diff_rules.py`: `evaluate(worktree, git, task, config) -> list[DiffViolation]`, run on the staged index right after `stage_all` and the secret scan, before the check command.

Inputs: `git diff --cached --name-status`, `git diff --cached --numstat`, `git diff --cached` (patch), all against `HEAD`.

| Rule id | Fires when |
|---|---|
| `deleted_file` | status `D` on any path |
| `removed_test` | a removed line matches a test definition (`def test_`, `it(`, `test(`, `describe(`, `#[test]`, `func Test`) and no added line in the diff contains the same name |
| `removed_export` | a removed top-level `def`/`class` (Python), `export function|const|class` (JS/TS), `pub fn` (Rust), `func Name` (Go) whose name is not re-added in the diff |
| `removed_route` | a removed line matches `@app.|@router.|app.(get|post|put|delete|patch)(|router.(get|...)(` and is not re-added |
| `protected_path` | a changed path matches `[git].protected_paths` (fnmatch with `**`) |
| `dependency_removed` | a removed line in `pyproject.toml`, `requirements*.txt`, `package.json`, `Cargo.toml`, `go.mod` looks like a dependency entry and is not re-added |
| `negative_balance` | total deleted minus added lines across source files exceeds `[guard].max_net_deleted_lines` (default 300) |

Violations are grouped into one `Decision(source="guard", decision_type="risk", destructive=True)` per iteration listing all of them, options `["approve", "deny"]`.

* Approve: continue (check, commit, review). Approved rule ids and paths are appended to `DECISIONS.md` so the reviewer sees them.
* Deny: `git reset --hard <last_commit>` plus `git clean -fd` in the worktree, event `rollback_iteration`, and the next implementer prompt: "The owner rejected these changes: <list>. The worktree was reset to the previous iteration. Proceed without them." The iteration ends without a commit or a review.

`removed_test`, `removed_export`, `removed_route`, `dependency_removed` are heuristics; false positives cost one owner answer, false negatives are caught by the reviewer's standing rule.

Config, new section:

```toml
[guard]
max_net_deleted_lines = 300
source_globs = ["**/*.py", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.rs", "**/*.go", "**/*.java", "**/*.cs"]
```

## 3. No progress detection and rollback

`src/orq/core/progress.py`: `ProgressTracker` keeps the per-iteration history (diff hash, check signature, next prompt) and returns the fired rule, if any, after each iteration:

* `same_diff`: `sha256(patch)` equal in 2 consecutive iterations (an empty diff twice also counts).
* `same_failure`: identical failing `CheckResult.signature` in 3 consecutive iterations.
* `same_prompt`: `difflib.SequenceMatcher(None, a, b).ratio() >= 0.9` between consecutive `next_prompt` values.

Fires once per distinct rule and streak. Decision: `source="orq"`, `decision_type="blocked"`, options `["continue", "rollback to iteration <n>", "abort"]` where `<n>` is the last iteration before the streak began. Recommendation index points to rollback.

* `continue`: history resets so the same streak does not fire again immediately.
* `rollback to iteration n`: `Runner.rollback_to(n)`: `git reset --hard <commit of iteration n>` and `git clean -fd`, drop the iteration commits after `n` from `state.json`, event `rollback`, and the next prompts (implementer and reviewer) carry "Iterations n+1..m were discarded by the owner: <one line each from the reviewer summaries>."
* `abort`: `ABORTED`.

Iteration commits are tracked in `state.json` as `commits: {"1": sha, "2": sha}`.

CLI `orq rollback <run_id> --to <n>` works only when the run has no live process (state `AWAITING_HUMAN`, `PAUSED_RATE_LIMIT`, `FAILED` or `ABORTED`, and the recorded `pid` is not alive). It performs the same reset, rewrites `state.json` so that `orq resume` continues at iteration `n+1`, and records the discarded range for the prompts.

## 4. Checkpoints and crash resume

### `state.json`

Written atomically (tmp + replace) at every checkpoint. Schema (version 1):

```json
{
  "version": 1,
  "run_id": "R7K2PQ",
  "state": "IMPLEMENTING",
  "pid": 12345,
  "iteration": 3,
  "phase": "implement",
  "branch": "orq/add-greeting",
  "worktree": "C:/orq-wt/sandbox/R7K2PQ",
  "repo_path": "C:/Users/me/.orq/repos/sandbox",
  "base_commit": "abc123",
  "last_commit": "def456",
  "commits": {"1": "…", "2": "def456"},
  "implementer_session": "uuid",
  "reviewer_session": null,
  "outcome": {"next_prompt": "…", "milestone": "m1", "done": false},
  "previous_check": {"ok": false, "exit_code": 1, "output_tail": "…", "timed_out": false},
  "pending_decision": {"decision_id": "D4F2", "kind": "guard_pre", "payload": {…}},
  "discarded": null,
  "reviewer_fallback_until": null,
  "rate_limit_until": null,
  "started_at": "2026-10-06T12:00:00Z",
  "elapsed_seconds": 1234.5
}
```

`phase` is one of `setup`, `implement`, `verify`, `review`, `finalize`, `await`, `done`. `pending_decision.kind` is one of `implementer`, `reviewer`, `guard_pre`, `guard_diff`, `secret`, `progress`, `error`, and `payload` holds what the continuation needs (denied actions, violation list, progress rule and target iteration, error text).

### Runner refactor

`Runner.execute()` becomes a loop over `phase`:

```
setup     -> implement
implement -> (decision pending: await) | verify
verify    -> (secret/diff decision: await) | review          (stage, scan, diff rules, check, commit)
review    -> (needs_human: await) | implement | finalize      (progress check happens here)
await     -> blocks on the human callback (interactive) or exits with AWAITING_HUMAN (resume mode)
finalize  -> done
```

Each phase function loads what it needs from the checkpoint and writes the next checkpoint before returning. `_ask` stores the decision, writes `pending_decision`, transitions to `AWAITING_HUMAN`, and then either calls the human callback (interactive `orq run`) or raises `_Stop(AWAITING_HUMAN)` when running headless. Applying an answer is a separate method `_apply_answer(decision, answer)` keyed by `pending_decision.kind`, used both by the interactive path and by `orq resume`.

`elapsed_seconds` accumulates across resumes so `max_wall_hours` still means total run time.

### Resume rules

`orq resume <run_id>`:

1. Load `state.json`; refuse when `state` is `DONE`.
2. If `pid` is alive, refuse ("run is still active"). If `<run_dir>/child.pid` names a live process (the adapter writes the CLI child's pid there), kill it (`taskkill /T /F` on Windows) and log it.
3. Verify the worktree exists and `HEAD == last_commit`. A different HEAD is an error that stops the resume and tells the owner.
4. Continue by `phase`:
   * `implement`: re-invoke the implementer with the stored session and a note: "Your previous turn was interrupted. The worktree holds your uncommitted work; continue from there." Uncommitted changes are kept (owner decision).
   * `verify`: re-run the verify phase from staging. Partial staging is idempotent.
   * `review`: re-run the reviewer for the same iteration (the commit exists).
   * `await`: if the pending decision is answered in SQLite, apply it and continue. Otherwise print the question and stay in `AWAITING_HUMAN`.
   * `finalize`: re-run finalize. `push` and `pr create` tolerate an existing branch and PR (`gh pr view` first).
   * `PAUSED_RATE_LIMIT` with `rate_limit_until` in the future: wait, then continue at the recorded phase.

Interactive `orq run` keeps asking in the terminal. Killing it and later running `orq answer` plus `orq resume` reaches the same place.

### New CLI commands

* `orq answer <decision_id> "<text>" | --approve | --deny`: validates the decision is pending, stores the answer (`answered_via="cli"`), appends to `DECISIONS.md`. Does not resume by itself.
* `orq resume <run_id>`: as above.
* `orq pause <run_id>`: creates `<run_dir>/pause.requested`; the loop checks it between phases and exits with `PAUSED` recorded in `state.json`. New `RunState.PAUSED` is added to the spec's side states.
* `orq abort <run_id>`: refuses if the process is alive; otherwise marks `ABORTED` and removes the worktree.

## 5. Rate limits and reviewer fallback

### Claude (implementer and Claude reviewer)

`src/orq/core/ratelimit.py`:

* `parse_claude_reset(text, now, zone_default) -> datetime | None` handles `resets 10pm (America/Cayenne)` and `resets 5:10am (Zone)`: next occurrence of that clock time in that zone.
* `reset_time(result) -> datetime | None` prefers `rate_limit.resetsAt` (epoch) when present, else the parsed text, else `now + 15 min`.

On `error_kind == "rate_limit"` from the implementer: event `rate_limit`, `rate_limit_until` written to `state.json`, transition `PAUSED_RATE_LIMIT`, print the wait, `asyncio.sleep` until reset plus 60 s jitter, then retry the same phase. The owner may kill the process and `orq resume` later. Retries are capped at `[limits].rate_limit_retries` (default 3) per iteration before the run pauses for the owner with an `error` decision.

### Codex and the reviewer router

`src/orq/adapters/router.py`: `ReviewerRouter(primary, fallback, config, clock)` implements `Agent`.

* After each primary call it reads `CodexReviewer.last_rate_limits` (the adapter parses the latest `token_count.rate_limits` snapshot from `~/.codex/sessions/<y>/<m>/<d>/rollout-*-<thread_id>.jsonl`, located by `thread_id`; missing file means no snapshot).
* Proactive: `primary.used_percent >= [reviewer].switch_at_used_percent` switches to the fallback until `primary.resets_at`.
* Reactive: a primary failure classified `rate_limit` (snapshot `rate_limit_reached_type` set, or message matching `usage limit|rate limit|quota`) switches immediately and the review is retried on the fallback in the same iteration.
* Events `reviewer_switched` and `reviewer_restored`; `state.json` carries `reviewer_fallback_until` so a resume keeps the choice.
* `CodexReviewer` classifies errors: `rate_limit`, `auth`, `invalid_output`, `error`.

### Unknown errors

A `FAILED` only happens on invariant violations (invalid reviewer output after retry, missing worktree). Any other agent error (`error_kind == "error"`, `"auth"`) raises an `error` decision: `source="orq"`, `decision_type="blocked"`, options `["retry", "abort"]`. `retry` repeats the phase.

## 6. Configuration additions

```toml
[limits]
rate_limit_retries = 3

[guard]
max_net_deleted_lines = 300
source_globs = ["**/*.py", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.rs", "**/*.go", "**/*.java", "**/*.cs"]

[git]
sandbox_repos = []   # empty allows any repo
```

## 7. Testing

Unit (fakes, no real CLIs):

* `tests/test_guard_rules.py`: every rule, allowed commands, worktree boundary, action key stability.
* `tests/test_guard_hook.py`: runs `hook.py` as a subprocess with payloads: deny JSON shape, token consumption, log line, non-matching tool.
* `tests/test_diff_rules.py`: a scratch repo per rule, approve and deny paths.
* `tests/test_progress.py`: the three rules and streak reset.
* `tests/test_loop.py` additions: denial becomes a decision; approve writes the token and the retry prompt; deny prompt; diff rule deny resets the worktree; progress decision and rollback; unknown error becomes a decision.
* `tests/test_resume.py`: checkpoint schema, resume from each phase with fakes, answered decision applied on resume, HEAD mismatch refused.
* `tests/test_cli.py` additions: `answer`, `resume`, `rollback`, `pause`, `abort`. Kill test: `orq run` in a subprocess with `fake_claude` scenario `hang`, process killed, `orq resume` finishes the run.
* `tests/test_ratelimit.py`: reset parsing with zones and midnight wrap, `resetsAt` preference, router switch and restore.

Integration (`-m integration`, real CLIs, sandbox repo):

* Implementer told to run `git reset --hard`: denied, decision raised, approve path runs it, deny path does not.
* Kill during an implementer turn, `orq resume` completes.

Findings go to `docs/phase2-findings.md` with the exact commands and samples, following the Phase 0 and 1 format.

## 8. Out of scope for Phase 2

Planning step, merge gate, model routing (Phase 3). Toast, WhatsApp, dashboard (Phase 4). Queue and concurrency (Phase 5).
