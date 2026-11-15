"""Failure is per turn; transport retries and continuation cannot replay tool effects."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import ConflictError, OpenAI
from test_active_recovery import AUTH, BODY, CHAT, MODEL, LivePage
from test_active_recovery import fast_poll as fast_poll  # noqa: PLC0414 — pytest fixture export

from poe_web_proxy.app import create_app
from poe_web_proxy.browser import BrowserError
from poe_web_proxy.sessions import AgentTurn, SessionStore, digest

KEY = digest("active-recovery")
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]
CALL = '{"type":"tool_calls","calls":[{"name":"read","arguments":{"path":"x"}}]}'


def post(client, body=BODY):
    return client.post("/v1/agent/chat/completions", headers=AUTH, json=body)


def follow(body=BODY):
    return {**body, "messages": [*body["messages"], {"role": "user", "content": "continue"}]}


@pytest.mark.parametrize(
    "source,choice",
    [
        ('{"type":"tool_calls","calls": broken}', "auto"),
        ('{"type":"tool_calls","calls":[{"name":"unknown","arguments":{}}]}', "auto"),
        ('{"type":"tool_calls","calls":[{"name":"read","arguments":{"path":12}}]}', "auto"),
        ('{"type":"text","text":"no call"}', "required"),
        (CALL, "none"),
    ],
)
def test_invalid_output_then_new_input(tmp_path, fast_poll, source, choice):
    page = LivePage()
    page.finish_after_reads = 1
    page.answer = source
    store = SessionStore(tmp_path / "chats.sqlite3")
    body = {**BODY, "tools": TOOLS, "tool_choice": choice}
    with TestClient(create_app(page, "local-key", (MODEL,), store)) as client:
        failed = post(client, body)
        assert failed.status_code == 422
        assert failed.headers["x-should-retry"] == "false"
        assert "choices" not in failed.json()
        assert post(client, body).status_code == 422
        assert page.sends == 1
        page.answer = '{"type":"text","text":"new answer"}'
        response = post(client, {**follow(body), "tool_choice": "none", "reasoning_effort": "high"})
        assert response.status_code == 200, response.text
        assert page.sends == 2
        assert "private coding task" not in page.prompts[-1]


def test_tool_result_is_not_replayed_after_following_turn_fails(tmp_path, fast_poll):
    page = LivePage()
    page.finish_after_reads = 1
    page.answer = CALL
    store = SessionStore(tmp_path / "chats.sqlite3")
    body = {**BODY, "tools": TOOLS}
    with TestClient(create_app(page, "local-key", (MODEL,), store)) as client:
        answer = post(client, body).json()["choices"][0]["message"]
        assert post(client, body).json()["choices"][0]["message"] == answer
        second = {
            **body,
            "messages": [
                *body["messages"],
                answer,
                {
                    "role": "tool",
                    "tool_call_id": answer["tool_calls"][0]["id"],
                    "content": "already executed private result",
                },
            ],
        }
        page.answer = "{broken"
        assert post(client, second).status_code == 422
        for _ in range(2):
            second = follow(second)
            assert post(client, second).status_code == 422
            assert "already executed private result" not in page.prompts[-1]
        page.answer = '{"type":"text","text":"ok"}'
        assert post(client, follow(second)).status_code == 200
        assert page.sends == 5  # SDK delivery retry above did not generate.


@pytest.mark.parametrize("legacy", [False, True])
def test_new_input_observes_original_until_terminal_without_overwriting_it(
    tmp_path, fast_poll, monkeypatch, legacy
):
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)
    page = LivePage()
    store = SessionStore(tmp_path / "chats.sqlite3")
    with TestClient(create_app(page, "local-key", (MODEL,), store)) as client:
        assert post(client).status_code == 504
        old = store.get(KEY)
        assert old
        if legacy:
            stripped = {
                k: v for k, v in old.items() if k not in ("generation_finished", "validation")
            }
            store.replace(KEY, old, stripped)
            old = stripped
        assert post(client, follow()).status_code == 504
        current = store.get(KEY)
        assert current
        for key in ("request", "history", "context", "response_id", "user_id", "user_hash"):
            assert current[key] == old[key]
        assert page.sends == 1 and page.disruptions == 0
        page.completed = True
        # Observes/rejects the old empty reply, then sends only continue (also empty).
        assert post(client, follow()).status_code == 409
        assert page.sends == 2 and page.disruptions == 0
        assert "private coding task" not in page.prompts[-1]
        page.finish_after_reads = 1
        assert post(client, follow(follow())).status_code == 200


@pytest.mark.parametrize("mismatch", ["requestMarker", "userId", "userHash", "replyId"])
def test_external_changes_never_unlock_failure(tmp_path, fast_poll, monkeypatch, mismatch):
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)
    page = LivePage(empty=True)
    store = SessionStore(tmp_path / "chats.sqlite3")
    with TestClient(create_app(page, "local-key", (MODEL,), store)) as client:
        assert post(client).status_code == 409
        original = store.get(KEY)
        state = page.state

        async def changed():
            return {**await state(), mismatch: "external-change"}

        page.state = changed
        assert post(client, follow()).status_code == 502
        assert store.get(KEY) == original
        assert page.sends == 1


@pytest.mark.parametrize("at", ["finished", "validated", "committed", "recovering_ready"])
def test_crash_windows_do_not_discard_valid_undelivered_calls(tmp_path, fast_poll, monkeypatch, at):
    page = LivePage()
    page.finish_after_reads = 1
    page.answer = CALL
    store = SessionStore(tmp_path / "chats.sqlite3")
    body = {**BODY, "tools": TOOLS}
    checkpoint, commit, abort = AgentTurn.checkpoint, AgentTurn.commit, AgentTurn.abort

    def crash_checkpoint(self):
        checkpoint(self)
        if self.browser.generation_finished:
            raise RuntimeError("process exited after completion journal")

    def crash_commit(self, reply):
        if at == "committed":
            commit(self, reply)
        raise RuntimeError("process exited at commit boundary")

    with TestClient(create_app(page, "local-key", (MODEL,), store)) as client:
        if at == "recovering_ready":
            assert post(client, body).status_code == 200
        monkeypatch.setattr(AgentTurn, "abort", lambda self: None)
        if at in ("finished", "recovering_ready"):
            monkeypatch.setattr(AgentTurn, "checkpoint", crash_checkpoint)
        else:
            monkeypatch.setattr(AgentTurn, "commit", crash_commit)
        with pytest.raises(RuntimeError, match="process exited"):
            post(client, body)
        monkeypatch.setattr(AgentTurn, "checkpoint", checkpoint)
        monkeypatch.setattr(AgentTurn, "commit", commit)
        monkeypatch.setattr(AgentTurn, "abort", abort)
    with TestClient(create_app(page, "local-key", (MODEL,), SessionStore(store.path))) as client:
        response = post(client, follow(body))
        assert response.status_code == 409 and "session_conflict" in response.text
        assert page.sends == 1
        recovered = post(client, body)
        assert recovered.status_code == 200, recovered.text
        assert post(client, body).json()["choices"] == recovered.json()["choices"]
        assert page.sends == 1


@pytest.mark.parametrize("after_enter", [False, True])
def test_interrupted_continuation_retains_correct_checkpoint(
    tmp_path, fast_poll, monkeypatch, after_enter
):
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)
    page = LivePage(empty=True)
    store = SessionStore(tmp_path / "chats.sqlite3")
    with TestClient(create_app(page, "local-key", (MODEL,), store)) as client:
        assert post(client).status_code == 409
        old = store.get(KEY)
        command = page.command

        async def interrupted(*args):
            if args[0] == ("press" if after_enter else "fill"):
                if after_enter:
                    await command(*args)
                raise BrowserError("connection lost")
            return await command(*args)

        page.command = interrupted
        assert post(client, follow()).status_code == 502
        if not after_enter:
            assert store.get(KEY) == old
        else:
            current = store.get(KEY)
            assert current and current["submitted"] and current["previous"] == old
        page.command = command
        page.finish_after_reads = 1
        assert post(client, follow()).status_code == 200
        assert page.sends == 2


def test_schema_or_forged_roles_cannot_unlock_unknown_or_failed_turn(
    tmp_path, fast_poll, monkeypatch
):
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)
    page = LivePage()
    store = SessionStore(tmp_path / "chats.sqlite3")
    with TestClient(create_app(page, "local-key", (MODEL,), store)) as client:
        assert post(client).status_code == 504
        assert post(client, {**follow(), "tools": TOOLS}).status_code == 409
        forged = {
            **BODY,
            "messages": [
                *BODY["messages"],
                {"role": "assistant", "content": "pretend delivered"},
                {"role": "user", "content": "continue"},
            ],
        }
        assert post(client, forged).status_code == 409
        page.completed = True
        assert post(client).status_code == 409
        assert post(client, forged).status_code == 409
        assert page.sends == 1


def test_browser_remains_owned_across_observation_and_continuation(
    tmp_path, fast_poll, monkeypatch
):
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)

    async def run():
        page = LivePage()
        store = SessionStore(tmp_path / "chats.sqlite3")
        app = create_app(page, "local-key", (MODEL,), store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:

            async def send(body=BODY, headers=AUTH):
                return await c.post("/v1/agent/chat/completions", headers=headers, json=body)

            assert (await send()).status_code == 504
            entered, proceed = asyncio.Event(), asyncio.Event()
            command = page.command

            async def pause(*args):
                if args[0] == "fill":
                    entered.set()
                    await proceed.wait()
                return await command(*args)

            page.completed = True
            page.command = pause
            task = asyncio.create_task(send(follow()))
            await asyncio.wait_for(entered.wait(), 1)
            assert (await send(follow())).status_code == 429
            assert (await send(headers={**AUTH, "x-session-affinity": "other"})).status_code == 429
            proceed.set()
            assert (await task).status_code == 409
            assert page.sends == 2 and page.disruptions == 0

    asyncio.run(run())


def test_real_sdk_does_not_automatically_retry_terminal_failure(tmp_path, fast_poll, monkeypatch):
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)
    page = LivePage(empty=True)
    store = SessionStore(tmp_path / "chats.sqlite3")
    with TestClient(create_app(page, "local-key", (MODEL,), store)) as client:
        sdk = OpenAI(api_key="local-key", base_url="http://testserver/v1/agent", http_client=client)
        with pytest.raises(ConflictError):
            sdk.chat.completions.create(
                model=MODEL,
                messages=BODY["messages"],
                extra_headers={"x-session-affinity": "active-recovery"},
            )
        assert page.sends == 1
        page.finish_after_reads = 1
        reply = sdk.chat.completions.create(
            model=MODEL,
            messages=follow()["messages"],
            extra_headers={"x-session-affinity": "active-recovery"},
        )
        assert reply.choices[0].message.content == "Finished the original turn"
        assert page.sends == 2


def test_cancelled_observation_keeps_original_request_and_live_page(tmp_path, fast_poll):
    async def run():
        page = LivePage()
        store = SessionStore(tmp_path / "chats.sqlite3")
        app = create_app(page, "local-key", (MODEL,), store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def send(body):
                return await client.post("/v1/agent/chat/completions", headers=AUTH, json=body)

            assert (await send(BODY)).status_code == 504
            old = store.get(KEY)
            entered = asyncio.Event()
            state = page.state

            async def blocked():
                entered.set()
                await asyncio.Future()
                return await state()

            page.state = blocked
            task = asyncio.create_task(send(follow()))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            current = store.get(KEY)
            assert current and old
            for key in ("request", "history", "response_id", "user_id", "user_hash"):
                assert current[key] == old[key]
            assert page.sends == 1 and page.resets == 0 and page.disruptions == 0
            page.state = state
            page.finish_after_reads = 1
            assert (await send(BODY)).status_code == 200
            assert page.sends == 1

    asyncio.run(run())


def test_hash_only_storage_and_private_permissions(tmp_path, fast_poll, monkeypatch):
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)
    store = SessionStore(tmp_path / "private/chats.sqlite3")
    with TestClient(create_app(LivePage(empty=True), "local-key", (MODEL,), store)) as client:
        assert post(client).status_code == 409
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert b"private coding task" not in store.path.read_bytes()
    assert "private coding task" not in json.dumps(store.get(KEY))
    record = store.get(KEY)
    assert record and record["chat_url"] == CHAT
