"""Resume from each phase with fakes. The 'crash' is a fake that raises after the runner wrote its checkpoint."""

import asyncio
import json
from pathlib import Path

import pytest

from orq.core.checkpoint import Checkpoint
from orq.core.loop import ResumeError
from orq.core.models import RunState
from orq.verify.ci import CiStatus
from orq.paths import OrqPaths
from tests.conftest import git
from tests.test_loop import FakeCi, FakeImplementer, FakePlanner, FakeReviewer, denied, env, ok, plan, review  # noqa: F401 - env fixture

pytestmark = pytest.mark.usefixtures("origin")


class Crash(RuntimeError):
    pass


class CrashingImplementer(FakeImplementer):
    """Writes partial work then raises, like a killed process would leave it."""

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        if self.calls == 0:
            self.calls += 1
            self.prompts.append(prompt)
            (cwd / "partial.txt").write_text("half\n", encoding="utf-8")
            raise Crash("killed")
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event, **kwargs)


class CrashingReviewer(FakeReviewer):
    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        if not self.prompts:
            self.prompts.append(prompt)
            raise Crash("killed")
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event, **kwargs)


def state_file(paths: OrqPaths, run_id: str) -> Path:
    return paths.run_dir(run_id) / "state.json"


def events(paths: OrqPaths, run_id: str) -> list[dict]:
    return [json.loads(l) for l in (paths.run_dir(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_crash_during_implement_resumes_same_iteration_with_partial_work(env) -> None:
    make, paths, _ = env
    implementer = CrashingImplementer([ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer)
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    cp = Checkpoint.load(state_file(paths, runner.run_id))
    assert cp.phase == "implement" and cp.iteration == 1 and cp.state == "IMPLEMENTING"

    resumed = make(implementer, reviewer, resume=runner.run_id)
    final = asyncio.run(resumed.execute())

    assert final is RunState.DONE
    assert "interrupted" in implementer.prompts[1].lower()
    assert (resumed.worktree / "partial.txt").exists()
    assert Checkpoint.load(state_file(paths, runner.run_id)).phase == "done"
    assert any(e["type"] == "resume" and e["phase"] == "implement" for e in events(paths, runner.run_id))


def test_crash_during_review_reruns_only_the_review(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([ok()])
    reviewer = CrashingReviewer([review("done", None)])
    runner = make(implementer, reviewer)
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    assert Checkpoint.load(state_file(paths, runner.run_id)).phase == "review"

    resumed = make(implementer, reviewer, resume=runner.run_id)

    assert asyncio.run(resumed.execute()) is RunState.DONE
    assert implementer.calls == 1 and len(reviewer.prompts) == 3  # crashed review, its re-run, the final review
    log = git("log", "--format=%s", cwd=resumed.worktree).splitlines()
    assert len([l for l in log if l.startswith("orq(")]) == 1


def test_pending_decision_answered_in_store_is_applied_on_resume(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([denied("git reset --hard"), ok()])
    reviewer = FakeReviewer([review("continue", "go"), review("done", None)])
    runner = make(implementer, reviewer, human=None)  # headless: a decision stops the process
    final = asyncio.run(runner.execute())
    assert final is RunState.AWAITING_HUMAN
    cp = Checkpoint.load(state_file(paths, runner.run_id))
    assert cp.phase == "await" and cp.pending_decision["kind"] == "guard_pre"

    runner.store.answer_decision(cp.pending_decision["decision_id"], answer="approve", answered_via="cli")
    resumed = make(implementer, reviewer, human=None, resume=runner.run_id)
    final = asyncio.run(resumed.execute())

    assert final is RunState.DONE
    assert "approved this action" in implementer.prompts[1]


def test_unanswered_decision_stays_awaiting(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([denied("git reset --hard"), ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), human=None)
    asyncio.run(runner.execute())

    resumed = make(implementer, FakeReviewer([]), human=None, resume=runner.run_id)

    assert asyncio.run(resumed.execute()) is RunState.AWAITING_HUMAN
    assert len(implementer.prompts) == 1
    assert len(runner.store.pending_decisions(runner.run_id)) == 1


def test_resume_refuses_when_head_moved(env) -> None:
    make, paths, _ = env
    implementer = CrashingImplementer([ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]))
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    git("commit", "-q", "--allow-empty", "-m", "someone else", cwd=runner.worktree)

    with pytest.raises(ResumeError, match="HEAD"):
        make(implementer, FakeReviewer([]), resume=runner.run_id)


def test_resume_accepts_iteration_commit_made_right_before_the_crash(env) -> None:
    make, paths, _ = env
    implementer = CrashingImplementer([ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]))
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    git("add", "-A", cwd=runner.worktree)
    git("commit", "-q", "-m", f"orq({runner.run_id}) iter 1: partial", cwd=runner.worktree)

    resumed = make(implementer, FakeReviewer([review("done", None)]), resume=runner.run_id)

    assert resumed.cp.last_commit == git("rev-parse", "HEAD", cwd=runner.worktree).strip()


def test_resume_refuses_done_run(env) -> None:
    make, paths, _ = env
    runner = make(FakeImplementer([ok()]), FakeReviewer([review("done", None)]))
    asyncio.run(runner.execute())
    with pytest.raises(ResumeError, match="DONE"):
        make(FakeImplementer([]), FakeReviewer([]), resume=runner.run_id)


def test_resume_refuses_unknown_run(env) -> None:
    make, paths, _ = env
    with pytest.raises(ResumeError, match="state.json"):
        make(FakeImplementer([]), FakeReviewer([]), resume="RNOPE1")


def test_pause_flag_stops_between_phases_and_resume_continues(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("continue", "go"), review("done", None)])
    runner = make(implementer, reviewer)
    (runner.rundir.path / "pause.requested").write_text("", encoding="utf-8")

    assert asyncio.run(runner.execute()) is RunState.PAUSED
    assert implementer.calls == 0

    resumed = make(implementer, reviewer, resume=runner.run_id)
    assert asyncio.run(resumed.execute()) is RunState.DONE
    assert not (runner.rundir.path / "pause.requested").exists()


def test_rollback_cli_resets_and_prepares_resume(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([ok(), ok(), ok()])
    reviewer = FakeReviewer([review("continue", "a"), review("continue", "b"), review("continue", "c")])
    runner = make(implementer, reviewer, human=None)
    assert asyncio.run(runner.execute()) is RunState.FAILED  # max_iterations = 3 in the fixture
    runner.cp.state = RunState.PAUSED.value  # FAILED runs cannot be resumed; pretend the owner paused it
    runner.cp.save(state_file(paths, runner.run_id))

    stopped = make(FakeImplementer([ok()]), FakeReviewer([review("done", None)]), resume=runner.run_id)
    stopped.rollback_cli(1)

    cp = Checkpoint.load(state_file(paths, runner.run_id))
    assert cp.iteration == 2 and cp.phase == "implement" and cp.last_commit == cp.commits["1"]
    assert "2" not in cp.commits and "rolled the worktree back to iteration 1" in cp.outcome["next_prompt"]
    assert not (Path(cp.worktree) / "greeting3.txt").exists() and (Path(cp.worktree) / "greeting1.txt").exists()
    with pytest.raises(ValueError, match="--to"):
        stopped.rollback_cli(5)


class CrashingPlanner(FakePlanner):
    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        if not self.prompts:
            self.prompts.append(prompt)
            raise Crash("killed")
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event, **kwargs)


def test_crash_during_planning_resumes_the_plan_phase(env) -> None:
    make, paths, _ = env
    planner = CrashingPlanner([plan(("a", "hard"))])
    implementer = FakeImplementer([ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), planner=planner)
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    assert Checkpoint.load(state_file(paths, runner.run_id)).phase == "plan"

    resumed = make(implementer, FakeReviewer([review("done", None)]), planner=planner, resume=runner.run_id)

    assert asyncio.run(resumed.execute()) is RunState.DONE
    assert len(planner.prompts) == 2 and Checkpoint.load(state_file(paths, runner.run_id)).plan["milestones"][0]["title"] == "a"


class CrashingCi(FakeCi):
    def wait(self, worktree, pr_number):
        if self.calls == 0:
            self.calls += 1
            raise Crash("killed")
        return super().wait(worktree, pr_number)


def test_crash_during_ci_wait_resumes_the_gate(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([ok()])
    reviewer = FakeReviewer([review("done", None), review("done", None)])
    runner = make(implementer, reviewer, ci=CrashingCi())
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    cp = Checkpoint.load(state_file(paths, runner.run_id))
    assert cp.phase == "gate_ci" and cp.pr_number == 7

    resumed = make(implementer, reviewer, ci=FakeCi(), resume=runner.run_id)

    assert asyncio.run(resumed.execute()) is RunState.DONE
    assert implementer.calls == 1 and len(reviewer.prompts) == 2
    assert [e["type"] for e in events(paths, runner.run_id)].count("pr") == 1  # the PR was not recreated


def test_crash_during_final_review_resumes_it(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([ok()])

    class FinalCrashReviewer(FakeReviewer):
        """The milestone review answers; the first final review call crashes."""

        async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
            if len(self.prompts) == 1:
                self.prompts.append(prompt)
                raise Crash("killed")
            return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event, **kwargs)

    reviewer = FinalCrashReviewer([review("done", None), review("done", None)])
    runner = make(implementer, reviewer)
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    assert Checkpoint.load(state_file(paths, runner.run_id)).phase == "gate_review"

    resumed = make(implementer, reviewer, resume=runner.run_id)
    assert asyncio.run(resumed.execute()) is RunState.DONE
