"""The hub runs under pythonw (no console): every child it starts must not open a console window of its own."""

import subprocess
import sys
from pathlib import Path

import pytest

from orq.core import procs
from orq.git import manager as manager_module
from orq.git.manager import GitManager

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="console windows are a Windows matter")


class Recorder:
    def __init__(self) -> None:
        self.flags: list[int] = []

    def __call__(self, *args, **kwargs):
        self.flags.append(kwargs.get("creationflags", 0))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")


def test_no_window_only_without_a_console(monkeypatch: pytest.MonkeyPatch) -> None:
    assert procs.has_console()  # pytest runs in a terminal
    assert procs.no_window() == 0  # a hidden console per child would nearly double each git call
    monkeypatch.setattr(procs, "has_console", lambda: False)
    assert procs.no_window() == subprocess.CREATE_NO_WINDOW


def test_git_and_gh_children_get_no_window_under_pythonw(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorder = Recorder()
    monkeypatch.setattr(manager_module.subprocess, "run", recorder)
    monkeypatch.setattr(procs, "has_console", lambda: False)

    GitManager().git("status", cwd=tmp_path)
    GitManager().show_file(tmp_path, "HEAD", "README.md")
    manager_module._run_gh(["repo", "view", "owner/repo"], tmp_path)

    assert recorder.flags == [subprocess.CREATE_NO_WINDOW] * 3


def test_spawned_runs_get_a_hidden_console_their_children_inherit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from orq.core import control
    from orq.core.checkpoint import Checkpoint
    from orq.paths import OrqPaths

    paths = OrqPaths(tmp_path / "home")
    paths.run_dir("RAAAAA").mkdir(parents=True)
    Checkpoint(run_id="RAAAAA", state="AWAITING_HUMAN", phase="await", branch="b", worktree=str(tmp_path), pid=0).save(
        paths.run_dir("RAAAAA") / "state.json")
    seen: dict = {}

    class FakePopen:
        pid = 4242

        def __init__(self, *args, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(control.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(control, "is_live", lambda cp: False)  # save() records this test's own pid

    assert control.spawn_resume(paths, "RAAAAA") == 4242
    # DETACHED_PROCESS would leave the run without a console, so git, claude and codex would each open a window.
    assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert not seen["creationflags"] & subprocess.DETACHED_PROCESS


def test_agent_processes_started_by_the_hub_get_a_hidden_console(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The chat runs claude from the hub itself; without a console it stopped after its first tool call (Phase 7.1)."""
    import asyncio

    from orq.adapters import base

    seen: dict = {}

    async def fake_exec(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(base.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(procs, "has_console", lambda: False)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(base.stream_process(["claude"], cwd=tmp_path, stdin_text="", log_path=tmp_path / "l.jsonl", env={}))
    assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW
