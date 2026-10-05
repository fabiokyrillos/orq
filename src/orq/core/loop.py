"""The run state machine and iteration loop (SPEC sections 6 and 7), Phase 1 subset.

Not here yet: guard hook and diff rules (Phase 2), rate-limit fallback (Phase 2), planning and merge gate (Phase 3).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orq.adapters.base import Agent, AgentResult
from orq.adapters.schema import validate_review
from orq.config import Config
from orq.core.models import Decision, RunRecord, RunState, new_decision_id, new_run_id
from orq.core.prompts import IMPLEMENTER_RULES, build_implementer_prompt, build_reviewer_prompt
from orq.core.task import Task
from orq.git.manager import GitManager
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir
from orq.verify.checks import CheckResult, run_check
from orq.verify.secrets import SecretScanner

HumanInput = Callable[[Decision], str]
Printer = Callable[[str], None]

DIFF_INLINE_LIMIT = 20_000
CHECK_TIMEOUT_SECONDS = 1800


class SandboxError(RuntimeError):
    pass


class _Stop(Exception):
    def __init__(self, state: RunState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


@dataclass
class _IterationOutcome:
    next_prompt: str | None
    milestone: str | None
    done: bool


class Runner:
    def __init__(self, *, config: Config, paths: OrqPaths, store: Store, task: Task, task_text: str,
                 git: GitManager, implementer: Agent, reviewer: Agent, scanner: SecretScanner, human: HumanInput,
                 clone_url: str | None = None, printer: Printer = print, run_id: str | None = None) -> None:
        if task.repo not in config.git.sandbox_repos:
            raise SandboxError(
                f"{task.repo} is not listed in [git].sandbox_repos; until the guard exists (Phase 2) orq only runs on sandbox repos"
            )
        self.config, self.paths, self.store, self.task = config, paths, store, task
        self.git, self.implementer, self.reviewer, self.scanner = git, implementer, reviewer, scanner
        self.human, self.clone_url, self.print = human, clone_url, printer
        self.run_id = run_id or new_run_id()
        self.branch = f"orq/{task.slug}"
        self.worktree: Path = config.git.worktree_root / task.repo.split("/")[-1] / self.run_id
        self.rundir = RunDir(paths.run_dir(self.run_id))
        self.rundir.create(task_text)
        self.store.create_run(RunRecord(run_id=self.run_id, repo=task.repo, task_title=task.title,
                                        branch=self.branch, worktree=str(self.worktree)))
        self._started = time.monotonic()
        self._last_commit: str | None = None
        self._implementer_session: str | None = None

    # state handling

    def _transition(self, state: RunState, **data: object) -> None:
        self.rundir.event("state", state=state.value, **data)
        self.store.set_state(self.run_id, state)
        self.rundir.write_state({"state": state.value, "iteration": self.store.get_run(self.run_id).iteration,
                                 "last_commit": self._last_commit, "implementer_session": self._implementer_session})
        self.print(f"[{self.run_id}] {state.value}" + (f" {data}" if data else ""))

    # entry point

    async def execute(self) -> RunState:
        try:
            self._setup()
            outcome = _IterationOutcome(next_prompt=None, milestone=None, done=False)
            previous_check: CheckResult | None = None
            for iteration in range(1, self.config.limits.max_iterations + 1):
                self._check_wall_time()
                self.store.set_iteration(self.run_id, iteration)
                outcome, previous_check = await self._iterate(iteration, outcome, previous_check)
                if outcome.done:
                    self._finalize()
                    self._transition(RunState.DONE)
                    return RunState.DONE
            raise _Stop(RunState.FAILED, f"max iterations ({self.config.limits.max_iterations}) reached")
        except _Stop as stop:
            self._transition(stop.state, reason=stop.reason)
            return stop.state

    def _setup(self) -> None:
        self._transition(RunState.QUEUED)
        repo_path = self.git.ensure_repo(self.task.repo, self.paths.repos, clone_url=self.clone_url)
        self.git.create_worktree(repo_path, self.worktree, branch=self.branch, base=self.task.base_branch)
        self._last_commit = self.git.head(self.worktree)
        self.rundir.event("worktree", path=str(self.worktree), branch=self.branch, base_commit=self._last_commit)

    def _check_wall_time(self) -> None:
        elapsed_hours = (time.monotonic() - self._started) / 3600
        if elapsed_hours > self.config.limits.max_wall_hours:
            raise _Stop(RunState.FAILED, f"max wall time ({self.config.limits.max_wall_hours} h) exceeded")

    # one iteration (SPEC section 7)

    async def _iterate(self, iteration: int, previous: _IterationOutcome, previous_check: CheckResult | None):
        itdir = self.rundir.iteration(iteration)
        self._transition(RunState.IMPLEMENTING, iteration=iteration)

        prompt = build_implementer_prompt(self.task, iteration=iteration, milestone=previous.milestone,
                                          next_prompt=previous.next_prompt, decisions=self.rundir.decisions_text(),
                                          previous_check=previous_check)
        (itdir / "implementer.prompt.md").write_text(prompt, encoding="utf-8")
        result = await self.implementer.run(prompt, cwd=self.worktree, log_path=itdir / "implementer.stream.jsonl",
                                            session_id=self._implementer_session, run_dir=self.rundir.path)
        self._record_agent("implementer", result)
        if result.session_id:
            self._implementer_session = result.session_id
            self.store.set_sessions(self.run_id, implementer_session=result.session_id)
        self._fail_on_error(result, "implementer")

        if result.decision:
            self._ask(Decision(decision_id=new_decision_id(), run_id=self.run_id, source="implementer",
                               decision_type=str(result.decision.get("decision_type", "ambiguity")),
                               question=str(result.decision.get("question", "")),
                               options=[str(o) for o in result.decision.get("options", [])],
                               recommendation=result.decision.get("recommendation")))
            return _IterationOutcome(next_prompt="Continue with the owner's answer above.", milestone=previous.milestone, done=False), previous_check

        self._transition(RunState.VERIFYING, iteration=iteration)
        self._scan_staged(itdir)
        check = run_check(self.task.check_command, cwd=self.worktree, timeout=CHECK_TIMEOUT_SECONDS)
        (itdir / "checks.txt").write_text(check.output, encoding="utf-8")
        self.rundir.event("check", iteration=iteration, ok=check.ok, exit_code=check.exit_code, signature=check.signature)

        summary = (result.text.strip().splitlines() or ["no report"])[0][:60]
        since = self._last_commit or self.git.head(self.worktree)
        commit = self.git.commit_all(self.worktree, f"orq({self.run_id}) iter {iteration}: {summary}")
        if commit:
            self._last_commit = commit
        self.rundir.event("commit", iteration=iteration, sha=commit)
        patch = self.git.diff(self.worktree, since=since)
        stat = self.git.diff_stat(self.worktree, since=since)
        (itdir / "diff.patch").write_text(patch, encoding="utf-8")

        self._transition(RunState.REVIEWING, iteration=iteration)
        review = await self._review(iteration, itdir, stat, patch, check, result.text)

        status = review["status"]
        if status == "needs_human":
            human = review.get("human") or {}
            self._ask(Decision(decision_id=new_decision_id(), run_id=self.run_id, source="reviewer",
                               decision_type=str(human.get("decision_type", "ambiguity")), question=str(human.get("question", "")),
                               options=[str(o) for o in human.get("options", [])], recommendation=human.get("recommendation")))
            return _IterationOutcome(next_prompt="Continue with the owner's answer above.", milestone=review.get("milestone"), done=False), check
        return _IterationOutcome(next_prompt=review.get("next_prompt"), milestone=review.get("milestone"), done=status == "done"), check

    async def _review(self, iteration: int, itdir: Path, stat: str, patch: str, check: CheckResult, report: str) -> dict:
        prompt = build_reviewer_prompt(self.task, iteration=iteration, milestone=None, diff_stat=stat,
                                       diff_path=str(itdir / "diff.patch"),
                                       diff_excerpt=patch if len(patch) <= DIFF_INLINE_LIMIT else None,
                                       check=check, implementer_report=report, decisions=self.rundir.decisions_text())
        (itdir / "reviewer.prompt.md").write_text(prompt, encoding="utf-8")
        result = await self.reviewer.run(prompt, cwd=self.worktree, log_path=itdir / "reviewer.stream.jsonl", run_dir=self.rundir.path)
        if result.error_kind == "invalid_output":
            self.rundir.event("reviewer_invalid_output", iteration=iteration, error=result.error)
            result = await self.reviewer.run(prompt, cwd=self.worktree, log_path=itdir / "reviewer.stream.jsonl", run_dir=self.rundir.path)
        self._record_agent("reviewer", result)
        self._fail_on_error(result, "reviewer")
        problems = validate_review(result.structured)
        if problems:
            raise _Stop(RunState.FAILED, "reviewer output invalid: " + "; ".join(problems))
        (itdir / "reviewer.output.json").write_text(json.dumps(result.structured, indent=2), encoding="utf-8")
        return result.structured  # type: ignore[return-value]

    # helpers

    def _record_agent(self, role: str, result: AgentResult) -> None:
        self.rundir.event(role, ok=result.ok, error_kind=result.error_kind, session_id=result.session_id,
                          usage=result.usage, rate_limit=result.rate_limit)

    def _fail_on_error(self, result: AgentResult, role: str) -> None:
        if result.ok:
            return
        if result.error_kind == "rate_limit":
            raise _Stop(RunState.PAUSED_RATE_LIMIT, f"{role}: {result.error}")
        raise _Stop(RunState.FAILED, f"{role} {result.error_kind}: {result.error}")

    def _scan_staged(self, itdir: Path) -> None:
        self.git.stage_all(self.worktree)
        scan = self.scanner.scan_staged(self.worktree, report_path=itdir / "gitleaks.staged.json")
        if scan.clean:
            return
        hits = ", ".join(f"{f.get('RuleID')} in {f.get('File')}:{f.get('StartLine')}" for f in scan.findings)
        answer = self._ask(Decision(decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk",
                                    question=f"gitleaks found secrets: {hits}. Fix them in the worktree, then answer 'rescan', or answer 'abort'.",
                                    options=["rescan", "abort"], recommendation=0))
        if answer.strip().lower() in ("abort", "1"):
            raise _Stop(RunState.ABORTED, "secret found, owner aborted")
        self._scan_staged(itdir)

    def _ask(self, decision: Decision) -> str:
        self.store.add_decision(decision)
        self.rundir.event("decision", decision_id=decision.decision_id, source=decision.source,
                          decision_type=decision.decision_type, question=decision.question, options=decision.options)
        self._transition(RunState.AWAITING_HUMAN, decision_id=decision.decision_id)
        raw = self.human(decision)
        answer = raw.strip()
        if answer.isdigit() and decision.options and 0 <= int(answer) < len(decision.options):
            answer = decision.options[int(answer)]
        self.store.answer_decision(decision.decision_id, answer=answer, answered_via="cli")
        self.rundir.append_decision(decision.decision_id, decision.question, answer)
        self.rundir.event("answer", decision_id=decision.decision_id, answer=answer)
        return answer

    def _finalize(self) -> None:
        self._transition(RunState.FINALIZING)
        scan = self.scanner.scan_range(self.worktree, f"origin/{self.task.base_branch}",
                                       report_path=self.rundir.path / "gitleaks.push.json")
        if not scan.clean:
            raise _Stop(RunState.FAILED, f"gitleaks found secrets in the branch history: {scan.findings}")
        self.git.push(self.worktree, self.branch)
        body = f"Automated by orq run {self.run_id}.\n\n{self.task.goal}\n\nCheck command: `{self.task.check_command}`"
        url = self.git.create_pr(self.worktree, base=self.task.base_branch, head=self.branch, title=self.task.title, body=body)
        self.rundir.event("pr", url=url)
        self.print(f"[{self.run_id}] PR opened: {url}")


def implementer_system_prompt() -> str:
    return IMPLEMENTER_RULES
