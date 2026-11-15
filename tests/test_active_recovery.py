"""Retry through the real browser/session/API chain while generation is still live."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from poe_web_proxy.app import create_app
from poe_web_proxy.browser import Browser
from poe_web_proxy.sessions import SessionStore, digest

MODEL = "Claude-Opus-5.5"
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "private coding task"}]}
AUTH = {"Authorization": "Bearer local-key", "x-session-affinity": "active-recovery"}
CHAT = "https://poe.com/chat/active-recovery"


class LivePage(Browser):
    """Navigating destroys the live page/stream, unlike re-reading its DOM."""

    def __init__(self, *, empty=False):
        super().__init__("live-page", timeout=0.04)
        self.url = "about:blank"
        self.started = False
        self.completed = empty
        self.cancelled = False
        self.finish_after_reads: int | None = None
        self.marker = None
        self.sends = 0
        self.resets = 0
        self.disruptions = 0

    async def start(self):
        pass

    async def close(self):
        pass

    async def reset(self):
        self.resets += 1
        await self.command("open", "about:blank")

    async def configure_effort(self, effort):
        pass

    async def command(self, *args):
        if args[0] == "open":
            if self.started and not self.completed and not self.cancelled:
                self.disruptions += 1
                self.cancelled = True
            self.url = args[1]
        elif args[0] == "fill":
            self.marker = args[2].rsplit("POE-PROXY-REQUEST:", 1)[-1]
        elif args == ("press", "Enter"):
            self.sends += 1
            self.started = True
            self.url = CHAT
        else:
            raise AssertionError(f"Unexpected browser operation: {args[0]}")
        return {}

    async def state(self):
        if not self.started or self.url != CHAT:
            return {"url": self.url, "ready": self.url != "about:blank", "count": 0}
        if self.finish_after_reads is not None and not self.cancelled:
            self.finish_after_reads -= 1
            if self.finish_after_reads == 0:
                self.completed = True
        terminal = self.cancelled or self.completed
        content = '{"type":"text","text":"Finished the original turn"}'
        return {
            "url": CHAT,
            "ready": True,
            "count": 2,
            "userId": "message-user",
            "userHash": "user-content-hash",
            "requestMarker": self.marker,
            "replyId": "message-reply",
            "replyAfterUser": True,
            "hasReply": True,
            "complete": True,
            "generating": not terminal,
            "thinking": True,
            "hasCompletionControls": terminal,
            "text": content
            if self.completed and not self.cancelled and self.finish_after_reads is not None
            else "",
            "agentText": content
            if self.completed and not self.cancelled and self.finish_after_reads is not None
            else None,
        }


@pytest.fixture
def fast_poll(monkeypatch):
    sleep = asyncio.sleep

    async def tick(_):
        await sleep(0)

    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", tick)


def test_timeout_then_retry_must_not_reload_the_still_generating_page(tmp_path, fast_poll):
    transport = LivePage()
    store = SessionStore(tmp_path / "bindings.sqlite3")
    with TestClient(create_app(transport, "local-key", (MODEL,), store)) as client:
        first = client.post("/v1/agent/chat/completions", headers=AUTH, json=BODY)
        assert first.status_code == 504
        assert transport.sends == 1 and transport.resets == 0
        pending = store.get(digest("active-recovery"))
        assert pending and pending["response_id"] == "message-reply"
        transport.finish_after_reads = 5
        recovered = client.post("/v1/agent/chat/completions", headers=AUTH, json=BODY)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["choices"][0]["message"]["content"] == "Finished the original turn"
        assert transport.sends == 1 and transport.disruptions == 0


@pytest.mark.parametrize("stream", [False, True])
def test_plain_chat_also_reports_terminal_empty_not_timeout(
    tmp_path, fast_poll, monkeypatch, stream
):
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)
    transport = LivePage(empty=True)
    with TestClient(create_app(transport, "local-key", (MODEL,))) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                **BODY,
                "reasoning_effort": "medium",
                "stream": stream,
            },
        )
        assert response.status_code == (200 if stream else 409)
        assert "upstream_incomplete_reply" in response.text
        assert transport.sends == 1 and transport.resets == 1


def test_terminal_empty_reply_is_nonretryable_and_does_not_hold_the_browser(
    tmp_path, fast_poll, monkeypatch
):
    # No actual answer exists: reporting another retryable timeout is incorrect.
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0, raising=False)
    transport = LivePage(empty=True)
    transport.timeout = 1
    store = SessionStore(tmp_path / "bindings.sqlite3")
    with TestClient(create_app(transport, "local-key", (MODEL,), store)) as client:
        response = client.post("/v1/agent/chat/completions", headers=AUTH, json=BODY)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "upstream_incomplete_reply"
        assert response.headers["x-should-retry"] == "false"
        assert transport.sends == 1 and transport.resets == 1
        pending = store.get(digest("active-recovery"))
        assert pending and pending["status"] == "uncertain"
        assert "private coding task" not in store.path.read_bytes().decode(errors="ignore")
        retry = client.post("/v1/agent/chat/completions", headers=AUTH, json=BODY)
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] == "upstream_incomplete_reply"
        assert transport.sends == 1
