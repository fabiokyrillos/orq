import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orq import __version__
from orq.cli import app
from orq.core.models import Decision, RunRecord, RunState
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir
from tests.conftest import git

FAKES = Path(__file__).parent / "fakes"
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


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OrqPaths:
    paths = OrqPaths(tmp_path / "home")
    paths.root.mkdir()
    monkeypatch.setenv("ORQ_HOME", str(paths.root))
    return paths


def test_version_flag_prints_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_status_lists_runs_and_pending_decisions(home: OrqPaths) -> None:
    store = Store(home.db)
    store.create_run(RunRecord(run_id="RAAAAA", repo="owner/sandbox", task_title="Add greeting", branch="orq/add-greeting"))
    store.set_state("RAAAAA", RunState.AWAITING_HUMAN)
    store.add_decision(Decision(decision_id="DBBBB", run_id="RAAAAA", source="reviewer", decision_type="risk",
                                question="Delete tests?", options=["keep", "delete"], recommendation=0))

    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "RAAAAA" in result.output and "AWAITING_HUMAN" in result.output and "Add greeting" in result.output

    detail = CliRunner().invoke(app, ["status", "RAAAAA"])

    assert "DBBBB" in detail.output and "Delete tests?" in detail.output


def test_status_unknown_run_fails(home: OrqPaths) -> None:
    result = CliRunner().invoke(app, ["status", "RZZZZZ"])

    assert result.exit_code == 1
    assert "RZZZZZ" in result.output


def test_logs_prints_events(home: OrqPaths) -> None:
    rundir = RunDir(home.run_dir("RAAAAA"))
    rundir.create("# Task: x\n")
    rundir.event("state", state="IMPLEMENTING")
    rundir.event("pr", url="https://example.invalid/pr/1")

    result = CliRunner().invoke(app, ["logs", "RAAAAA"])

    assert result.exit_code == 0
    assert "IMPLEMENTING" in result.output and "https://example.invalid/pr/1" in result.output


def test_run_refuses_repo_outside_sandbox_list(home: OrqPaths, tmp_path: Path) -> None:
    home.config.write_text('[git]\nsandbox_repos = ["other/repo"]\n', encoding="utf-8")
    task = tmp_path / "TASK.md"
    task.write_text(TASK, encoding="utf-8")

    result = CliRunner().invoke(app, ["run", str(task)])

    assert result.exit_code == 1
    assert "sandbox_repos" in result.output


def test_run_end_to_end_with_fake_clis(home: OrqPaths, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home.config.write_text(
        f'[git]\nworktree_root = "{(tmp_path / "wt").as_posix()}"\nsandbox_repos = ["owner/sandbox"]\n[merge]\npoll_seconds = 0.05\n', encoding="utf-8"
    )
    task = tmp_path / "TASK.md"
    task.write_text(TASK, encoding="utf-8")
    py = sys.executable
    monkeypatch.setenv("ORQ_CLAUDE_EXE", f"{py} {FAKES / 'fake_claude.py'}")
    monkeypatch.setenv("ORQ_CODEX_CMD", f"{py} {FAKES / 'fake_codex.py'}")
    monkeypatch.setenv("ORQ_GH_CMD", f"{py} {FAKES / 'fake_gh.py'}")
    monkeypatch.setenv("FAKE_RECORD", str(tmp_path / "rec.json"))
    monkeypatch.setenv("FAKE_GH_RECORD", str(tmp_path / "gh.jsonl"))
    monkeypatch.setenv("FAKE_SCENARIO", "ok")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "done")

    result = CliRunner().invoke(app, ["run", str(task), "--clone-url", str(origin)])

    assert result.exit_code == 0, result.output
    assert "DONE" in result.output and "pull/42" in result.output
    gh_calls = [json.loads(l) for l in (tmp_path / "gh.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [c[:2] for c in gh_calls][:2] == [["pr", "view"], ["pr", "create"]]
    assert ["pr", "merge", "42", "--squash", "--delete-branch"] in gh_calls
    assert [c[:2] for c in gh_calls].count(["pr", "checks"]) >= 2  # no checks reported, pending, then pass
    assert "merged" in result.output.lower()
    runs = Store(home.db).list_runs()
    assert runs[0].state is RunState.DONE
    assert "refs/heads/orq/add-greeting" in git("ls-remote", "--heads", str(origin), cwd=tmp_path)


# Phase 2 commands


def seeded_run(home: OrqPaths, state: str = "AWAITING_HUMAN", phase: str = "await", pid: int = 999999):
    from orq.core.checkpoint import Checkpoint
    store = Store(home.db)
    store.create_run(RunRecord(run_id="RAAAAA", repo="o/s", task_title="t", branch="b"))
    store.set_state("RAAAAA", RunState(state))
    rundir = RunDir(home.run_dir("RAAAAA"))
    rundir.create(TASK)
    cp = Checkpoint(run_id="RAAAAA", state=state, phase=phase, branch="b", worktree=str(home.root / "wt"))
    cp.save(rundir.path / "state.json")
    # save() stamps the current pid; pretend another process (dead or alive) owns the run.
    state_path = rundir.path / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["pid"] = pid
    state_path.write_text(json.dumps(data), encoding="utf-8")
    return store, rundir


def test_answer_records_decision(home: OrqPaths) -> None:
    store, rundir = seeded_run(home)
    store.add_decision(Decision(decision_id="DBBBB", run_id="RAAAAA", source="guard", decision_type="risk",
                                question="Allow?", options=["approve", "deny"]))

    result = CliRunner().invoke(app, ["answer", "DBBBB", "--approve"])

    assert result.exit_code == 0, result.output
    assert "orq resume RAAAAA" in result.output
    assert Store(home.db).get_decision("DBBBB").answer == "approve"
    assert "DBBBB" in (rundir.path / "DECISIONS.md").read_text(encoding="utf-8")
    again = CliRunner().invoke(app, ["answer", "DBBBB", "--deny"])
    assert again.exit_code == 1 and "already answered" in again.output


def test_answer_with_text_and_validation(home: OrqPaths) -> None:
    store, _ = seeded_run(home)
    store.add_decision(Decision(decision_id="DBBBB", run_id="RAAAAA", source="implementer", decision_type="business",
                                question="Tax?", options=["before", "after"]))
    assert CliRunner().invoke(app, ["answer", "DBBBB"]).exit_code == 1
    assert CliRunner().invoke(app, ["answer", "DBBBB", "text", "--approve"]).exit_code == 1
    assert CliRunner().invoke(app, ["answer", "DZZZZ", "--approve"]).exit_code == 1
    ok = CliRunner().invoke(app, ["answer", "DBBBB", "after taxes please"])
    assert ok.exit_code == 0, ok.output
    assert Store(home.db).get_decision("DBBBB").answer == "after taxes please"


def test_pause_writes_flag(home: OrqPaths) -> None:
    _, rundir = seeded_run(home, state="IMPLEMENTING", phase="implement")
    result = CliRunner().invoke(app, ["pause", "RAAAAA"])
    assert result.exit_code == 0, result.output
    assert (rundir.path / "pause.requested").exists()
    assert CliRunner().invoke(app, ["pause", "RNOPE1"]).exit_code == 1


def test_abort_marks_inactive_run(home: OrqPaths) -> None:
    seeded_run(home)
    result = CliRunner().invoke(app, ["abort", "RAAAAA"])
    assert result.exit_code == 0, result.output
    assert Store(home.db).get_run("RAAAAA").state is RunState.ABORTED
    from orq.core.checkpoint import Checkpoint
    assert Checkpoint.load(home.run_dir("RAAAAA") / "state.json").phase == "done"


def test_abort_refuses_live_run(home: OrqPaths) -> None:
    import os
    seeded_run(home, pid=os.getpid())
    result = CliRunner().invoke(app, ["abort", "RAAAAA"])
    assert result.exit_code == 1 and "still running" in result.output


def test_resume_reports_missing_run(home: OrqPaths) -> None:
    result = CliRunner().invoke(app, ["resume", "RNOPE1"])
    assert result.exit_code == 1 and "state.json" in result.output


def test_rollback_validates_target(home: OrqPaths) -> None:
    seeded_run(home, state="PAUSED", phase="implement")
    result = CliRunner().invoke(app, ["rollback", "RAAAAA", "--to", "3"])
    assert result.exit_code == 1 and ("--to" in result.output or "worktree" in result.output)


def test_run_kill_and_resume_with_fake_clis(home: OrqPaths, origin: Path, tmp_path: Path) -> None:
    """Exit criterion for crash resume: kill orq mid-implementer, resume, reach DONE."""
    import os
    import subprocess
    import time

    from orq.core.checkpoint import Checkpoint
    from orq.core.procs import kill_tree, pid_alive

    home.config.write_text(f'[git]\nworktree_root = "{(tmp_path / "wt").as_posix()}"\n[merge]\npoll_seconds = 0.05\n', encoding="utf-8")
    task = tmp_path / "TASK.md"
    task.write_text(TASK, encoding="utf-8")
    py = sys.executable
    env = {**os.environ, "ORQ_HOME": str(home.root), "ORQ_CLAUDE_EXE": f"{py} {FAKES / 'fake_claude.py'}",
           "ORQ_CODEX_CMD": f"{py} {FAKES / 'fake_codex.py'}", "ORQ_GH_CMD": f"{py} {FAKES / 'fake_gh.py'}",
           "FAKE_RECORD": str(tmp_path / "rec.json"), "FAKE_GH_RECORD": str(tmp_path / "gh.jsonl"),
           "FAKE_SCENARIO": "hang", "FAKE_CODEX_SCENARIO": "done", "PYTHONUTF8": "1"}
    proc = subprocess.Popen([py, "-m", "orq", "run", str(task), "--clone-url", str(origin)], env=env, cwd=str(tmp_path),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    child_pid = None
    try:
        deadline = time.time() + 90
        run_id = None
        while time.time() < deadline and run_id is None:
            runs = Store(home.db).list_runs()
            if runs and runs[0].state is RunState.IMPLEMENTING and (home.run_dir(runs[0].run_id) / "child.pid").exists():
                run_id = runs[0].run_id
                child_pid = int((home.run_dir(run_id) / "child.pid").read_text(encoding="utf-8").strip())
            time.sleep(0.5)
        assert run_id, "run never reached IMPLEMENTING"
        # Kill only orq, leaving the fake claude child behind as an orphan.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"], capture_output=True)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
    assert pid_alive(child_pid), "the hanging fake claude should still be alive after orq was killed"
    cp = Checkpoint.load(home.run_dir(run_id) / "state.json")
    assert cp.phase == "implement" and cp.state == "IMPLEMENTING"

    env["FAKE_SCENARIO"] = "ok"
    resumed = subprocess.run([py, "-m", "orq", "resume", run_id], env=env, cwd=str(tmp_path), capture_output=True,
                             text=True, encoding="utf-8", timeout=180)

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert not pid_alive(child_pid)
    assert Store(home.db).get_run(run_id).state is RunState.DONE
    assert "interrupted" in (home.iteration_dir(run_id, 1) / "implementer.prompt.md").read_text(encoding="utf-8").lower()
    events = [json.loads(l) for l in (home.run_dir(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["type"] == "orphan_killed" for e in events) and any(e["type"] == "resume" for e in events)


def test_run_no_prompt_stops_at_decision_and_resume_continues(home: OrqPaths, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home.config.write_text(f'[git]\nworktree_root = "{(tmp_path / "wt").as_posix()}"\n[merge]\npoll_seconds = 0.05\n', encoding="utf-8")
    task = tmp_path / "TASK.md"
    task.write_text(TASK, encoding="utf-8")
    py = sys.executable
    monkeypatch.setenv("ORQ_CLAUDE_EXE", f"{py} {FAKES / 'fake_claude.py'}")
    monkeypatch.setenv("ORQ_CODEX_CMD", f"{py} {FAKES / 'fake_codex.py'}")
    monkeypatch.setenv("ORQ_GH_CMD", f"{py} {FAKES / 'fake_gh.py'}")
    monkeypatch.setenv("FAKE_RECORD", str(tmp_path / "rec.json"))
    monkeypatch.setenv("FAKE_GH_RECORD", str(tmp_path / "gh.jsonl"))
    monkeypatch.setenv("FAKE_SCENARIO", "denied")  # the fake reports a denied `git reset --hard`
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "done")

    result = CliRunner().invoke(app, ["run", str(task), "--clone-url", str(origin), "--no-prompt"])

    assert result.exit_code == 1, result.output
    run = Store(home.db).list_runs()[0]
    assert run.state is RunState.AWAITING_HUMAN
    pending = Store(home.db).pending_decisions(run.run_id)
    assert len(pending) == 1 and pending[0].source == "guard"

    assert CliRunner().invoke(app, ["answer", pending[0].decision_id, "--deny"]).exit_code == 0
    monkeypatch.setenv("FAKE_SCENARIO", "ok")
    resumed = CliRunner().invoke(app, ["resume", run.run_id, "--no-prompt"])

    assert resumed.exit_code == 0, resumed.output
    assert Store(home.db).get_run(run.run_id).state is RunState.DONE


def test_answer_maps_option_index(home: OrqPaths) -> None:
    store, _ = seeded_run(home)
    store.add_decision(Decision(decision_id="DBBBB", run_id="RAAAAA", source="reviewer", decision_type="blocked",
                                question="Approve?", options=["Approve it (Recommended)", "Leave as-is"]))
    assert CliRunner().invoke(app, ["answer", "DBBBB", "1"]).exit_code == 0
    assert Store(home.db).get_decision("DBBBB").answer == "Leave as-is"


def test_status_shows_milestone_and_pr(home: OrqPaths) -> None:
    from orq.core.checkpoint import Checkpoint
    store, rundir = seeded_run(home, state="IMPLEMENTING", phase="implement")
    cp = Checkpoint.load(rundir.path / "state.json")
    cp.plan = {"summary": "s", "milestones": [{"title": "write it", "goal": "g", "done_when": "d", "difficulty": "hard"},
                                              {"title": "test it", "goal": "g", "done_when": "d", "difficulty": "mechanical"}]}
    cp.milestone_index = 1
    cp.pr_url = "https://github.com/o/r/pull/9"
    cp.save(rundir.path / "state.json")

    result = CliRunner().invoke(app, ["status", "RAAAAA"])

    assert result.exit_code == 0, result.output
    assert "milestone 2/2: test it [mechanical]" in result.output and "pull/9" in result.output
