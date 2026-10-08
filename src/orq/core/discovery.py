"""Project discovery (Phase 7): the owner's local checkouts first, then the GitHub repos that are not on this PC.

Scanning only reads the owner's folders: `git remote`, `git branch` and `git --no-optional-locks status`, which does
not refresh the owner's index.
"""

from __future__ import annotations

import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from orq.core.projects import ProjectError, parse_remote
from orq.git.manager import GitError, GitManager, GhRunner
from orq.store.db import Store

# Folders that never hold the owner's checkouts, or are too big to walk.
SKIP_DIRS = {"node_modules", "venv", "dist", "build", "target", "__pycache__", "site-packages"}
GITHUB_REPOS = "user/repos?per_page=100&affiliation=owner,organization_member"
GITHUB_FIELDS = ".[] | {full_name, private, description, pushed_at, archived}"


@dataclass(frozen=True)
class LocalRepo:
    path: str
    repo: str      # owner/repo of its origin
    branch: str    # empty on a detached HEAD
    dirty: int     # uncommitted entries (`git status --porcelain` lines)


@dataclass(frozen=True)
class RemoteRepo:
    repo: str
    private: bool
    description: str
    pushed_at: str


def scan_local(roots: list[str], depth: int, git: GitManager) -> list[LocalRepo]:
    """Checkouts with a GitHub origin under `roots`, up to `depth` folder levels down.

    A folder with a `.git` directory is a checkout and the walk stops there. A `.git` file (linked worktree,
    submodule) is skipped: its main repo is the one to list. Hidden folders and SKIP_DIRS are not walked.
    """
    checkouts: dict[str, Path] = {}
    for root in roots:
        start = Path(root).expanduser()
        if not start.is_dir():
            continue
        queue: deque[tuple[Path, int]] = deque((child, 1) for child in _subdirs(start))
        while queue:
            folder, level = queue.popleft()
            dot_git = folder / ".git"
            if dot_git.is_dir():
                checkouts.setdefault(str(folder.resolve()).lower(), folder)
                continue
            if dot_git.exists() or level >= depth:
                continue
            queue.extend((child, level + 1) for child in _subdirs(folder))
    # One checkout at a time: concurrent git children on Windows sometimes stall ~5 s each (Phase 7 finding).
    found = [r for r in (_local_repo(folder, git) for folder in checkouts.values()) if r is not None]
    return sorted(found, key=lambda r: (r.repo.lower(), r.path.lower()))


def _subdirs(folder: Path) -> list[Path]:
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return []
    return [c for c in children if c.is_dir() and not c.name.startswith(".") and c.name.lower() not in SKIP_DIRS]


def _local_repo(folder: Path, git: GitManager) -> LocalRepo | None:
    try:
        repo = parse_remote(git.git("remote", "get-url", "origin", cwd=folder))
        branch = git.git("branch", "--show-current", cwd=folder).strip()
        status = git.git("--no-optional-locks", "status", "--porcelain", cwd=folder)
    except (GitError, ProjectError):
        return None
    return LocalRepo(path=str(folder), repo=repo, branch=branch, dirty=len([l for l in status.splitlines() if l.strip()]))


def list_github(gh: GhRunner) -> list[RemoteRepo]:
    """The owner's repos and those of the owner's organizations, without archived ones, newest push first."""
    out = gh(["api", "--paginate", GITHUB_REPOS, "--jq", GITHUB_FIELDS], Path.home())
    repos = []
    for line in out.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("archived"):
            continue
        repos.append(RemoteRepo(repo=item["full_name"], private=bool(item.get("private")),
                                description=item.get("description") or "", pushed_at=item.get("pushed_at") or ""))
    return sorted(repos, key=lambda r: r.pushed_at, reverse=True)


def candidates(store: Store, git: GitManager, roots: list[str], depth: int) -> dict:
    """What the Add project page offers: local folders first, then GitHub repos that are not on this PC."""
    added = {p.repo.lower() for p in store.list_projects() if p.status != "removed"}  # a removed project can come back
    with ThreadPoolExecutor(max_workers=1) as pool:
        listing = pool.submit(list_github, git.gh)  # gh runs while the folders are scanned
        local = scan_local(roots, depth, git)
        try:
            remote, error = listing.result(), None
        except (GitError, json.JSONDecodeError, KeyError) as exc:
            remote, error = [], str(exc)
    private = {r.repo.lower(): r.private for r in remote}
    on_pc = {r.repo.lower() for r in local}
    return {
        "roots": list(roots),
        "local": [{**asdict(r), "added": r.repo.lower() in added, "private": private.get(r.repo.lower())} for r in local],
        "github": [{**asdict(r), "added": r.repo.lower() in added} for r in remote if r.repo.lower() not in on_pc],
        "github_error": error,
    }
