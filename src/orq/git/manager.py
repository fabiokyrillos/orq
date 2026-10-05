"""Worktrees, branches, commits, push and PRs via git and gh (SPEC section 10.6)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

GhRunner = Callable[[list[str], Path], str]


class GitError(RuntimeError):
    pass


def _run_gh(args: list[str], cwd: Path) -> str:
    gh = shutil.which("gh") or "gh"
    proc = subprocess.run([gh, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise GitError(f"gh {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


class GitManager:
    def __init__(self, gh: GhRunner = _run_gh) -> None:
        self._gh = gh

    def git(self, *args: str, cwd: Path) -> str:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def ensure_repo(self, repo: str, repos_root: Path, clone_url: str | None = None) -> Path:
        """Clone `owner/repo` under repos_root (or fetch if present) and return the checkout path."""
        name = repo.split("/")[-1]
        path = repos_root / name
        if (path / ".git").exists():
            self.git("fetch", "--prune", "origin", cwd=path)
            return path
        repos_root.mkdir(parents=True, exist_ok=True)
        url = clone_url or f"https://github.com/{repo}.git"
        self.git("clone", "-q", "-c", "core.longpaths=true", "-c", "core.autocrlf=false", url, str(path), cwd=repos_root)
        return path

    def create_worktree(self, repo_path: Path, worktree: Path, branch: str, base: str) -> None:
        self.git("fetch", "origin", base, cwd=repo_path)
        self.git("config", "core.longpaths", "true", cwd=repo_path)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "-b", branch, str(worktree), f"origin/{base}", cwd=repo_path)
        # Worktrees share the repo config, but these are also set here in case the repo was not cloned by orq.
        self.git("config", "core.longpaths", "true", cwd=worktree)
        self.git("config", "core.autocrlf", "false", cwd=worktree)

    def remove_worktree(self, repo_path: Path, worktree: Path, branch: str | None = None) -> None:
        self.git("worktree", "remove", "--force", str(worktree), cwd=repo_path)
        self.git("worktree", "prune", cwd=repo_path)
        if branch:
            self.git("branch", "-D", branch, cwd=repo_path)

    def head(self, worktree: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=worktree).strip()

    def commit_all(self, worktree: Path, message: str) -> str | None:
        """Stage everything and commit; return the new sha, or None when there is nothing to commit."""
        self.git("add", "-A", cwd=worktree)
        if not self.git("status", "--porcelain", cwd=worktree).strip():
            return None
        self.git("commit", "-q", "-m", message, cwd=worktree)
        return self.head(worktree)

    def stage_all(self, worktree: Path) -> None:
        self.git("add", "-A", cwd=worktree)

    def diff(self, worktree: Path, since: str) -> str:
        return self.git("diff", since, "HEAD", cwd=worktree)

    def diff_stat(self, worktree: Path, since: str) -> str:
        return self.git("diff", "--stat", since, "HEAD", cwd=worktree)

    def push(self, worktree: Path, branch: str) -> None:
        self.git("push", "-u", "origin", branch, cwd=worktree)

    def create_pr(self, worktree: Path, base: str, head: str, title: str, body: str) -> str:
        out = self._gh(["pr", "create", "--base", base, "--head", head, "--title", title, "--body", body], worktree)
        return out.strip().splitlines()[-1]
