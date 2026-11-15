"""Agent envelopes travel as code, not as lossy rendered Markdown."""

import asyncio
import html
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from poe_web_proxy.agent import AgentCompletion
from poe_web_proxy.app import create_app
from poe_web_proxy.browser import Browser
from poe_web_proxy.sessions import SessionStore, digest
from poe_web_proxy.turn import BrowserTurn

MODEL = "Claude-Opus-5.5"
CODE = 'git show HEAD~1 HEAD~2; rg -n "pixel"; echo **literal**; echo `name`; echo \\path'
ENVELOPE = json.dumps(
    {"type": "tool_calls", "calls": [{"name": "codemode", "arguments": {"code": CODE}}]},
    ensure_ascii=False,
)
BODY = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Inspect the changes"}],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "codemode",
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


class RenderedBrowser(Browser):
    def __init__(self, source, text='json\n{"type":"tool_calls","calls": broken}'):
        super().__init__("agent-transport-test")
        self.source = source
        self.text = text
        self.turns = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def command(self, *args):
        return {}

    async def configure_effort(self, effort):
        pass

    async def generate(self, model, prompt, turn=None):
        assert turn is not None
        self.turns.append(turn)
        self.states = iter(
            [
                {
                    "ready": True,
                    "count": 0,
                    "url": turn.chat_url,
                    "replyId": turn.reply_id,
                    "complete": True,
                },
                {"hasReply": True, "text": "partial rendered text", "complete": False},
                {
                    "hasReply": True,
                    "text": self.text,
                    "agentText": self.source,
                    "complete": True,
                    "replyId": f"message-{len(self.turns)}",
                    "url": "https://poe.com/chat/test-format",
                },
            ]
        )
        async for delta in super().generate(model, prompt, turn):
            yield delta

    async def state(self):
        return next(self.states)


def test_stateless_agent_forwards_exact_code_not_rendered_text():
    browser = RenderedBrowser(ENVELOPE)
    with TestClient(create_app(browser, "test-key", (MODEL,))) as client:
        response = client.post(
            "/v1/agent/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={**BODY, "stream": True},
        )
    assert response.status_code == 200
    event = next(line[6:] for line in response.text.splitlines() if line.startswith("data: {"))
    call = json.loads(event)["choices"][0]["delta"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"code": CODE}
    assert browser.turns[0].structured


@pytest.mark.parametrize("source", [None, "", "   ", '{"type":"tool_calls","calls":[]}'])
def test_missing_or_invalid_code_envelope_is_not_forwarded(source):
    with TestClient(create_app(RenderedBrowser(source), "test-key", (MODEL,))) as client:
        response = client.post(
            "/v1/agent/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json=BODY,
        )
    assert response.status_code == 502
    assert "no tool call was forwarded" in response.json()["error"]["message"]


@pytest.mark.parametrize("tool_choice", ["auto", "none"])
def test_plain_final_answer_keeps_bound_session_usable(tmp_path, tool_choice):
    answer = "描边有两类：外轮廓和内部棱线。\n先做方案 A。你计划支持网页版吗？"
    browser = RenderedBrowser(None, answer)
    store = SessionStore(tmp_path / "chats.sqlite3")
    headers = {"Authorization": "Bearer test-key", "x-session-affinity": "plain-final"}
    body = {**BODY, "tool_choice": tool_choice}
    with TestClient(create_app(browser, "test-key", (MODEL,), store)) as client:
        first = client.post("/v1/agent/chat/completions", headers=headers, json=body)
        assert first.status_code == 200, first.text
        message = first.json()["choices"][0]["message"]
        assert message == {"role": "assistant", "content": answer}
        record = store.get(digest("plain-final"))
        assert record and record["status"] == "ready"
        second = client.post(
            "/v1/agent/chat/completions",
            headers=headers,
            json={
                **body,
                "messages": [*body["messages"], message, {"role": "user", "content": "选 A"}],
            },
        )
        assert second.status_code == 200, second.text
        assert len(browser.turns) == 2
        assert browser.turns[1].chat_url == record["chat_url"]


@pytest.mark.parametrize(
    "text",
    [
        '{"type":"tool_calls","calls": broken}',
        'Here is a call: {"type":"tool_calls","calls": broken}',
        'json\n{"type":"text","text":"broken"quotes"}',
        '[{"name":"read","arguments":{}}]',
    ],
)
def test_damaged_protocol_never_falls_back_to_plain_answer(text):
    with TestClient(create_app(RenderedBrowser(None, text), "test-key", (MODEL,))) as client:
        response = client.post(
            "/v1/agent/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json=BODY,
        )
        assert response.status_code == 502


def test_damaged_protocol_still_blocks_duplicate_send(tmp_path):
    browser = RenderedBrowser(None, '{"type":"tool_calls","calls": broken}')
    store = SessionStore(tmp_path / "chats.sqlite3")
    headers = {"Authorization": "Bearer test-key", "x-session-affinity": "bad-protocol"}
    with TestClient(create_app(browser, "test-key", (MODEL,), store)) as client:
        first = client.post("/v1/agent/chat/completions", headers=headers, json=BODY)
        assert first.status_code == 502
        retry = client.post("/v1/agent/chat/completions", headers=headers, json=BODY)
        assert retry.status_code == 409
        assert len(browser.turns) == 1
        record = store.get(digest("bad-protocol"))
        assert record and record["status"] == "uncertain"


def test_required_tool_choice_rejects_plain_answer():
    with TestClient(create_app(RenderedBrowser(None, "A natural answer"), "test-key", (MODEL,))) as client:
        response = client.post(
            "/v1/agent/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={**BODY, "tool_choice": "required"},
        )
        assert response.status_code == 502


def test_structured_flag_defaults_to_plain_chat():
    assert not BrowserTurn().structured


@pytest.mark.parametrize("context", [True, False])
def test_initial_and_continued_prompts_require_lossless_code_block(context):
    prompt = AgentCompletion.model_validate(BODY).prompt(context=context)
    assert "fenced ```json" in prompt
    assert "no text outside the block" in prompt
    contract = prompt[prompt.rindex("Response protocol for your NEXT reply") :]
    assert prompt.rindex("Response protocol for your NEXT reply") > prompt.index('"transcript"')
    assert '"type":"text"' in contract
    assert '"type":"tool_calls"' in contract
    assert '"arguments"' in contract
    assert "at most one tool call" in contract


@pytest.mark.skipif(
    os.environ.get("POE_PROXY_BROWSER_TESTS") != "1",
    reason="opt-in local browser test; no Poe account or points required",
)
def test_real_dom_preserves_agent_code_and_rejects_ambiguous_blocks(tmp_path):
    code_block = (
        '<div class="MarkdownCodeBlock_container__fixture">'
        '<div>json</div><pre><code class="language-json">'
        + html.escape(ENVELOPE)
        + '</code></pre><button>Copy</button></div>'
    )
    thinking = '<div class="MarkdownThinkingBlock_root__fixture">Private thinking</div>'
    fixtures = [
        (code_block, ENVELOPE),
        (thinking + code_block, ENVELOPE),
        ('<p>{"code":"HEAD<del>1 HEAD</del>2; "pixel""}</p>', None),
        ("<p>Commentary</p>" + code_block, None),
        (code_block + "<p>Commentary</p>", None),
        (code_block + code_block, None),
        (code_block.replace("language-json", "language-python"), None),
    ]

    async def run():
        browser = Browser("poe-agent-format-" + uuid.uuid4().hex[:12])
        try:
            await browser.start()
            for index, (body, expected) in enumerate(fixtures):
                page = tmp_path / f"fixture-{index}.html"
                page.write_text(
                    '<textarea></textarea><div id="message-1" data-complete="true">'
                    '<div class="Message_leftSideMessageBubble__fixture">'
                    '<div class="Message_selectableText__fixture">' + body + "</div></div></div>"
                )
                await browser.command("open", page.as_uri())
                state = await browser.state()
                assert state["agentText"] == expected, index
                if expected:
                    arguments = json.loads(state["agentText"])["calls"][0]["arguments"]
                    assert arguments["code"] == CODE
        finally:
            await browser.close()

    asyncio.run(run())
