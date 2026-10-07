"""Run control shared by the CLI, the dashboard and WhatsApp commands: pause, abort, resume (SPEC 14, 9.3)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from orq.core.checkpoint import Checkpoint
from orq.core.models import RunState
from orq.core.procs import pid_alive
from orq.git.manager import GitError, GitManager
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir

PAUSE_FLAG = "pause.requested"


class ControlError(RuntimeError):
    pass


def load_checkpoint(paths: OrqPaths, run_id: str) -> Checkpoint:
    cp = Checkpoint.load(paths.run_dir(run_id) / "state.json")
    if cp is None:
        raise ControlError(f"no run {run_id} (no state.json)")
    return cp


def is_live(cp: Checkpoint) -> bool:
    return pid_alive(cp.pid)


def request_pause(paths: OrqPaths, run_id: str) -> bool:
    """Drop the pause flag; return whether a live process will pick it up."""
    cp = load_checkpoint(paths, run_id)
    (paths.run_dir(run_id) / PAUSE_FLAG).write_text("", encoding="utf-8")
    RunDir(paths.run_dir(run_id)).event("pause_requested", live=is_live(cp))
    return is_live(cp)


def abort_run(paths: OrqPaths, run_id: str, *, git: GitManager | None = None) -> str | None:
    """Mark a stopped run ABORTED and remove its worktree. Returns a warning when the worktree could not be removed."""
    cp = load_checkpoint(paths, run_id)
    if is_live(cp):
        raise ControlError(f"{run_id} is still running (pid {cp.pid}); pause it first")
    warning = None
    if cp.repo_path and Path(cp.worktree).exists():
        try:
            (git or GitManager()).remove_worktree(Path(cp.repo_path), Path(cp.worktree), branch=cp.branch)
        except GitError as exc:
            warning = f"worktree not removed: {exc}"
    rundir = RunDir(paths.run_dir(run_id))
    cp.state, cp.phase = RunState.ABORTED.value, "done"
    cp.save(rundir.path / "state.json")
    Store(paths.db).set_state(run_id, RunState.ABORTED)
    rundir.event("state", state=RunState.ABORTED.value, reason="aborted by owner")
    return warning


def spawn_resume(paths: OrqPaths, run_id: str) -> int:
    """Start `orq resume <run_id>` as a detached process (waits for answers, no terminal). Returns its pid."""
    cp = load_checkpoint(paths, run_id)
    if is_live(cp):
        raise ControlError(f"{run_id} is already running (pid {cp.pid})")
    if cp.state in (RunState.DONE.value, RunState.ABORTED.value):
        raise ControlError(f"{run_id} is {cp.state}")
    log = (paths.run_dir(run_id) / "resume.log").open("a", encoding="utf-8")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    env = {**os.environ, "ORQ_HOME": str(paths.root), "PYTHONUTF8": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "orq", "resume", run_id], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            env=env, creationflags=flags, close_fds=True)
    RunDir(paths.run_dir(run_id)).event("resume_spawned", pid=proc.pid)
    return proc.pid
