"""Phase 0 probe: git worktree whose files exceed MAX_PATH (260), with and without core.longpaths."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from _common import P0, git

REPO = P0 / "lp"
WORKTREE = Path.home() / ".orq" / "worktrees" / "orq-phase0-sandbox" / "R0TEST"
# 4 segments x 42 chars: short enough inside REPO, past 260 inside WORKTREE.
DEEP_REL = Path(*["d" * 42] * 4) / "deep_file_with_a_reasonably_long_name.txt"


def show(label: str, proc: subprocess.CompletedProcess) -> None:
    text = (proc.stdout + proc.stderr).strip().replace("\n", " | ")
    print(f"  {label}: exit={proc.returncode} {text[:300]}")


def remove_worktree() -> None:
    show("worktree remove", git("worktree", "remove", "--force", str(WORKTREE), cwd=REPO))
    git("worktree", "prune", cwd=REPO)
    git("branch", "-D", "orq/phase0-longpath", cwd=REPO)
    print("  worktree dir still exists:", WORKTREE.exists())


def main() -> None:
    if not REPO.exists():
        REPO.mkdir(parents=True)
        git("init", "-b", "main", cwd=REPO)
        git("config", "user.name", "orq-probe", cwd=REPO)
        git("config", "user.email", "orq-probe@example.invalid", cwd=REPO)
        deep = REPO / DEEP_REL
        deep.parent.mkdir(parents=True)
        deep.write_text("deep content\n", encoding="utf-8")
        git("add", "-A", cwd=REPO)
        show("commit", git("commit", "-q", "-m", "deep file", cwd=REPO))

    target = WORKTREE / DEEP_REL
    print("source path length:", len(str(REPO / DEEP_REL)), " worktree path length:", len(str(target)))

    print("\n--- negative control: core.longpaths unset")
    git("config", "--unset", "core.longpaths", cwd=REPO)
    show("worktree add", git("worktree", "add", "-b", "orq/phase0-longpath", str(WORKTREE), "main", cwd=REPO))
    show("status", git("status", "--porcelain", cwd=WORKTREE) if WORKTREE.exists() else git("worktree", "list", cwd=REPO))
    remove_worktree()

    print("\n--- core.longpaths=true")
    git("config", "core.longpaths", "true", cwd=REPO)
    show("worktree add", git("worktree", "add", "-b", "orq/phase0-longpath", str(WORKTREE), "main", cwd=REPO))
    show("status", git("status", "--porcelain", cwd=WORKTREE))

    print("\n--- tool compatibility on the deep file")
    for label, path in (("python plain", str(target)), ("python \\\\?\\ prefix", "\\\\?\\" + str(target))):
        try:
            print(f"  {label}: OK {Path(path).read_text(encoding='utf-8').strip()!r}")
        except OSError as exc:
            print(f"  {label}: FAIL {type(exc).__name__}: {exc.strerror}")
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Get-Content -LiteralPath '{target}'"],
        capture_output=True, text=True,
    )
    show("powershell 5.1 Get-Content", ps)
    gitleaks = shutil.which("gitleaks")
    if gitleaks:
        gl = subprocess.run([gitleaks, "dir", "--no-banner", str(WORKTREE)], capture_output=True, text=True)
        show("gitleaks dir", gl)

    print("\n--- cleanup")
    remove_worktree()


if __name__ == "__main__":
    main()
