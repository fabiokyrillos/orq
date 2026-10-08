import json
from pathlib import Path

import pytest

from orq.core.discovery import candidates, list_github, scan_local
from orq.core.models import Project
from orq.git.manager import GitError, GitManager
from orq.store.db import Store
from tests.conftest import git


def make_repo(folder: Path, origin: str | None) -> Path:
    folder.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=folder)
    git("config", "user.name", "t", cwd=folder)
    git("config", "user.email", "t@example.invalid", cwd=folder)
    (folder / "README.md").write_text("x\n", encoding="utf-8")
    git("add", "-A", cwd=folder)
    git("commit", "-q", "-m", "init", cwd=folder)
    if origin:
        git("remote", "add", "origin", origin, cwd=folder)
    return folder


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An owner's projects folder: repos at several depths, plus things the scan must leave out."""
    base = tmp_path / "GitHub"
    alpha = make_repo(base / "Alpha Stuff" / "alpha", "https://github.com/owner/alpha.git")
    git("checkout", "-q", "-b", "fix/thing", cwd=alpha)
    (alpha / "wip.txt").write_text("not committed\n", encoding="utf-8")
    beta = make_repo(base / "beta", "git@github.com:owner/beta.git")
    git("worktree", "add", "-q", "-b", "side", str(base / "beta-side"), cwd=beta)  # a linked worktree: .git is a file
    git("worktree", "add", "-q", "-b", "inner", str(beta / ".worktrees" / "inner"), cwd=beta)
    (beta / ".git" / "info" / "exclude").write_text(".worktrees/\n", encoding="utf-8")  # as an owner's .gitignore would
    make_repo(base / "elsewhere", "https://gitlab.com/owner/elsewhere.git")
    make_repo(base / "no-origin", None)
    make_repo(base / "node_modules" / "pkg", "https://github.com/someone/pkg.git")
    make_repo(base / ".cache" / "hidden", "https://github.com/someone/hidden.git")
    make_repo(base / "a" / "b" / "c" / "deep", "https://github.com/owner/deep.git")
    return base


def test_scan_finds_github_repos_and_skips_the_rest(root: Path) -> None:
    found = scan_local([str(root)], 3, GitManager())

    assert [(r.repo, Path(r.path).relative_to(root).as_posix()) for r in found] == [
        ("owner/alpha", "Alpha Stuff/alpha"), ("owner/beta", "beta")]
    alpha = found[0]
    assert (alpha.branch, alpha.dirty) == ("fix/thing", 1)
    assert found[1].dirty == 0


def test_scan_depth_and_missing_roots(root: Path, tmp_path: Path) -> None:
    deeper = scan_local([str(root), str(tmp_path / "nope")], 4, GitManager())
    assert "owner/deep" in [r.repo for r in deeper]
    assert scan_local([str(root)], 1, GitManager())[0].repo == "owner/beta"


def gh_listing(repos: list[dict], calls: list[list[str]] | None = None, fail: bool = False):
    def run(args: list[str], cwd: Path) -> str:
        if calls is not None:
            calls.append(args)
        if fail:
            raise GitError("gh api failed (1): HTTP 401")
        assert args[0] == "api"
        return "".join(json.dumps(r) + "\n" for r in repos)
    return run


REPOS = [
    {"full_name": "owner/old", "private": False, "description": "old one", "pushed_at": "2025-01-01T00:00:00Z", "archived": False},
    {"full_name": "owner/alpha", "private": True, "description": None, "pushed_at": "2026-10-01T00:00:00Z", "archived": False},
    {"full_name": "org/team-app", "private": True, "description": "team", "pushed_at": "2026-09-01T00:00:00Z", "archived": False},
    {"full_name": "owner/attic", "private": False, "description": "", "pushed_at": "2026-10-05T00:00:00Z", "archived": True},
]


def test_list_github_covers_orgs_drops_archived_and_sorts_by_push() -> None:
    calls: list[list[str]] = []
    repos = list_github(gh_listing(REPOS, calls))

    assert [r.repo for r in repos] == ["owner/alpha", "org/team-app", "owner/old"]
    assert repos[0].private and repos[1].description == "team" and repos[0].description == ""
    assert "affiliation=owner,organization_member" in " ".join(calls[0]) and "--paginate" in calls[0]


def test_candidates_mark_added_and_leave_local_repos_out_of_github(root: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    store.upsert_project(Project(repo="owner/beta", name="beta"))
    store.upsert_project(Project(repo="owner/old", name="old"))

    result = candidates(store, GitManager(gh=gh_listing(REPOS)), [str(root)], 3)

    assert result["roots"] == [str(root)]
    assert [(r["repo"], r["added"]) for r in result["local"]] == [("owner/alpha", False), ("owner/beta", True)]
    assert result["local"][0]["private"] is True  # known from the GitHub listing
    assert [(r["repo"], r["added"]) for r in result["github"]] == [("org/team-app", False), ("owner/old", True)]
    assert result["github_error"] is None


def test_candidates_still_list_local_folders_when_gh_fails(root: Path, tmp_path: Path) -> None:
    result = candidates(Store(tmp_path / "orq.db"), GitManager(gh=gh_listing([], fail=True)), [str(root)], 3)

    assert [r["repo"] for r in result["local"]] == ["owner/alpha", "owner/beta"]
    assert result["github"] == [] and "HTTP 401" in result["github_error"]
    assert result["local"][0]["private"] is None


def test_a_removed_project_can_be_added_again(root: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    store.upsert_project(Project(repo="owner/beta", name="beta", status="removed"))
    store.upsert_project(Project(repo="owner/old", name="old", status="archived"))

    result = candidates(store, GitManager(gh=gh_listing(REPOS)), [str(root)], 3)

    assert [(r["repo"], r["added"]) for r in result["local"]] == [("owner/alpha", False), ("owner/beta", False)]
    assert ("owner/old", True) in [(r["repo"], r["added"]) for r in result["github"]]  # archived: restore it instead
