"""A retry may read a confirmed existing send, never replay it or its tool effects."""

import json

import pytest
from fastapi.testclient import TestClient

from poe_web_proxy.agent import AgentCompletion
from poe_web_proxy.app import create_app
from poe_web_proxy.sessions import AgentTurn, SessionConflict, SessionStore, digest

MODEL = "Claude-Opus-5.5"
AUTH = {"Authorization": "Bearer test-key", "x-session-affinity": "recover-me"}
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "private request"}]}


class Interrupted:
    def __init__(self):
        self.sends = 0
        self.reads = 0
        self.resets = 0
        self.timeout = True

    async def start(self):
        pass

    async def close(self):
        pass

    async def reset(self):
        self.resets += 1

    async def generate(self, model, prompt, turn=None):
        assert turn
        if turn.recovering:
            self.reads += 1
        else:
            self.sends += 1
            turn.submitted = True
            turn.chat_url = "https://poe.com/chat/recover-test"
            turn.user_id = f"message-user-{self.sends}"
            turn.response_id = f"message-reply-{self.sends}"
            turn.checkpoint()
        if self.timeout:
            raise TimeoutError
        turn.reply_id = turn.response_id
        turn.reply_received = True
        yield json.dumps({"type": "text", "text": "Recovered answer"})


def request(client, body=BODY, headers=AUTH):
    return client.post("/v1/agent/chat/completions", headers=headers, json=body)


def test_timeout_retry_reads_existing_send_and_then_continues_same_chat(tmp_path):
    store = SessionStore(tmp_path / "bindings.sqlite3")
    transport = Interrupted()
    with TestClient(create_app(transport, "test-key", (MODEL,), store)) as client:
        assert request(client).status_code == 504
        assert transport.resets == 0  # Never navigate away from an unfinished paid generation.
        pending = store.get(digest("recover-me"))
        assert pending and pending["status"] == "uncertain"
        assert pending["user_id"] == "message-user-1"
        assert "private request" not in store.path.read_bytes().decode(errors="ignore")
        other = request(client, headers={**AUTH, "x-session-affinity": "other-session"})
        assert other.status_code == 429
        transport.timeout = False
        recovered = request(client)
        assert recovered.status_code == 200, recovered.text
        assert transport.sends == 1 and transport.reads == 1
        assert transport.resets == 1
        answer = recovered.json()["choices"][0]["message"]
        body = {
            **BODY,
            "messages": [*BODY["messages"], answer, {"role": "user", "content": "next"}],
        }
        assert request(client, body).status_code == 200
        assert transport.sends == 2
        ready = store.get(digest("recover-me"))
        assert ready and ready["status"] == "ready"


def test_recovery_survives_restart_and_completed_response_retries_are_read_only(tmp_path):
    store = SessionStore(tmp_path / "bindings.sqlite3")
    first = Interrupted()
    with TestClient(create_app(first, "test-key", (MODEL,), store)) as client:
        assert request(client).status_code == 504
    second = Interrupted()
    second.timeout = False
    with TestClient(create_app(second, "test-key", (MODEL,), SessionStore(store.path))) as client:
        recovered = request(client)
        assert recovered.status_code == 200, recovered.text
        again = request(client)
        assert again.status_code == 200, again.text
        assert recovered.json()["choices"] == again.json()["choices"]
        assert second.sends == 0 and second.reads == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"messages": [{"role": "user", "content": "different request"}]},
        {"reasoning_effort": "high"},
        {"tool_choice": "none"},
        {"model": "GPT-6.1-Sol"},
    ],
)
def test_different_request_cannot_recover_or_replay_an_uncertain_send(tmp_path, patch):
    store = SessionStore(tmp_path / "bindings.sqlite3")
    transport = Interrupted()
    with TestClient(create_app(transport, "test-key", (MODEL, "GPT-6.1-Sol"), store)) as client:
        assert request(client).status_code == 504
        response = request(client, {**BODY, **patch})
        assert response.status_code == 409
        assert transport.sends == 1 and transport.reads == 0


def test_failed_recovery_reservation_does_not_rollback_another_owner(tmp_path):
    store = SessionStore(tmp_path / "bindings.sqlite3")
    transport = Interrupted()
    with TestClient(create_app(transport, "test-key", (MODEL,), store)) as client:
        assert request(client).status_code == 504
    body = AgentCompletion.model_validate(BODY)
    first = AgentTurn(body, "recover-me", store)
    second = AgentTurn(body, "recover-me", store)
    first.reserve()
    with pytest.raises(SessionConflict):
        second.reserve()
    second.abort()
    pending = store.get(digest("recover-me"))
    assert pending and pending["status"] == "pending"
    first.abort()
    restored = store.get(digest("recover-me"))
    assert restored and restored["status"] == "uncertain"


def test_recovered_tool_call_id_is_stable_across_response_delivery_retries(tmp_path):
    class Tool(Interrupted):
        async def generate(self, model, prompt, turn=None):
            assert turn
            if turn.recovering:
                self.reads += 1
            else:
                self.sends += 1
                turn.submitted = True
                turn.chat_url = "https://poe.com/chat/recover-test"
                turn.user_id = "message-user-1"
                turn.response_id = "message-reply-1"
                turn.checkpoint()
            turn.reply_id = turn.response_id
            turn.reply_received = True
            yield '{"type":"tool_calls","calls":[{"name":"read","arguments":{"path":"x"}}]}'

    body = {
        **BODY,
        "tools": [
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
        ],
    }
    store = SessionStore(tmp_path / "bindings.sqlite3")
    transport = Tool()
    with TestClient(create_app(transport, "test-key", (MODEL,), store)) as client:
        first = request(client, body)
        retry = request(client, body)
        assert first.status_code == retry.status_code == 200
        assert first.json()["choices"] == retry.json()["choices"]
        assert transport.sends == 1 and transport.reads == 1
