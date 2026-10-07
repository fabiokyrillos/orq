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

    assert path == tmp_path / "repos" / "owner" / "sandbox"  # owner kept: two owners may both have a `sandbox`
    assert (path / ".git").is_dir()

    again = manager.ensure_repo("owner/sandbox", tmp_path / "repos", clone_url=str(origin))
    assert again == path


def test_lock_path_is_next_to_the_clone_for_repo_and_worktree(manager: GitManager, repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt" / "sandbox" / "R1"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")

    expected = repo.parent / "sandbox.orq.lock"
    assert manager.lock_path(repo) == expected
    assert manager.lock_path(worktree) == expected


def test_shared_ref_writes_wait_for_the_repo_lock(origin: Path, repo: Path, tmp_path: Path) -> None:
    from orq.core.locks import LockTimeout
    from tests.test_locks import hold_in_child

    manager = GitManager(lock_timeout=0.5)
    worktree = tmp_path / "wt" / "sandbox" / "R1"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")
    child = hold_in_child(manager.lock_path(repo), 10)
    try:
        with pytest.raises(LockTimeout):
            manager.push(worktree, "orq/x")
        with pytest.raises(LockTimeout):
            manager.ensure_repo("owner/sandbox", tmp_path / "repos", clone_url=str(origin))
        assert manager.head(worktree)  # read-only commands do not take the lock
    finally:
        child.kill()
        child.wait()


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


# Phase 3: gate operations


def seed_push(origin: Path, tmp_path: Path, name: str, content: str) -> None:
    seed = tmp_path / "seed"
    (seed / name).write_text(content, encoding="utf-8")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", f"seed {name}", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)


def test_base_moved_and_clean_rebase(manager: GitManager, repo: Path, origin: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")
    (worktree / "new.txt").write_text("hello\n", encoding="utf-8")
    manager.commit_all(worktree, "iter 1")
    assert manager.base_moved(worktree, "main") is False

    seed_push(origin, tmp_path, "other.txt", "other\n")

    assert manager.base_moved(worktree, "main") is True
    assert manager.rebase_onto_base(worktree, "main") is True
    assert (worktree / "other.txt").exists() and (worktree / "new.txt").exists()
    assert manager.base_moved(worktree, "main") is False


def test_conflicting_rebase_is_aborted(manager: GitManager, repo: Path, origin: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")
    (worktree / "README.md").write_text("ours\n", encoding="utf-8")
    manager.commit_all(worktree, "iter 1")
    seed_push(origin, tmp_path, "README.md", "theirs\n")

    assert manager.rebase_onto_base(worktree, "main") is False
    assert (worktree / "README.md").read_text(encoding="utf-8") == "ours\n"
    assert not (worktree / ".git" / "rebase-merge").exists() and git("status", "--porcelain", cwd=worktree).strip() == ""


def test_force_push_updates_remote(manager: GitManager, repo: Path, origin: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    manager.create_worktree(repo, worktree, branch="orq/x", base="main")
    (worktree / "new.txt").write_text("hello\n", encoding="utf-8")
    manager.commit_all(worktree, "iter 1")
    manager.push(worktree, "orq/x")
    git("commit", "-q", "--amend", "-m", "iter 1 amended", cwd=worktree)

    manager.force_push(worktree, "orq/x")

    remote = git("ls-remote", "--heads", str(origin), "orq/x", cwd=tmp_path).split()[0]
    assert remote == manager.head(worktree)


def test_pr_number_state_and_merge_via_gh(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_gh(args: list[str], cwd: Path) -> str:
        calls.append(args)
        if args[:2] == ["pr", "view"] and "number" in args[-3]:
            return "12\n"
        if args[:2] == ["pr", "view"]:
            return "MERGED\n"
        return ""

    manager = GitManager(gh=fake_gh)
    assert manager.pr_number(tmp_path, "orq/x") == 12
    manager.merge_pr(tmp_path, 12, strategy="squash")
    assert manager.pr_state(tmp_path, 12) == "MERGED"
    assert calls[1] == ["pr", "merge", "12", "--squash", "--delete-branch"]
    with pytest.raises(GitError, match="strategy"):
        manager.merge_pr(tmp_path, 12, strategy="fast-forward")


def test_pr_number_none_when_gh_fails(tmp_path: Path) -> None:
    def failing(args: list[str], cwd: Path) -> str:
        raise GitError("no pull requests found")

    assert GitManager(gh=failing).pr_number(tmp_path, "orq/x") is None
