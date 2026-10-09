"""Project conversations from a WhatsApp group (Phase 7.2).

n8n tags the group's messages with `channel = "chat"`; the hub hands them here. `PROJETO <name>` picks the project
(kept until changed), `NOVA` starts a new conversation, anything else is a question for the read-only chat agent.
Answers go back to the group. orq never knows the group id: n8n routes `channel = "chat"`.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections import deque

from orq.config import Config
from orq.core.chat import ChatError, ask
from orq.core.models import Project
from orq.core.whatsapp_format import split_message, to_whatsapp
from orq.git.manager import GitManager
from orq.paths import OrqPaths
from orq.store.db import Store

log = logging.getLogger(__name__)

CHANNEL = "chat"
PROJECT_KEY = "wa_chat.project"
CHAT_KEY = "wa_chat.chat.{repo}"
HELP = ("💬 *Conversa sobre o código*\n\n"
        "*PROJETO <nome>*: escolhe o projeto (fica valendo até trocar)\n"
        "*PROJETO*: lista os projetos\n"
        "*NOVA*: começa uma conversa nova sobre o projeto\n"
        "Qualquer outra mensagem é uma pergunta. O Claude só lê o código: não altera nada nem inicia runs.\n"
        "Decisões e comandos (STATUS, APPROVE…) continuam no chat privado.")


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)).strip()


class ChatBridge:
    def __init__(self, store: Store, paths: OrqPaths, config: Config, client, git: GitManager, agent, *,
                 clone_url: str | None = None, poll_seconds: float = 1) -> None:
        self.store, self.paths, self.config, self.client = store, paths, config, client
        self.git, self.agent, self.clone_url = git, agent, clone_url
        self.pending: deque[str] = deque()
        self._poll = poll_seconds

    def submit(self, text: str) -> None:
        self.pending.append(text)

    async def drain(self) -> None:
        while self.pending:
            await self.handle(self.pending.popleft())

    async def loop(self) -> None:
        while True:
            try:
                await self.drain()
            except Exception:  # noqa: BLE001 - one bad message must not stop the conversations
                log.exception("whatsapp chat failed")
            await asyncio.sleep(self._poll)

    def _send(self, text: str) -> None:
        self.client.send(text, channel=CHANNEL)

    def _active(self) -> list[Project]:
        return sorted((p for p in self.store.list_projects() if p.status == "active"),
                      key=lambda p: (not p.pinned, p.name.casefold()))

    def _listing(self) -> str:
        current = self.store.kv_get(PROJECT_KEY, "") or ""
        rows = [f"{'👉 ' if p.repo == current else '• '}{p.name} ({p.repo})" for p in self._active()]
        return "\n".join(rows) or "(nenhum projeto ativo)"

    def _match(self, query: str) -> list[Project]:
        wanted = _fold(query)
        projects = self._active()
        exact = [p for p in projects if wanted in (_fold(p.repo), _fold(p.repo.split("/")[-1]), _fold(p.name))]
        if exact:
            return exact
        return [p for p in projects if wanted in _fold(p.repo) or wanted in _fold(p.name)]

    async def handle(self, text: str) -> None:
        message = text.strip()
        if not message:
            return
        word, _, rest = message.partition(" ")
        command = _fold(word)
        if command in ("ajuda", "help", "?"):
            self._send(HELP)
        elif command == "projeto":
            self._pick(rest.strip())
        elif command == "nova" and not rest.strip():
            project = self._current()
            if project is None:
                self._send(f"Escolha o projeto primeiro: *PROJETO <nome>*\n{self._listing()}")
                return
            self.store.kv_set(CHAT_KEY.format(repo=project.repo), "")
            self._send(f"🆕 Nova conversa sobre *{project.name}*. Pode perguntar.")
        else:
            await self._question(message)

    def _current(self) -> Project | None:
        repo = self.store.kv_get(PROJECT_KEY, "") or ""
        project = self.store.get_project(repo) if repo else None
        return project if project is not None and project.status == "active" else None

    def _pick(self, query: str) -> None:
        if not query:
            self._send(f"📁 Projetos (escolha com *PROJETO <nome>*):\n{self._listing()}")
            return
        found = self._match(query)
        if not found:
            self._send(f"⚠️ Nenhum projeto com \"{query}\".\n{self._listing()}")
        elif len(found) > 1:
            self._send("⚠️ Mais de um projeto combina, seja mais específico:\n" + "\n".join(f"• {p.name} ({p.repo})" for p in found))
        else:
            self.store.kv_set(PROJECT_KEY, found[0].repo)
            self._send(f"📁 Projeto: *{found[0].name}* ({found[0].repo}). Pode perguntar.")

    async def _question(self, text: str) -> None:
        project = self._current()
        if project is None:
            self._send(f"Escolha o projeto primeiro: *PROJETO <nome>*\n{self._listing()}")
            return
        key = CHAT_KEY.format(repo=project.repo)
        chat_id = self.store.kv_get(key, "") or None
        self._send(f"🔎 lendo o código de *{project.name}*…")
        try:
            try:
                result = await ask(self.paths, self.config, self.store, self.git, self.agent, project.repo, text,
                                   chat_id=chat_id, clone_url=self.clone_url, style="whatsapp", via="whatsapp")
            except ChatError as exc:
                if not (chat_id and str(exc).startswith("no chat")):
                    raise
                # deleted on the dashboard: carry on in a new conversation
                result = await ask(self.paths, self.config, self.store, self.git, self.agent, project.repo, text,
                                   clone_url=self.clone_url, style="whatsapp", via="whatsapp")
        except ChatError as exc:
            self._send(f"⚠️ Não consegui responder: {exc}")
            return
        self.store.kv_set(key, result["chat_id"])
        for part in split_message(to_whatsapp(result["answer"])):
            self._send(part)
