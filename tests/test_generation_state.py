"""Generation completion is not the same thing as Poe's DOM data-complete flag."""

import asyncio

import pytest

from poe_web_proxy.browser import Browser, BrowserError
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


def test_optimistic_message_ids_are_not_persisted_as_send_identity(monkeypatch):
    original_sleep = asyncio.sleep

    async def tick(_):
        await original_sleep(0)

    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", tick)
    marker = "a" * 64
    stable = final(
        userId="message-user-server",
        userHash="server-hash",
        requestMarker=marker,
        replyAfterUser=True,
    )
    browser = BrowserTrace(
        [
            {"ready": True, "count": 0},
            final(
                userId="message-optimistic",
                userHash="temporary",
                requestMarker=marker,
                replyAfterUser=False,
                url="https://poe.com/GPT-6.1-Sol",
            ),
            stable,
            stable,
            stable,
        ]
    )
    checkpoints = []
    turn = BrowserTurn(structured=True, request_key=marker)
    turn.on_progress = lambda: checkpoints.append((turn.user_id, turn.response_id))
    assert collect(browser, turn) == ['{"type":"text","text":"OK"}']
    assert turn.user_id == "message-user-server"
    assert all(user != "message-optimistic" for user, _ in checkpoints)


@pytest.mark.parametrize("mutation", [{}, {"userHash": "changed"}, {"replyId": "message-other"}])
def test_recovery_only_reads_the_verified_request_and_reply(monkeypatch, mutation):
    original_sleep = asyncio.sleep

    async def tick(_):
        await original_sleep(0)

    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", tick)
    marker = "a" * 64
    state = {
        **final(
            ready=True,
            userId="message-user",
            userHash="original-hash",
            requestMarker=marker,
            replyAfterUser=True,
        ),
        **mutation,
    }
    browser = BrowserTrace([state])
    turn = BrowserTurn(
        chat_url="https://poe.com/chat/trace",
        reply_id="message-before",
        response_id="message-new",
        user_id="message-user",
        user_hash="original-hash",
        request_key=marker,
        structured=True,
        recovering=True,
        submitted=True,
    )
    if mutation:
        with pytest.raises(BrowserError):
            collect(browser, turn)
    else:
        assert collect(browser, turn) == ['{"type":"text","text":"OK"}']
    assert browser.commands == []  # Same-document recovery must never reload the live stream.


def test_recovery_can_open_a_retained_reply_after_restart_but_not_another_active_chat(monkeypatch):
    original_sleep = asyncio.sleep

    async def tick(_):
        await original_sleep(0)

    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", tick)
    turn = BrowserTurn(
        chat_url="https://poe.com/chat/trace",
        recovering=True,
        structured=True,
        reply_id="message-before",
        response_id="message-new",
    )
    browser = BrowserTrace(
        [
            {"url": "about:blank"},
            final(ready=True),
            final(),
            final(),
        ]
    )
    assert collect(browser, turn) == ['{"type":"text","text":"OK"}']
    assert browser.commands == [("open", turn.chat_url)]
    other = BrowserTrace([{"url": "https://poe.com/chat/another", "generating": True}])
    with pytest.raises(BrowserError, match="refusing to navigate"):
        collect(other, turn)
    assert other.commands == []


def test_completion_controls_do_not_hide_active_generation_or_delayed_answer(monkeypatch):
    original_sleep = asyncio.sleep

    async def tick(_):
        await original_sleep(0)

    monkeypatch.setattr("poe_web_proxy.browser.asyncio.sleep", tick)
    monkeypatch.setattr("poe_web_proxy.browser.EMPTY_REPLY_GRACE", 0)
    empty = final(text="", agentText=None, hasCompletionControls=True)
    browser = BrowserTrace(
        [
            {"ready": True, "count": 0},
            empty,
            {**empty, "generating": True},
            empty,  # A new terminal-empty observation must not reuse the earlier timer.
            final(hasCompletionControls=True),
            final(hasCompletionControls=True),
        ]
    )
    assert collect(browser, BrowserTurn(structured=True)) == ['{"type":"text","text":"OK"}']


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
