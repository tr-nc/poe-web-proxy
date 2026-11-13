"""Disconnects at the actual ASGI send boundary, before upstream generation."""

import asyncio
import json

import httpx
import pytest
from starlette.requests import ClientDisconnect
from starlette.types import Message

from poe_web_proxy.app import create_app


@pytest.mark.parametrize("disconnect_at", ["http.response.start", "http.response.body"])
def test_early_stream_disconnect_releases_reservation(disconnect_at):
    async def run():
        class Fake:
            resets = 0
            calls = 0

            async def generate(self, model, prompt):
                self.calls += 1
                yield "fresh reply"

            async def reset(self):
                self.resets += 1

        fake = Fake()
        app = create_app(fake, "test-key", ("GPT-6.1-Sol",))
        body = {"model": "GPT-6.1-Sol", "messages": [{"role": "user", "content": "hi"}]}
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 54321),
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer test-key"),
            ],
        }
        received = False

        async def receive() -> Message:
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": json.dumps({**body, "stream": True}).encode(),
                    "more_body": False,
                }
            return await asyncio.Future[Message]()

        async def send(message):
            if message["type"] == disconnect_at:
                raise OSError("Client socket closed")

        with pytest.raises((OSError, ClientDisconnect)):
            await app(scope, receive, send)
        assert fake.resets == 1
        assert fake.calls == 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions", json=body, headers={"Authorization": "Bearer test-key"}
            )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "fresh reply"
        assert fake.resets == 2

    asyncio.run(run())
