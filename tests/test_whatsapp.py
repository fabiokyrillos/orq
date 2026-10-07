import json

import httpx
import pytest

from orq.config import Config
from orq.notify.whatsapp import InboundMessage, WhatsAppClient


class FakeN8n:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.acked: list[list[int]] = []
        self.inbox: list[dict] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != "Bearer secret-token":
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path
        if path.endswith("/orq/notify") and request.method == "POST":
            self.sent.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/orq/replies") and request.method == "GET":
            since = int(request.url.params.get("since", "0"))
            return httpx.Response(200, json={"messages": [m for m in self.inbox if m["id"] > since]})
        if path.endswith("/orq/replies/ack") and request.method == "POST":
            self.acked.append(json.loads(request.content)["ids"])
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


@pytest.fixture
def n8n() -> FakeN8n:
    return FakeN8n()


def client(n8n: FakeN8n, token: str = "secret-token") -> WhatsAppClient:
    return WhatsAppClient("https://vps.example/webhook/", token, http=httpx.Client(transport=httpx.MockTransport(n8n.handler)))


def test_send_posts_text_with_bearer_token(n8n: FakeN8n) -> None:
    client(n8n).send("*[orq] hello*")
    assert n8n.sent == ["*[orq] hello*"]
    assert str(n8n.requests[0].url) == "https://vps.example/webhook/orq/notify"


def test_replies_since_cursor_are_sorted_and_ack(n8n: FakeN8n) -> None:
    n8n.inbox = [{"id": 3, "text": "D7K2 1", "received_at": "t3"}, {"id": 2, "text": "STATUS"}, {"id": 1, "text": "old"}]
    c = client(n8n)
    assert c.replies(since=1) == [InboundMessage(2, "STATUS", None), InboundMessage(3, "D7K2 1", "t3")]
    c.ack([2, 3])
    c.ack([])
    assert n8n.acked == [[2, 3]]


def test_replies_accepts_a_bare_list(n8n: FakeN8n) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 5, "text": "x"}, {"bad": True}])

    c = WhatsAppClient("https://v/webhook", "t", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.replies(0) == [InboundMessage(5, "x", None)]


def test_wrong_token_raises(n8n: FakeN8n) -> None:
    with pytest.raises(httpx.HTTPStatusError):
        client(n8n, token="nope").send("x")


def test_from_config_requires_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config()
    config.notify.n8n_token_env = "ORQ_TEST_TOKEN_NOT_SET"  # a real ORQ_N8N_TOKEN may exist in the user profile
    assert WhatsAppClient.from_config(config) is None
    config.notify.n8n_base_url = "https://vps.example/webhook"
    assert WhatsAppClient.from_config(config) is None
    monkeypatch.setenv("ORQ_TEST_TOKEN_NOT_SET", "secret-token")
    c = WhatsAppClient.from_config(config)
    assert c is not None and c.base_url == "https://vps.example/webhook"


def test_read_secret_prefers_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from orq.notify.whatsapp import read_secret
    monkeypatch.setenv("ORQ_TEST_SECRET", " abc ")
    assert read_secret("ORQ_TEST_SECRET") == "abc"
    monkeypatch.delenv("ORQ_TEST_SECRET", raising=False)
    assert read_secret("ORQ_TEST_SECRET_MISSING_FOR_SURE") == ""
