"""WhatsApp through the owner's n8n webhooks (SPEC 9.3). orq never sees phone numbers; n8n routes to the owner."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from orq.config import Config


@dataclass(frozen=True)
class InboundMessage:
    id: int
    text: str
    received_at: str | None = None


class WhatsAppClient:
    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None, timeout: float = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @classmethod
    def from_config(cls, config: Config, http: httpx.Client | None = None) -> WhatsAppClient | None:
        """None when WhatsApp is not configured: no base URL, or the token variable is missing."""
        base = config.notify.n8n_base_url.strip()
        token = os.environ.get(config.notify.n8n_token_env, "").strip()
        if not base or not token:
            return None
        return cls(base, token, http=http)

    def send(self, text: str) -> None:
        response = self._http.post(f"{self.base_url}/orq/notify", json={"text": text}, headers=self._headers)
        response.raise_for_status()

    def replies(self, since: int) -> list[InboundMessage]:
        response = self._http.get(f"{self.base_url}/orq/replies", params={"since": since}, headers=self._headers)
        response.raise_for_status()
        data = response.json()
        items = data.get("messages", []) if isinstance(data, dict) else data
        out: list[InboundMessage] = []
        for item in items or []:
            if not isinstance(item, dict) or "id" not in item:
                continue
            out.append(InboundMessage(id=int(item["id"]), text=str(item.get("text", "")), received_at=item.get("received_at")))
        return sorted(out, key=lambda m: m.id)

    def ack(self, ids: list[int]) -> None:
        if not ids:
            return
        response = self._http.post(f"{self.base_url}/orq/replies/ack", json={"ids": ids}, headers=self._headers)
        response.raise_for_status()
