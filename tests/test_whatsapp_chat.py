import asyncio
from pathlib import Path

import pytest

from orq.adapters.base import AgentResult
from orq.config import Config
from orq.core.chat import get_chat, list_chats
from orq.core.models import Project
from orq.git.manager import GitManager
from orq.hub.whatsapp_chat import ChatBridge
from orq.hub.whatsapp_tasks import WhatsAppTasks
from orq.notify.whatsapp import InboundMessage
from orq.paths import OrqPaths
from orq.store.db import Store


class Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []
        self.inbox: list[InboundMessage] = []
        self.acked: list[list[int]] = []

    def send(self, text: str, channel: str | None = None) -> None:
        self.sent.append((text, channel))

    def replies(self, since: int) -> list[InboundMessage]:
        return [m for m in self.inbox if m.id > since]

    def ack(self, ids: list[int]) -> None:
        self.acked.append(list(ids))


class Agent:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[dict] = []

    async def run(self, prompt, *, cwd, log_path, session_id=None, model=None, system_prompt=None, **kwargs) -> AgentResult:
        self.calls.append({"prompt": prompt, "session_id": session_id, "system_prompt": system_prompt})
        return AgentResult(ok=True, text=self.answers.pop(0), session_id=f"S{len(self.calls)}" if session_id is None else session_id,
                           usage={"input_tokens": 1, "output_tokens": 1})


@pytest.fixture
def env(tmp_path: Path, origin: Path):
    paths, config = OrqPaths(tmp_path / "home"), Config()
    config.git.worktree_root = tmp_path / "wt"
    store = Store(paths.db)
    store.upsert_project(Project(repo="owner/sandbox", name="Sandbox Vôlei"))
    store.upsert_project(Project(repo="owner/other", name="other"))
    store.upsert_project(Project(repo="owner/gone", name="gone", status="removed"))
    return paths, config, store, Client()


def bridge(env, origin: Path, agent: Agent) -> ChatBridge:
    paths, config, store, client = env
    return ChatBridge(store, paths, config, client, GitManager(), agent, clone_url=str(origin))


def texts(client: Client) -> list[str]:
    return [t for t, channel in client.sent if channel == "chat"]


def test_a_question_without_a_project_asks_to_pick_one(env, origin: Path) -> None:
    b = bridge(env, origin, Agent([]))
    asyncio.run(b.handle("O que esse projeto faz?"))
    [reply] = texts(env[3])
    assert "PROJETO" in reply and "Sandbox Vôlei" in reply and "other" in reply and "gone" not in reply


def test_pick_ask_follow_up_and_start_over(env, origin: Path) -> None:
    paths, _, _, client = env
    agent = Agent(["## Resumo\n\n**slug.py** faz isso.", "Dois.", "Começando de novo."])
    b = bridge(env, origin, agent)

    asyncio.run(b.handle("projeto volei"))  # accents and case do not matter; a unique part of the name is enough
    asyncio.run(b.handle("O que o repo faz?"))
    asyncio.run(b.handle("E o segundo exemplo?"))
    asyncio.run(b.handle("NOVA"))
    asyncio.run(b.handle("Outra pergunta"))

    sent = texts(client)
    assert sent[0].startswith("📁") and "owner/sandbox" in sent[0]
    assert sent[1].startswith("🔎") and sent[2] == "*Resumo*\n\n*slug.py* faz isso."
    assert sent[4] == "Dois." and sent[5].startswith("🆕") and sent[7] == "Começando de novo."
    assert agent.calls[1]["session_id"] == "S1" and agent.calls[2]["session_id"] is None  # NOVA: a fresh session
    assert "WhatsApp" in agent.calls[0]["system_prompt"]
    chats = list_chats(paths, "owner/sandbox")
    assert len(chats) == 2 and {c["via"] for c in chats} == {"whatsapp"}
    first = get_chat(paths, "owner/sandbox", chats[-1]["chat_id"])
    assert [m.get("via") for m in first["messages"]] == ["whatsapp"] * 4


def test_project_listing_unknown_and_ambiguous_names(env, origin: Path) -> None:
    paths, config, store, client = env
    store.upsert_project(Project(repo="owner/sandbox-two", name="Sandbox Two"))
    b = bridge(env, origin, Agent([]))

    asyncio.run(b.handle("PROJETO"))
    asyncio.run(b.handle("PROJETO nada"))
    asyncio.run(b.handle("PROJETO sand"))  # "sandbox" alone would be the exact repo name of owner/sandbox
    asyncio.run(b.handle("AJUDA"))

    listing, unknown, ambiguous, help_text = texts(client)
    assert "Sandbox Vôlei" in listing and "Sandbox Two" in listing
    assert unknown.startswith("⚠️") and ambiguous.startswith("⚠️") and "Sandbox Two" in ambiguous
    assert "PROJETO" in help_text and "NOVA" in help_text


def test_a_conversation_deleted_on_the_dashboard_starts_over(env, origin: Path) -> None:
    paths, _, store, client = env
    b = bridge(env, origin, Agent(["a", "b"]))
    asyncio.run(b.handle("PROJETO other"))
    asyncio.run(b.handle("primeira"))
    from orq.core.chat import delete_chat

    delete_chat(paths, "owner/other", list_chats(paths, "owner/other")[0]["chat_id"])
    asyncio.run(b.handle("segunda"))

    assert texts(client)[-1] == "b" and len(list_chats(paths, "owner/other")) == 1


def test_group_messages_go_to_the_bridge_and_the_rest_to_the_commands(env, origin: Path) -> None:
    paths, config, store, client = env
    b = bridge(env, origin, Agent([]))
    tasks = WhatsAppTasks(store, paths, config, client, chat=b)
    client.inbox = [InboundMessage(1, "PROJETO other", None, "chat"), InboundMessage(2, "STATUS", None, "owner")]

    assert tasks.inbound_once() == 2
    assert list(b.pending) == ["PROJETO other"]
    assert [channel for _, channel in client.sent] == [None]  # STATUS answered in the private chat
    assert client.acked == [[1, 2]]
    asyncio.run(b.drain())
    assert texts(client)[0].startswith("📁")
