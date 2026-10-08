"""Projects (Phase 5): one GitHub repo each, identified by `owner/repo`. orq always works in its own clone.

A local folder is only a shortcut to find the `owner/repo` behind its `origin` remote; orq never creates worktrees there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

from orq.config import Config
from orq.core.checkpoint import Checkpoint
from orq.core.models import Project, RunState
from orq.git.manager import GitError, GitManager
from orq.paths import OrqPaths
from orq.store.db import Store

FINISHED = (RunState.DONE, RunState.FAILED, RunState.ABORTED)

_REMOTE_RE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)(?P<slug>[\w.-]+/[\w.-]+?)(?:\.git)?/?$")
_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class ProjectError(ValueError):
    pass


def parse_remote(url: str) -> str:
    match = _REMOTE_RE.match(url.strip())
    if not match:
        raise ProjectError(f"not a GitHub remote: {url.strip()!r}")
    return match.group("slug")


def resolve_source(source: str, git: GitManager) -> tuple[str, str | None]:
    """`owner/repo` -> (slug, None); a local folder -> (slug of its origin, folder)."""
    text = source.strip()
    folder = Path(text).expanduser()
    if folder.is_dir():
        try:
            url = git.git("remote", "get-url", "origin", cwd=folder)
        except GitError as exc:
            raise ProjectError(f"{folder} has no origin remote: {exc}") from exc
        return parse_remote(url), str(folder)
    if _SLUG_RE.match(text):
        return text, None
    raise ProjectError(f"give owner/repo or a local folder with a GitHub origin, got {text!r}")


def add_project(store: Store, git: GitManager, source: str, *, name: str | None = None, base_branch: str | None = None,
                check_command: str | None = None, max_concurrent: int | None = None) -> Project:
    """Resolve, validate with `gh repo view` and register a project. An existing project only gets the values given."""
    slug, local_path = resolve_source(source, git)
    try:
        info = json.loads(git.gh(["repo", "view", slug, "--json", "nameWithOwner,defaultBranchRef"], Path.home()))
    except GitError as exc:
        raise ProjectError(str(exc)) from exc
    repo = str(info.get("nameWithOwner") or slug)
    existing = store.get_project(repo)
    if existing is not None:
        changes = {k: v for k, v in (("name", name), ("base_branch", base_branch), ("check_command", check_command),
                                     ("max_concurrent", max_concurrent), ("local_path", local_path)) if v is not None}
        if existing.status != "active":
            changes["status"] = "active"  # Phase 7.1: adding an archived or removed project brings it back
        if changes:
            store.update_project(repo, **changes)
    else:
        default_branch = ((info.get("defaultBranchRef") or {}).get("name")) or "main"
        store.upsert_project(Project(repo=repo, name=name or repo.split("/")[-1], base_branch=base_branch or default_branch,
                                     check_command=check_command or "", max_concurrent=max_concurrent, local_path=local_path))
    stored = store.get_project(repo)
    assert stored is not None
    return stored


# Phase 7.1: pin, archive, remove. orq never touches the owner's folder here either.


def _get(store: Store, repo: str) -> Project:
    project = store.get_project(repo)
    if project is None:
        raise ProjectError(f"no project {repo}")
    return project


def _refuse_live_runs(store: Store, repo: str, action: str) -> None:
    live = [run.run_id for run in store.list_runs() if run.repo == repo and run.state not in FINISHED]
    if live:
        raise ProjectError(f"cannot {action} {repo} while these runs are not finished: {', '.join(live)}")


def set_pinned(store: Store, repo: str, pinned: bool) -> None:
    _get(store, repo)
    store.update_project(repo, pinned=pinned)


def archive_project(store: Store, repo: str) -> None:
    """Hide the project; its clone and settings stay, and restoring brings it back as it was."""
    _get(store, repo)
    _refuse_live_runs(store, repo, "archive")
    store.update_project(repo, status="archived")


def restore_project(store: Store, repo: str) -> None:
    _get(store, repo)
    store.update_project(repo, status="active")


def _rmtree(path: Path) -> None:
    def writable_then_retry(func, name, _exc) -> None:  # git writes its packs read-only
        os.chmod(name, stat.S_IWRITE)
        func(name)

    shutil.rmtree(path, onexc=writable_then_retry)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def remove_project(store: Store, paths: OrqPaths, config: Config, repo: str) -> None:
    """Delete orq's clone and the worktrees of the project's runs, then hide the project. Run history stays.

    Only paths under orq's own roots are deleted; the owner's folder (local_path) is forgotten, never touched.
    """
    _get(store, repo)
    _refuse_live_runs(store, repo, "remove")
    for run in store.list_runs():
        if run.repo != repo:
            continue
        cp = Checkpoint.try_load(paths.run_dir(run.run_id) / "state.json")
        worktree = Path(cp.worktree) if cp and cp.worktree else None
        if worktree and worktree.exists() and _inside(worktree, config.git.worktree_root):
            _rmtree(worktree)
    owner, name = repo.split("/", 1)
    chat_checkout = config.git.worktree_root / name / "_chat"  # the conversations' checkout; their history stays
    if chat_checkout.exists():
        _rmtree(chat_checkout)
    clone = paths.repos / owner / name
    if clone.exists() and _inside(clone, paths.repos):
        _rmtree(clone)
    clone.with_name(name + ".orq.lock").unlink(missing_ok=True)
    store.update_project(repo, status="removed", local_path=None, pinned=False)
