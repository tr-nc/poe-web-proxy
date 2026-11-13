import asyncio
import json
import threading
from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient

from poe_web_proxy.app import create_app
from poe_web_proxy.browser import Browser, BrowserError

MODEL = "GPT-6.1-Sol"
HEADERS = {"Authorization": "Bearer local-test-key"}
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]}


class Fake:
    def __init__(self, failure=None, reset_failure=False):
        self.failure = failure
        self.reset_failure = reset_failure
        self.calls = []
        self.resets = 0
        self.starts = 0
        self.closes = 0

    async def start(self):
        self.starts += 1

    async def close(self):
        self.closes += 1

    async def generate(self, model, prompt, turn=None) -> AsyncGenerator[str, None]:
        self.calls.append((model, prompt))
        if self.failure:
            raise self.failure
        yield "Hello"
        yield " world"

    async def reset(self):
        self.resets += 1
        if self.reset_failure:
            raise BrowserError("reset failed")


def test_nonstream_real_contract_and_fresh_request():
    fake = Fake()
    with TestClient(create_app(fake, "local-test-key", (MODEL,))) as client:
        for _ in range(2):
            r = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"] == "Hello world"
            assert "usage" not in r.json()
        assert fake.resets == 2
        assert fake.calls == [(MODEL, "hello"), (MODEL, "hello")]


def test_stream_deltas_and_finish():
    fake = Fake()
    with TestClient(create_app(fake, "local-test-key", (MODEL,))) as client:
        r = client.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "stream": True})
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
        assert events[-1] == "[DONE]"
        chunks = [json.loads(e) for e in events[:-1]]
        assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Hello world"
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert len({c["id"] for c in chunks}) == 1
        assert fake.resets == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"tools": []},
        {"temperature": 0},
        {"max_tokens": 20},
        {"messages": [{"role": "system", "content": "secret"}]},
        {"messages": BODY["messages"] * 2},
        {"messages": [{"role": "user", "content": ""}]},
    ],
)
def test_reject_unsupported_without_contacting_browser(patch):
    fake = Fake()
    with TestClient(create_app(fake, "local-test-key", (MODEL,))) as client:
        r = client.post("/v1/chat/completions", headers=HEADERS, json={**BODY, **patch})
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"
        assert not fake.calls


def test_auth_and_allowlist():
    fake = Fake()
    with TestClient(create_app(fake, "local-test-key", (MODEL,))) as client:
        assert client.get("/v1/models").status_code == 401
        assert client.post("/v1/chat/completions", json=BODY).status_code == 401
        assert client.get("/v1/models", headers=HEADERS).json()["data"][0]["id"] == MODEL
        r = client.post(
            "/v1/chat/completions", headers=HEADERS, json={**BODY, "model": "unconfigured"}
        )
        assert r.status_code == 404
        assert not fake.calls


@pytest.mark.parametrize(
    "failure,status", [(BrowserError("login required"), 502), (TimeoutError(), 504)]
)
def test_failure_releases_browser(failure, status):
    fake = Fake(failure)
    with TestClient(create_app(fake, "local-test-key", (MODEL,))) as client:
        r = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        assert r.status_code == status
        fake.failure = None
        assert client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 200
        assert fake.resets == 2


def test_stream_failure_never_claims_success():
    fake = Fake(BrowserError("login required"))
    with TestClient(create_app(fake, "local-test-key", (MODEL,))) as client:
        r = client.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "stream": True})
        assert "upstream_error" in r.text
        assert "[DONE]" not in r.text
        assert '"finish_reason": "stop"' not in r.text
        assert fake.resets == 1


def test_cleanup_failure_quarantines_session():
    fake = Fake(reset_failure=True)
    with TestClient(create_app(fake, "local-test-key", (MODEL,))) as client:
        assert client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 200
        assert client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 503
        assert len(fake.calls) == 1


def test_concurrency_rejected_without_mixing_requests():
    entered = threading.Event()
    finish = threading.Event()

    class Blocking(Fake):
        async def generate(self, model, prompt, turn=None):
            entered.set()
            while not finish.is_set():
                await asyncio.sleep(0.01)
            yield "first response"

    fake = Blocking()
    results = []
    with TestClient(create_app(fake, "local-test-key", (MODEL,))) as client:
        thread = threading.Thread(
            target=lambda: results.append(
                client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
            )
        )
        thread.start()
        try:
            assert entered.wait(3)
            r = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
            assert r.status_code == 429
        finally:
            finish.set()
            thread.join(5)
        assert results[0].status_code == 200
        assert fake.resets == 1


class ScriptedBrowser(Browser):
    def __init__(self, states):
        super().__init__("test")
        self.states = iter(states)
        self.commands = []

    async def command(self, *args):
        self.commands.append(args)
        return {}

    async def state(self):
        return next(self.states)


def collect(browser):
    async def run():
        return [delta async for delta in browser.generate(MODEL, "unique prompt")]

    return asyncio.run(run())


def test_browser_sends_exact_prompt_and_streams_observed_text():
    b = ScriptedBrowser(
        [
            {"ready": True, "count": 0},
            {"text": "One", "hasReply": True, "complete": False},
            {"text": "One two", "hasReply": True, "complete": True},
        ]
    )
    assert collect(b) == ["One", " two"]
    assert b.commands == [
        ("open", f"https://poe.com/{MODEL}"),
        ("fill", "textarea", "unique prompt"),
        ("press", "Enter"),
    ]


@pytest.mark.parametrize(
    "states",
    [
        [{"login": True}],
        [{"ready": True, "count": 2}],
        [{"ready": True, "count": 0}, {"text": "", "hasReply": True, "complete": True}],
        [
            {"ready": True, "count": 0},
            {"text": "abc", "hasReply": True},
            {"text": "xyz", "hasReply": True},
        ],
    ],
)
def test_browser_fails_closed(states):
    with pytest.raises(BrowserError):
        collect(ScriptedBrowser(states))


def test_browser_timeout():
    async def run():
        browser = Browser("test", timeout=0.01)

        # Use an actual async sleeper so timeout cancels the pending command.
        async def slow(*args):
            await asyncio.sleep(1)
            return {}

        browser.command = slow
        with pytest.raises(TimeoutError):
            async for _ in browser.generate(MODEL, "hello"):
                pass

    asyncio.run(run())
