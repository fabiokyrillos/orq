import subprocess
from pathlib import Path

import pytest


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A local bare repo with one commit on main, standing in for GitHub."""
    bare = tmp_path / "origin.git"
    git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed), cwd=tmp_path)
    git("config", "user.name", "seed", cwd=seed)
    git("config", "user.email", "seed@example.invalid", cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    return bare
