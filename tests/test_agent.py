import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from poe_web_proxy.agent import AgentCompletion
from poe_web_proxy.app import create_app
from poe_web_proxy.browser import BrowserError

MODEL = "Claude-Opus-5.5"
AUTH = {"Authorization": "Bearer test-key"}
TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}
BODY = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "Follow user instructions"},
        {"role": "user", "content": [{"type": "text", "text": "Read example.txt"}]},
    ],
    "tools": [TOOL],
    "max_completion_tokens": None,
}
CALL = {"type": "tool_calls", "calls": [{"name": "read", "arguments": {"path": "example.txt"}}]}


class Fake:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def reset(self):
        pass

    async def generate(self, model, prompt, turn=None):
        self.prompts.append(prompt)
        yield next(self.replies)


def test_tool_round_trip_keeps_transcript_and_schema():
    fake = Fake([json.dumps(CALL), '{"type":"text","text":"The file contains 42."}'])
    with TestClient(create_app(fake, "test-key", (MODEL,))) as client:
        first = client.post("/v1/agent/chat/completions", headers=AUTH, json=BODY)
        assert first.status_code == 200
        choice = first.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        message = choice["message"]
        call = message["tool_calls"][0]
        assert json.loads(call["function"]["arguments"]) == {"path": "example.txt"}
        transcript = [
            *BODY["messages"],
            message,
            {"role": "tool", "tool_call_id": call["id"], "content": "42"},
        ]
        second = client.post(
            "/v1/agent/chat/completions", headers=AUTH, json={**BODY, "messages": transcript}
        )
        assert second.status_code == 200
        assert second.json()["choices"][0]["message"]["content"] == "The file contains 42."
        assert '"content": "42"' in fake.prompts[1]
        assert '"role": "system"' in fake.prompts[1]
        assert '"additionalProperties": false' in fake.prompts[0]


def test_stream_contains_only_validated_call_not_raw_envelope():
    fake = Fake([json.dumps(CALL)])
    with TestClient(create_app(fake, "test-key", (MODEL,))) as client:
        response = client.post(
            "/v1/agent/chat/completions", headers=AUTH, json={**BODY, "stream": True}
        )
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")
    ]
    call = events[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["index"] == 0 and call["function"]["name"] == "read"
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert "[DONE]" in response.text


@pytest.mark.parametrize(
    "reply",
    [
        "not JSON",
        "```json\n{}\n```",
        "[]",
        '{"type":"text","text":""}',
        '{"type":"text","text":"ok","calls":[]}',
        '{"type":"tool_calls","calls":[]}',
        '{"type":"tool_calls","calls":[{"name":"bash","arguments":{}}]}',
        '{"type":"tool_calls","calls":[{"name":"read","arguments":{"path":2}}]}',
        '{"type":"tool_calls","calls":[{"name":"read","arguments":{"path":"x","extra":1}}]}',
        '{"type":"tool_calls","calls":[{"name":"read","arguments":"{}"}]}',
    ],
)
def test_bad_model_output_is_never_forwarded_as_tool_call(reply):
    request = AgentCompletion.model_validate(BODY)
    with pytest.raises(BrowserError):
        request.reply(reply)
    with TestClient(create_app(Fake([reply]), "test-key", (MODEL,))) as client:
        response = client.post(
            "/v1/agent/chat/completions", headers=AUTH, json={**BODY, "stream": True}
        )
    assert response.status_code == 422
    assert "tool_calls" not in response.json()["error"]


@pytest.mark.parametrize(
    "patch",
    [
        {"max_completion_tokens": 4000},
        {"temperature": 0},
        {"tools": [TOOL, TOOL]},
        {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "bad",
                        "parameters": {"$ref": "https://example.com/schema.json"},
                    },
                }
            ]
        },
        {
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
            ]
        },
        {"messages": [{"role": "tool", "content": "42", "tool_call_id": "missing"}]},
        {"messages": [{"role": "assistant", "content": None}]},
    ],
)
def test_unsupported_requests_fail_before_browser(patch):
    fake = Fake([])
    with TestClient(create_app(fake, "test-key", (MODEL,))) as client:
        response = client.post("/v1/agent/chat/completions", headers=AUTH, json={**BODY, **patch})
    assert response.status_code == 400
    assert not fake.prompts


def test_oversized_prompt_and_invalid_tool_choice():
    request = AgentCompletion.model_validate(
        {**BODY, "messages": [{"role": "user", "content": "x" * 200001}]}
    )
    with pytest.raises(ValueError, match="200000"):
        request.prompt()
    with pytest.raises(ValidationError):
        AgentCompletion.model_validate({**BODY, "tools": [], "tool_choice": "required"})
    request = AgentCompletion.model_validate({**BODY, "tool_choice": "none"})
    with pytest.raises(BrowserError):
        request.reply(json.dumps(CALL))
