import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from orq.core.locks import LockTimeout, file_lock

HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from orq.core.locks import file_lock
    with file_lock(Path(sys.argv[1])):
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
""")


def hold_in_child(path: Path, seconds: float) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(path), str(seconds)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


def test_lock_is_reentrant_free_after_release(tmp_path: Path) -> None:
    path = tmp_path / "repo.orq.lock"
    with file_lock(path):
        pass
    with file_lock(path, timeout=0.5):
        pass


def test_second_process_waits_then_times_out(tmp_path: Path) -> None:
    path = tmp_path / "repo.orq.lock"
    child = hold_in_child(path, 5)
    try:
        started = time.monotonic()
        with pytest.raises(LockTimeout):
            with file_lock(path, timeout=0.6, poll=0.1):
                pass
        assert time.monotonic() - started >= 0.5
    finally:
        child.kill()
        child.wait()


def test_lock_released_when_holder_dies(tmp_path: Path) -> None:
    path = tmp_path / "repo.orq.lock"
    child = hold_in_child(path, 30)
    child.kill()
    child.wait()

    with file_lock(path, timeout=2, poll=0.05):
        pass
