import os
import subprocess
import sys
from pathlib import Path

import pytest

from orq.core.checkpoint import Checkpoint
from orq.core.procs import kill_tree, pid_alive


def test_checkpoint_roundtrip_and_atomic_write(tmp_path: Path) -> None:
    cp = Checkpoint(run_id="R1", state="IMPLEMENTING", phase="implement", iteration=2, branch="orq/x",
                    worktree="C:/wt", repo_path="C:/repo", base_commit="b", last_commit="c", commits={"1": "c"})
    cp.outcome = {"next_prompt": "go", "milestone": "m", "done": False}
    cp.pending_decision = {"decision_id": "D1", "kind": "guard_pre", "payload": {"remaining": []}}
    path = tmp_path / "state.json"

    cp.save(path)

    assert not (tmp_path / "state.json.tmp").exists()
    loaded = Checkpoint.load(path)
    assert loaded == cp and loaded.version == 1 and loaded.pid == os.getpid()


def test_missing_checkpoint_returns_none(tmp_path: Path) -> None:
    assert Checkpoint.load(tmp_path / "nope.json") is None


def test_unknown_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version": 99}', encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        Checkpoint.load(path)


def test_pid_alive_and_kill_tree() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert pid_alive(proc.pid)
        kill_tree(proc.pid)
        proc.wait(timeout=10)
        assert not pid_alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not pid_alive(999999)
    assert not pid_alive(None)
