import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from poe_web_proxy.app import create_app
from poe_web_proxy.browser import EFFORT_SELECT, READ_EFFORT, Browser, BrowserError
from poe_web_proxy.sessions import SessionStore, digest
from poe_web_proxy.turn import BrowserTurn

LEVELS = ["low", "medium", "high", "xhigh", "max"]
MODEL = "Claude-Opus-5.5"
AUTH = {"Authorization": "Bearer test-key", "x-session-affinity": "effort-session"}
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "first question"}]}


class Fake:
    def __init__(self):
        self.calls = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def reset(self):
        pass

    async def generate(self, model, prompt, turn=None):
        self.calls.append(
            (prompt, turn.reasoning_effort if turn else None, turn.chat_url if turn else None)
        )
        if turn:
            turn.submitted = True
            turn.chat_url = "https://poe.com/chat/effort-test"
            turn.reply_id = "message-" + str(len(self.calls))
        yield json.dumps({"type": "text", "text": "answer"})


@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/agent/chat/completions"])
@pytest.mark.parametrize("level", LEVELS)
def test_http_effort_reaches_browser_without_becoming_prompt_text(endpoint, level):
    fake = Fake()
    with TestClient(create_app(fake, "test-key", (MODEL,))) as client:
        result = client.post(
            endpoint,
            headers={"Authorization": AUTH["Authorization"]},
            json={**BODY, "reasoning_effort": level},
        )
    assert result.status_code == 200
    assert fake.calls[0][1] == level
    assert "reasoning_effort" not in fake.calls[0][0]


@pytest.mark.parametrize("level", ["off", "none", "minimal", "invalid", 10])
def test_unsupported_levels_fail_before_send(level):
    fake = Fake()
    with TestClient(create_app(fake, "test-key", (MODEL,))) as client:
        for endpoint in ("/v1/chat/completions", "/v1/agent/chat/completions"):
            result = client.post(endpoint, headers=AUTH, json={**BODY, "reasoning_effort": level})
            assert result.status_code == 400
    assert not fake.calls


def test_effort_change_preserves_chat_and_incremental_history(tmp_path):
    store = SessionStore(tmp_path / "private/chats.sqlite3")
    fake = Fake()
    with TestClient(create_app(fake, "test-key", (MODEL,), store)) as client:
        first = client.post(
            "/v1/agent/chat/completions", headers=AUTH, json={**BODY, "reasoning_effort": "low"}
        )
        assert first.status_code == 200
        before = store.get(digest("effort-session"))
        assert before
        follow = [
            *BODY["messages"],
            first.json()["choices"][0]["message"],
            {"role": "user", "content": "second question"},
        ]
        result = client.post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={**BODY, "reasoning_effort": "max", "messages": follow},
        )
        assert result.status_code == 200
        assert fake.calls[1][1:] == ("max", before["chat_url"])
        assert "first question" not in fake.calls[1][0]
        after = store.get(digest("effort-session"))
        assert after and after["context"] == before["context"]


def test_native_dropdown_is_selected_verified_and_closed():
    async def run():
        browser = Browser("test")
        controls = iter(
            [
                {"button": True, "open": False, "options": []},
                {"button": True, "open": True, "options": LEVELS, "value": "medium"},
                {"button": True, "open": True, "options": LEVELS, "value": "max"},
            ]
        )
        commands = []

        async def command(*args):
            commands.append(args)
            if args[0] == "eval":
                assert args[1] == READ_EFFORT
                return {"result": next(controls)}
            return {}

        browser.command = command
        await browser.configure_effort("max")
        assert ("select", EFFORT_SELECT, "max") in commands
        assert sum(args[0] == "click" for args in commands) == 2

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing-control", "missing-option", "readback-mismatch"])
def test_effort_failure_never_submits(monkeypatch, failure):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", no_sleep)

    async def run():
        browser = Browser("test")
        commands = []

        async def state():
            return {"ready": True, "count": 0}

        async def command(*args):
            commands.append(args)
            if failure == "missing-control":
                return {"result": {"button": False, "options": []}}
            return {
                "result": {
                    "button": True,
                    "open": True,
                    "options": ["low", "medium", "high"] if failure == "missing-option" else LEVELS,
                    "value": "medium",
                }
            }

        browser.command, browser.state = command, state
        turn = BrowserTurn(reasoning_effort="max")
        with pytest.raises(BrowserError, match="not sent"):
            async for _ in browser.generate(MODEL, "question", turn):
                pass
        assert not turn.submitted
        assert all(args[0] not in ("press", "fill") for args in commands)

    asyncio.run(run())


def test_unspecified_opus_effort_resets_to_medium_instead_of_inheriting_max():
    async def run():
        browser = Browser("test")
        efforts = []
        states = iter(
            [
                {"ready": True, "count": 0},
                {"hasReply": True, "complete": True, "text": "ok"},
                {"hasReply": True, "complete": True, "text": "ok"},
            ]
        )

        async def command(*args):
            return {}

        async def state():
            return next(states)

        async def configure(effort):
            efforts.append(effort)

        browser.command, browser.state, browser.configure_effort = command, state, configure
        result = [part async for part in browser.generate(MODEL, "question")]
        assert result == ["ok"] and efforts == ["medium"]

    asyncio.run(run())
