# Phase 3 design: autonomous close

Date: 2026-10-06. Source of truth remains `docs/SPEC.md` (sections 4, 6, 7, 10.7, 12, 15). This document fixes the open design points for Phase 3. Contradictions found during implementation update the spec in the same change.

Phase 3 scope (SPEC section 15): planning step with optional approval; merge gate (section 10.7) including rebase and CI re-run; model routing per milestone.

Exit criterion: a task goes from TASK.md to a merged PR with zero owner input when no decision is needed.

Decisions taken with the owner (2026-10-06):

* A repo with no CI checks after a grace period asks the owner (`merge without CI` or `abort`); no silent merge.
* A rebase conflict stops with an owner decision in this phase; automatic resolution by the implementer is deferred.
* The planner may raise `needs_human` before any code is written.

## 1. Planning

### Contract (new SPEC section 8.5)

The planner is the reviewer agent at `[reviewer].final_effort` with a strict output schema (Codex rules: `additionalProperties: false`, every property required, optional values nullable):

```json
{
  "status": "plan | needs_human",
  "summary": "string",
  "milestones": [
    {"title": "string", "goal": "string", "done_when": "string", "difficulty": "hard | mechanical"}
  ],
  "human": { "decision_type": "...", "question": "...", "options": ["..."], "recommendation": 0 }
}
```

`human` is null unless `status` is `needs_human`; `milestones` is empty then. One to eight milestones, ordered. `difficulty` drives the implementer model (section 3).

### Flow

* New phase `plan` between `setup` and `implement`. Prompt: task block, repo hint ("read the repository"), the planner standing rules, and `plan_feedback` from the owner when replanning.
* `status: needs_human` raises a decision of kind `planner`; the answer lands in `DECISIONS.md` and the planner runs again.
* `## Plan approval: required` raises a decision of kind `plan_approval` (`AWAITING_PLAN_APPROVAL`) with options `approve` and `revise`; a free-text answer counts as `revise` with that text as feedback, and the planner runs again. `skip` goes straight to `implement`.
* The plan is written to `<run_dir>/PLAN.md` (markdown) and kept in the checkpoint (`plan`, `milestone_index`). Replans overwrite both.
* Both prompts carry the full plan and the current milestone (title, goal, done_when). The reviewer's `done` means "this milestone is done"; orq advances `milestone_index` and starts the next iteration with "Milestone N done. Start milestone N+1: ...". `done` on the last milestone enters the gate.

### Output contracts in the adapters

`adapters/schema.py` gains `OutputContract(name, schema, validate)` with `REVIEW_CONTRACT` and `PLAN_CONTRACT`. `Agent.run` gains three optional keyword arguments: `model`, `effort`, `contract`. The implementer honours `model`; the reviewers honour `effort` and `contract` (default `REVIEW_CONTRACT`); the router passes all three through.

## 2. Merge gate

`finalize` is split into resumable sub-phases, each with its own checkpoint write:

```
finalize    push branch; create the PR unless one exists; record pr_number, pr_url   -> gate_ci
gate_ci     wait for GitHub checks (section 2.1); rebase if base moved (2.2)         -> gate_review | implement
gate_review final review at final_effort, mode "final" (2.3)                         -> gate_merge | implement
gate_merge  no pending decisions, secret scan of the range, merge, cleanup (2.4)     -> done
```

### 2.1 CI (`verify/ci.py`)

`CiWatcher.wait(worktree, pr_number) -> CiStatus` polls `gh pr checks <n> --json name,state,bucket,link` every `[merge].poll_seconds` (default 20) for up to `[merge].ci_timeout_minutes` (default 60).

* `no checks reported` (exit 1) counts as pending during `[merge].ci_grace_minutes` (default 5); after that with still no checks the status is `none` and the runner raises the owner decision from the first bullet above.
* All buckets `pass` or `skipping` -> `success`.
* Any bucket `fail`: the run id comes from the check's `link` (`/actions/runs/<id>/job/<jid>`). `gh run view <id> --json conclusion,jobs` with every job lacking steps (or an annotation about no runner acquired) is an infrastructure failure: `gh run rerun <id>` and keep waiting, at most `[merge].max_ci_reruns` (default 3) per gate round. Otherwise `failure` with the tail of `gh run view <id> --log-failed` (4000 chars) in `CiStatus.failed_log`.
* Timeout -> `timeout`; the runner asks the owner (`keep waiting`, `abort`).

A real failure sends the run back to `implement` with the failed log as the next prompt (a new iteration, then the gate restarts). `gate_rounds` counts gate restarts; past `[merge].max_gate_rounds` (default 3) the owner is asked.

### 2.2 Base moved

Before the final review: `git fetch origin <base>`; if `origin/<base>` is not an ancestor of HEAD, `git rebase origin/<base>`. Success -> `git push --force-with-lease origin <branch>` (orq's own push, never the implementer's) and back to `gate_ci`. Conflict -> `git rebase --abort` and a `blocked` decision with options `retry` (after the owner fixed things by hand) and `abort`.

### 2.3 Final review

The reviewer runs with `effort=final_effort`, mode `final`: the prompt carries the whole task, the full plan, the PR diff stat against the base and the CI result, and asks for `done` only when every acceptance criterion holds. `continue` or `done` with blocker/major issues -> back to `implement` with the issues as the prompt (gate round + 1). `needs_human` -> decision as usual.

### 2.4 Merge

Preconditions re-checked: no pending decisions in the store, `gitleaks` range scan clean. Then `gh pr merge <n> --<strategy> --delete-branch` (`[git].merge_strategy`, default `squash`), `gh pr view <n> --json state` must say `MERGED`, worktree and local branch removed, `pr_merged` event, `DONE`.

## 3. Model routing

* Implementer model per milestone: `hard` -> `[implementer].default_model`, `mechanical` -> `[implementer].mechanical_model`. Passed as `model=` on every implementer call; Phase 0 confirmed `--resume` with a different `--model` keeps the context.
* Reviewer effort: `routine_effort` for milestone reviews, `final_effort` for planning and the final review.
* Events `implementer_model` (per iteration) and `reviewer_effort` are recorded in `events.jsonl` for the findings.

## 4. Checkpoint additions

`plan` (the planner output), `milestone_index`, `plan_feedback`, `pr_number`, `pr_url`, `gate_rounds`, `ci_reruns`. Phases: `plan`, `gate_ci`, `gate_review`, `gate_merge` added to the phase list.

## 5. Configuration

```toml
[merge]
poll_seconds = 20
ci_timeout_minutes = 60
ci_grace_minutes = 5
max_ci_reruns = 3
max_gate_rounds = 3
```

## 6. Testing

* `tests/test_schema.py` (or additions to `test_adapters.py`): plan contract validation, adapters honour `model`, `effort` and `contract`.
* `tests/test_ci.py`: scripted `gh` callable; pending then success; grace period and `none`; infra failure reruns then success; real failure returns the log; timeout.
* `tests/test_loop.py`: planning writes PLAN.md and milestones; plan approval `required` pauses, `approve` continues, free text replans; planner `needs_human`; milestone advance on `done`; implementer model per difficulty; gate: CI failure goes back to implement with the log; base moved triggers rebase and a second CI wait; final review `continue` loops once; merge calls `gh pr merge` and removes the worktree; max gate rounds asks the owner; no CI asks the owner.
* `tests/test_resume.py`: resume from `plan`, `gate_ci` and `gate_review`.
* `tests/fakes/fake_gh.py`: scripted answers for `pr checks`, `run view`, `run rerun`, `pr merge`, `pr view` driven by `FAKE_GH_SCENARIO` and a counter file, so the CLI end-to-end test reaches `DONE` through the gate.
* Real runs on the sandbox: one with `Plan approval: skip` reaching MERGED with zero input (exit criterion); one with `required` exercising the approval.

## 7. Out of scope

Automatic conflict resolution, notifications, dashboard (Phase 4), queue (Phase 5).
