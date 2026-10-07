"""Queue (Phase 5): enqueue builds a resumable run without a process; the hub dispatcher spawns queued runs within limits."""

from pathlib import Path

import pytest

from orq.config import Config
from orq.core.checkpoint import Checkpoint
from orq.core.models import RunState
from orq.core.queue import QueueError, enqueue
from orq.core.task import TaskError
from orq.hub.dispatcher import Dispatcher
from orq.paths import OrqPaths
from orq.store.db import Store

TASK = """# Task: Add greeting
## Repo
{repo}, base branch main
## Goal
Add greeting.txt.
## Acceptance criteria
- [ ] greeting.txt exists
## Check command
python -c "print('ok')"
## Plan approval
skip
"""


@pytest.fixture
def home(tmp_path: Path) -> OrqPaths:
    paths = OrqPaths(tmp_path / "home")
    paths.root.mkdir()
    return paths


def config(global_limit: int = 2, project: int = 1) -> Config:
    cfg = Config()
    cfg.limits.max_concurrent_runs = global_limit
    cfg.queue.project_concurrency = project
    cfg.git.worktree_root = Path("C:/orq-wt-test")
    return cfg


def test_enqueue_creates_a_resumable_run_without_a_process(home: OrqPaths) -> None:
    store = Store(home.db)

    run_id = enqueue(home, store, config(), TASK.format(repo="owner/a"), clone_url="C:/somewhere/origin.git")

    run = store.get_run(run_id)
    assert run is not None and run.state is RunState.QUEUED and run.repo == "owner/a"
    cp = Checkpoint.load(home.run_dir(run_id) / "state.json")
    assert cp is not None and cp.phase == "setup" and cp.pid == 0 and cp.clone_url == "C:/somewhere/origin.git"
    assert cp.branch == "orq/add-greeting" and cp.worktree.replace("\\", "/").endswith(f"/a/{run_id}")
    assert (home.run_dir(run_id) / "TASK.md").read_text(encoding="utf-8").startswith("# Task: Add greeting")
    assert '"queued"' in (home.run_dir(run_id) / "events.jsonl").read_text(encoding="utf-8")
    assert store.get_project("owner/a") is not None


def test_enqueue_rejects_invalid_task_and_sandbox_mismatch(home: OrqPaths) -> None:
    store = Store(home.db)
    with pytest.raises(TaskError):
        enqueue(home, store, config(), "# Task: nothing else")
    cfg = config()
    cfg.git.sandbox_repos = ["owner/other"]
    with pytest.raises(QueueError, match="sandbox_repos"):
        enqueue(home, store, cfg, TASK.format(repo="owner/a"))
    assert store.list_runs() == []


class Spawner:
    def __init__(self) -> None:
        self.spawned: list[str] = []

    def __call__(self, paths: OrqPaths, run_id: str) -> int:
        self.spawned.append(run_id)
        return 4242


def test_dispatcher_spawns_within_global_and_project_limits(home: OrqPaths) -> None:
    store = Store(home.db)
    a1 = enqueue(home, store, config(), TASK.format(repo="owner/a"))
    a2 = enqueue(home, store, config(), TASK.format(repo="owner/a"))
    b1 = enqueue(home, store, config(), TASK.format(repo="owner/b"))
    c1 = enqueue(home, store, config(), TASK.format(repo="owner/c"))
    spawn = Spawner()
    clock = [100.0]
    dispatcher = Dispatcher(store, home, config(global_limit=2), spawn=spawn, clock=lambda: clock[0], alive=lambda pid: pid == 7)

    assert dispatcher.tick() == [a1, b1]          # a2 waits for project owner/a, c1 for the global limit
    assert dispatcher.tick() == []                 # just spawned: not again, and they count as taking slots

    # a1's process took its slot; b1 never showed up (crashed before registering) and is retried after the grace period
    store.try_acquire_slot(a1, "owner/a", 7, fresh=True, global_limit=2, default_project_limit=1, alive=lambda pid: pid == 7)
    clock[0] += 61
    assert dispatcher.tick() == [b1]
    assert a2 not in spawn.spawned and c1 not in spawn.spawned


def test_dispatcher_prefers_runs_that_already_started_and_skips_live_ones(home: OrqPaths) -> None:
    store = Store(home.db)
    fresh = enqueue(home, store, config(), TASK.format(repo="owner/a"))
    started = enqueue(home, store, config(), TASK.format(repo="owner/b"))
    live = enqueue(home, store, config(), TASK.format(repo="owner/c"))
    cp = Checkpoint.load(home.run_dir(started) / "state.json")
    cp.phase = "implement"                          # waited for a slot after an answer, then the PC restarted
    cp.save(home.run_dir(started) / "state.json", pid=0)
    cp = Checkpoint.load(home.run_dir(live) / "state.json")
    cp.save(home.run_dir(live) / "state.json", pid=7)  # its own process is up and waiting for a slot
    spawn = Spawner()

    picked = Dispatcher(store, home, config(global_limit=1), spawn=spawn, alive=lambda pid: pid == 7).tick()

    assert picked == [started]
    assert fresh not in spawn.spawned and live not in spawn.spawned


def test_dispatcher_ignores_runs_that_are_not_queued(home: OrqPaths) -> None:
    store = Store(home.db)
    run_id = enqueue(home, store, config(), TASK.format(repo="owner/a"))
    store.set_state(run_id, RunState.ABORTED)

    assert Dispatcher(store, home, config(), spawn=Spawner()).tick() == []
