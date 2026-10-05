"""Create (or reset) the local throwaway git repo used by the Phase 0 probes."""

from __future__ import annotations

import shutil
import stat

from _common import SCRATCH, git


def _force_remove(func, path, _exc):
    # .git objects are read-only on Windows
    import os

    os.chmod(path, stat.S_IWRITE)
    func(path)


def main() -> None:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, onerror=_force_remove)
    SCRATCH.mkdir(parents=True)
    git("init", "-b", "main", cwd=SCRATCH)
    git("config", "user.name", "orq-probe", cwd=SCRATCH)
    git("config", "user.email", "orq-probe@example.invalid", cwd=SCRATCH)
    (SCRATCH / "app.py").write_text(
        "def price_with_tax(price: float, tax_rate: float) -> float:\n    return price * (1 + tax_rate)\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=SCRATCH)
    git("commit", "-m", "first commit", cwd=SCRATCH)
    (SCRATCH / "notes.txt").write_text("second commit marker\n", encoding="utf-8")
    git("add", "-A", cwd=SCRATCH)
    git("commit", "-m", "second commit", cwd=SCRATCH)
    print(git("log", "--oneline", cwd=SCRATCH).stdout)


if __name__ == "__main__":
    main()
