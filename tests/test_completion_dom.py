"""Positive terminal controls belong to the observed message pair, not an older reply."""

import asyncio
import os
import uuid

import pytest

from poe_web_proxy.browser import Browser


@pytest.mark.skipif(
    os.environ.get("POE_PROXY_BROWSER_TESTS") != "1",
    reason="opt-in local browser test; no Poe account or points required",
)
def test_completion_controls_are_scoped_and_thinking_remains_excluded(tmp_path):
    def pair(identifier, *, footer=False, disabled=False):
        control = (
            '<section class="ChatMessageActionBar_actionBar__fixture">'
            f'<button aria-label="Retry" {"disabled" if disabled else ""}>Retry</button></section>'
            if footer
            else ""
        )
        return (
            '<div class="ChatMessagesView_messageTuple__fixture">'
            f'<div id="message-{identifier}" data-complete="true">'
            '<div class="Message_leftSideMessageBubble__fixture">'
            '<div class="Message_selectableText__fixture">'
            '<div class="MarkdownThinkingBlock_root__fixture">Private thinking</div>'
            "</div></div></div>" + control + "</div>"
        )

    async def run():
        browser = Browser("poe-terminal-dom-" + uuid.uuid4().hex[:12])
        try:
            await browser.start()
            for index, (footer, disabled, stop) in enumerate(
                [
                    (False, False, False),
                    (True, False, False),
                    (True, True, False),
                    (True, False, True),
                ]
            ):
                page = tmp_path / f"fixture-{index}.html"
                page.write_text(
                    "<textarea></textarea>"
                    + pair("old", footer=True)
                    + pair("latest", footer=footer, disabled=disabled)
                    + ('<button aria-label="Stop message">Stop</button>' if stop else "")
                )
                await browser.command("open", page.as_uri())
                state = await browser.state()
                assert state["replyId"] == "message-latest"
                assert state["hasCompletionControls"] == (footer and not disabled)
                assert state["generating"] == stop
                assert state["text"] == "" and state["agentText"] is None
        finally:
            await browser.close()

    asyncio.run(run())
