"""GitHub Actions status for the merge gate (SPEC 10.7), read through `gh`.

Phase 0 facts baked in: `gh pr checks` exits 1 with "no checks reported" for about a minute after the PR
is created; free runners can sit in QUEUED for many minutes; a job no runner picked up ends as a failure
with zero steps and should be re-run, not treated as a broken build.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from orq.git.manager import GhRunner, GitError

_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
_NO_CHECKS_RE = re.compile(r"no checks reported", re.IGNORECASE)
_NO_RUNNER_RE = re.compile(r"not acquired by Runner", re.IGNORECASE)
PASSING_BUCKETS = {"pass", "skipping"}
FAILING_BUCKETS = {"fail", "cancel"}
LOG_TAIL = 4000


@dataclass
class CiStatus:
    state: str  # success | failure | none | timeout
    checks: list[dict] = field(default_factory=list)
    failed_log: str | None = None
    reruns: int = 0
    waited_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state == "success"


class CiWatcher:
    def __init__(self, gh: GhRunner, *, poll_seconds: float, timeout_minutes: float, grace_minutes: float, max_reruns: int,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self._gh = gh
        self.poll_seconds, self.timeout_seconds, self.grace_seconds = poll_seconds, timeout_minutes * 60, grace_minutes * 60
        self.max_reruns = max_reruns
        self._sleep, self._clock = sleep, clock

    def wait(self, worktree: Path, pr_number: int) -> CiStatus:
        started = self._clock()
        reruns = 0
        rerun_ids: set[str] = set()
        while True:
            elapsed = self._clock() - started
            checks = self._checks(worktree, pr_number)
            if checks is None:
                if elapsed > self.grace_seconds:
                    return CiStatus("none", waited_seconds=elapsed)
            elif all(c.get("bucket") in PASSING_BUCKETS for c in checks):
                return CiStatus("success", checks=checks, reruns=reruns, waited_seconds=elapsed)
            else:
                failing = [c for c in checks if c.get("bucket") in FAILING_BUCKETS]
                if failing:
                    outcome = self._handle_failures(worktree, failing, reruns, rerun_ids)
                    if isinstance(outcome, CiStatus):
                        outcome.checks, outcome.reruns, outcome.waited_seconds = checks, reruns, elapsed
                        return outcome
                    reruns = outcome
            if elapsed + self.poll_seconds > self.timeout_seconds:
                return CiStatus("timeout", checks=checks or [], reruns=reruns, waited_seconds=elapsed)
            self._sleep(self.poll_seconds)

    def _checks(self, worktree: Path, pr_number: int) -> list[dict] | None:
        try:
            out = self._gh(["pr", "checks", str(pr_number), "--json", "name,state,bucket,link"], worktree)
        except GitError as exc:
            if _NO_CHECKS_RE.search(str(exc)):
                return None
            raise
        data = json.loads(out or "[]")
        return data if isinstance(data, list) else None

    def _handle_failures(self, worktree: Path, failing: list[dict], reruns: int, rerun_ids: set[str]) -> CiStatus | int:
        """Re-run infrastructure failures (bounded); return a failure status for real ones, else the new rerun count."""
        for check in failing:
            match = _RUN_ID_RE.search(str(check.get("link", "")))
            run_id = match.group(1) if match else None
            if run_id and run_id not in rerun_ids and reruns < self.max_reruns and self._is_infra_failure(worktree, run_id):
                self._gh(["run", "rerun", run_id], worktree)
                rerun_ids.add(run_id)
                reruns += 1
                continue
            if run_id and run_id in rerun_ids and reruns < self.max_reruns and self._is_infra_failure(worktree, run_id):
                # The re-run is itself still showing the old failure; wait for the new attempt.
                continue
            return CiStatus("failure", failed_log=self._failed_log(worktree, run_id))
        return reruns

    def _is_infra_failure(self, worktree: Path, run_id: str) -> bool:
        try:
            out = self._gh(["run", "view", run_id, "--json", "conclusion,jobs"], worktree)
            data = json.loads(out or "{}")
        except (GitError, json.JSONDecodeError):
            return False
        jobs = data.get("jobs") or []
        if not jobs:
            return True
        return all(not job.get("steps") for job in jobs) or _NO_RUNNER_RE.search(out) is not None

    def _failed_log(self, worktree: Path, run_id: str | None) -> str:
        if not run_id:
            return "(no run id in the check link)"
        try:
            return self._gh(["run", "view", run_id, "--log-failed"], worktree)[-LOG_TAIL:]
        except GitError as exc:
            return f"(log unavailable: {exc})"
