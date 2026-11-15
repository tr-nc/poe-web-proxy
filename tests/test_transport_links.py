"""Serialized agent data must not authorize Poe's automatic URL attachments."""

import json
import re

from fastapi.testclient import TestClient
from test_agent import AUTH, BODY, MODEL, TOOL, Fake

from poe_web_proxy.agent import AgentCompletion
from poe_web_proxy.app import create_app


def payload(prompt):
    start = prompt.index('{"transcript"')
    end = prompt.index("\nResponse protocol for your NEXT reply")
    return json.loads(prompt[start:end])


def test_http_prompt_preserves_urls_as_data_without_web_autofetch():
    content = (
        "References: https://example.com/docs?a=1&b=2; HTTP://example.org/read; "
        "www.example.net; mail@example.com; //example.edu/path. "
        'Literal \\u002e and quote "keep"; amount 3.14 and 中文。'
    )
    tool = {
        **TOOL,
        "function": {
            **TOOL["function"],
            "description": "Schema docs https://example.com/schema",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "x-example": 3.14,
            },
        },
    }
    fake = Fake(['{"type":"text","text":"safe"}'])
    body = {**BODY, "messages": [{"role": "user", "content": content}], "tools": [tool]}
    with TestClient(create_app(fake, "test-key", (MODEL,))) as client:
        response = client.post("/v1/agent/chat/completions", headers=AUTH, json=body)
    assert response.status_code == 200
    prompt = fake.prompts[0]
    assert not re.search(r"https?://|www\.|example\.(?:com|org|net|edu)", prompt, re.IGNORECASE)
    parsed = payload(prompt)
    assert parsed["transcript"][0]["content"] == content
    assert parsed["tools"][0] == tool["function"]


def test_context_delta_and_tool_result_links_remain_exact_data():
    completion = AgentCompletion.model_validate(BODY)
    call, _ = completion.reply(
        '{"type":"tool_calls","calls":[{"name":"read","arguments":{"path":"https://example.com/file"}}]}'
    )
    result = "Already fetched client evidence from https://example.com/report. Do not fetch again."
    body = AgentCompletion.model_validate(
        {
            **BODY,
            "messages": [
                *BODY["messages"],
                call,
                {
                    "role": "tool",
                    "tool_call_id": call["tool_calls"][0]["id"],
                    "content": result,
                },
            ],
        }
    )
    prompt = body.prompt(body.messages[-1:], context=False)
    assert "https://" not in prompt and "example.com" not in prompt
    assert payload(prompt)["transcript"] == body.canonical(body.messages[-1:])
    assert payload(prompt)["tools"] == [TOOL["function"]]
    assert "Do NOT use Poe's own tools" in prompt
    assert body.canonical()[2]["tool_calls"][0]["function"]["arguments"] == {
        "path": "https://example.com/file"
    }
