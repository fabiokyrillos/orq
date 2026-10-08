"""The owner's conversations about a project (Phase 7.1).

Claude answers with read tools only, in a detached worktree of orq's clone kept at `<worktree_root>/<repo>/_chat` and
moved to a fresh `origin/<base>` before every message. A conversation keeps one Claude session, resumed from that same
path. Files: `<chats>/<owner>__<repo>/<chat_id>/meta.json`, `messages.jsonl`, `claude.stream.jsonl`.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from orq.config import Config
from orq.git.manager import GitError, GitManager
from orq.paths import OrqPaths
from orq.store.db import Store

DEFAULT_MODEL = "opus"
_locks: dict[str, asyncio.Lock] = {}


class ChatError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _project_dir(paths: OrqPaths, repo: str) -> Path:
    return paths.chats / repo.replace("/", "__", 1)


def _chat_dir(paths: OrqPaths, repo: str, chat_id: str) -> Path:
    if not chat_id.isalnum():
        raise ChatError(f"no chat {chat_id}")
    return _project_dir(paths, repo) / chat_id


def _read_meta(folder: Path) -> dict:
    return json.loads((folder / "meta.json").read_text(encoding="utf-8"))


def _write_meta(folder: Path, meta: dict) -> None:
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _append(folder: Path, message: dict) -> None:
    with (folder / "messages.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, ensure_ascii=False) + "\n")


def list_chats(paths: OrqPaths, repo: str) -> list[dict]:
    root = _project_dir(paths, repo)
    chats = [_read_meta(folder) for folder in root.iterdir() if (folder / "meta.json").exists()] if root.exists() else []
    return sorted(chats, key=lambda m: m["updated_at"], reverse=True)


def get_chat(paths: OrqPaths, repo: str, chat_id: str) -> dict:
    folder = _chat_dir(paths, repo, chat_id)
    if not (folder / "meta.json").exists():
        raise ChatError(f"no chat {chat_id}")
    lines = (folder / "messages.jsonl").read_text(encoding="utf-8").splitlines() if (folder / "messages.jsonl").exists() else []
    return {**_read_meta(folder), "messages": [json.loads(line) for line in lines if line.strip()]}


def delete_chat(paths: OrqPaths, repo: str, chat_id: str) -> None:
    folder = _chat_dir(paths, repo, chat_id)
    if not folder.exists():
        raise ChatError(f"no chat {chat_id}")
    shutil.rmtree(folder)


def chat_worktree(config: Config, repo: str) -> Path:
    return config.git.worktree_root / repo.split("/", 1)[1] / "_chat"


def _prepare(paths: OrqPaths, config: Config, store: Store, git: GitManager, repo: str, clone_url: str | None) -> Path:
    project = store.get_project(repo)
    if project is None or project.status == "removed":
        raise ChatError(f"no project {repo}")
    repo_path = git.ensure_repo(repo, paths.repos, clone_url=clone_url, seed=project.local_path)
    worktree = chat_worktree(config, repo)
    git.detached_worktree_at_base(repo_path, worktree, project.base_branch)
    return worktree


async def ask(paths: OrqPaths, config: Config, store: Store, git: GitManager, agent, repo: str, message: str, *,
              chat_id: str | None = None, model: str | None = None, clone_url: str | None = None) -> dict:
    """Send one message and wait for the answer: {chat_id, answer}. A failed answer raises ChatError and is kept."""
    text = message.strip()
    if not text:
        raise ChatError("empty message")
    if store.get_project(repo) is None:
        raise ChatError(f"no project {repo}")
    if chat_id is None:
        chat_id = "C" + datetime.now().strftime("%y%m%d%H%M%S") + secrets.token_hex(2).upper()
        folder = _chat_dir(paths, repo, chat_id)
        folder.mkdir(parents=True)
        _write_meta(folder, {"chat_id": chat_id, "project": repo, "title": text.splitlines()[0][:80], "session_id": None,
                             "model": model or DEFAULT_MODEL, "created_at": _now(), "updated_at": _now()})
    folder = _chat_dir(paths, repo, chat_id)
    if not (folder / "meta.json").exists():
        raise ChatError(f"no chat {chat_id}")
    lock = _locks.setdefault(f"{repo}/{chat_id}", asyncio.Lock())
    if lock.locked():
        raise ChatError("this conversation is still waiting for an answer")
    async with lock:
        meta = _read_meta(folder)
        use_model = model or meta.get("model") or DEFAULT_MODEL
        _append(folder, {"role": "user", "text": text, "ts": _now()})
        try:
            worktree = await asyncio.to_thread(_prepare, paths, config, store, git, repo, clone_url)
        except GitError as exc:
            _append(folder, {"role": "error", "text": f"could not update the checkout: {exc}", "ts": _now()})
            raise ChatError(str(exc)) from exc
        result = await agent.run(text, cwd=worktree, log_path=folder / "claude.stream.jsonl", session_id=meta.get("session_id"),
                                 model=use_model)
        if not result.ok:
            error = (result.error or "").strip()
            if not error or error.startswith("{"):  # the tail of the stream, not a message: the CLI just stopped
                error = "claude stopped without an answer (see claude.stream.jsonl in the conversation's folder)"
            _append(folder, {"role": "error", "text": error, "kind": result.error_kind, "ts": _now()})
            raise ChatError(error)
        _append(folder, {"role": "assistant", "text": result.text, "ts": _now(), "model": use_model, "usage": result.usage,
                         "rate_limit": result.rate_limit})
        _write_meta(folder, {**meta, "session_id": result.session_id or meta.get("session_id"), "model": use_model,
                             "updated_at": _now()})
    return {"chat_id": chat_id, "answer": result.text}
