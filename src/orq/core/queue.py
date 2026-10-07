"""Queued runs (Phase 5). A queued run is a run dir, a checkpoint at phase `setup` with no process, and a `runs` row.

The hub's dispatcher starts it with `orq resume`; from then on it is an ordinary run that takes a slot before working.
"""

from __future__ import annotations

from datetime import datetime, timezone

from orq.config import Config
from orq.core.checkpoint import Checkpoint
from orq.core.models import RunRecord, RunState, new_run_id
from orq.core.task import Task, parse_task
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir


class QueueError(ValueError):
    pass


def check_sandbox(config: Config, task: Task) -> None:
    if config.git.sandbox_repos and task.repo not in config.git.sandbox_repos:
        raise QueueError(f"{task.repo} is not listed in [git].sandbox_repos (empty list allows any repo)")


def create_run(config: Config, paths: OrqPaths, store: Store, task: Task, task_text: str, *, clone_url: str | None = None,
               run_id: str | None = None) -> Checkpoint:
    """Run dir, TASK.md, first checkpoint (phase `setup`, state QUEUED) and the `runs` row. Shared by `orq run` and the queue."""
    run_id = run_id or new_run_id()
    branch = f"orq/{task.slug}"
    worktree = config.git.worktree_root / task.repo.split("/")[-1] / run_id
    RunDir(paths.run_dir(run_id)).create(task_text)
    cp = Checkpoint(run_id=run_id, state=RunState.QUEUED.value, phase="setup", branch=branch, worktree=str(worktree),
                    clone_url=clone_url, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.create_run(RunRecord(run_id=run_id, repo=task.repo, task_title=task.title, branch=branch, worktree=str(worktree)))
    return cp


def enqueue(paths: OrqPaths, store: Store, config: Config, task_text: str, *, clone_url: str | None = None) -> str:
    """Queue a task without starting it. Raises TaskError or QueueError before anything is written."""
    task = parse_task(task_text)
    check_sandbox(config, task)
    cp = create_run(config, paths, store, task, task_text, clone_url=clone_url)
    cp.save(paths.run_dir(cp.run_id) / "state.json", pid=0)
    RunDir(paths.run_dir(cp.run_id)).event("queued", repo=task.repo, title=task.title)
    return cp.run_id
