# Phase 3 findings

**Date:** 2026-10-06
**Status:** COMPLETE. Exit criterion met on the sandbox repo: run `RV348F` went from TASK.md to a merged PR with zero owner input (2 min 49 s). Run `RSPZNW` exercised plan approval with a revision and also merged.

Design: `docs/superpowers/specs/2026-10-06-phase3-autonomous-close-design.md`. Plan: `docs/superpowers/plans/2026-10-06-phase3-autonomous-close.md`.

## 1. Exit-criterion runs on the sandbox

Task: `~/.orq/tasks/phase3-greeting.md` (`greet.py` plus `unittest` tests, `Plan approval: skip`). Implementer: Opus/Sonnet by milestone. Planner and reviewer: Codex gpt-5.5 (`high` for planning and the final review, `low` for milestone reviews). All runs headless (`--no-prompt`).

### Attempt 1, run `RTBQ45`: aborted, two prompt problems

* The task text said the run "must go from TASK.md to a merged PR with no owner input". The planner turned that into milestones 3 and 4, "Run verification" and "Open and merge PR"; the reviewer then listed "no local evidence shows the PR has been merged" as a major issue, and in iteration 2 the implementer stopped with a decision marker asking who should push and merge. Lesson: orchestration concerns must never appear in TASK.md, and the planner needs an explicit ownership rule.
* The reviewer reported `python is not recognized on PATH` as a blocker although the orchestrator's passing `checks.txt` was in its prompt (Codex runs in a read-only sandbox without python, known since Phase 1).

### Attempt 2, run `RRBMQC`: aborted, the reviewer still tried to run the check

With the ownership and "do not re-run the check" rules added to the prompts, the planner still produced a third milestone "Run check command", and Codex still answered `needs_human` with "Should the environment be fixed to provide python?" while `checks.txt` read `Ran 2 tests ... OK`. Prompt text alone does not fix either behaviour.

Deterministic fixes (`src/orq/core/hygiene.py`):

* Milestones whose title or goal describe running the check, committing, pushing, opening or merging the PR are dropped from the plan (`milestones_dropped` event). If every milestone would be dropped the plan is kept as is.
* When the orchestrator's check passed and the review complains about a missing tool (`python is not recognized`, `cannot find`, `could not run the check`...), the review is re-run once with a correction appended to the prompt (`reviewer_sandbox_retry`). Complaints that survive the retry are stripped: matching issues are removed and a `needs_human` about the sandbox becomes `continue` (`reviewer_sandbox_complaint_stripped`). A complaint with a failing check is left alone: it may be a real question.

### Attempt 3, run `RV348F`: TASK.md to merged PR, zero owner input, 2 min 49 s

```
21:13:46 QUEUED             worktree from 99ad6ef
21:13:48 PLANNING           codex high, 24 s; 3 milestones proposed, "Run check command" dropped -> 2 kept, both mechanical
21:14:12 IMPLEMENTING 1     sonnet, 22 s: greet.py and test_greet.py written in one turn
21:14:36 VERIFYING 1        check ok (Ran 2 tests, OK), commit fa09c64
21:14:36 REVIEWING 1        codex low, 20 s: done -> milestone 1/2 done
21:14:56 IMPLEMENTING 2     sonnet, 10 s: nothing left to do for milestone 2, no changes
21:15:07 VERIFYING 2        check ok, nothing to commit
21:15:07 REVIEWING 2        codex low, 28 s: done (last milestone) -> gate
21:15:35 FINALIZING push    push, gh pr create -> PR #4
21:15:42 gate_ci            base not moved; checks passed after 22 s (one poll; the free runner was quick this time)
21:16:06 gate_review        codex high, 22 s: done, no issues
21:16:28 gate_merge         no pending decisions, range scan clean, gh pr merge --squash --delete-branch, state MERGED (4075fdf)
21:16:35 DONE               worktree and local branch removed; remote branch deleted by gh
```

Confirmed on GitHub afterwards: PR #4 `MERGED`, `main` now holds `greet.py` and `test_greet.py`, only `main` remains as a remote branch.

Observations:

* The hygiene rules fired exactly once each where expected (`milestones_dropped`), and the reviewer did not complain about its sandbox this time with the explicit "authoritative, do not run it yourself" header, so no `reviewer_sandbox_retry` was needed.
* Two milestones for a 20-line task is one too many: the implementer finished both in iteration 1 and iteration 2 was an empty round trip (10 s Sonnet plus 28 s Codex). A clean `done` with an empty diff is not treated as a stall, by design. Cheaper would be letting the reviewer mark several milestones done at once; left for a later phase.
* Total agent time: planner 24 s, implementer 32 s, milestone reviews 48 s, final review 22 s, CI 22 s. Wall clock 2 min 49 s.

### Run `RSPZNW`: plan approval required, revision by free text, merged

Task: `~/.orq/tasks/phase3-farewell.md` (`farewell()` next to `greet()`, `Plan approval: required`).

```
21:17:08 PLANNING                 codex high, 30 s; 3 milestones, "Run check" dropped -> 2 kept
21:17:38 AWAITING_PLAN_APPROVAL   DPSAC: "Plan proposed (2 milestones) ... approve | revise"
21:18:04 orq answer DPSAC "One milestone is enough: add farewell and its tests together."
21:18:10 PLANNING                 replanned with the feedback in the prompt, 23 s -> 1 milestone "Add farewell and tests"
21:18:33 AWAITING_PLAN_APPROVAL   DR2ZJ -> orq answer DR2ZJ --approve
21:18:34 IMPLEMENTING 1           sonnet, 21 s; check ok; commit 2ebeb07
21:18:56 REVIEWING 1              codex low, 19 s: done
21:19:15 FINALIZING               PR #5; checks passed after 21 s; final review (codex high, 65 s) done; squash merge; worktree removed
21:20:57 DONE                     PR #5 MERGED: greet.py and test_greet.py
```

Confirmed: free-text answers to the plan approval reach the planner as a revision request and the revised plan is asked for approval again; `approve` starts the first iteration. Two owner inputs in total, both intended.

## 2. Other findings

* The planner output contract (SPEC 8.5) worked first time with Codex `--output-schema`: both plans validated, difficulty tags were all `mechanical` for this small task, so every implementer turn ran on Sonnet (`implementer_model` events).
* The planner reads the repository before planning (its summary lists the root files); 24 to 50 s at `high` effort.
* The free sandbox runner picked both PR jobs up within 25 s on these runs (Phase 0 saw 10-minute queues and 15-minute infrastructure cancellations); the re-run path stays covered by `tests/test_ci.py` only.
* Worktrees of runs that ended before the gate existed (`RV6BP5`, `RFZFFC`) and their local branches are still under `C:\orq-wt` and the repo clone; `orq abort` or a manual `git worktree remove` cleans them. Runs that merge clean up after themselves.
* `orq answer` with free text on a plan approval stores the text verbatim; the planner prompt shows it under "The owner asked for a revised plan".

## 3. Spec changes made in Phase 3

* Section 6: phases `plan`, `gate_ci`, `gate_review`, `gate_merge`.
* Section 7: milestone semantics of `done`; clean `done` never counts as no progress.
* Section 8.5: planner output contract.
* Section 10.7: gate implementation notes.
* Section 12: model routing per milestone confirmed.
* Section 13: `[merge]` and `keep_worktree`.
* Section 15: Phase 3 status.

## 4. Carried into Phase 4

* Automatic rebase-conflict resolution by the implementer (owner decision for now).
* The Codex sandbox cannot run python; the reviewer prompt and the hygiene rules work around it. A `windows.sandbox` setting that exposes the user-profile Python would remove the need.
