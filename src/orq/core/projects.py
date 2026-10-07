"""Projects (Phase 5): one GitHub repo each, identified by `owner/repo`. orq always works in its own clone.

A local folder is only a shortcut to find the `owner/repo` behind its `origin` remote; orq never creates worktrees there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from orq.core.models import Project
from orq.git.manager import GitError, GitManager
from orq.store.db import Store

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
        if changes:
            store.update_project(repo, **changes)
    else:
        default_branch = ((info.get("defaultBranchRef") or {}).get("name")) or "main"
        store.upsert_project(Project(repo=repo, name=name or repo.split("/")[-1], base_branch=base_branch or default_branch,
                                     check_command=check_command or "", max_concurrent=max_concurrent, local_path=local_path))
    stored = store.get_project(repo)
    assert stored is not None
    return stored
