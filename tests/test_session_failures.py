import asyncio

import pytest

from poe_web_proxy.agent import AgentCompletion
from poe_web_proxy.browser import Browser, BrowserError
from poe_web_proxy.sessions import AgentTurn, SessionConflict, SessionStore, digest
from poe_web_proxy.turn import BrowserTurn


def test_failed_reservation_never_rolls_back_another_owner(tmp_path):
    store = SessionStore(tmp_path / "sessions/chats.sqlite3")
    request = AgentCompletion.model_validate(
        {"model": "Claude-Opus-5.5", "messages": [{"role": "user", "content": "hi"}]}
    )
    first = AgentTurn(request, "same-session", store)
    second = AgentTurn(request, "same-session", store)
    first.reserve()
    with pytest.raises(SessionConflict):
        second.reserve()
    second.abort()
    record = store.get(digest("same-session"))
    assert record and record["status"] == "pending"
    # A pre-Enter reservation has no effects; restart may prepare it again.
    restarted = AgentTurn(request, "same-session", SessionStore(store.path))
    assert not restarted.browser.submitted
    first.abort()  # no send happened, so this owner may release the reservation
    assert store.get(digest("same-session")) is None
    # Missing legacy submitted evidence is NOT an explicit pre-Enter checkpoint.
    legacy = {"status": "pending", "model": request.model}
    store.replace(digest("same-session"), None, legacy)
    with pytest.raises(SessionConflict, match="unacknowledged"):
        AgentTurn(request, "same-session", store)


def test_external_chat_changes_fail_before_sending():
    async def run():
        browser = Browser("test")
        commands = []

        async def command(*args):
            commands.append(args)
            return {}

        async def state():
            return {
                "ready": True,
                "url": "https://poe.com/chat/known",
                "replyId": "message-unexpected",
                "complete": True,
            }

        browser.command, browser.state = command, state
        turn = BrowserTurn(chat_url="https://poe.com/chat/known", reply_id="message-expected")
        with pytest.raises(BrowserError, match="changed externally"):
            async for _ in browser.generate("Claude-Opus-5.5", "new message", turn):
                pass
        assert not turn.submitted
        assert commands == []  # Already on the same document; no need to reload it.

    asyncio.run(run())
