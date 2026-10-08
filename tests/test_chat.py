import asyncio
import json
import sys
from pathlib import Path

import pytest

from orq.adapters.base import AgentResult
from orq.adapters.claude import ClaudeChat
from orq.config import Config
from orq.core.chat import ChatError, ask, delete_chat, get_chat, list_chats
from orq.core.models import Project
from orq.git.manager import GitManager
from orq.paths import OrqPaths
from orq.store.db import Store
from tests.conftest import git

FAKES = Path(__file__).parent / "fakes"


def test_claude_chat_is_read_only_and_resumes_its_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_SCENARIO", "ok")
    agent = ClaudeChat(argv_prefix=[sys.executable, str(FAKES / "fake_claude.py")])

    first = asyncio.run(agent.run("O que o repo faz?", cwd=tmp_path, log_path=tmp_path / "s.jsonl", model="opus"))
    argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob" and "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--model") + 1] == "opus" and "--session-id" in argv and "--strict-mcp-config" in argv
    assert "Portuguese" in argv[argv.index("--append-system-prompt") + 1]
    assert first.ok and first.session_id and first.usage

    asyncio.run(agent.run("E depois?", cwd=tmp_path, log_path=tmp_path / "s.jsonl", session_id=first.session_id))
    argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--resume") + 1] == first.session_id


class FakeChatAgent:
    def __init__(self, results: list[AgentResult]) -> None:
        self.results = results
        self.calls: list[dict] = []

    async def run(self, prompt, *, cwd, log_path, session_id=None, model=None, **kwargs) -> AgentResult:
        self.calls.append({"prompt": prompt, "cwd": cwd, "session_id": session_id, "model": model,
                           "readme": (cwd / "README.md").read_text(encoding="utf-8")})
        log_path.write_text('{"type": "result"}\n', encoding="utf-8")
        return self.results.pop(0)


def answer(text: str, session: str = "S1") -> AgentResult:
    return AgentResult(ok=True, text=text, session_id=session, usage={"input_tokens": 10, "output_tokens": 5},
                       rate_limit={"status": "allowed"})


@pytest.fixture
def env(tmp_path: Path, origin: Path):
    paths, config = OrqPaths(tmp_path / "home"), Config()
    config.git.worktree_root = tmp_path / "wt"
    store = Store(paths.db)
    store.upsert_project(Project(repo="owner/sandbox", name="sandbox"))
    return paths, config, store


def test_a_conversation_reads_the_latest_base_and_keeps_its_session(env, origin: Path, tmp_path: Path) -> None:
    paths, config, store = env
    agent = FakeChatAgent([answer("É um sandbox."), answer("Agora tem mais.")])

    first = asyncio.run(ask(paths, config, store, GitManager(), agent, "owner/sandbox", "O que é isto?", clone_url=str(origin)))
    seed = tmp_path / "seed"
    (seed / "README.md").write_text("seed\nnew line\n", encoding="utf-8")
    git("commit", "-qam", "more", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    second = asyncio.run(ask(paths, config, store, GitManager(), agent, "owner/sandbox", "E agora?", chat_id=first["chat_id"],
                             clone_url=str(origin)))

    assert agent.calls[0]["cwd"] == config.git.worktree_root / "sandbox" / "_chat"
    assert agent.calls[0]["session_id"] is None and agent.calls[1]["session_id"] == "S1"
    assert agent.calls[1]["readme"] == "seed\nnew line\n"  # refreshed to origin/main before the second message
    assert agent.calls[0]["model"] == "opus"
    chat = get_chat(paths, "owner/sandbox", first["chat_id"])
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant", "user", "assistant"]
    assert chat["messages"][1]["usage"]["input_tokens"] == 10 and chat["title"] == "O que é isto?"
    assert second["answer"] == "Agora tem mais."
    assert [c["chat_id"] for c in list_chats(paths, "owner/sandbox")] == [first["chat_id"]]


def test_a_failed_answer_is_kept_and_the_session_is_not_lost(env, origin: Path) -> None:
    paths, config, store = env
    agent = FakeChatAgent([answer("Oi."), AgentResult(ok=False, error="You've hit your session limit", error_kind="rate_limit"),
                           answer("Voltei.")])

    first = asyncio.run(ask(paths, config, store, GitManager(), agent, "owner/sandbox", "Oi", clone_url=str(origin)))
    with pytest.raises(ChatError, match="session limit"):
        asyncio.run(ask(paths, config, store, GitManager(), agent, "owner/sandbox", "E aí?", chat_id=first["chat_id"],
                        clone_url=str(origin)))
    asyncio.run(ask(paths, config, store, GitManager(), agent, "owner/sandbox", "De novo", chat_id=first["chat_id"],
                    clone_url=str(origin)))

    assert agent.calls[2]["session_id"] == "S1"
    roles = [m["role"] for m in get_chat(paths, "owner/sandbox", first["chat_id"])["messages"]]
    assert roles == ["user", "assistant", "user", "error", "user", "assistant"]


def test_chats_are_per_project_and_can_be_deleted(env, origin: Path) -> None:
    paths, config, store = env
    first = asyncio.run(ask(paths, config, store, GitManager(), FakeChatAgent([answer("a")]), "owner/sandbox", "x",
                            clone_url=str(origin)))

    with pytest.raises(ChatError, match="no project"):
        asyncio.run(ask(paths, config, store, GitManager(), FakeChatAgent([]), "owner/none", "x"))
    with pytest.raises(ChatError, match="no chat"):
        asyncio.run(ask(paths, config, store, GitManager(), FakeChatAgent([]), "owner/sandbox", "x", chat_id="CNOPE"))
    delete_chat(paths, "owner/sandbox", first["chat_id"])
    assert list_chats(paths, "owner/sandbox") == []
