import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from orq.adapters.base import AgentResult
from orq.config import Config
from orq.core.loop import Runner, SandboxError
from orq.core.models import Decision, RunState
from orq.core.task import parse_task
from orq.git.manager import GitError, GitManager
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.verify.ci import CiStatus
from orq.verify.secrets import SecretScanner
from tests.conftest import git

TASK = """# Task: Add greeting
## Repo
owner/sandbox, base branch main
## Goal
Add greeting.txt with hello.
## Acceptance criteria
- [ ] greeting.txt exists
## Check command
python -c "print('checks ok')"
## Plan approval
skip
"""


@dataclass
class FakeImplementer:
    """Writes a file per call so every iteration has a diff; returns scripted results."""

    results: list[AgentResult]
    name: str = "fake-implementer"
    prompts: list[str] = field(default_factory=list)
    calls: int = 0
    models: list = field(default_factory=list)

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        self.prompts.append(prompt)
        self.models.append(kwargs.get("model"))
        self.calls += 1
        (cwd / f"greeting{self.calls}.txt").write_text("hello\n", encoding="utf-8")
        log_path.write_text('{"type":"result"}\n', encoding="utf-8")
        result = self.results.pop(0)
        if result.session_id is None:
            result.session_id = session_id or "impl-session"
        return result


@dataclass
class FakeReviewer:
    outputs: list[AgentResult]
    name: str = "fake-reviewer"
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        self.prompts.append(prompt)
        log_path.write_text("{}\n", encoding="utf-8")
        # Out of scripted answers: the final review at the merge gate passes by default.
        return self.outputs.pop(0) if self.outputs else review("done", None)


def plan(*milestones: tuple[str, str], status: str = "plan", human: dict | None = None) -> AgentResult:
    """FakePlanner result: milestones are (title, difficulty) pairs."""
    items = [{"title": t, "goal": f"goal of {t}", "done_when": f"{t} is in place", "difficulty": d} for t, d in milestones]
    return AgentResult(ok=True, structured={"status": status, "summary": "the plan", "milestones": items, "human": human}, session_id="p1")


@dataclass
class FakePlanner:
    outputs: list[AgentResult]
    name: str = "fake-planner"
    prompts: list[str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        self.prompts.append(prompt)
        self.calls.append(kwargs)
        log_path.write_text("{}\n", encoding="utf-8")
        return self.outputs.pop(0)


def review(status: str, next_prompt: str | None = "keep going", human: dict | None = None) -> AgentResult:
    return AgentResult(ok=True, structured={"status": status, "summary": f"review says {status}", "milestone": "m1",
                                            "next_prompt": next_prompt, "issues": [], "human": human}, session_id="t1")


def ok(text: str = "I changed things") -> AgentResult:
    return AgentResult(ok=True, text=text)


def clean_scanner() -> SecretScanner:
    def runner(args, cwd):
        Path(args[args.index("--report-path") + 1]).write_text("[]", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")
    return SecretScanner(runner=runner)


class FakeCi:
    """Scripted CI outcomes; the last one repeats."""

    def __init__(self, statuses: list[CiStatus] | None = None) -> None:
        self.statuses = list(statuses or [CiStatus("success")])
        self.calls = 0

    def wait(self, worktree, pr_number):
        self.calls += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


@pytest.fixture
def env(tmp_path: Path, origin: Path):
    paths = OrqPaths(tmp_path / "orq-home")
    config = Config()
    config.git.worktree_root = tmp_path / "wt"
    config.git.sandbox_repos = ["owner/sandbox"]
    config.git.keep_worktree = True  # most tests inspect the worktree after DONE
    config.limits.max_iterations = 3
    pr_calls: list[list[str]] = []

    def fake_gh(args, cwd):
        pr_calls.append(args)
        if args[:2] == ["pr", "view"] and "number" in args[-3]:
            return "7\n"
        if args[:2] == ["pr", "view"] and "state" in args[-3]:
            return "MERGED\n"
        if args[:2] == ["pr", "view"]:
            raise GitError("no pull requests found")
        return "https://github.com/owner/sandbox/pull/1\n"

    def make(implementer, reviewer, scanner=None, human=lambda d: "0", task_text=TASK, resume=None, planner=None, ci=None):
        """human=None means headless: a decision stops the process instead of prompting."""
        store = Store(paths.db)
        planner = planner or FakePlanner([plan(("the whole task", "hard"))])
        ci = ci or FakeCi()
        if resume:
            return Runner.resume(run_id=resume, config=config, paths=paths, store=store, git=GitManager(gh=fake_gh),
                                 implementer=implementer, reviewer=reviewer, scanner=scanner or clean_scanner(), human=human,
                                 planner=planner, ci=ci)
        return Runner(
            config=config, paths=paths, store=store, task=parse_task(task_text), task_text=task_text,
            git=GitManager(gh=fake_gh), implementer=implementer, reviewer=reviewer,
            scanner=scanner or clean_scanner(), human=human, clone_url=str(origin), planner=planner, ci=ci,
        )

    return make, paths, pr_calls


def states(paths: OrqPaths, run_id: str) -> list[str]:
    lines = (paths.run_dir(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(l)["state"] for l in lines if json.loads(l)["type"] == "state"]


def test_happy_path_ends_with_open_pr(env, tmp_path: Path) -> None:
    make, paths, pr_calls = env
    implementer = FakeImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("continue", "now add tests"), review("done", None)])
    runner = make(implementer, reviewer)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert states(paths, runner.run_id)[-2:] == ["FINALIZING", "DONE"]
    assert ["pr", "create"] == pr_calls[1][:2] and ["pr", "merge", "7", "--squash", "--delete-branch"] in pr_calls
    worktree = runner.worktree
    log = git("log", "--format=%s", cwd=worktree).splitlines()
    assert log[0].startswith(f"orq({runner.run_id}) iter 2:")
    assert log[1].startswith(f"orq({runner.run_id}) iter 1:")
    assert (paths.iteration_dir(runner.run_id, 1) / "diff.patch").exists()
    assert (paths.iteration_dir(runner.run_id, 1) / "checks.txt").read_text(encoding="utf-8").startswith("checks ok")
    assert (paths.iteration_dir(runner.run_id, 2) / "reviewer.output.json").exists()
    assert "now add tests" in implementer.prompts[1]
    assert "checks ok" in reviewer.prompts[0]
    assert runner.store.get_run(runner.run_id).state is RunState.DONE


def test_implementer_session_is_resumed(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([ok(), ok()])
    runner = make(implementer, FakeReviewer([review("continue"), review("done", None)]))

    asyncio.run(runner.execute())

    assert runner.store.get_run(runner.run_id).implementer_session == "impl-session"


def test_decision_marker_pauses_and_injects_answer(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []

    def human(decision: Decision) -> str:
        asked.append(decision)
        return "1"

    marker = ok("Need a decision.\n```orq-decision\n{\"decision_type\": \"business\", \"question\": \"Tax first?\", \"options\": [\"yes\", \"no\"], \"recommendation\": 0}\n```")
    marker.decision = {"decision_type": "business", "question": "Tax first?", "options": ["yes", "no"], "recommendation": 0}
    implementer = FakeImplementer([marker, ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), human=human)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert asked[0].question == "Tax first?" and asked[0].source == "implementer"
    assert "AWAITING_HUMAN" in states(paths, runner.run_id)
    decisions_md = (paths.run_dir(runner.run_id) / "DECISIONS.md").read_text(encoding="utf-8")
    assert "Tax first?" in decisions_md and "no" in decisions_md
    assert "Tax first?" in implementer.prompts[1] and "no" in implementer.prompts[1]
    assert runner.store.pending_decisions(runner.run_id) == []


def test_reviewer_needs_human_pauses(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    human_obj = {"decision_type": "risk", "question": "Delete old tests?", "options": ["keep", "delete"], "recommendation": 0}
    reviewer = FakeReviewer([review("needs_human", None, human=human_obj), review("done", None)])
    runner = make(FakeImplementer([ok(), ok()]), reviewer, human=lambda d: asked.append(d) or "0")

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert asked[0].source == "reviewer" and asked[0].decision_type == "risk"
    assert "Delete old tests?" in reviewer.prompts[1]


def test_max_iterations_fails_the_run(env) -> None:
    make, paths, _ = env
    runner = make(FakeImplementer([ok(), ok(), ok()]), FakeReviewer([review("continue")] * 3))

    final = asyncio.run(runner.execute())

    assert final is RunState.FAILED
    assert runner.store.get_run(runner.run_id).iteration == 3


def test_secret_hit_asks_owner_and_abort_aborts(env) -> None:
    make, paths, _ = env

    def leaky(args, cwd):
        Path(args[args.index("--report-path") + 1]).write_text('[{"RuleID": "github-pat", "File": "greeting1.txt", "StartLine": 1}]', encoding="utf-8")
        return subprocess.CompletedProcess(args, 1, "", "")

    asked: list[Decision] = []
    runner = make(FakeImplementer([ok()]), FakeReviewer([]), scanner=SecretScanner(runner=leaky),
                  human=lambda d: asked.append(d) or "abort")

    final = asyncio.run(runner.execute())

    assert final is RunState.ABORTED
    assert asked[0].decision_type == "risk" and "github-pat" in asked[0].question


def test_non_sandbox_repo_is_refused_before_any_work(env) -> None:
    make, paths, _ = env

    with pytest.raises(SandboxError, match="owner/other"):
        make(FakeImplementer([]), FakeReviewer([]), task_text=TASK.replace("owner/sandbox", "owner/other"))


def test_commit_only_contains_files_staged_before_the_check_command(env) -> None:
    make, paths, _ = env
    task_text = TASK.replace("python -c \"print('checks ok')\"", "python -c \"open('junk.txt','w').write('x'); print('ok')\"")
    runner = make(FakeImplementer([ok()]), FakeReviewer([review("done", None)]), task_text=task_text)

    asyncio.run(runner.execute())

    committed = git("show", "--name-only", "--format=", "HEAD", cwd=runner.worktree).split()
    assert "greeting1.txt" in committed
    assert "junk.txt" not in committed


def test_done_with_major_issue_is_treated_as_continue(env) -> None:
    make, paths, _ = env
    flawed = review("done", None)
    flawed.structured["issues"] = [{"severity": "major", "description": "tracked __pycache__ files"}]
    implementer = FakeImplementer([ok(), ok()])
    runner = make(implementer, FakeReviewer([flawed, review("done", None)]))

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert implementer.calls == 2
    assert "tracked __pycache__ files" in implementer.prompts[1]


def test_branch_gets_run_id_suffix_when_name_is_taken(env, origin: Path, tmp_path: Path) -> None:
    make, paths, _ = env
    seed = tmp_path / "seed"
    git("branch", "orq/add-greeting", cwd=seed)
    git("push", "-q", "origin", "orq/add-greeting", cwd=seed)
    runner = make(FakeImplementer([ok()]), FakeReviewer([review("done", None)]))

    asyncio.run(runner.execute())

    assert runner.branch == f"orq/add-greeting-{runner.run_id.lower()}"
    assert runner.store.get_run(runner.run_id).branch == runner.branch


# Phase 2: guard denials


def denied(command: str, text: str = "I need to run that command.") -> AgentResult:
    return AgentResult(ok=True, text=text, permission_denials=[{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": command}}])


class NoWriteImplementer(FakeImplementer):
    """Like FakeImplementer, but the first call changes nothing (it only asked for a destructive action)."""

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        if self.calls == 0:
            self.prompts.append(prompt)
            self.calls += 1
            log_path.write_text("{}\n", encoding="utf-8")
            return self.results.pop(0)
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event, **kwargs)


def test_guard_denial_becomes_destructive_decision_and_approve_writes_token(env) -> None:
    make, paths, _ = env
    answers: list[Decision] = []

    def human(decision: Decision) -> str:
        answers.append(decision)
        return "approve"

    implementer = FakeImplementer([denied("git reset --hard HEAD~1"), ok()])
    reviewer = FakeReviewer([review("continue", "carry on"), review("done", None)])
    runner = make(implementer, reviewer, human=human)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    guard = [d for d in answers if d.source == "guard"]
    assert len(guard) == 1 and guard[0].destructive and guard[0].decision_type == "risk"
    assert "git reset --hard HEAD~1" in guard[0].question
    from orq.guard.rules import action_key
    assert (paths.allow_tokens(runner.run_id) / action_key("Bash", {"command": "git reset --hard HEAD~1"})).exists()
    assert "approved this action" in implementer.prompts[1] and "git reset --hard HEAD~1" in implementer.prompts[1]
    assert "carry on" in implementer.prompts[1]


def test_guard_denial_denied_by_owner_tells_implementer_to_proceed(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([denied("rm -rf build"), ok()])
    reviewer = FakeReviewer([review("continue", "carry on"), review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: "deny")

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert "denied this action" in implementer.prompts[1] and "rm -rf build" in implementer.prompts[1]
    assert not any(paths.allow_tokens(runner.run_id).iterdir())


def test_same_denied_action_twice_in_one_turn_asks_once(env) -> None:
    make, paths, _ = env
    result = AgentResult(ok=True, text="x", permission_denials=[
        {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "git clean -fdx"}},
        {"tool_name": "Bash", "tool_use_id": "t2", "tool_input": {"command": "git clean -fdx"}},
    ])
    asked: list[Decision] = []
    runner = make(FakeImplementer([result, ok()]), FakeReviewer([review("continue"), review("done", None)]),
                  human=lambda d: asked.append(d) or "deny")
    asyncio.run(runner.execute())
    assert len([d for d in asked if d.source == "guard"]) == 1


def test_denial_without_other_work_skips_review(env) -> None:
    make, paths, _ = env
    implementer = NoWriteImplementer([denied("git reset --hard"), ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: "approve")

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert len(reviewer.prompts) == 2  # iteration 2 review and the final review; the empty iteration 1 was not reviewed
    assert "approved this action" in implementer.prompts[1]


# Phase 2: diff rules


class DeletingImplementer(FakeImplementer):
    """First call deletes README.md (a deleted_file violation); later calls behave like FakeImplementer."""

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        if self.calls == 0:
            (cwd / "README.md").unlink()
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event, **kwargs)


def test_diff_rule_violation_denied_resets_worktree(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    implementer = DeletingImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: asked.append(d) or "deny")

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    guard = [d for d in asked if d.source == "guard"]
    assert guard and "deleted_file" in guard[0].question and guard[0].destructive
    assert (runner.worktree / "README.md").exists()            # the reset restored it
    assert "rejected these changes" in implementer.prompts[1]
    assert len(reviewer.prompts) == 2                           # iteration 1 was not reviewed; iteration 2 plus the final review
    log = git("log", "--format=%s", cwd=runner.worktree).splitlines()
    assert [l for l in log if l.startswith("orq(")] == [f"orq({runner.run_id}) iter 2: I changed things"]


def test_diff_rule_violation_approved_commits(env) -> None:
    make, paths, _ = env
    implementer = DeletingImplementer([ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: "approve")

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert not (runner.worktree / "README.md").exists()
    assert "deleted_file" in runner.rundir.decisions_text()


# Phase 2: no progress


class SameDiffImplementer(FakeImplementer):
    """Creates same.txt once, then rewrites identical content: iterations 2 and 3 produce empty diffs."""

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        self.prompts.append(prompt)
        self.calls += 1
        (cwd / "same.txt").write_text("same\n", encoding="utf-8")
        log_path.write_text("{}\n", encoding="utf-8")
        return self.results.pop(0)


def test_no_progress_asks_and_rollback_discards_iterations(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []

    def human(decision: Decision) -> str:
        asked.append(decision)
        return decision.options[1] if decision.source == "orq" else "0"

    implementer = SameDiffImplementer([ok(), ok(), ok(), ok()])
    reviewer = FakeReviewer([review("continue", "add the tests"), review("continue", "fix the docs"),
                             review("continue", "polish"), review("done", None)])
    runner = make(implementer, reviewer, human=human)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    blocked = [d for d in asked if d.decision_type == "blocked"]
    assert blocked and blocked[0].options[1] == "rollback to iteration 1" and "same_diff" in blocked[0].question
    log = git("log", "--format=%s", cwd=runner.worktree).splitlines()
    assert [l for l in log if l.startswith("orq(")] == [f"orq({runner.run_id}) iter 1: I changed things"]
    # Iterations 2 and 3 were discarded; the fourth call is iteration 2 again and both sides hear about it.
    assert "discarded" in implementer.prompts[3].lower() and "iteration 2" in implementer.prompts[3] and "## Iteration 2" in implementer.prompts[3]
    assert "discarded" in reviewer.prompts[3].lower()
    assert runner.store.get_run(runner.run_id).iteration == 2
    archived = [p.name for p in (paths.run_dir(runner.run_id) / "iterations").iterdir() if "discarded" in p.name]
    assert len(archived) == 2


def test_no_progress_continue_resets_streak(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    implementer = SameDiffImplementer([ok(), ok(), ok(), ok()])
    reviewer = FakeReviewer([review("continue", "add the tests"), review("continue", "fix the docs"),
                             review("continue", "polish the names"), review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: asked.append(d) or "continue")
    runner.config.limits.max_iterations = 4

    assert asyncio.run(runner.execute()) is RunState.DONE
    assert len([d for d in asked if d.decision_type == "blocked"]) == 1


def test_no_progress_abort(env) -> None:
    make, paths, _ = env
    implementer = SameDiffImplementer([ok(), ok(), ok()])
    reviewer = FakeReviewer([review("continue", "add the tests"), review("continue", "fix the docs"), review("continue", "polish")])
    runner = make(implementer, reviewer, human=lambda d: "abort")
    assert asyncio.run(runner.execute()) is RunState.ABORTED


# Phase 2: rate limits and agent errors


def rate_limited() -> AgentResult:
    return AgentResult(ok=False, error="You've hit your session limit · resets 10pm (America/Cayenne)", error_kind="rate_limit",
                       rate_limit={"status": "rejected", "resetsAt": 0})


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("orq.core.loop.asyncio.sleep", fake_sleep)
    return slept


def test_rate_limit_waits_then_retries_same_iteration(env, no_sleep) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([rate_limited(), ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]))

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE and no_sleep and implementer.calls == 2
    assert "PAUSED_RATE_LIMIT" in states(paths, runner.run_id)
    assert runner.store.get_run(runner.run_id).iteration == 1


def test_rate_limit_retries_exhausted_asks_owner(env, no_sleep) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    implementer = FakeImplementer([rate_limited()] * 4 + [ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), human=lambda d: asked.append(d) or "retry")

    assert asyncio.run(runner.execute()) is RunState.DONE
    assert asked and asked[0].decision_type == "blocked" and "rate limit" in asked[0].question
    assert implementer.calls == 5


def test_agent_error_becomes_retry_decision(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    implementer = FakeImplementer([AgentResult(ok=False, error="API Error: 529 Overloaded", error_kind="error"), ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), human=lambda d: asked.append(d) or "retry")

    assert asyncio.run(runner.execute()) is RunState.DONE
    assert asked[0].source == "orq" and "529" in asked[0].question and implementer.calls == 2


def test_agent_error_abort(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([AgentResult(ok=False, error="boom", error_kind="error")])
    runner = make(implementer, FakeReviewer([]), human=lambda d: "abort")
    assert asyncio.run(runner.execute()) is RunState.ABORTED


def test_router_fallback_state_roundtrips_through_checkpoint(env) -> None:
    make, paths, _ = env
    from orq.adapters.router import ReviewerRouter
    from orq.core.checkpoint import Checkpoint
    primary = FakeReviewer([AgentResult(ok=False, error="usage limit", error_kind="rate_limit")])
    fallback = FakeReviewer([review("done", None)])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    runner = make(FakeImplementer([ok()]), router)

    assert asyncio.run(runner.execute()) is RunState.DONE

    cp = Checkpoint.load(paths.run_dir(runner.run_id) / "state.json")
    assert cp.reviewer_fallback_until == 1000.0 + 3600.0
    types = [json.loads(l)["type"] for l in (paths.run_dir(runner.run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "reviewer_switched" in types

    # A resumed runner hands the recorded choice back to a fresh router.
    fresh = ReviewerRouter(FakeReviewer([]), FakeReviewer([]), switch_at_used_percent=90, clock=lambda: 1000.0)
    cp.state, cp.phase = RunState.PAUSED.value, "finalize"
    cp.save(paths.run_dir(runner.run_id) / "state.json")
    resumed = make(FakeImplementer([]), fresh, resume=runner.run_id)
    assert fresh.fallback_until == 4600.0 and resumed.cp.reviewer_fallback_until == 4600.0


def test_reviewer_question_in_a_turn_with_denials_still_asks_the_guard(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []

    def human(decision: Decision) -> str:
        asked.append(decision)
        return "approve" if decision.source == "guard" else "0"

    human_obj = {"decision_type": "blocked", "question": "Approve the deletion?", "options": ["yes", "no"], "recommendation": 0}
    implementer = FakeImplementer([denied("rm -rf legacy"), ok()])
    reviewer = FakeReviewer([review("needs_human", None, human=human_obj), review("done", None)])
    runner = make(implementer, reviewer, human=human)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert [d.source for d in asked] == ["reviewer", "guard"]
    from orq.guard.rules import action_key
    assert (paths.allow_tokens(runner.run_id) / action_key("Bash", {"command": "rm -rf legacy"})).exists()
    assert "approved this action" in implementer.prompts[1] and "Approve the deletion?" in implementer.prompts[1]


# Phase 3: planner, milestones, model routing


def test_planning_runs_first_and_milestones_drive_prompts_and_models(env) -> None:
    make, paths, _ = env
    planner = FakePlanner([plan(("write greet.py", "hard"), ("add tests", "mechanical"))])
    implementer = FakeImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("done", None), review("done", None)])
    runner = make(implementer, reviewer, planner=planner)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert states(paths, runner.run_id)[:3] == ["QUEUED", "PLANNING", "IMPLEMENTING"]
    assert planner.calls[0]["effort"] == "high" and planner.calls[0]["contract"].name == "plan"
    assert "write greet.py" in (paths.run_dir(runner.run_id) / "PLAN.md").read_text(encoding="utf-8")
    assert "Current milestone (1/2): write greet.py" in implementer.prompts[0]
    assert "Current milestone (2/2): add tests" in implementer.prompts[1] and "Start milestone 2" in implementer.prompts[1]
    assert implementer.models == ["opus", "sonnet"]
    assert "Current milestone (1/2)" in reviewer.prompts[0] and "Current milestone (2/2)" in reviewer.prompts[1]
    events = [json.loads(l) for l in (paths.run_dir(runner.run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["model"] for e in events if e["type"] == "implementer_model"] == ["opus", "sonnet"]
    assert any(e["type"] == "milestone_done" and e["milestone"] == 1 for e in events)
    assert runner.store.get_run(runner.run_id).iteration == 2


def test_plan_approval_required_pauses_and_free_text_replans(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    answers = iter(["make it one milestone", "approve"])
    planner = FakePlanner([plan(("a", "hard"), ("b", "hard")), plan(("a and b", "hard"))])
    implementer = FakeImplementer([ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), planner=planner,
                  human=lambda d: asked.append(d) or next(answers), task_text=TASK.replace("skip", "required"))

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert "AWAITING_PLAN_APPROVAL" in states(paths, runner.run_id)
    assert [d.source for d in asked] == ["planner", "planner"] and asked[0].options == ["approve", "revise"]
    assert "make it one milestone" in planner.prompts[1] and "revised plan" in planner.prompts[1]
    assert "Current milestone (1/1): a and b" in implementer.prompts[0]
    assert "a and b" in (paths.run_dir(runner.run_id) / "PLAN.md").read_text(encoding="utf-8")


def test_plan_approval_required_headless_stops_then_resumes(env) -> None:
    make, paths, _ = env
    planner = FakePlanner([plan(("a", "hard"))])
    implementer = FakeImplementer([ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), planner=planner, human=None, task_text=TASK.replace("skip", "required"))

    assert asyncio.run(runner.execute()) is RunState.AWAITING_PLAN_APPROVAL
    assert runner.store.get_run(runner.run_id).state is RunState.AWAITING_PLAN_APPROVAL
    pending = runner.store.pending_decisions(runner.run_id)[0]
    runner.store.answer_decision(pending.decision_id, answer="approve", answered_via="cli")

    resumed = make(implementer, FakeReviewer([review("done", None)]), planner=FakePlanner([]), human=None, resume=runner.run_id)
    assert asyncio.run(resumed.execute()) is RunState.DONE


def test_planner_needs_human_asks_then_replans(env) -> None:
    make, paths, _ = env
    human_obj = {"decision_type": "ambiguity", "question": "Which greeting?", "options": ["Hello", "Hi"], "recommendation": 0}
    planner = FakePlanner([plan(status="needs_human", human=human_obj), plan(("greet", "hard"))])
    asked: list[Decision] = []
    runner = make(FakeImplementer([ok()]), FakeReviewer([review("done", None)]), planner=planner, human=lambda d: asked.append(d) or "1")

    assert asyncio.run(runner.execute()) is RunState.DONE
    assert asked[0].question == "Which greeting?" and asked[0].source == "planner"
    assert "Which greeting?" in planner.prompts[1] and "Hi" in planner.prompts[1]


def test_planner_invalid_output_twice_fails(env) -> None:
    make, paths, _ = env
    bad = AgentResult(ok=False, error="not a plan", error_kind="invalid_output")
    runner = make(FakeImplementer([]), FakeReviewer([]), planner=FakePlanner([bad, bad]))
    assert asyncio.run(runner.execute()) is RunState.FAILED


def test_reviewer_gets_routine_effort(env) -> None:
    make, paths, _ = env
    reviewer = FakeReviewer([review("done", None)])
    seen: list[dict] = []
    original = reviewer.run

    async def spy(prompt, **kwargs):
        seen.append(kwargs)
        return await original(prompt, **kwargs)

    reviewer.run = spy  # type: ignore[method-assign]
    runner = make(FakeImplementer([ok()]), reviewer)
    assert asyncio.run(runner.execute()) is RunState.DONE
    assert seen[0]["effort"] == "low" and seen[0]["contract"].name == "review"


def test_done_milestones_with_empty_diffs_are_not_a_stall(env) -> None:
    make, paths, _ = env
    planner = FakePlanner([plan(("a", "mechanical"), ("b", "mechanical"), ("c", "mechanical"))])
    implementer = NoWriteImplementer([ok(), ok(), ok()])  # never changes anything
    implementer.results = [ok(), ok(), ok()]
    asked: list[Decision] = []
    reviewer = FakeReviewer([review("done", None)] * 3)
    runner = make(implementer, reviewer, planner=planner, human=lambda d: asked.append(d) or "abort")

    assert asyncio.run(runner.execute()) is RunState.DONE
    assert asked == []


# Phase 3: merge gate


def gate_events(paths, run_id: str) -> list[dict]:
    return [json.loads(l) for l in (paths.run_dir(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_gate_happy_path_merges_and_removes_worktree(env) -> None:
    make, paths, pr_calls = env
    reviewer = FakeReviewer([review("done", None), review("done", None)])  # milestone review, then the final review
    runner = make(FakeImplementer([ok()]), reviewer)
    runner.config.git.keep_worktree = False

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert ["pr", "merge", "7", "--squash", "--delete-branch"] in pr_calls
    assert not runner.worktree.exists()
    types = [e["type"] for e in gate_events(paths, runner.run_id)]
    assert types[-3:] == ["pr_merged", "worktree_removed", "state"] and "ci" in types and "final_review" in types
    assert "Final review before merge" in reviewer.prompts[1] and "checks passed" in reviewer.prompts[1]
    assert runner.cp.pr_number == 7 and runner.cp.pr_url.endswith("/pull/1")


def test_gate_ci_failure_goes_back_to_implement_with_the_log(env) -> None:
    make, paths, pr_calls = env
    ci = FakeCi([CiStatus("failure", failed_log="pytest: 1 failed"), CiStatus("success")])
    implementer = FakeImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("done", None), review("done", None), review("done", None)])
    runner = make(implementer, reviewer, ci=ci)

    assert asyncio.run(runner.execute()) is RunState.DONE
    assert "GitHub Actions failed" in implementer.prompts[1] and "pytest: 1 failed" in implementer.prompts[1]
    assert ci.calls == 2 and runner.cp.gate_rounds == 1
    assert len([c for c in pr_calls if c[:2] == ["pr", "create"]]) == 1  # the second finalize reused the PR


def test_gate_base_moved_rebases_and_force_pushes(env, origin: Path, tmp_path: Path) -> None:
    make, paths, pr_calls = env
    reviewer = FakeReviewer([review("done", None), review("done", None)])
    runner = make(FakeImplementer([ok()]), reviewer)
    seed = tmp_path / "seed"

    class MovingCi(FakeCi):
        def wait(self, worktree, pr_number):
            if self.calls == 0:  # base moves while the first CI wait is in progress
                (seed / "moved.txt").write_text("x\n", encoding="utf-8")
                git("add", "-A", cwd=seed)
                git("commit", "-q", "-m", "someone else", cwd=seed)
                git("push", "-q", "origin", "main", cwd=seed)
            return super().wait(worktree, pr_number)

    runner.ci = MovingCi()
    assert asyncio.run(runner.execute()) is RunState.DONE
    events = gate_events(paths, runner.run_id)
    assert any(e["type"] == "base_moved_before_merge" for e in events) and any(e["type"] == "rebased" for e in events)
    assert runner.ci.calls == 2
    assert (runner.worktree / "moved.txt").exists() and (runner.worktree / "greeting1.txt").exists()
    remote_head = git("rev-parse", "orq/add-greeting", cwd=origin).strip()
    assert remote_head == git("rev-parse", "HEAD", cwd=runner.worktree).strip()


def test_gate_rebase_conflict_asks_owner(env, tmp_path: Path) -> None:
    make, paths, _ = env
    asked: list[Decision] = []

    class ConflictImplementer(FakeImplementer):
        async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
            (cwd / "README.md").write_text("ours\n", encoding="utf-8")
            return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event, **kwargs)

    seed = tmp_path / "seed"

    class ConflictingCi(FakeCi):
        def wait(self, worktree, pr_number):
            if self.calls == 0:  # someone lands a conflicting change on main while CI runs
                (seed / "README.md").write_text("theirs\n", encoding="utf-8")
                git("add", "-A", cwd=seed)
                git("commit", "-q", "-m", "conflict", cwd=seed)
                git("push", "-q", "origin", "main", cwd=seed)
            return super().wait(worktree, pr_number)

    runner = make(ConflictImplementer([ok()]), FakeReviewer([review("done", None)]), ci=ConflictingCi(),
                  human=lambda d: asked.append(d) or "abort")

    assert asyncio.run(runner.execute()) is RunState.ABORTED
    assert asked[-1].options == ["retry", "abort"] and "conflicts" in asked[-1].question


def test_gate_final_review_continue_loops_once(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([ok(), ok()])
    not_done = review("continue", "add a docstring")
    reviewer = FakeReviewer([review("done", None), not_done, review("done", None), review("done", None)])
    runner = make(implementer, reviewer)

    assert asyncio.run(runner.execute()) is RunState.DONE
    assert "Final review before merge: add a docstring" in implementer.prompts[1]
    assert len(reviewer.prompts) == 4 and runner.cp.gate_rounds == 1


def test_gate_done_with_major_issue_is_not_done(env) -> None:
    make, paths, _ = env
    flawed = review("done", None)
    flawed.structured["issues"] = [{"severity": "major", "description": "README still mentions the old name"}]
    implementer = FakeImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("done", None), flawed, review("done", None), review("done", None)])
    runner = make(implementer, reviewer)
    assert asyncio.run(runner.execute()) is RunState.DONE
    assert "README still mentions the old name" in implementer.prompts[1]


def test_gate_rounds_exhausted_asks_owner(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    ci = FakeCi([CiStatus("failure", failed_log="boom")])
    implementer = FakeImplementer([ok()] * 6)
    reviewer = FakeReviewer([review("done", None)] * 6)
    runner = make(implementer, reviewer, ci=ci, human=lambda d: asked.append(d) or "abort")
    runner.config.limits.max_iterations = 10
    runner.config.merge.max_gate_rounds = 2

    assert asyncio.run(runner.execute()) is RunState.ABORTED
    assert asked[-1].options == ["keep going", "abort"] and "3 times" in asked[-1].question


def test_gate_no_ci_asks_and_merge_without_ci_continues(env) -> None:
    make, paths, pr_calls = env
    asked: list[Decision] = []
    runner = make(FakeImplementer([ok()]), FakeReviewer([review("done", None), review("done", None)]), ci=FakeCi([CiStatus("none")]),
                  human=lambda d: asked.append(d) or "merge without CI")
    assert asyncio.run(runner.execute()) is RunState.DONE
    assert asked[-1].decision_type == "risk" and ["pr", "merge", "7", "--squash", "--delete-branch"] in pr_calls
    assert any(e["type"] == "ci_skipped" for e in gate_events(paths, runner.run_id))


def test_gate_ci_timeout_keep_waiting(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    ci = FakeCi([CiStatus("timeout"), CiStatus("success")])
    runner = make(FakeImplementer([ok()]), FakeReviewer([review("done", None), review("done", None)]), ci=ci,
                  human=lambda d: asked.append(d) or "keep waiting")
    assert asyncio.run(runner.execute()) is RunState.DONE and ci.calls == 2
    assert asked[-1].options == ["keep waiting", "abort"]


def test_gate_final_review_needs_human(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    human_obj = {"decision_type": "business", "question": "Ship without the badge?", "options": ["yes", "no"], "recommendation": 0}
    implementer = FakeImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("done", None), review("needs_human", None, human=human_obj), review("done", None), review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: asked.append(d) or "0")
    assert asyncio.run(runner.execute()) is RunState.DONE
    assert asked[0].question == "Ship without the badge?" and "Ship without the badge?" in implementer.prompts[1]
