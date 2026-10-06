# Phase 3 Autonomous Close Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take a run from TASK.md to a merged PR with zero owner input when no decision is needed: planner with optional approval, model routing per milestone, and the full merge gate (CI wait with infra re-runs, rebase on base move, final review, squash merge, cleanup).

**Architecture:** The planner reuses the reviewer adapters with a second output contract. The gate is three new resumable phases (`gate_ci`, `gate_review`, `gate_merge`) after `finalize`, driven by a `CiWatcher` over `gh` and new `GitManager` operations. Everything is checkpointed in `state.json` like Phase 2.

**Tech Stack:** Python 3.12, `typer`, `asyncio`, `gh`, `pytest`. Design: `docs/superpowers/specs/2026-10-06-phase3-autonomous-close-design.md`.

---

## File structure

Create: `src/orq/verify/ci.py`, `tests/test_ci.py`.
Modify: `src/orq/adapters/schema.py` (contracts), `src/orq/adapters/base.py` (protocol kwargs), `src/orq/adapters/claude.py`, `src/orq/adapters/codex.py`, `src/orq/adapters/router.py`, `src/orq/config.py` (`[merge]`), `src/orq/core/checkpoint.py`, `src/orq/core/prompts.py` (planner prompt, plan blocks, final review mode), `src/orq/core/loop.py` (phases `plan`, `gate_*`), `src/orq/git/manager.py` (rebase, force push, merge, pr number), `src/orq/cli.py` (planner wiring, status shows milestone), `tests/fakes/fake_gh.py`, `tests/test_adapters.py`, `tests/test_loop.py`, `tests/test_resume.py`, `tests/test_cli.py`, `tests/test_config.py`, `docs/SPEC.md`, `docs/phase3-findings.md`.

---

### Task 1: output contracts and per-call model/effort

**Files:** `src/orq/adapters/schema.py`, `base.py`, `claude.py`, `codex.py`, `router.py`; tests in `tests/test_adapters.py`, `tests/test_router.py`.

- [ ] Tests: `validate_plan` accepts a good plan, rejects missing milestones / bad difficulty / `needs_human` without `human`; `PLAN_SCHEMA` has `additionalProperties: false` everywhere; `ClaudeImplementer.run(model="sonnet")` puts `--model sonnet` in argv; `CodexReviewer.run(effort="high", contract=PLAN_CONTRACT)` puts `model_reasoning_effort="high"` and writes the plan schema to `reviewer.schema.json`, and validates with the plan validator (fake codex scenario `plan`); `ClaudeReviewer.run(contract=PLAN_CONTRACT)` passes the plan schema to `--json-schema`; the router forwards `effort` and `contract` to both agents.
- [ ] Implement: `OutputContract(name, schema, validate)`, `PLAN_SCHEMA`, `validate_plan`, `REVIEW_CONTRACT`, `PLAN_CONTRACT`; `Agent.run(..., model=None, effort=None, contract=None)`; adapters honour them; `fake_codex.py` scenario `plan` emits a plan object.
- [ ] `uv run pytest -q`, commit `feat(adapters): plan contract, per-call model and reasoning effort`.

### Task 2: config, checkpoint, git operations, CI watcher

**Files:** `src/orq/config.py`, `src/orq/core/checkpoint.py`, `src/orq/git/manager.py`, `src/orq/verify/ci.py`; tests `tests/test_config.py`, `tests/test_git.py`, `tests/test_ci.py`.

- [ ] Tests (config): `[merge]` keys load with defaults. Tests (git, against the `origin` fixture): `base_moved` false then true after a push to main from the seed clone; `rebase_onto_base` returns True on a clean rebase and False (with `--abort`) on a conflict; `force_push` updates the remote ref; `pr_number` parses `gh pr view --json number` through the gh callable; `merge_pr` calls `gh pr merge <n> --squash --delete-branch` and `merged` checks `gh pr view --json state`.
- [ ] Tests (ci): a scripted gh callable returning a sequence of responses; `wait` returns `success` after pending; `none` after the grace period with no checks; infra failure (`run view` jobs without steps) triggers `run rerun` and then success; real failure returns `failure` with `failed_log`; timeout returns `timeout`; `sleep` is injected and the sequence of sleeps is asserted.
- [ ] Implement `MergeConfig`, checkpoint fields (`plan`, `milestone_index`, `plan_feedback`, `pr_number`, `pr_url`, `gate_rounds`, `ci_reruns`), `GitManager.base_moved/rebase_onto_base/force_push/pr_number/merge_pr/pr_state`, `CiStatus`, `CiWatcher`.
- [ ] Commit `feat: merge config, gate checkpoint fields, git gate operations and CI watcher`.

### Task 3: planner phase and milestones in the runner

**Files:** `src/orq/core/prompts.py`, `src/orq/core/loop.py`, `src/orq/cli.py`; tests `tests/test_loop.py`, `tests/test_resume.py`, `tests/test_cli.py`.

- [ ] Tests: planning runs before iteration 1, writes `PLAN.md`, checkpoint has milestones; implementer prompt carries the current milestone and the plan; implementer called with `model=sonnet` for a `mechanical` milestone and `opus` for `hard`; reviewer `done` on milestone 1 of 2 starts iteration 2 with the next milestone, `done` on the last enters finalize; `Plan approval: required` raises `plan_approval` (state `AWAITING_PLAN_APPROVAL`), `approve` continues, free text replans with the feedback in the planner prompt; planner `needs_human` asks and replans; resume from `plan`; `orq status` shows `milestone 1/2: <title>`.
- [ ] Implement: `PLANNER_RULES`, `build_planner_prompt`, plan blocks in both prompts (`plan_block(plan, index)`), `Runner.__init__(planner: Agent | None)` (defaults to the reviewer), phase `plan`, decision kinds `planner` and `plan_approval`, milestone advance in `_review`, `_implementer_model()`. `FakePlanner` in tests.
- [ ] Commit `feat(core): planner phase with optional approval, milestones and model routing`.

### Task 4: merge gate phases

**Files:** `src/orq/core/loop.py`, `src/orq/core/prompts.py`; tests `tests/test_loop.py`, `tests/test_resume.py`.

- [ ] Tests (fake `CiWatcher` and fake gh in the `env` fixture): happy path reaches `DONE` with `pr merge` called, worktree removed, `pr_merged` event; CI `failure` goes back to `implement` with the log in the prompt and the gate runs again (`gate_rounds` 1); base moved triggers rebase, force push and a second CI wait; final review `continue` loops back once then `done` merges; `max_gate_rounds` exceeded asks the owner; CI `none` asks the owner and `merge without CI` continues; CI `timeout` asks; rebase conflict asks `retry`/`abort`; resume from `gate_ci` and `gate_review`.
- [ ] Implement phases `finalize` (push, PR, record number), `gate_ci`, `gate_review` (mode `final`, `effort=final_effort`), `gate_merge`; decision kinds `ci_none`, `ci_timeout`, `rebase_conflict`, `gate_rounds`.
- [ ] Commit `feat(core): merge gate with CI wait, rebase, final review and squash merge`.

### Task 5: CLI end to end through the gate, docs, real runs

- [ ] `tests/fakes/fake_gh.py`: `FAKE_GH_SCENARIO=gate` answers `pr view --json number`, `pr checks` (pending once, then pass, via a counter file `FAKE_GH_STATE`), `pr merge`, `pr view --json state` (`MERGED`); the CLI end-to-end test asserts `DONE` and a `pr merge` call.
- [ ] Real runs on the sandbox: `~/.orq/tasks/phase3-*.md` with `Plan approval: skip` must reach `MERGED` with zero input; a second task with `required`. Record timings, CI queue times, infra re-runs if any.
- [ ] `docs/phase3-findings.md`; SPEC sections 6 (phases), 7 (milestones), 8.5 (plan contract), 10.7 (implementation notes), 12 (routing confirmed), 13 (`[merge]`), 15 (status).
- [ ] Commit `docs: Phase 3 findings and spec updates`.
