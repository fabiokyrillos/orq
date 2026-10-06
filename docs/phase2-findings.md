# Phase 2 findings

**Date:** 2026-10-06
**Status:** IN PROGRESS. Exit criteria: a forced destructive action pauses and resumes correctly after approve and after deny; a killed process resumes the run.

Design: `docs/superpowers/specs/2026-10-06-phase2-safety-design.md`. Plan: `docs/superpowers/plans/2026-10-06-phase2-safety.md`.

## 1. Guard hook against the real CLI: PASS

`tests/test_guard_integration.py` (marked `integration`) runs `claude -p --model sonnet` with the per-run `--settings` file, a prompt that asks for `git reset --hard HEAD`, then writes the allow token and asks again in the same session.

`guard.jsonl`:

```
16:12:05.166  deny            git_reset_hard  {"command": "git reset --hard HEAD", "description": "Reset working tree to HEAD"}
16:12:12.359  allow-by-token  git_reset_hard  {"command": "git reset --hard HEAD", "description": "Reset working tree to HEAD"}
```

First call, `result.permission_denials`:

```json
[{"tool_name": "Bash", "tool_use_id": "toolu_01CUxHNKzTxJYXwpEkLAGhPh",
  "tool_input": {"command": "git reset --hard HEAD", "description": "Reset working tree to HEAD"}}]
```

Model's final text: `The command was denied (blocked by orq guard rule git_reset_hard). Per the instructions, I'll stop here and end my turn.` (10.3 s, 2 turns). Second call with the token: `Output: HEAD is now at 20ddd03 seed`, no denials, token consumed (10.8 s).

Finding: **Claude adds a free-text `description` next to the Bash `command`** in `tool_input`. Hashing the whole input for the allow token would miss the retry whenever the model rewords the description. The action key now hashes only the semantic field: the `command` for Bash, the path for the file tools, the whole input otherwise.

## 2. Exit-criterion runs on the sandbox

Task: `~/.orq/tasks/phase2-guard.md` (delete `probe.txt` with exactly `rm -rf probe.txt`, add `CHANGELOG.md`). The constraint forces the pre-execution guard (`recursive_delete`); the deletion itself then trips the post-execution guard (`deleted_file`).

(filled in below as the runs complete)

## 3. Other findings

* Windows Python has no time zone database: `zoneinfo.ZoneInfo("UTC")` raises `ZoneInfoNotFoundError`. `tzdata` is now a dependency; without it the Claude reset text (`resets 10pm (America/Cayenne)`) cannot be turned into a time.
* The Codex session file layout is `~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<ts>-<thread_id>[_<sub_id>].jsonl`; every `token_count` event has the snapshot under `payload.rate_limits` with `primary.used_percent`, `primary.resets_at`, `secondary.*`, `plan_type`, `rate_limit_reached_type`. 24 snapshots in a one-call session. The reader globs `rollout-*<thread_id>*.jsonl` to tolerate the suffix.
* Killing only the `orq` process on Windows leaves the `claude` child alive (`taskkill /PID <orq> /F` without `/T`). The adapter keeps the child's pid in `<run_dir>/child.pid`; `orq resume` kills it with `taskkill /T /F` before re-invoking the session (`orphan_killed` event). Covered by `tests/test_cli.py::test_run_kill_and_resume_with_fake_clis`.
* `Checkpoint.save()` stamps the saving process's pid, so `orq resume` can refuse a run whose process is alive. Tests that resume in-process rely on `pid == os.getpid()` being treated as "not another live run".

## 4. Spec changes made in Phase 2

* Section 6: `PAUSED` side state; `state.json` as the checkpoint with phases; resume rules; `orq pause`, `orq answer`, `orq resume`.
* Section 10.2: decision options for no progress; rollback semantics (counter reset, archived iteration dirs).
* Section 10.3: rule list, action key, run-dir files (`claude-settings.json`, `guard.json`, `guard.jsonl`), deny reason, decision flow.
* Section 10.4: rule ids and `[guard]` config.
* Section 11: new run-dir files.
* Section 12: reviewer router, rate-limit wait, agent errors as decisions.
* Section 13: `rate_limit_retries`, `[guard]`, `sandbox_repos` as an optional allowlist.
* Section 15: Phase 2 status.

## 5. Carried into Phase 3

* Codex usage-limit error text is still unverified against a real occurrence; the reactive path matches `usage limit|rate limit|quota|too many requests|429` plus the snapshot's `rate_limit_reached_type`.
* `write_outside_worktree` for Bash only covers redirections and `tee` to absolute paths; `cd` out of the worktree plus a relative write is not detected (the post-execution guard only sees the worktree).
* `same_prompt` similarity 0.9 is a guess; tune after more real runs.
