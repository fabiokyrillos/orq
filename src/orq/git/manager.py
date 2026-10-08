"""Worktrees, branches, commits, push and PRs via git and gh (SPEC section 10.6)."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from orq.adapters.base import split_command
from orq.core.locks import file_lock
from orq.core.procs import no_window

GhRunner = Callable[[list[str], Path], str]


class GitError(RuntimeError):
    pass


def gh_argv() -> list[str]:
    override = os.environ.get("ORQ_GH_CMD")
    if override:
        return split_command(override)
    return [shutil.which("gh") or "gh"]


def _run_gh(args: list[str], cwd: Path) -> str:
    proc = subprocess.run([*gh_argv(), *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", creationflags=no_window())
    if proc.returncode != 0:
        raise GitError(f"gh {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


class GitManager:
    def __init__(self, gh: GhRunner = _run_gh, *, lock_timeout: float = 600) -> None:
        self._gh = gh
        self._lock_timeout = lock_timeout
        self._lock_paths: dict[Path, Path] = {}

    @property
    def gh(self) -> GhRunner:
        return self._gh

    def git(self, *args: str, cwd: Path) -> str:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                              creationflags=no_window())
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def lock_path(self, path: Path) -> Path:
        """`<clone>.orq.lock` next to the clone, for the clone itself or any of its worktrees."""
        if path not in self._lock_paths:
            try:
                clone = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path).strip()).parent
            except GitError:
                clone = path  # not a checkout (gh-only calls in tests): a private lock is harmless
            self._lock_paths[path] = clone.with_name(clone.name + ".orq.lock")
        return self._lock_paths[path]

    def _locked(self, path: Path):
        """Concurrent runs share one clone: serialise the commands that write shared refs or the worktree list."""
        return file_lock(self.lock_path(path), timeout=self._lock_timeout)

    def ensure_repo(self, repo: str, repos_root: Path, clone_url: str | None = None, *, seed: Path | str | None = None,
                    on_clone: Callable[[str], None] | None = None) -> Path:
        """Clone `owner/repo` to repos_root/owner/repo (or fetch if present) and return the checkout path.

        Phase 7: `seed` is the owner's own checkout of the repo. A new clone borrows its objects (`--reference`) and copies
        them (`--dissociate`), so nothing is downloaded twice and the clone does not depend on the folder. The folder is
        only read. `on_clone` gets `local:<git dir>` or `github`.
        """
        owner, name = repo.split("/", 1)
        path = repos_root / owner / name
        self._lock_paths[path] = path.with_name(name + ".orq.lock")
        with self._locked(path):
            if (path / ".git").exists():
                self.git("fetch", "--prune", "origin", cwd=path)
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            url = clone_url or f"https://github.com/{repo}.git"
            clone = ["clone", "-q", "-c", "core.longpaths=true", "-c", "core.autocrlf=false"]
            seed_dir = self._seed_git_dir(Path(seed)) if seed else None
            if seed_dir is not None:
                try:
                    self.git(*clone, "--reference", str(seed_dir), "--dissociate", url, str(path), cwd=path.parent)
                except GitError:
                    shutil.rmtree(path, ignore_errors=True)  # a shallow or broken seed: start over without it
                    seed_dir = None
            if seed_dir is None:
                self.git(*clone, url, str(path), cwd=path.parent)
        if on_clone:
            on_clone(f"local:{seed_dir.as_posix()}" if seed_dir else "github")
        return path

    def _seed_git_dir(self, folder: Path) -> Path | None:
        """The git dir shared by `folder` and its worktrees, or None when `folder` is not the top of a checkout."""
        if not folder.is_dir():
            return None
        try:
            top = Path(self.git("rev-parse", "--show-toplevel", cwd=folder).strip())
            common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=folder).strip())
        except GitError:
            return None
        if top.resolve() != folder.resolve():  # a plain folder inside some other repo
            return None
        return common.resolve()

    def create_worktree(self, repo_path: Path, worktree: Path, branch: str, base: str) -> None:
        with self._locked(repo_path):
            self.git("fetch", "origin", base, cwd=repo_path)
            self.git("config", "core.longpaths", "true", cwd=repo_path)
            worktree.parent.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "add", "-b", branch, str(worktree), f"origin/{base}", cwd=repo_path)
        # Worktrees share the repo config, but these are also set here in case the repo was not cloned by orq.
        self.git("config", "core.longpaths", "true", cwd=worktree)
        self.git("config", "core.autocrlf", "false", cwd=worktree)

    def detached_worktree_at_base(self, repo_path: Path, worktree: Path, base: str) -> None:
        """Phase 7.1: a worktree with no branch at a fresh origin/<base> (the owner's chat reads it); kept at one path."""
        with self._locked(repo_path):
            self.git("fetch", "origin", base, cwd=repo_path)
            if (worktree / ".git").exists():
                self.git("checkout", "-q", "--detach", "-f", f"origin/{base}", cwd=worktree)
                self.git("clean", "-fdq", cwd=worktree)
                return
            worktree.parent.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "prune", cwd=repo_path)
            self.git("worktree", "add", "-q", "--detach", str(worktree), f"origin/{base}", cwd=repo_path)

    def remove_worktree(self, repo_path: Path, worktree: Path, branch: str | None = None) -> None:
        with self._locked(repo_path):
            self.git("worktree", "remove", "--force", str(worktree), cwd=repo_path)
            self.git("worktree", "prune", cwd=repo_path)
            if branch:
                self.git("branch", "-D", branch, cwd=repo_path)

    def head(self, worktree: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=worktree).strip()

    def stage_all(self, worktree: Path) -> None:
        self.git("add", "-A", cwd=worktree)

    def staged_files(self, worktree: Path) -> list[str]:
        return [line for line in self.git("diff", "--cached", "--name-only", cwd=worktree).splitlines() if line.strip()]

    def staged_name_status(self, worktree: Path) -> list[tuple[str, str]]:
        """[(status, path)] for the index vs HEAD; a rename reports as ('R', new_path)."""
        out: list[tuple[str, str]] = []
        for line in self.git("diff", "--cached", "--name-status", "-M", cwd=worktree).splitlines():
            parts = line.split("	")
            if len(parts) >= 2 and parts[0]:
                out.append((parts[0][0], parts[-1]))
        return out

    def staged_numstat(self, worktree: Path) -> list[tuple[int, int, str]]:
        """[(added, deleted, path)] for text files in the index vs HEAD; binary files are skipped."""
        out: list[tuple[int, int, str]] = []
        for line in self.git("diff", "--cached", "--numstat", cwd=worktree).splitlines():
            parts = line.split("	", 2)
            if len(parts) == 3 and parts[0] != "-":
                out.append((int(parts[0]), int(parts[1]), parts[2]))
        return out

    def show_file(self, worktree: Path, ref: str, path: str) -> str | None:
        """The file's content at `ref`, or None when it does not exist there."""
        proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=str(worktree), capture_output=True, text=True, creationflags=no_window(),
                              encoding="utf-8", errors="replace")
        return proc.stdout if proc.returncode == 0 else None

    def staged_patch(self, worktree: Path) -> str:
        return self.git("diff", "--cached", "--no-color", "-M", cwd=worktree)

    def reset_hard(self, worktree: Path, sha: str) -> None:
        """Discard everything after sha, including untracked files (the owner rejected the changes)."""
        self.git("reset", "-q", "--hard", sha, cwd=worktree)
        self.git("clean", "-fdq", cwd=worktree)

    def commit_staged(self, worktree: Path, message: str) -> str | None:
        """Commit what is staged; return the new sha, or None when the index is clean.

        Files created after staging (for example __pycache__ from the check command) are left out.
        """
        if not self.git("diff", "--cached", "--name-only", cwd=worktree).strip():
            return None
        self.git("commit", "-q", "-m", message, cwd=worktree)
        return self.head(worktree)

    def commit_all(self, worktree: Path, message: str) -> str | None:
        """Stage everything and commit; return the new sha, or None when there is nothing to commit."""
        self.stage_all(worktree)
        return self.commit_staged(worktree, message)

    def branch_exists(self, repo_path: Path, branch: str) -> bool:
        local = self.git("branch", "--list", branch, cwd=repo_path).strip()
        remote = self.git("branch", "--list", "-r", f"origin/{branch}", cwd=repo_path).strip()
        return bool(local or remote)

    def diff(self, worktree: Path, since: str) -> str:
        return self.git("diff", since, "HEAD", cwd=worktree)

    def diff_stat(self, worktree: Path, since: str) -> str:
        return self.git("diff", "--stat", since, "HEAD", cwd=worktree)

    def push(self, worktree: Path, branch: str) -> None:
        with self._locked(worktree):
            self.git("push", "-u", "origin", branch, cwd=worktree)

    def commit_subject_and_parent(self, worktree: Path, ref: str = "HEAD") -> tuple[str, str]:
        out = self.git("log", "-1", "--format=%s%n%P", ref, cwd=worktree).splitlines()
        subject = out[0] if out else ""
        parents = out[1].split() if len(out) > 1 else []
        return subject, (parents[0] if parents else "")

    def base_moved(self, worktree: Path, base: str) -> bool:
        """True when origin/<base> has commits that are not in HEAD (the PR needs a rebase)."""
        with self._locked(worktree):
            self.git("fetch", "origin", base, cwd=worktree)
        try:
            self.git("merge-base", "--is-ancestor", f"origin/{base}", "HEAD", cwd=worktree)
            return False
        except GitError:
            return True

    def rebase_onto_base(self, worktree: Path, base: str) -> bool:
        """Rebase HEAD onto a freshly fetched origin/<base>; on conflict abort and return False."""
        with self._locked(worktree):
            self.git("fetch", "origin", base, cwd=worktree)
        try:
            self.git("rebase", f"origin/{base}", cwd=worktree)
            return True
        except GitError:
            try:
                self.git("rebase", "--abort", cwd=worktree)
            except GitError:
                pass
            return False

    # Phase 6: a conflicting rebase stays in progress so the implementer can resolve it; orq drives every git step.

    def start_rebase(self, worktree: Path, base: str) -> list[str]:
        """Rebase HEAD onto a fresh origin/<base>; return the conflicted files (empty when the rebase completed)."""
        with self._locked(worktree):
            self.git("fetch", "origin", base, cwd=worktree)
        try:
            self.git("rebase", f"origin/{base}", cwd=worktree)
            return []
        except GitError:
            conflicts = self.conflicted_files(worktree)
            if not conflicts:
                raise
            return conflicts

    def conflicted_files(self, worktree: Path) -> list[str]:
        return [line for line in self.git("diff", "--name-only", "--diff-filter=U", cwd=worktree).splitlines() if line.strip()]

    def rebase_in_progress(self, worktree: Path) -> bool:
        return any((Path(self.git("rev-parse", "--path-format=absolute", "--git-path", name, cwd=worktree).strip())).exists()
                   for name in ("rebase-merge", "rebase-apply"))

    def files_with_conflict_markers(self, worktree: Path, files: list[str]) -> list[str]:
        marked = []
        for name in files:
            path = worktree / name
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if any(line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "<<<<<<<" for line in lines):
                marked.append(name)
        return marked

    def stage(self, worktree: Path, files: list[str]) -> None:
        if files:
            self.git("add", "--", *files, cwd=worktree)

    def continue_rebase(self, worktree: Path) -> list[str]:
        """Continue after the conflicts were resolved; return the next commit's conflicts (empty when done)."""
        try:
            self.git("-c", "core.editor=true", "rebase", "--continue", cwd=worktree)
            return []
        except GitError:
            conflicts = self.conflicted_files(worktree)
            if not conflicts:
                raise
            return conflicts

    def abort_rebase(self, worktree: Path) -> None:
        if self.rebase_in_progress(worktree):
            self.git("rebase", "--abort", cwd=worktree)

    def force_push(self, worktree: Path, branch: str) -> None:
        """orq's own push after a rebase; the implementer never gets to do this (guard rule git_force_push)."""
        with self._locked(worktree):
            self.git("push", "--force-with-lease", "origin", branch, cwd=worktree)

    def pr_number(self, worktree: Path, head: str) -> int | None:
        try:
            out = self._gh(["pr", "view", head, "--json", "number", "-q", ".number"], worktree).strip()
        except GitError:
            return None
        return int(out) if out.isdigit() else None

    def pr_state(self, worktree: Path, number: int) -> str:
        return self._gh(["pr", "view", str(number), "--json", "state", "-q", ".state"], worktree).strip()

    def merge_pr(self, worktree: Path, number: int, strategy: str = "squash") -> None:
        if strategy not in ("squash", "merge", "rebase"):
            raise GitError(f"unknown merge strategy {strategy}")
        with self._locked(worktree):  # gh updates the local remote-tracking refs as well
            self._gh(["pr", "merge", str(number), f"--{strategy}", "--delete-branch"], worktree)

    def pr_url(self, worktree: Path, head: str) -> str | None:
        """URL of the open PR for `head`, or None when there is none (a resumed finalize must not open a second PR)."""
        try:
            out = self._gh(["pr", "view", head, "--json", "url", "-q", ".url"], worktree)
        except GitError:
            return None
        return out.strip() or None

    def create_pr(self, worktree: Path, base: str, head: str, title: str, body: str) -> str:
        out = self._gh(["pr", "create", "--base", base, "--head", head, "--title", title, "--body", body], worktree)
        return out.strip().splitlines()[-1]
