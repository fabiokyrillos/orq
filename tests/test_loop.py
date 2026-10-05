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
from orq.git.manager import GitManager
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
        return "https://github.com/owner/sandbox/pull/1\n"

    def make(implementer, reviewer, scanner=None, human=None, task_text=TASK):
        return Runner(
            config=config, paths=paths, store=Store(paths.db), task=parse_task(task_text), task_text=task_text,
            git=GitManager(gh=fake_gh), implementer=implementer, reviewer=reviewer,
            scanner=scanner or clean_scanner(), human=human or (lambda d: "0"), clone_url=str(origin),
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
    assert pr_calls and pr_calls[0][:2] == ["pr", "create"]
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


def test_implementer_rate_limit_pauses_run(env) -> None:
    make, paths, _ = env
    limited = AgentResult(ok=False, error="You've hit your session limit", error_kind="rate_limit")
    runner = make(FakeImplementer([limited]), FakeReviewer([]))

    final = asyncio.run(runner.execute())

    assert final is RunState.PAUSED_RATE_LIMIT


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
