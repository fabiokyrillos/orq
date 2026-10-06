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

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
        self.prompts.append(prompt)
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

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
        self.prompts.append(prompt)
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


@pytest.fixture
def env(tmp_path: Path, origin: Path):
    paths = OrqPaths(tmp_path / "orq-home")
    config = Config()
    config.git.worktree_root = tmp_path / "wt"
    config.git.sandbox_repos = ["owner/sandbox"]
    config.limits.max_iterations = 3
    pr_calls: list[list[str]] = []

    def fake_gh(args, cwd):
        pr_calls.append(args)
        if args[:2] == ["pr", "view"]:
            raise GitError("no pull requests found")
        return "https://github.com/owner/sandbox/pull/1\n"

    def make(implementer, reviewer, scanner=None, human=lambda d: "0", task_text=TASK, resume=None):
        """human=None means headless: a decision stops the process instead of prompting."""
        store = Store(paths.db)
        if resume:
            return Runner.resume(run_id=resume, config=config, paths=paths, store=store, git=GitManager(gh=fake_gh),
                                 implementer=implementer, reviewer=reviewer, scanner=scanner or clean_scanner(), human=human)
        return Runner(
            config=config, paths=paths, store=store, task=parse_task(task_text), task_text=task_text,
            git=GitManager(gh=fake_gh), implementer=implementer, reviewer=reviewer,
            scanner=scanner or clean_scanner(), human=human, clone_url=str(origin),
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
    assert states(paths, runner.run_id)[-3:] == ["REVIEWING", "FINALIZING", "DONE"]
    assert [c[:2] for c in pr_calls] == [["pr", "view"], ["pr", "create"]]
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

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
        if self.calls == 0:
            self.prompts.append(prompt)
            self.calls += 1
            log_path.write_text("{}\n", encoding="utf-8")
            return self.results.pop(0)
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event)


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
    assert len(reviewer.prompts) == 1  # the empty first iteration was not reviewed
    assert "approved this action" in implementer.prompts[1]


# Phase 2: diff rules


class DeletingImplementer(FakeImplementer):
    """First call deletes README.md (a deleted_file violation); later calls behave like FakeImplementer."""

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
        if self.calls == 0:
            (cwd / "README.md").unlink()
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event)


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
    assert len(reviewer.prompts) == 1                           # iteration 1 was not reviewed
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

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
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
    implementer = SameDiffImplementer([ok(), ok(), ok()])
    reviewer = FakeReviewer([review("continue", "add the tests"), review("continue", "fix the docs"), review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: asked.append(d) or "continue")

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
