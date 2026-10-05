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
    task = tmp_path / "TASK.md"
    task.write_text(TASK, encoding="utf-8")

    result = CliRunner().invoke(app, ["run", str(task)])

    assert result.exit_code == 1
    assert "sandbox_repos" in result.output


def test_run_end_to_end_with_fake_clis(home: OrqPaths, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home.config.write_text(
        f'[git]\nworktree_root = "{(tmp_path / "wt").as_posix()}"\nsandbox_repos = ["owner/sandbox"]\n', encoding="utf-8"
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
    assert gh_calls[0][:2] == ["pr", "create"]
    runs = Store(home.db).list_runs()
    assert runs[0].state is RunState.DONE
    assert "refs/heads/orq/add-greeting" in git("ls-remote", "--heads", str(origin), cwd=tmp_path)
