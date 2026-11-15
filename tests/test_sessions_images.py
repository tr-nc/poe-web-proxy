import asyncio
import base64
import io
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from poe_web_proxy.app import create_app
from poe_web_proxy.browser import Browser, BrowserError
from poe_web_proxy.images import ImageURL
from poe_web_proxy.sessions import SessionStore, digest
from poe_web_proxy.turn import BrowserTurn

MODEL = "Claude-Opus-5.5"
AUTH = {"Authorization": "Bearer test-key", "x-session-affinity": "pi-session-one"}
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "FIRST_PRIVATE_QUESTION"}]}


def image_url(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


class Fake:
    def __init__(self):
        self.calls = []
        self.failure: BrowserError | None = None
        self.submit = True

    async def start(self):
        pass

    async def close(self):
        pass

    async def reset(self):
        pass

    async def generate(self, model, prompt, turn=None):
        self.calls.append((prompt, turn.chat_url if turn else None, turn.images if turn else ()))
        if turn:
            turn.submitted = self.submit
            turn.chat_url = turn.chat_url or "https://poe.com/chat/" + uuid.uuid4().hex
            turn.reply_id = "message-" + uuid.uuid4().hex
        if self.failure:
            raise self.failure
        yield json.dumps({"type": "text", "text": "ANSWER_" + str(len(self.calls))})


def request(client, body=BODY, headers=AUTH):
    return client.post("/v1/agent/chat/completions", headers=headers, json=body)


def follow(body, result, content="SECOND_QUESTION"):
    return {
        **body,
        "messages": [
            *body["messages"],
            result.json()["choices"][0]["message"],
            {"role": "user", "content": content},
        ],
    }


def test_same_session_appends_only_new_messages_and_survives_restart(tmp_path):
    store = SessionStore(tmp_path / "sessions/chats.sqlite3")
    fake = Fake()
    with TestClient(create_app(fake, "test-key", (MODEL,), store)) as client:
        first = request(client)
        assert first.status_code == 200
        old = store.get(digest("pi-session-one"))
        assert old is not None
        second_body = follow(BODY, first)
        second = request(client, second_body)
        assert second.status_code == 200
        assert fake.calls[1][1] == old["chat_url"]
        assert "FIRST_PRIVATE_QUESTION" not in fake.calls[1][0]
        assert "ANSWER_1" not in fake.calls[1][0]
        assert "SECOND_QUESTION" in fake.calls[1][0]
        other = request(client, headers={**AUTH, "x-session-affinity": "other-session"})
        assert other.status_code == 200
        other_row = store.get(digest("other-session"))
        assert other_row is not None and other_row["chat_url"] != old["chat_url"]
    # New API instance and new store object use the durable binding, not process memory.
    restored = SessionStore(store.path)
    with TestClient(create_app(fake, "test-key", (MODEL,), restored)) as client:
        third = request(client, follow(second_body, second, "THIRD_QUESTION"))
        assert third.status_code == 200
        assert fake.calls[-1][1] == old["chat_url"]
        assert "SECOND_QUESTION" not in fake.calls[-1][0]
    assert store.path.stat().st_mode & 0o777 == 0o600
    raw = store.path.read_bytes()
    assert b"FIRST_PRIVATE_QUESTION" not in raw  # only transcript hashes are persisted


def test_context_updates_do_not_replay_old_history(tmp_path):
    store = SessionStore(tmp_path / "sessions/chats.sqlite3")
    fake = Fake()
    with TestClient(create_app(fake, "test-key", (MODEL,), store)) as client:
        first = request(client)
        body = follow(BODY, first)
        body["messages"].insert(0, {"role": "system", "content": "NEW_INSTRUCTIONS"})
        assert request(client, body).status_code == 200
        assert "NEW_INSTRUCTIONS" in fake.calls[-1][0]
        assert "FIRST_PRIVATE_QUESTION" not in fake.calls[-1][0]


def test_divergence_model_switch_and_header_conflicts_fail_before_send(tmp_path):
    store = SessionStore(tmp_path / "sessions/chats.sqlite3")
    fake = Fake()
    with TestClient(create_app(fake, "test-key", (MODEL, "other-model"), store)) as client:
        first = request(client)
        assert request(client).status_code == 409
        assert request(client, {**follow(BODY, first), "model": "other-model"}).status_code == 409
        assert request(client, headers={**AUTH, "session_id": "different"}).status_code == 400
        assert len(fake.calls) == 1


@pytest.mark.parametrize("submitted", [True, False])
def test_failed_send_blocks_ambiguous_retries_but_allows_unsent_retry(tmp_path, submitted):
    store = SessionStore(tmp_path / "sessions/chats.sqlite3")
    fake = Fake()
    fake.failure, fake.submit = BrowserError("failed"), submitted
    with TestClient(create_app(fake, "test-key", (MODEL,), store)) as client:
        assert request(client).status_code == 502
        fake.failure = None
        retry = request(client)
        assert retry.status_code == (409 if submitted else 200)
        assert len(fake.calls) == (1 if submitted else 2)


def test_images_are_uploaded_only_on_the_turn_that_introduces_them(tmp_path):
    store = SessionStore(tmp_path / "sessions/chats.sqlite3")
    fake = Fake()
    picture = {"type": "image_url", "image_url": {"url": image_url()}}
    body = {
        **BODY,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Look at this image"}, picture]}
        ],
    }
    with TestClient(create_app(fake, "test-key", (MODEL,), store)) as client:
        first = request(client, body)
        assert first.status_code == 200
        assert len(fake.calls[0][2]) == 1
        assert "base64" not in fake.calls[0][0]
        assert image_url() not in fake.calls[0][0]
        assert request(client, follow(body, first)).status_code == 200
        assert fake.calls[-1][2] == ()
        # Plain Chat Completions also supports actual image attachments.
        plain = client.post("/v1/chat/completions", headers=AUTH, json=body)
        assert plain.status_code == 200
        assert len(fake.calls[-1][2]) == 1


def test_oversized_plain_text_array_fails_before_browser():
    fake = Fake()
    with TestClient(create_app(fake, "test-key", (MODEL,))) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "x" * 200001}]}],
            },
        )
    assert response.status_code == 400
    assert not fake.calls


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/image.png",
        "file:///etc/passwd",
        "data:image/png;base64,bm90LWFuLWltYWdl",
        "data:image/png;base64,%%%",
    ],
)
def test_invalid_images_are_rejected_without_remote_fetch(url):
    with pytest.raises(ValueError):
        ImageURL(url=url).attachment()


def test_browser_resume_ignores_previous_reply_and_cleans_image_files():
    async def run():
        url = "https://poe.com/chat/known-chat"
        states = iter(
            [
                {"ready": True, "url": url, "replyId": "message-old", "complete": True},
                {"canSend": True, "attachments": 1},
                {
                    "hasReply": True,
                    "url": url,
                    "replyId": "message-old",
                    "complete": True,
                    "text": "DO NOT RETURN THIS OLD ANSWER",
                },
                {
                    "hasReply": True,
                    "url": url,
                    "replyId": "message-new",
                    "complete": True,
                    "text": "new answer",
                },
                {
                    "hasReply": True,
                    "url": url,
                    "replyId": "message-new",
                    "complete": True,
                    "text": "new answer",
                },
            ]
        )
        browser = Browser("test")
        uploads = []

        async def command(*args):
            if args[0] == "upload":
                path = Path(args[2])
                assert path.exists() and path.stat().st_mode & 0o777 == 0o600
                uploads.append(path)
            return {}

        async def state():
            return next(states)

        async def configure_effort(effort):
            assert effort == "medium"

        browser.command, browser.state = command, state
        browser.configure_effort = configure_effort
        turn = BrowserTurn(
            chat_url=url, reply_id="message-old", images=(ImageURL(url=image_url()).attachment(),)
        )
        result = "".join([piece async for piece in browser.generate(MODEL, "next question", turn)])
        assert result == "new answer"
        assert turn.chat_url == url and turn.reply_id == "message-new" and turn.submitted
        assert uploads and not uploads[0].exists()

    asyncio.run(run())
