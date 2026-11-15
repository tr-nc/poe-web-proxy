"""Generation completion is not the same thing as Poe's DOM data-complete flag."""

import asyncio

import pytest

from poe_web_proxy.browser import Browser
from poe_web_proxy.turn import BrowserTurn


class BrowserTrace(Browser):
    def __init__(self, states, timeout: float = 1):
        super().__init__("generation-trace", timeout=timeout)
        self.states = iter(states)
        self.last = {}
        self.commands = []
        self.observations = 0

    async def command(self, *args):
        self.commands.append(args)
        return {}

    async def state(self):
        self.observations += 1
        self.last = next(self.states, self.last)
        return self.last


def final(text='{"type":"text","text":"OK"}', **patch):
    return {
        "hasReply": True,
        "replyId": "message-new",
        "complete": True,
        "generating": False,
        "text": "json\n" + text if text else "",
        "agentText": text,
        "url": "https://poe.com/chat/trace",
        **patch,
    }


def collect(browser, turn):
    async def run():
        return [part async for part in browser.generate("GPT-6.1-Sol", "prompt", turn)]

    return asyncio.run(run())


def test_thinking_and_data_complete_while_stop_is_visible_are_not_terminal(monkeypatch):
    async def tick(_):
        await original_sleep(0)

    original_sleep = asyncio.sleep
    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", tick)
    browser = BrowserTrace(
        [
            {"ready": True, "count": 0},
            final(text="", agentText=None, generating=True, thinking=True),
            final('{"type":', generating=True),
            final(generating=True),
            final(),
            final(),
        ]
    )
    parts = collect(browser, BrowserTurn(structured=True))
    assert parts == ['{"type":"text","text":"OK"}']
    assert browser.observations >= 6
    assert browser.commands.count(("press", "Enter")) == 1


def test_empty_thinking_only_reply_waits_instead_of_reporting_bad_json(monkeypatch):
    async def tick(_):
        await original_sleep(0)

    original_sleep = asyncio.sleep
    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", tick)
    browser = BrowserTrace(
        [{"ready": True, "count": 0}, final(text="", agentText=None, thinking=True)],
        timeout=0.02,
    )
    turn = BrowserTurn(structured=True)
    with pytest.raises(TimeoutError):
        collect(browser, turn)
    assert turn.submitted


def test_terminal_text_must_settle_before_it_can_be_parsed(monkeypatch):
    async def tick(_):
        await original_sleep(0)

    original_sleep = asyncio.sleep
    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", tick)
    browser = BrowserTrace(
        [
            {"ready": True, "count": 0},
            final('{"type":"text","text":"O'),
            final(),
            final(),
        ]
    )
    assert collect(browser, BrowserTurn(structured=True)) == ['{"type":"text","text":"OK"}']
    assert browser.observations >= 4
