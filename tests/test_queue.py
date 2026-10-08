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


# auto-resume (Phase 6)

def parked(home: OrqPaths, store: Store, repo: str, *, answered: bool, state: RunState = RunState.AWAITING_HUMAN) -> str:
    from orq.core.answers import record_answer
    from orq.core.models import Decision
    run_id = enqueue(home, store, config(), TASK.format(repo=repo))
    decision = Decision(decision_id="D" + run_id[1:5], run_id=run_id, source="reviewer", decision_type="business", question="q?",
                        options=["a", "b"])
    store.add_decision(decision)
    cp = Checkpoint.load(home.run_dir(run_id) / "state.json")
    cp.phase, cp.state = "await", state.value
    cp.pending_decision = {"decision_id": decision.decision_id, "kind": "reviewer", "payload": {}}
    cp.save(home.run_dir(run_id) / "state.json", pid=0)   # its process exited at the decision (--no-prompt, crash, reboot)
    store.set_state(run_id, state)
    if answered:
        record_answer(store, home, decision.decision_id, "0", via="whatsapp")
    return run_id


def test_dispatcher_resumes_answered_runs_without_a_process(home: OrqPaths) -> None:
    store = Store(home.db)
    answered = parked(home, store, "owner/a", answered=True)
    waiting = parked(home, store, "owner/b", answered=False)
    plan = parked(home, store, "owner/c", answered=True, state=RunState.AWAITING_PLAN_APPROVAL)
    paused = enqueue(home, store, config(), TASK.format(repo="owner/d"))
    store.set_state(paused, RunState.PAUSED)
    spawn = Spawner()

    picked = Dispatcher(store, home, config(global_limit=3), spawn=spawn).tick()

    assert sorted(picked) == sorted([answered, plan]) and waiting not in spawn.spawned and paused not in spawn.spawned


def test_answered_runs_go_before_fresh_queued_ones(home: OrqPaths) -> None:
    store = Store(home.db)
    fresh = enqueue(home, store, config(), TASK.format(repo="owner/a"))
    answered = parked(home, store, "owner/b", answered=True)

    assert Dispatcher(store, home, config(global_limit=1), spawn=Spawner()).tick() == [answered]
    assert fresh


# editable queue (Phase 6)

def test_queue_order_and_moves(home: OrqPaths) -> None:
    store = Store(home.db)
    a = enqueue(home, store, config(), TASK.format(repo="owner/a"))
    b = enqueue(home, store, config(), TASK.format(repo="owner/b"))
    c = enqueue(home, store, config(), TASK.format(repo="owner/c"))
    assert [r.run_id for r in store.queued_runs()] == [a, b, c]

    store.move_in_queue(c, "top")
    assert [r.run_id for r in store.queued_runs()] == [c, a, b]
    store.move_in_queue(a, "down")
    assert [r.run_id for r in store.queued_runs()] == [c, b, a]
    store.move_in_queue(c, "bottom")
    store.move_in_queue(b, "up")  # already first: no change
    assert [r.run_id for r in store.queued_runs()] == [b, a, c]

    spawn = Spawner()
    Dispatcher(store, home, config(global_limit=1), spawn=spawn).tick()
    assert spawn.spawned == [b]  # the dispatcher follows the queue order


def test_a_spawned_run_that_registered_and_released_its_slot_no_longer_counts(home: OrqPaths) -> None:
    store = Store(home.db)
    first = enqueue(home, store, config(), TASK.format(repo="owner/a"))
    second = enqueue(home, store, config(), TASK.format(repo="owner/b"))
    spawn = Spawner()
    clock = [100.0]
    dispatcher = Dispatcher(store, home, config(global_limit=1), spawn=spawn, clock=lambda: clock[0], alive=lambda pid: pid == 7)
    assert dispatcher.tick() == [first]

    store.try_acquire_slot(first, "owner/a", 7, fresh=True, global_limit=1, default_project_limit=1, alive=lambda pid: pid == 7)
    clock[0] += 5
    assert dispatcher.tick() == []                     # first holds the only slot
    store.release_slot(first)                           # it parks at a decision (its process stays alive, waiting)
    store.set_state(first, RunState.AWAITING_HUMAN)
    clock[0] += 5                                       # still inside the 60 s spawn grace

    assert dispatcher.tick() == [second]


def test_dispatcher_reads_the_slot_settings_on_every_tick(home: OrqPaths) -> None:
    store = Store(home.db)
    runs = [enqueue(home, store, config(), TASK.format(repo=f"owner/r{i}")) for i in range(3)]
    spawn = Spawner()
    dispatcher = Dispatcher(store, home, config(global_limit=1), spawn=spawn, clock=lambda: 100.0, alive=lambda pid: False)

    assert dispatcher.tick() == runs[:1]
    store.set_settings({"limits.max_concurrent_runs": 3})  # changed in the dashboard (Phase 7.1)
    assert dispatcher.tick() == runs[1:]
