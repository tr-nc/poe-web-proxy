"""Own one isolated agent-browser session; never read unrelated chat history."""

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from typing import Any


class BrowserError(Exception):
    pass


# These selectors and data-complete were observed in the current Poe DOM, not
# copied from a reverse-engineered library. Fail closed when the contract changes.
READ_MESSAGE = r"""
(() => {
  const messages = [...document.querySelectorAll('[id^="message-"][data-complete]')];
  const replies = messages.filter(e => e.querySelector('[class*="Message_leftSideMessageBubble__"]'));
  const reply = replies.at(-1);
  const input = document.querySelector('textarea');
  const login = [...document.querySelectorAll('a,button')].some(e =>
    /^(Log in|Sign in|登录|登入)$/i.test(e.textContent.trim()));
  return {
    login,
    ready: !!input,
    count: messages.length,
    text: reply?.querySelector('[class*="Message_selectableText__"]')?.innerText ?? '',
    complete: reply?.getAttribute('data-complete') === 'true',
    hasReply: !!reply
  };
})()
"""


class Browser:
    def __init__(self, session: str, timeout: float = 120):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            raise ValueError("Invalid browser session name")
        self.session = session
        self.timeout = timeout

    async def command(self, *args: str) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            "agent-browser",
            "--session",
            self.session,
            "--json",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout=35)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        try:
            result = json.loads(out)
        except (ValueError, TypeError):
            raise BrowserError("Invalid agent-browser response; run agent-browser doctor") from None
        if process.returncode or not result.get("success"):
            # Browser errors may contain page content or auth data. Do not forward them.
            raise BrowserError("Browser command failed; inspect the dedicated browser window")
        return result.get("data", {})

    async def state(self) -> dict:
        result = await self.command("eval", READ_MESSAGE)
        value = result.get("result", result)
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise BrowserError("Poe page structure changed")
        return value

    async def reset(self):
        # Leaving the page also stops reading/generating into the next request.
        # This is not a promise that Poe will refund points or cancel server work.
        await self.command("open", "about:blank")

    async def generate(self, model: str, prompt: str) -> AsyncGenerator[str, None]:
        async with asyncio.timeout(self.timeout):
            await self.command("open", f"https://poe.com/{model}")
            for _ in range(40):
                state = await self.state()
                if state.get("login"):
                    raise BrowserError("Poe login required; authenticate the dedicated browser")
                if state.get("ready"):
                    break
                await asyncio.sleep(0.25)
            else:
                raise BrowserError("Poe composer unavailable; check login and model access")
            if state.get("count"):
                raise BrowserError("Expected a new chat; refusing to reuse existing history")
            await self.command("fill", "textarea", prompt)
            await self.command("press", "Enter")
            previous = ""
            while True:
                state = await self.state()
                if state.get("login"):
                    raise BrowserError("Poe session expired")
                text = state.get("text", "")
                if not text.startswith(previous):
                    raise BrowserError("Poe revised streamed text; cannot safely append deltas")
                if text != previous:
                    yield text[len(previous) :]
                    previous = text
                if state.get("hasReply") and state.get("complete"):
                    if not previous:
                        raise BrowserError("Poe returned an empty response")
                    return
                await asyncio.sleep(0.3)
