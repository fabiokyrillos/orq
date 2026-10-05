from pathlib import Path

import pytest

from orq.git.manager import GitError, GitManager
from tests.conftest import git


@pytest.fixture
def manager() -> GitManager:
    return GitManager()


@pytest.fixture
def repo(manager: GitManager, origin: Path, tmp_path: Path) -> Path:
    return manager.ensure_repo("owner/sandbox", tmp_path / "repos", clone_url=str(origin))


def test_ensure_repo_clones_then_fetches(manager: GitManager, origin: Path, tmp_path: Path) -> None:
    path = manager.ensure_repo("owner/sandbox", tmp_path / "repos", clone_url=str(origin))

    assert path == tmp_path / "repos" / "sandbox"
    assert (path / ".git").is_dir()

    again = manager.ensure_repo("owner/sandbox", tmp_path / "repos", clone_url=str(origin))
    assert again == path


def test_create_worktree_on_new_branch_from_origin_base(manager: GitManager, repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt" / "sandbox" / "R1"

    manager.create_worktree(repo, worktree, branch="orq/add-thing", base="main")

    assert (worktree / "README.md").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree).strip() == "orq/add-thing"
    assert git("config", "core.longpaths", cwd=worktree).strip() == "true"
    assert git("config", "core.autocrlf", cwd=worktree).strip() == "false"


def test_commit_all_returns_sha_and_none_when_clean(manager: GitManager, repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")
    base_sha = manager.head(worktree)

    (worktree / "new.txt").write_text("hello\n", encoding="utf-8")
    sha = manager.commit_all(worktree, "orq(R1) iter 1: add new.txt")

    assert sha is not None and sha != base_sha
    assert manager.head(worktree) == sha
    assert git("log", "-1", "--format=%s", cwd=worktree).strip() == "orq(R1) iter 1: add new.txt"
    assert manager.commit_all(worktree, "nothing") is None


def test_diff_since_commit(manager: GitManager, repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")
    base_sha = manager.head(worktree)
    (worktree / "new.txt").write_text("hello\n", encoding="utf-8")
    manager.commit_all(worktree, "iter 1")

    patch = manager.diff(worktree, since=base_sha)
    stat = manager.diff_stat(worktree, since=base_sha)

    assert "+hello" in patch and "new.txt" in patch
    assert "new.txt" in stat and "1 +" in stat


def test_push_publishes_branch(manager: GitManager, repo: Path, origin: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")
    (worktree / "new.txt").write_text("hello\n", encoding="utf-8")
    manager.commit_all(worktree, "iter 1")

    manager.push(worktree, "orq/x")

    assert "refs/heads/orq/x" in git("ls-remote", "--heads", str(origin), cwd=tmp_path)


def test_remove_worktree_deletes_dir_and_branch(manager: GitManager, repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")

    manager.remove_worktree(repo, worktree, branch="orq/x")

    assert not worktree.exists()
    assert "orq/x" not in git("branch", "--list", cwd=repo)


def test_git_failure_raises_with_stderr(manager: GitManager, tmp_path: Path) -> None:
    with pytest.raises(GitError, match="not a git repository"):
        manager.head(tmp_path)


def test_create_pr_uses_gh_and_returns_url(repo: Path, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_gh(args: list[str], cwd: Path) -> str:
        calls.append(args)
        return "https://github.com/owner/sandbox/pull/7\n"

    manager = GitManager(gh=fake_gh)

    url = manager.create_pr(tmp_path, base="main", head="orq/x", title="Add thing", body="body")

    assert url == "https://github.com/owner/sandbox/pull/7"
    assert calls == [["pr", "create", "--base", "main", "--head", "orq/x", "--title", "Add thing", "--body", "body"]]
