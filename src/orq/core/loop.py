"""The run state machine (SPEC sections 6, 7, 10 and 12).

Every step reads and writes the Checkpoint (state.json), so a pause, an owner decision or a crash all
resume through the same code path: `execute()` dispatches on `checkpoint.phase` until the run ends.

Not here yet: planning and the merge gate (Phase 3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from orq.adapters.base import Agent, AgentResult
from orq.adapters.schema import PLAN_CONTRACT, REVIEW_CONTRACT, validate_plan, validate_review
from orq.config import Config
from orq.core.checkpoint import Checkpoint
from orq.core.models import Decision, RunRecord, RunState, new_decision_id, new_run_id
from orq.core.procs import kill_tree, pid_alive
from orq.core.progress import ProgressTracker
from orq.core.prompts import (IMPLEMENTER_RULES, build_implementer_prompt, build_planner_prompt, build_reviewer_prompt,
                              guard_outcome_lines, plan_markdown)
from orq.core.ratelimit import claude_reset_time
from orq.core.task import Task, parse_task
from orq.git.manager import GitManager
from orq.guard.diff_rules import evaluate_staged
from orq.guard.rules import action_key, describe_action
from orq.guard.settings import write_hook_settings
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir
from orq.verify.checks import CheckResult, run_check
from orq.verify.ci import CiStatus, CiWatcher
from orq.verify.secrets import SecretScanner

HumanInput = Callable[[Decision], str]
Printer = Callable[[str], None]

DIFF_INLINE_LIMIT = 20_000
CHECK_TIMEOUT_SECONDS = 1800
PAUSE_FLAG = "pause.requested"
RATE_LIMIT_JITTER_SECONDS = 60
_PHASE_STATE = {"plan": RunState.PLANNING, "implement": RunState.IMPLEMENTING, "verify": RunState.VERIFYING,
                "review": RunState.REVIEWING, "finalize": RunState.FINALIZING, "gate_ci": RunState.FINALIZING,
                "gate_review": RunState.FINALIZING, "gate_merge": RunState.FINALIZING}


class SandboxError(RuntimeError):
    pass


class ResumeError(RuntimeError):
    pass


class _Stop(Exception):
    def __init__(self, state: RunState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


class _Yield(Exception):
    """A decision was raised mid-phase; control returns to the main loop, now in the `await` phase."""


class Runner:
    def __init__(self, *, config: Config, paths: OrqPaths, store: Store, task: Task, task_text: str,
                 git: GitManager, implementer: Agent, reviewer: Agent, scanner: SecretScanner, human: HumanInput | None,
                 clone_url: str | None = None, printer: Printer = print, run_id: str | None = None,
                 checkpoint: Checkpoint | None = None, planner: Agent | None = None, ci: CiWatcher | None = None) -> None:
        if config.git.sandbox_repos and task.repo not in config.git.sandbox_repos:
            raise SandboxError(f"{task.repo} is not listed in [git].sandbox_repos (empty list allows any repo)")
        self.config, self.paths, self.store, self.task = config, paths, store, task
        self.git, self.implementer, self.reviewer, self.scanner = git, implementer, reviewer, scanner
        self.planner = planner or reviewer  # SPEC 4: the planner is the reviewer agent at high effort
        self.ci = ci or CiWatcher(git.gh, poll_seconds=config.merge.poll_seconds, timeout_minutes=config.merge.ci_timeout_minutes,
                                  grace_minutes=config.merge.ci_grace_minutes, max_reruns=config.merge.max_ci_reruns)
        self.human, self.clone_url, self.print = human, clone_url, printer
        self._resumed_at = time.monotonic()
        if checkpoint is None:
            self.run_id = run_id or new_run_id()
            branch = f"orq/{task.slug}"
            worktree = config.git.worktree_root / task.repo.split("/")[-1] / self.run_id
            self.rundir = RunDir(paths.run_dir(self.run_id))
            self.rundir.create(task_text)
            self.cp = Checkpoint(run_id=self.run_id, state=RunState.QUEUED.value, phase="setup", branch=branch,
                                 worktree=str(worktree), clone_url=clone_url,
                                 started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self.store.create_run(RunRecord(run_id=self.run_id, repo=task.repo, task_title=task.title, branch=branch,
                                            worktree=str(worktree)))
        else:
            self.run_id, self.cp = checkpoint.run_id, checkpoint
            self.rundir = RunDir(paths.run_dir(self.run_id))
        self.progress = ProgressTracker(self.cp.progress_history)
        write_hook_settings(self.rundir.path, worktree=self.worktree, protected_paths=config.git.protected_paths)
        if self.cp.reviewer_fallback_until is not None and hasattr(self.reviewer, "fallback_until"):
            self.reviewer.fallback_until = self.cp.reviewer_fallback_until  # type: ignore[attr-defined]
        self._save()

    @property
    def worktree(self) -> Path:
        return Path(self.cp.worktree)

    @property
    def branch(self) -> str:
        return self.cp.branch

    @classmethod
    def resume(cls, *, run_id: str, config: Config, paths: OrqPaths, store: Store, git: GitManager, implementer: Agent,
               reviewer: Agent, scanner: SecretScanner, human: HumanInput | None, printer: Printer = print,
               planner: Agent | None = None, ci: CiWatcher | None = None) -> Runner:
        """Rebuild a Runner from state.json. Refuses live runs, finished runs and worktrees that moved."""
        rundir = RunDir(paths.run_dir(run_id))
        cp = Checkpoint.load(rundir.path / "state.json")
        if cp is None:
            raise ResumeError(f"no state.json for run {run_id}")
        if cp.phase == "done" or cp.state in (RunState.DONE.value, RunState.ABORTED.value):
            raise ResumeError(f"run {run_id} is {cp.state}")
        if cp.pid != os.getpid() and pid_alive(cp.pid):
            raise ResumeError(f"run {run_id} is still active (pid {cp.pid})")
        child = rundir.path / "child.pid"
        if child.exists():
            try:
                pid = int(child.read_text(encoding="utf-8").strip() or 0)
            except ValueError:
                pid = 0
            if pid_alive(pid):
                kill_tree(pid)
                rundir.event("orphan_killed", pid=pid)
            child.unlink(missing_ok=True)
        task_text = (rundir.path / "TASK.md").read_text(encoding="utf-8")
        task = parse_task(task_text)
        if cp.phase != "setup":
            worktree = Path(cp.worktree)
            if not worktree.exists():
                raise ResumeError(f"worktree {cp.worktree} is missing")
            head = git.head(worktree)
            if cp.last_commit and head != cp.last_commit:
                # The crash may have landed between the iteration commit and the next checkpoint write.
                subject, parent = git.commit_subject_and_parent(worktree)
                if parent == cp.last_commit and subject.startswith(f"orq({run_id}) iter {cp.iteration}:"):
                    cp.last_commit = head
                    cp.commits[str(cp.iteration)] = head
                else:
                    raise ResumeError(f"worktree HEAD {head[:8]} differs from the last recorded commit {cp.last_commit[:8]}")
        if cp.phase == "implement":
            cp.interrupted = True
        (rundir.path / PAUSE_FLAG).unlink(missing_ok=True)
        rundir.event("resume", phase=cp.phase, iteration=cp.iteration, state=cp.state)
        return cls(config=config, paths=paths, store=store, task=task, task_text=task_text, git=git, implementer=implementer,
                   reviewer=reviewer, scanner=scanner, human=human, printer=printer, checkpoint=cp, planner=planner, ci=ci)

    # persistence

    def _save(self) -> None:
        self.cp.progress_history = self.progress.history
        now = time.monotonic()
        self.cp.elapsed_seconds += now - self._resumed_at
        self._resumed_at = now
        self.cp.save(self.rundir.path / "state.json")

    def _transition(self, state: RunState, **data: object) -> None:
        self.rundir.event("state", state=state.value, **data)
        self.store.set_state(self.run_id, state)
        self.cp.state = state.value
        self._save()
        self.print(f"[{self.run_id}] {state.value}" + (f" {data}" if data else ""))

    def _set_phase(self, phase: str) -> None:
        self.cp.phase = phase
        self._save()

    # main loop

    async def execute(self) -> RunState:
        try:
            while True:
                if (self.rundir.path / PAUSE_FLAG).exists():
                    raise _Stop(RunState.PAUSED, "pause requested by the owner")
                self._check_wall_time()
                await self._wait_rate_limit_if_needed()
                phase = self.cp.phase
                if phase == "setup":
                    self._setup()
                elif phase == "plan":
                    await self._plan()
                elif phase == "implement":
                    await self._implement()
                elif phase == "verify":
                    self._verify()
                elif phase == "review":
                    await self._review()
                elif phase == "await":
                    self._await()
                elif phase == "finalize":
                    self._finalize()
                elif phase == "gate_ci":
                    self._gate_ci()
                elif phase == "gate_review":
                    await self._gate_review()
                elif phase == "gate_merge":
                    if self._gate_merge():
                        self._set_phase("done")
                        self._transition(RunState.DONE)
                        return RunState.DONE
                elif phase == "done":
                    return RunState.DONE
                else:
                    raise _Stop(RunState.FAILED, f"unknown phase {phase}")
        except _Yield:
            return await self.execute()
        except _Stop as stop:
            if stop.state is RunState.AWAITING_HUMAN:  # already recorded when the decision was raised
                return RunState(self.cp.state)
            self._transition(stop.state, reason=stop.reason)
            return stop.state

    def _check_wall_time(self) -> None:
        elapsed_hours = (self.cp.elapsed_seconds + (time.monotonic() - self._resumed_at)) / 3600
        if elapsed_hours > self.config.limits.max_wall_hours:
            raise _Stop(RunState.FAILED, f"max wall time ({self.config.limits.max_wall_hours} h) exceeded")

    async def _wait_rate_limit_if_needed(self) -> None:
        until = self.cp.rate_limit_until
        if until is None:
            return
        remaining = until - time.time()
        if remaining > 0:
            if self.cp.state != RunState.PAUSED_RATE_LIMIT.value:
                self._transition(RunState.PAUSED_RATE_LIMIT, until=datetime.fromtimestamp(until, tz=timezone.utc).isoformat())
            await asyncio.sleep(remaining + RATE_LIMIT_JITTER_SECONDS)
        self.cp.rate_limit_until = None
        self._save()

    # phases

    def _setup(self) -> None:
        self._transition(RunState.QUEUED)
        repo_path = self.git.ensure_repo(self.task.repo, self.paths.repos, clone_url=self.clone_url or self.cp.clone_url)
        if self.git.branch_exists(repo_path, self.cp.branch):
            # A previous run of the same task left its branch behind; keep both apart.
            self.cp.branch = f"{self.cp.branch}-{self.run_id.lower()}"
            self.store.set_branch(self.run_id, self.cp.branch)
        self.git.create_worktree(repo_path, self.worktree, branch=self.cp.branch, base=self.task.base_branch)
        self.cp.repo_path = str(repo_path)
        self.cp.base_commit = self.cp.last_commit = self.git.head(self.worktree)
        self.rundir.event("worktree", path=str(self.worktree), branch=self.cp.branch, base_commit=self.cp.base_commit)
        self._set_phase("plan")

    async def _plan(self) -> None:
        """Planning step (SPEC 8.5): milestones with difficulty, optional owner approval."""
        self._transition(RunState.PLANNING)
        prompt = build_planner_prompt(self.task, decisions=self.rundir.decisions_text(), feedback=self.cp.plan_feedback)
        (self.rundir.path / "planner.prompt.md").write_text(prompt, encoding="utf-8")
        result = await self._call("planner", self.planner, prompt, self.rundir.path / "planner.stream.jsonl", None,
                                  effort=self.config.reviewer.final_effort, contract=PLAN_CONTRACT)
        if result.error_kind == "invalid_output":
            self.rundir.event("planner_invalid_output", error=result.error)
            result = await self._call("planner", self.planner, prompt, self.rundir.path / "planner.stream.jsonl", None,
                                      effort=self.config.reviewer.final_effort, contract=PLAN_CONTRACT)
        problems = validate_plan(result.structured)
        if problems:
            raise _Stop(RunState.FAILED, "planner output invalid: " + "; ".join(problems))
        plan: dict = result.structured  # type: ignore[assignment]
        if plan["status"] == "needs_human":
            h = plan.get("human") or {}
            self._raise_decision("planner", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="planner",
                decision_type=str(h.get("decision_type", "ambiguity")), question=str(h.get("question", "")),
                options=[str(o) for o in h.get("options", [])], recommendation=h.get("recommendation")), payload={})
            return
        self.cp.plan = {"summary": plan.get("summary", ""), "milestones": plan["milestones"]}
        self.cp.milestone_index = 0
        self.cp.plan_feedback = None
        (self.rundir.path / "PLAN.md").write_text(plan_markdown(self.cp.plan), encoding="utf-8")
        self.rundir.event("plan", milestones=[(m["title"], m["difficulty"]) for m in plan["milestones"]], summary=plan.get("summary", ""))
        if self.task.plan_approval == "required":
            titles = "\n".join(f"{i + 1}. [{m['difficulty']}] {m['title']}" for i, m in enumerate(plan["milestones"]))
            self._raise_decision("plan_approval", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="planner", decision_type="business",
                question=f"Plan proposed ({len(plan['milestones'])} milestones):\n{titles}\nApprove it, or answer with what to change.",
                options=["approve", "revise"], recommendation=0), payload={}, state=RunState.AWAITING_PLAN_APPROVAL)
            return
        self._next_iteration()

    def _current_milestone(self) -> dict | None:
        milestones = (self.cp.plan or {}).get("milestones") or []
        if not milestones:
            return None
        return milestones[min(self.cp.milestone_index, len(milestones) - 1)]

    def _implementer_model(self) -> str:
        milestone = self._current_milestone() or {}
        if milestone.get("difficulty") == "mechanical":
            return self.config.implementer.mechanical_model
        return self.config.implementer.default_model

    def _next_iteration(self) -> None:
        if self.cp.iteration >= self.config.limits.max_iterations:
            raise _Stop(RunState.FAILED, f"max iterations ({self.config.limits.max_iterations}) reached")
        self.cp.iteration += 1
        self.store.set_iteration(self.run_id, self.cp.iteration)
        self.cp.denied_actions, self.cp.guard_approved, self.cp.guard_denied = [], [], []
        self.cp.diff_approved, self.cp.report, self.cp.diff_hash = False, "", None
        self._set_phase("implement")

    async def _implement(self) -> None:
        it = self.cp.iteration
        itdir = self.rundir.iteration(it)
        self._transition(RunState.IMPLEMENTING, iteration=it)
        prompt = build_implementer_prompt(
            self.task, iteration=it, milestone=self.cp.outcome.get("milestone"), next_prompt=self.cp.outcome.get("next_prompt"),
            decisions=self.rundir.decisions_text(), previous_check=_check_from(self.cp.previous_check),
            discarded=self.cp.discarded, interrupted=self.cp.interrupted, plan=self.cp.plan, milestone_index=self.cp.milestone_index,
        )
        (itdir / "implementer.prompt.md").write_text(prompt, encoding="utf-8")
        model = self._implementer_model()
        self.rundir.event("implementer_model", iteration=it, model=model, milestone=self.cp.milestone_index + 1)
        result = await self._call("implementer", self.implementer, prompt, itdir / "implementer.stream.jsonl", self.cp.implementer_session,
                                  model=model)
        self.cp.interrupted = False
        if result.session_id:
            self.cp.implementer_session = result.session_id
            self.store.set_sessions(self.run_id, implementer_session=result.session_id)
        self.cp.report = result.text[:20_000]
        self.cp.denied_actions = self._denied_actions(result)
        if result.decision:
            d = result.decision
            self._raise_decision("implementer", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="implementer",
                decision_type=str(d.get("decision_type", "ambiguity")), question=str(d.get("question", "")),
                options=[str(o) for o in d.get("options", [])], recommendation=d.get("recommendation")), payload={})
            return
        self._set_phase("verify")

    def _verify(self) -> None:
        """Stage, scan, apply the diff rules, run the check, commit only the index (SPEC 7 steps 5 to 8)."""
        it = self.cp.iteration
        itdir = self.rundir.iteration(it)
        self._transition(RunState.VERIFYING, iteration=it)
        self.git.stage_all(self.worktree)
        scan = self.scanner.scan_staged(self.worktree, report_path=itdir / "gitleaks.staged.json")
        if not scan.clean:
            hits = ", ".join(f"{f.get('RuleID')} in {f.get('File')}:{f.get('StartLine')}" for f in scan.findings)
            self._raise_decision("secret", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk",
                question=f"gitleaks found secrets: {hits}. Fix them in the worktree, then answer 'rescan', or answer 'abort'.",
                options=["rescan", "abort"], recommendation=0), payload={})
            return
        if not self.cp.diff_approved:
            violations = evaluate_staged(self.git, self.worktree, protected_paths=self.config.git.protected_paths, guard=self.config.guard)
            if violations:
                listing = "\n".join(f"- {v}" for v in violations)
                self.rundir.event("diff_rules", iteration=it, violations=[str(v) for v in violations])
                self._raise_decision("guard_diff", Decision(
                    decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk", destructive=True,
                    question=f"The diff guard flagged these changes:\n{listing}\nAllow them?", options=["approve", "deny"],
                    recommendation=1), payload={"listing": listing})
                return
        if self.cp.denied_actions and not self.git.staged_files(self.worktree):
            # The implementer stopped to ask for a destructive action and changed nothing: nothing to check or review.
            self.rundir.event("review_skipped", iteration=it, reason="no changes, guard decisions pending")
            self._start_guard_decisions()
            return
        check = run_check(self.task.check_command, cwd=self.worktree, timeout=CHECK_TIMEOUT_SECONDS)
        (itdir / "checks.txt").write_text(check.output, encoding="utf-8")
        self.rundir.event("check", iteration=it, ok=check.ok, exit_code=check.exit_code, signature=check.signature)
        self.cp.previous_check = _check_to(check)
        since = self.cp.last_commit or self.git.head(self.worktree)
        summary = (self.cp.report.strip().splitlines() or ["no report"])[0][:60]
        # Only what was staged and scanned before the check command gets committed.
        commit = self.git.commit_staged(self.worktree, f"orq({self.run_id}) iter {it}: {summary}")
        if commit:
            self.cp.last_commit = commit
        self.cp.commits[str(it)] = self.cp.last_commit or ""
        self._save()
        self.rundir.event("commit", iteration=it, sha=commit)
        patch = self.git.diff(self.worktree, since=since)
        (itdir / "diff.patch").write_text(patch, encoding="utf-8")
        self.cp.diff_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
        self._set_phase("review")

    async def _review(self) -> None:
        it = self.cp.iteration
        itdir = self.rundir.iteration(it)
        self._transition(RunState.REVIEWING, iteration=it)
        since = self.cp.commits.get(str(it - 1)) or self.cp.base_commit or self.cp.last_commit
        patch = (itdir / "diff.patch").read_text(encoding="utf-8") if (itdir / "diff.patch").exists() else ""
        check = _check_from(self.cp.previous_check) or CheckResult(ok=False, exit_code=None, output="(no check output)", timed_out=False)
        prompt = build_reviewer_prompt(
            self.task, iteration=it, milestone=self.cp.outcome.get("milestone"), diff_stat=self.git.diff_stat(self.worktree, since=since),
            diff_path=str(itdir / "diff.patch"), diff_excerpt=patch if len(patch) <= DIFF_INLINE_LIMIT else None,
            check=check, implementer_report=self.cp.report, decisions=self.rundir.decisions_text(), discarded=self.cp.discarded,
            plan=self.cp.plan, milestone_index=self.cp.milestone_index,
        )
        (itdir / "reviewer.prompt.md").write_text(prompt, encoding="utf-8")
        effort = self.config.reviewer.routine_effort
        result = await self._call("reviewer", self.reviewer, prompt, itdir / "reviewer.stream.jsonl", None, effort=effort, contract=REVIEW_CONTRACT)
        if result.error_kind == "invalid_output":
            self.rundir.event("reviewer_invalid_output", iteration=it, error=result.error)
            result = await self._call("reviewer", self.reviewer, prompt, itdir / "reviewer.stream.jsonl", None, effort=effort, contract=REVIEW_CONTRACT)
        problems = validate_review(result.structured)
        if problems:
            raise _Stop(RunState.FAILED, "reviewer output invalid: " + "; ".join(problems))
        review: dict = result.structured  # type: ignore[assignment]
        (itdir / "reviewer.output.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
        self.cp.discarded = None
        self.cp.summaries[str(it)] = str(review.get("summary", ""))

        status = review["status"]
        if status == "needs_human":
            h = review.get("human") or {}
            self.cp.outcome = {"next_prompt": "Continue with the owner's answer above.", "milestone": review.get("milestone"), "done": False}
            self._raise_decision("reviewer", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="reviewer",
                decision_type=str(h.get("decision_type", "ambiguity")), question=str(h.get("question", "")),
                options=[str(o) for o in h.get("options", [])], recommendation=h.get("recommendation")), payload={})
            return
        serious = [i for i in review.get("issues", []) if i.get("severity") in ("blocker", "major")]
        if status == "done" and serious:
            # Deterministic rule: a review that lists blocker/major issues is not done, whatever it says.
            self.rundir.event("done_overridden", iteration=it, issues=serious)
            next_prompt = "The reviewer reported these issues; fix them:\n" + "\n".join(f"- [{i['severity']}] {i['description']}" for i in serious)
            done = False
        else:
            next_prompt, done = review.get("next_prompt"), status == "done"
        milestones = (self.cp.plan or {}).get("milestones") or []
        if done and self.cp.milestone_index < len(milestones) - 1:
            # This milestone is done; the task is not. Move on without entering the gate.
            self.rundir.event("milestone_done", iteration=it, milestone=self.cp.milestone_index + 1, of=len(milestones))
            self.cp.milestone_index += 1
            nxt = milestones[self.cp.milestone_index]
            next_prompt = (f"Milestone {self.cp.milestone_index} is done. Start milestone {self.cp.milestone_index + 1}: {nxt['title']}.\n"
                           f"Goal: {nxt['goal']}\nDone when: {nxt['done_when']}")
            done = False
        self.cp.outcome = {"next_prompt": next_prompt, "milestone": review.get("milestone"), "done": done}

        # A clean `done` (milestone advanced or task finished) is progress by definition; only stalls are recorded.
        rule = None if status == "done" and not serious else self.progress.record(
            it, diff_hash=self.cp.diff_hash or "", failure_signature=None if check.ok else check.signature, next_prompt=next_prompt)
        if rule is not None:
            self.rundir.event("no_progress", iteration=it, rule=rule.rule, rollback_to=rule.rollback_to, detail=rule.detail)
            target = f"rollback to iteration {rule.rollback_to}"
            self._raise_decision("progress", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                question=f"No progress ({rule.rule}): {rule.detail}. What now?", options=["continue", target, "abort"],
                recommendation=1), payload={"rollback_to": rule.rollback_to, "target": target})
            return
        self._after_review()

    def _after_review(self) -> None:
        if self.cp.denied_actions:
            self._start_guard_decisions()
        elif self.cp.outcome.get("done"):
            self._set_phase("finalize")
        else:
            self._next_iteration()

    # merge gate (SPEC 10.7): finalize -> gate_ci -> gate_review -> gate_merge

    def _finalize(self) -> None:
        """Push the branch and make sure a PR exists; the gate phases take it from there."""
        self._transition(RunState.FINALIZING, step="push")
        self._scan_range_or_fail()
        self.git.push(self.worktree, self.cp.branch)
        if self.cp.pr_number:
            self.rundir.event("pr", url=self.cp.pr_url, number=self.cp.pr_number, reused=True)
            self._set_phase("gate_ci")
            return
        url = self.git.pr_url(self.worktree, head=self.cp.branch)
        if url is None:
            body = f"Automated by orq run {self.run_id}.\n\n{self.task.goal}\n\nCheck command: `{self.task.check_command}`"
            url = self.git.create_pr(self.worktree, base=self.task.base_branch, head=self.cp.branch, title=self.task.title, body=body)
            self.print(f"[{self.run_id}] PR opened: {url}")
        number = self.git.pr_number(self.worktree, head=self.cp.branch)
        if number is None:
            raise _Stop(RunState.FAILED, f"cannot read the PR number for {self.cp.branch}")
        self.cp.pr_url, self.cp.pr_number = url, number
        self.rundir.event("pr", url=url, number=number)
        self._set_phase("gate_ci")

    def _scan_range_or_fail(self) -> None:
        scan = self.scanner.scan_range(self.worktree, f"origin/{self.task.base_branch}", report_path=self.rundir.path / "gitleaks.push.json")
        if not scan.clean:
            raise _Stop(RunState.FAILED, f"gitleaks found secrets in the branch history: {scan.findings}")

    def _gate_ci(self) -> None:
        """Rebase when the base moved, then wait for GitHub Actions (re-running infrastructure failures)."""
        self._transition(RunState.FINALIZING, step="ci", round=self.cp.gate_rounds + 1)
        if self.git.base_moved(self.worktree, self.task.base_branch):
            if not self.git.rebase_onto_base(self.worktree, self.task.base_branch):
                self._raise_decision("rebase_conflict", Decision(
                    decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                    question=f"Rebasing {self.cp.branch} onto origin/{self.task.base_branch} hit conflicts (rebase aborted). "
                             f"Resolve by hand in {self.worktree}, then answer retry; or abort.",
                    options=["retry", "abort"], recommendation=0), payload={})
                return
            self.cp.last_commit = self.git.head(self.worktree)
            self.git.force_push(self.worktree, self.cp.branch)
            self.rundir.event("rebased", onto=self.task.base_branch, head=self.cp.last_commit)
        status = self.ci.wait(self.worktree, self.cp.pr_number or 0)
        self.cp.ci_reruns += status.reruns
        self.rundir.event("ci", state=status.state, reruns=status.reruns, waited_seconds=round(status.waited_seconds),
                          checks=[{"name": c.get("name"), "bucket": c.get("bucket")} for c in status.checks])
        if status.state == "success":
            self._set_phase("gate_review")
        elif status.state == "failure":
            log = (status.failed_log or "").strip()[-4000:]
            self._gate_round_failed("GitHub Actions failed on the PR. Failed log tail:\n```\n" + log + "\n```\nFix the cause so CI passes.")
        elif status.state == "none":
            self._raise_decision("ci_none", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="risk",
                question=f"No GitHub checks appeared on PR #{self.cp.pr_number} within the grace period. Merge without CI?",
                options=["merge without CI", "abort"], recommendation=1), payload={})
        else:
            self._raise_decision("ci_timeout", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                question=f"GitHub checks on PR #{self.cp.pr_number} did not finish within the timeout. Keep waiting?",
                options=["keep waiting", "abort"], recommendation=0), payload={})

    def _gate_round_failed(self, next_prompt: str) -> None:
        """CI failed or the final review was not done: one more implementer iteration, then the gate restarts."""
        self.cp.gate_rounds += 1
        self.rundir.event("gate_round_failed", round=self.cp.gate_rounds, reason=next_prompt[:200])
        if self.cp.gate_rounds > self.config.merge.max_gate_rounds:
            self._raise_decision("gate_rounds", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                question=f"The merge gate failed {self.cp.gate_rounds} times (limit {self.config.merge.max_gate_rounds}). "
                         f"Last reason: {next_prompt[:300]}. Keep going or abort?",
                options=["keep going", "abort"], recommendation=1), payload={"next_prompt": next_prompt})
            return
        self.cp.outcome = {"next_prompt": next_prompt, "milestone": self.cp.outcome.get("milestone"), "done": False}
        self._next_iteration()

    async def _gate_review(self) -> None:
        """Final reviewer pass at high effort against the whole task (SPEC 10.7 step 2)."""
        self._transition(RunState.FINALIZING, step="final_review", round=self.cp.gate_rounds + 1)
        it = self.cp.iteration
        itdir = self.rundir.iteration(it)
        base = f"origin/{self.task.base_branch}"
        check = _check_from(self.cp.previous_check) or CheckResult(ok=False, exit_code=None, output="(no check output)", timed_out=False)
        ci_note = f"PR #{self.cp.pr_number}: checks passed" if self.cp.ci_reruns == 0 else f"PR #{self.cp.pr_number}: checks passed after {self.cp.ci_reruns} infrastructure re-run(s)"
        prompt = build_reviewer_prompt(
            self.task, iteration=it, milestone=self.cp.outcome.get("milestone"), diff_stat=self.git.diff_stat(self.worktree, since=base),
            diff_path=str(itdir / "diff.patch"), diff_excerpt=None, check=check, implementer_report=self.cp.report,
            decisions=self.rundir.decisions_text(), plan=self.cp.plan, milestone_index=self.cp.milestone_index, final=True, ci_result=ci_note,
        )
        (itdir / "final_review.prompt.md").write_text(prompt, encoding="utf-8")
        effort = self.config.reviewer.final_effort
        result = await self._call("reviewer", self.reviewer, prompt, itdir / "final_review.stream.jsonl", None, effort=effort, contract=REVIEW_CONTRACT)
        if result.error_kind == "invalid_output":
            self.rundir.event("reviewer_invalid_output", iteration=it, error=result.error, final=True)
            result = await self._call("reviewer", self.reviewer, prompt, itdir / "final_review.stream.jsonl", None, effort=effort, contract=REVIEW_CONTRACT)
        problems = validate_review(result.structured)
        if problems:
            raise _Stop(RunState.FAILED, "final reviewer output invalid: " + "; ".join(problems))
        review: dict = result.structured  # type: ignore[assignment]
        (itdir / "final_review.output.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
        self.rundir.event("final_review", status=review["status"], summary=str(review.get("summary", ""))[:300])
        if review["status"] == "needs_human":
            h = review.get("human") or {}
            self.cp.outcome = {"next_prompt": "Continue with the owner's answer above.", "milestone": self.cp.outcome.get("milestone"), "done": False}
            self._raise_decision("reviewer", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="reviewer",
                decision_type=str(h.get("decision_type", "ambiguity")), question=str(h.get("question", "")),
                options=[str(o) for o in h.get("options", [])], recommendation=h.get("recommendation")), payload={})
            return
        serious = [i for i in review.get("issues", []) if i.get("severity") in ("blocker", "major")]
        if review["status"] == "done" and not serious:
            self._set_phase("gate_merge")
            return
        fixes = "\n".join(f"- [{i['severity']}] {i['description']}" for i in serious)
        prompt_text = review.get("next_prompt") or "The final review did not pass."
        if fixes:
            prompt_text += "\nIssues:\n" + fixes
        self._gate_round_failed("Final review before merge: " + prompt_text)

    def _gate_merge(self) -> bool:
        """Preconditions re-checked, then merge, verify, clean up (SPEC 10.7 steps 3 to 5). False: back to gate_ci."""
        self._transition(RunState.FINALIZING, step="merge")
        if self.store.pending_decisions(self.run_id):
            raise _Stop(RunState.FAILED, "pending decisions at merge time")
        if self.git.base_moved(self.worktree, self.task.base_branch):
            self.rundir.event("base_moved_before_merge")
            self._set_phase("gate_ci")
            return False
        self._scan_range_or_fail()
        number = self.cp.pr_number or 0
        self.git.merge_pr(self.worktree, number, strategy=self.config.git.merge_strategy)
        state = self.git.pr_state(self.worktree, number)
        if state != "MERGED":
            raise _Stop(RunState.FAILED, f"gh pr merge returned but PR #{number} is {state}")
        self.rundir.event("pr_merged", number=number, url=self.cp.pr_url, strategy=self.config.git.merge_strategy)
        self.print(f"[{self.run_id}] PR merged: {self.cp.pr_url}")
        if not self.config.git.keep_worktree and self.cp.repo_path:
            self.git.remove_worktree(Path(self.cp.repo_path), self.worktree, branch=self.cp.branch)
            self.rundir.event("worktree_removed", path=str(self.worktree))
        return True

    # agents

    async def _call(self, role: str, agent: Agent, prompt: str, log_path: Path, session_id: str | None, **options: object) -> AgentResult:
        """Run an agent; wait out Claude usage limits; turn any other failure into an owner decision."""
        while True:
            result = await agent.run(prompt, cwd=self.worktree, log_path=log_path, session_id=session_id, run_dir=self.rundir.path, **options)
            self.rundir.event(role, ok=result.ok, error_kind=result.error_kind, session_id=result.session_id,
                              usage=result.usage, rate_limit=result.rate_limit)
            self._persist_router_state()
            if result.ok or result.error_kind == "invalid_output":
                self.cp.rate_limit_retries = 0
                return result
            if result.error_kind == "rate_limit" and self.cp.rate_limit_retries < self.config.limits.rate_limit_retries:
                until = claude_reset_time(result)
                self.cp.rate_limit_retries += 1
                self.cp.rate_limit_until = until.timestamp()
                self.rundir.event("rate_limit", role=role, until=until.isoformat(), attempt=self.cp.rate_limit_retries, error=result.error)
                await self._wait_rate_limit_if_needed()
                self._transition(_PHASE_STATE[self.cp.phase], iteration=self.cp.iteration, after="rate_limit")
                continue
            kind = "rate limit" if result.error_kind == "rate_limit" else result.error_kind
            self._raise_decision("error", Decision(
                decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                question=f"{role} failed ({kind}): {(result.error or '')[:500]}. Retry or abort?", options=["retry", "abort"],
                recommendation=0), payload={"phase": self.cp.phase})
            raise _Yield()

    def _persist_router_state(self) -> None:
        until = getattr(self.reviewer, "fallback_until", None)
        if until == self.cp.reviewer_fallback_until:
            return
        self.cp.reviewer_fallback_until = until
        self._save()
        if until is None:
            self.rundir.event("reviewer_restored", reviewer=getattr(self.reviewer, "name", "reviewer"))
        else:
            self.rundir.event("reviewer_switched", reviewer=getattr(self.reviewer, "name", "reviewer"),
                              until=datetime.fromtimestamp(until, tz=timezone.utc).isoformat())

    def _denied_actions(self, result: AgentResult) -> list[dict]:
        seen: dict[str, dict] = {}
        for denial in result.permission_denials:
            tool = str(denial.get("tool_name", ""))
            tool_input = denial.get("tool_input") or {}
            key = action_key(tool, tool_input)
            seen.setdefault(key, {"key": key, "tool_name": tool, "tool_input": tool_input, "description": describe_action(tool, tool_input)})
        return list(seen.values())

    # decisions

    def _raise_decision(self, kind: str, decision: Decision, payload: dict, state: RunState = RunState.AWAITING_HUMAN) -> None:
        """Record the decision and park the run in the `await` phase. Does not block; `_await` does."""
        self.store.add_decision(decision)
        self.rundir.event("decision", decision_id=decision.decision_id, source=decision.source, decision_type=decision.decision_type,
                          question=decision.question, options=decision.options, kind=kind)
        self.cp.pending_decision = {"decision_id": decision.decision_id, "kind": kind, "payload": payload}
        self.cp.phase = "await"
        self._transition(state, decision_id=decision.decision_id)

    def _await(self) -> None:
        pending = self.cp.pending_decision
        if not pending:
            raise _Stop(RunState.FAILED, "await phase without a pending decision")
        decision = self.store.get_decision(pending["decision_id"])
        if decision is None:
            raise _Stop(RunState.FAILED, f"decision {pending['decision_id']} missing from the store")
        if decision.status == "answered":
            # Answered out of process (`orq answer`); that command already logged the `answer` event.
            self.rundir.event("answer_applied", decision_id=decision.decision_id, answer=decision.answer)
        else:
            if self.human is None:
                self.print(f"[{self.run_id}] waiting for: orq answer {decision.decision_id} ...")
                raise _Stop(RunState.AWAITING_HUMAN, "decision pending")
            raw = self.human(decision).strip()
            answer = decision.options[int(raw)] if raw.isdigit() and decision.options and 0 <= int(raw) < len(decision.options) else raw
            self.store.answer_decision(decision.decision_id, answer=answer, answered_via="cli")
            self.rundir.append_decision(decision.decision_id, decision.question, answer)
            self.rundir.event("answer", decision_id=decision.decision_id, answer=answer, via="terminal")
            decision = self.store.get_decision(decision.decision_id)
        self.cp.pending_decision = None
        self._apply_answer(pending["kind"], pending.get("payload") or {}, decision.answer or "")

    def _apply_answer(self, kind: str, payload: dict, answer: str) -> None:
        a = answer.strip().lower()
        if kind == "planner":
            self._set_phase("plan")  # the answer is in DECISIONS.md; plan again
        elif kind == "plan_approval":
            if a == "approve":
                self._next_iteration()
            else:
                self.cp.plan_feedback = answer if a != "revise" else "Revise the plan."
                self._set_phase("plan")
        elif kind in ("implementer", "reviewer"):
            # The question ended the iteration; the answer sits in DECISIONS.md for both sides.
            self.cp.outcome = {"next_prompt": "Continue with the owner's answer above.", "milestone": self.cp.outcome.get("milestone"), "done": False}
            if self.cp.denied_actions:
                # Guard denials from the same turn still need their own approve/deny (seen in the Phase 2 sandbox run).
                self._start_guard_decisions()
            else:
                self._next_iteration()
        elif kind == "guard_pre":
            action = payload["action"]
            if a == "approve":
                (self.paths.allow_tokens(self.run_id) / action["key"]).write_text("approved", encoding="utf-8")
                self.rundir.event("guard_token", action_key=action["key"], description=action["description"])
                self.cp.guard_approved.append(action["description"])
            else:
                self.cp.guard_denied.append(action["description"])
            self.cp.denied_actions = payload.get("remaining", [])
            if self.cp.denied_actions:
                self._start_guard_decisions()
                return
            block = guard_outcome_lines(self.cp.guard_approved, self.cp.guard_denied)
            base = self.cp.outcome.get("next_prompt")
            self.cp.outcome = {"next_prompt": f"{base}\n\n{block}" if base else block, "milestone": self.cp.outcome.get("milestone"), "done": False}
            self._next_iteration()
        elif kind == "guard_diff":
            if a == "approve":
                self.cp.diff_approved = True
                self._set_phase("verify")
            else:
                self.git.reset_hard(self.worktree, self.cp.last_commit or self.cp.base_commit or "HEAD")
                self.rundir.event("rollback_iteration", iteration=self.cp.iteration, to=self.cp.last_commit)
                text = ("The owner rejected these changes:\n" + payload.get("listing", "") +
                        "\nThe worktree was reset to the previous iteration. Proceed without them.")
                self.cp.outcome = {"next_prompt": text, "milestone": self.cp.outcome.get("milestone"), "done": False}
                if self.cp.denied_actions:
                    self._start_guard_decisions()
                else:
                    self._next_iteration()
        elif kind == "secret":
            if a in ("abort", "1"):
                raise _Stop(RunState.ABORTED, "secret found, owner aborted")
            self._set_phase("verify")
        elif kind == "progress":
            if a == "abort":
                raise _Stop(RunState.ABORTED, "owner aborted after no progress")
            if a == str(payload.get("target", "")).lower():
                text = self.rollback_to(int(payload["rollback_to"]), upto=self.cp.iteration)
                self.cp.outcome = {"next_prompt": text, "milestone": self.cp.outcome.get("milestone"), "done": False}
            else:
                self.progress.reset()
            self._after_review()
        elif kind == "error":
            if a == "abort":
                raise _Stop(RunState.ABORTED, "owner aborted after an agent error")
            self.cp.rate_limit_retries = 0
            self._set_phase(payload.get("phase", self.cp.phase))
        elif kind in ("rebase_conflict", "ci_timeout"):
            if a == "abort":
                raise _Stop(RunState.ABORTED, f"owner aborted at the merge gate ({kind})")
            self._set_phase("gate_ci")
        elif kind == "ci_none":
            if a == "abort":
                raise _Stop(RunState.ABORTED, "owner aborted: no CI on the repo")
            self.rundir.event("ci_skipped", by="owner")
            self._set_phase("gate_review")
        elif kind == "gate_rounds":
            if a == "abort":
                raise _Stop(RunState.ABORTED, "owner aborted after repeated merge gate failures")
            self.cp.gate_rounds = 0
            self.cp.outcome = {"next_prompt": payload.get("next_prompt"), "milestone": self.cp.outcome.get("milestone"), "done": False}
            self._next_iteration()
        else:
            raise _Stop(RunState.FAILED, f"unknown decision kind {kind}")

    def _start_guard_decisions(self) -> None:
        action, *remaining = self.cp.denied_actions
        self._raise_decision("guard_pre", Decision(
            decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk", destructive=True,
            question=f"The implementer tried a destructive action: {action['description']}. Allow it once?",
            options=["approve", "deny"], recommendation=1), payload={"action": action, "remaining": remaining})

    # rollback

    def rollback_to(self, target: int, *, upto: int) -> str:
        """Reset the worktree to the commit of iteration `target` (0 = base) and describe what was discarded."""
        sha = self.cp.commits.get(str(target)) if target > 0 else self.cp.base_commit
        sha = sha or self.cp.base_commit or "HEAD"
        self.git.reset_hard(self.worktree, sha)
        self.cp.last_commit = sha
        lines = [f"- iteration {i}: {self.cp.summaries.get(str(i), '(no review)')}" for i in range(target + 1, upto + 1)]
        for i in range(target + 1, upto + 1):
            self.cp.commits.pop(str(i), None)
            self.cp.summaries.pop(str(i), None)
            self.rundir.archive_iteration(i)
        self.progress.discard_after(target)
        # Discarded iterations do not count toward max_iterations; the owner approved every rollback.
        self.cp.iteration = target
        self.cp.discarded = f"Iterations {target + 1}..{upto} were discarded by the owner:\n" + "\n".join(lines)
        self.rundir.event("rollback", to_iteration=target, sha=sha, discarded=list(range(target + 1, upto + 1)))
        self._save()
        return f"The owner rolled the worktree back to iteration {target}. Start again from there.\n" + self.cp.discarded

    def rollback_cli(self, target: int) -> None:
        """`orq rollback`: only for a run with no live process. Leaves the run ready for `orq resume`."""
        if not 0 <= target < self.cp.iteration:
            raise ValueError(f"--to must be between 0 and {self.cp.iteration - 1} for run {self.run_id}")
        text = self.rollback_to(target, upto=self.cp.iteration)
        if self.cp.pending_decision:
            self.rundir.event("decision_dropped", decision_id=self.cp.pending_decision["decision_id"], reason="rollback")
        self.cp.pending_decision = None
        self.cp.denied_actions = []
        self.cp.outcome = {"next_prompt": text, "milestone": None, "done": False}
        self.cp.state = RunState.PAUSED.value
        self.store.set_state(self.run_id, RunState.PAUSED)
        self._next_iteration()


def _check_to(check: CheckResult) -> dict:
    return {"ok": check.ok, "exit_code": check.exit_code, "output": check.output[-8000:], "timed_out": check.timed_out}


def _check_from(data: dict | None) -> CheckResult | None:
    if not data:
        return None
    return CheckResult(ok=data["ok"], exit_code=data["exit_code"], output=data["output"], timed_out=data["timed_out"])


def implementer_system_prompt() -> str:
    return IMPLEMENTER_RULES
