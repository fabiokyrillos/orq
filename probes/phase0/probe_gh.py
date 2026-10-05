"""Phase 0 probe for gh: PR from a worktree, checks polling, squash merge.

The sandbox repo is created by the owner beforehand; this script only pushes to it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from _common import OUT, P0

REPO_NAME = "orq-phase0-sandbox"
CLONE = P0 / "sandbox"
WORKTREE = Path.home() / ".orq" / "worktrees" / REPO_NAME / "R0GH"
BRANCH = "orq/phase0-probe"
WORKFLOW = """name: ci
on:
  push:
    branches: [main]
  pull_request:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -c "print('ok')"
"""
LOG: list[dict] = []


def sh(label: str, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, encoding="utf-8")
    out = (proc.stdout + proc.stderr).strip()
    LOG.append({"label": label, "argv": argv, "cwd": str(cwd), "exit": proc.returncode, "output": out})
    print(f"  [{label}] exit={proc.returncode} {out.replace(chr(10), ' | ')[:400]}")
    return proc


def main() -> None:
    owner = sh("whoami", ["gh", "api", "user", "--jq", ".login"]).stdout.strip()
    slug = f"{owner}/{REPO_NAME}"
    if sh("repo view", ["gh", "repo", "view", slug, "--json", "name,visibility"]).returncode != 0:
        raise SystemExit(f"{slug} does not exist; the owner creates it first")

    if not CLONE.exists():
        sh("clone", ["gh", "repo", "clone", slug, str(CLONE)])
    sh("longpaths", ["git", "config", "core.longpaths", "true"], CLONE)
    sh("pull main", ["git", "pull", "--ff-only", "origin", "main"], CLONE)

    workflow = CLONE / ".github" / "workflows" / "ci.yml"
    if not workflow.exists():
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW, encoding="utf-8", newline="\n")
        sh("add workflow", ["git", "add", "-A"], CLONE)
        sh("commit workflow", ["git", "commit", "-m", "ci: add trivial workflow"], CLONE)
        sh("push main", ["git", "push", "origin", "main"], CLONE)

    sh("worktree add", ["git", "worktree", "add", "-b", BRANCH, str(WORKTREE), "main"], CLONE)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (WORKTREE / "probe.txt").write_text(f"probe {stamp}\n", encoding="utf-8", newline="\n")
    sh("add", ["git", "add", "-A"], WORKTREE)
    sh("commit", ["git", "commit", "-m", "orq(R0GH) iter 1: probe change"], WORKTREE)
    gitleaks = shutil.which("gitleaks")
    if gitleaks:
        sh("gitleaks pre-push", [gitleaks, "git", "--log-opts=origin/main..HEAD", "--redact", "--no-banner", "."], WORKTREE)
    sh("push branch", ["git", "push", "-u", "origin", BRANCH], WORKTREE)

    created = sh("pr create", ["gh", "pr", "create", "--base", "main", "--head", BRANCH,
                               "--title", "orq phase0 probe", "--body", "Automated Phase 0 probe PR."], WORKTREE)
    number = created.stdout.strip().rsplit("/", 1)[-1]

    # Poll immediately to capture the "no checks yet" race.
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        checks = sh("pr checks", ["gh", "pr", "checks", number, "--json", "name,state,bucket"], WORKTREE)
        try:
            rows = json.loads(checks.stdout or "[]")
        except json.JSONDecodeError:
            rows = []
        if rows and all(r.get("bucket") != "pending" for r in rows):
            break
        time.sleep(10)
    sh("pr view pre-merge", ["gh", "pr", "view", number, "--json", "state,mergeable,mergeStateStatus"], WORKTREE)

    sh("pr merge (from worktree)", ["gh", "pr", "merge", number, "--squash", "--delete-branch"], WORKTREE)
    sh("pr view post-merge", ["gh", "pr", "view", number, "--repo", slug, "--json", "state,mergeCommit"])
    sh("remote heads", ["git", "ls-remote", "--heads", "origin"], CLONE)
    sh("local branches", ["git", "branch", "--list"], CLONE)
    sh("worktree list", ["git", "worktree", "list"], CLONE)

    sh("worktree remove", ["git", "worktree", "remove", "--force", str(WORKTREE)], CLONE)
    sh("branch delete", ["git", "branch", "-D", BRANCH], CLONE)
    sh("worktree prune", ["git", "worktree", "prune"], CLONE)

    (OUT / "h-gh-log.json").write_text(json.dumps(LOG, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
