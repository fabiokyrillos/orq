import json
from pathlib import Path

import pytest

from orq.core.models import RunRecord
from orq.core.projects import ProjectError, add_project, parse_remote, resolve_source
from orq.git.manager import GitError, GitManager
from orq.store.db import Store
from tests.conftest import git


def fake_gh(calls: list[list[str]], *, missing: bool = False, default_branch: str = "main"):
    def run(args: list[str], cwd: Path) -> str:
        calls.append(args)
        if args[:2] == ["repo", "view"]:
            if missing:
                raise GitError("gh repo view failed (1): Could not resolve to a Repository")
            return json.dumps({"nameWithOwner": args[2], "defaultBranchRef": {"name": default_branch}})
        raise AssertionError(f"unexpected gh call {args}")
    return run


@pytest.mark.parametrize("url", [
    "https://github.com/fabiokyrillos/orq-phase0-sandbox.git",
    "https://github.com/fabiokyrillos/orq-phase0-sandbox",
    "git@github.com:fabiokyrillos/orq-phase0-sandbox.git",
    "ssh://git@github.com/fabiokyrillos/orq-phase0-sandbox.git",
])
def test_parse_remote_accepts_github_urls(url: str) -> None:
    assert parse_remote(url) == "fabiokyrillos/orq-phase0-sandbox"


def test_parse_remote_rejects_other_hosts() -> None:
    with pytest.raises(ProjectError):
        parse_remote("https://gitlab.com/a/b.git")


def test_resolve_source_from_slug_and_folder(tmp_path: Path) -> None:
    assert resolve_source("owner/repo", GitManager()) == ("owner/repo", None)
    folder = tmp_path / "work"
    folder.mkdir()
    git("init", "-q", cwd=folder)
    git("remote", "add", "origin", "git@github.com:owner/from-folder.git", cwd=folder)

    assert resolve_source(str(folder), GitManager()) == ("owner/from-folder", str(folder))


def test_resolve_source_rejects_folder_without_github_origin(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    git("init", "-q", cwd=folder)
    with pytest.raises(ProjectError, match="origin"):
        resolve_source(str(folder), GitManager())
    with pytest.raises(ProjectError):
        resolve_source("not a repo", GitManager())


def test_add_project_validates_with_gh_and_takes_default_branch(tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    calls: list[list[str]] = []

    project = add_project(store, GitManager(gh=fake_gh(calls, default_branch="trunk")), "owner/repo", check_command="uv run pytest -q")

    assert calls[0][:3] == ["repo", "view", "owner/repo"]
    assert project.repo == "owner/repo" and project.name == "repo" and project.base_branch == "trunk"
    assert store.get_project("owner/repo") == project
    assert project.check_command == "uv run pytest -q" and project.max_concurrent is None


def test_add_project_refuses_unknown_repo(tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    with pytest.raises(ProjectError, match="Could not resolve"):
        add_project(store, GitManager(gh=fake_gh([], missing=True)), "owner/missing")
    assert store.list_projects() == []


def test_adding_an_existing_project_updates_only_what_is_given(tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    store.create_run(RunRecord(run_id="R1", repo="owner/repo", task_title="t", branch="b"))  # registered by its runs

    project = add_project(store, GitManager(gh=fake_gh([])), "owner/repo", check_command="pytest")
    again = add_project(store, GitManager(gh=fake_gh([])), "owner/repo", name="Nice name")

    assert project.check_command == "pytest"
    assert (again.name, again.check_command) == ("Nice name", "pytest")
    assert len(store.list_projects()) == 1


def test_runs_register_their_project(tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    store.create_run(RunRecord(run_id="R1", repo="owner/legacy", task_title="t", branch="b"))

    project = store.get_project("owner/legacy")
    assert project is not None and project.name == "legacy" and project.base_branch == "main"


def test_store_open_registers_projects_of_existing_runs(tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    store.create_run(RunRecord(run_id="R1", repo="owner/old", task_title="t", branch="b"))
    store._conn.execute("DELETE FROM projects")  # a database from before Phase 5
    store._conn.commit()

    reopened = Store(tmp_path / "orq.db")

    assert [p.repo for p in reopened.list_projects()] == ["owner/old"]


def test_update_project(tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    add_project(store, GitManager(gh=fake_gh([])), "owner/repo")

    store.update_project("owner/repo", name="Repo", max_concurrent=2, check_command="make test")

    project = store.get_project("owner/repo")
    assert project is not None and (project.name, project.max_concurrent, project.check_command) == ("Repo", 2, "make test")


# Phase 7.1: pin, archive, remove


from orq.config import Config  # noqa: E402
from orq.core.checkpoint import Checkpoint  # noqa: E402
from orq.core.models import Project, RunState  # noqa: E402
from orq.core.projects import archive_project, remove_project, restore_project, set_pinned  # noqa: E402
from orq.paths import OrqPaths  # noqa: E402


def finished_run(store: Store, paths: OrqPaths, run_id: str, repo: str, state: RunState, worktree: Path | None = None) -> None:
    store.create_run(RunRecord(run_id=run_id, repo=repo, task_title="t", branch="b"))
    store.set_state(run_id, state)
    paths.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    Checkpoint(run_id=run_id, state=state.value, phase="done", branch="b", worktree=str(worktree or "")).save(
        paths.run_dir(run_id) / "state.json")


def test_status_and_pin_round_trip_and_survive_upsert(tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    add_project(store, GitManager(gh=fake_gh([])), "owner/repo")
    set_pinned(store, "owner/repo", True)
    store.update_project("owner/repo", name="Renamed")

    project = store.get_project("owner/repo")
    assert (project.status, project.pinned, project.name) == ("active", True, "Renamed")
    assert Store(tmp_path / "orq.db").get_project("owner/repo").pinned is True


def test_archive_and_restore_refuse_while_a_run_is_live(tmp_path: Path) -> None:
    store, paths = Store(tmp_path / "orq.db"), OrqPaths(tmp_path / "home")
    add_project(store, GitManager(gh=fake_gh([])), "owner/repo")
    store.create_run(RunRecord(run_id="RLIVE1", repo="owner/repo", task_title="t", branch="b"))  # QUEUED

    with pytest.raises(ProjectError, match="RLIVE1"):
        archive_project(store, "owner/repo")
    store.set_state("RLIVE1", RunState.DONE)
    archive_project(store, "owner/repo")
    assert store.get_project("owner/repo").status == "archived"
    restore_project(store, "owner/repo")
    assert store.get_project("owner/repo").status == "active"


def test_remove_deletes_orq_clone_and_run_worktrees_but_never_the_owner_folder(tmp_path: Path) -> None:
    store, paths, config = Store(tmp_path / "orq.db"), OrqPaths(tmp_path / "home"), Config()
    config.git.worktree_root = tmp_path / "wt"
    owner_folder = tmp_path / "Projetos" / "repo"
    (owner_folder / ".git").mkdir(parents=True)
    clone = paths.repos / "owner" / "repo"
    (clone / ".git" / "objects").mkdir(parents=True)
    readonly = clone / ".git" / "objects" / "pack.idx"
    readonly.write_text("x", encoding="utf-8")
    readonly.chmod(0o444)  # git writes its packs read-only; removal must cope
    paths.repos.joinpath("owner", "repo.orq.lock").write_text("", encoding="utf-8")
    worktree = config.git.worktree_root / "repo" / "RDONE1"
    worktree.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    store.upsert_project(Project(repo="owner/repo", name="repo", local_path=str(owner_folder)))
    finished_run(store, paths, "RDONE1", "owner/repo", RunState.DONE, worktree)
    finished_run(store, paths, "RODD01", "owner/repo", RunState.FAILED, outside)  # a path outside worktree_root stays

    remove_project(store, paths, config, "owner/repo")

    assert not clone.exists() and not paths.repos.joinpath("owner", "repo.orq.lock").exists()
    assert not worktree.exists() and outside.exists() and (owner_folder / ".git").is_dir()
    project = store.get_project("owner/repo")
    assert (project.status, project.local_path) == ("removed", None)
    assert store.get_run("RDONE1") is not None  # history stays


def test_remove_refuses_while_a_run_waits_for_the_owner(tmp_path: Path) -> None:
    store, paths = Store(tmp_path / "orq.db"), OrqPaths(tmp_path / "home")
    add_project(store, GitManager(gh=fake_gh([])), "owner/repo")
    finished_run(store, paths, "RWAIT1", "owner/repo", RunState.AWAITING_HUMAN)

    with pytest.raises(ProjectError, match="RWAIT1"):
        remove_project(store, paths, Config(), "owner/repo")
    assert store.get_project("owner/repo").status == "active"


def test_adding_a_removed_or_archived_project_makes_it_active_again(tmp_path: Path) -> None:
    store = Store(tmp_path / "orq.db")
    add_project(store, GitManager(gh=fake_gh([])), "owner/repo")
    archive_project(store, "owner/repo")
    assert add_project(store, GitManager(gh=fake_gh([])), "owner/repo").status == "active"
    store.update_project("owner/repo", status="removed")
    assert add_project(store, GitManager(gh=fake_gh([])), "owner/repo").status == "active"
