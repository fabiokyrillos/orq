# Phase 1 findings

**Date:** 2026-10-06
**Status:** COMPLETE. Exit criterion met: a real task on the sandbox repo went from `TASK.md` to an open PR with no copy and paste.

## The exit-criterion run

Task: `~/.orq/tasks/phase1-greeting.md` (add `greet.py` plus `unittest` tests, check command `python -m unittest discover -q`).

```
run RV6BP5  repo fabiokyrillos/orq-phase0-sandbox  branch orq/add-greeting-module
14:36:39 QUEUED -> worktree C:\orq-wt\orq-phase0-sandbox\RV6BP5 from 99ad6ef
14:36:41 IMPLEMENTING iter 1   claude (opus) 28 s, 7.7k cache-creation tokens
14:37:09 VERIFYING             gitleaks clean, check ok (exit 0), commit 1df781d
14:37:11 REVIEWING             codex (gpt-5.5, low, --ignore-user-config) 54 s -> status done
14:38:04 FINALIZING            gitleaks range clean, push, gh pr create
14:38:11 DONE                  https://github.com/fabiokyrillos/orq-phase0-sandbox/pull/2   (CI: pass)
```

Total 92 s, one iteration, one Opus call, one Codex call.

## Problems the run exposed, all fixed with tests

1. **Generated files were committed.** The PR carried `__pycache__/*.pyc`: the check command creates them and the iteration commit re-staged everything after the check. Fix: the iteration stages and scans *before* the check, and commits only the index (`commit_staged`). Files that appear during the check never reach the commit, and the commit is exactly what gitleaks scanned.
2. **Reviewer said `done` with a `major` issue open.** Codex flagged the `.pyc` files as major and still returned `done`. Fix, two layers: the reviewer rules now say `done` requires no blocker or major issue, and orq enforces it deterministically: `done` plus any blocker/major issue is treated as `continue` with a `next_prompt` built from the issues (`done_overridden` event).
3. **Second run of the same task would crash.** The branch `orq/<slug>` already exists locally and on origin after the first run. Fix: when the branch exists, the run uses `orq/<slug>-<run_id>` and records it.

## Other findings

* **Codex cannot run `python` inside its read-only sandbox** on this machine: `Acesso negado` for the user-profile Python install. `git` commands work. The reviewer does not need to re-run checks because orq passes `checks.txt` output in the prompt, but a reviewer prompt that asks it to run the check command will fail.
* **`--ignore-user-config` is worth it:** on the Phase 0 trivial prompt, 27.8k -> 19.8k input tokens and 43 s -> 12 s, and the MCP auth errors in stderr disappear. Default on (`reviewer.codex_ignore_user_config`).
* **Windows console encoding:** a Python child of the check runner wrote cp1252; `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` are forced on check commands.
* **External CLIs are overridable** with `ORQ_CLAUDE_EXE`, `ORQ_CODEX_CMD`, `ORQ_GH_CMD` (space-separated argv, quotes allowed). The CLI end-to-end test drives a full run through fake CLIs against a local bare remote.

## Spec changes made in Phase 1

* Section 7: stage and scan before the check; commit only the index; `done` with blocker/major issues is `continue`.
* Section 10.6: branch collision rule.
* Section 15: Phase 1 marked complete.

## Carried into Phase 2

* PR #2 on the sandbox still contains the `.pyc` files; close it unmerged and let a Phase 2 run produce a clean one.
* No guard hook yet: the implementer runs with `--dangerously-skip-permissions` and only `sandbox_repos` is allowed.
* Rate limits: `PAUSED_RATE_LIMIT` stops the run; backoff and the Claude reviewer fallback are Phase 2.
* Codex usage-limit error text still unverified.
