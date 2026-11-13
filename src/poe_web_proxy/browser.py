"""Own one isolated agent-browser session; never read unrelated chat history."""

import asyncio
import json
import os
import re
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from .turn import BrowserTurn, ReasoningEffort


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
  const selectable = reply?.querySelector('[class*="Message_selectableText__"]');
  // Native thinking headers/content live inside the same selectable wrapper.
  // Exclude those DOM subtrees, not matching strings in the user's answer.
  // Restore styles synchronously before the browser can paint a frame.
  const thinking = [...(selectable?.querySelectorAll('[class*="MarkdownThinkingBlock_root__"]') ?? [])];
  const styles = thinking.map(e => e.getAttribute('style'));
  let text = '';
  try {
    thinking.forEach(e => e.style.setProperty('display', 'none', 'important'));
    text = selectable?.innerText ?? '';
  } finally {
    thinking.forEach((e, i) => styles[i] === null ? e.removeAttribute('style') : e.setAttribute('style', styles[i]));
  }
  const login = [...document.querySelectorAll('a,button')].some(e =>
    /^(Log in|Sign in|登录|登入)$/i.test(e.textContent.trim()));
  return {
    login,
    url: location.href,
    replyId: reply?.id ?? null,
    attachments: document.querySelectorAll('[class*="ChatMessageInputAttachments_attachment__"]').length,
    canSend: [...document.querySelectorAll('button')].some(e =>
      /发送信息|Send message|^Send$/i.test(e.getAttribute('aria-label') ?? '') && !e.disabled),
    ready: !!input,
    count: messages.length,
    text,
    complete: reply?.getAttribute('data-complete') === 'true',
    hasReply: !!reply
  };
})()
"""


EFFORT_SELECT = (
    'select:has(option[value="low"]):has(option[value="medium"]):has(option[value="high"])'
)
READ_EFFORT = """
(() => {
  const button = document.querySelector('button[data-button-custom-ui="true"]');
  const selects = [...document.querySelectorAll('select')].filter(e =>
    ['low', 'medium', 'high'].every(v => [...e.options].some(o => o.value === v)));
  const select = selects.length === 1 ? selects[0] : null;
  return {
    button: !!button, open: button?.getAttribute('aria-expanded') === 'true',
    options: select ? [...select.options].map(o => o.value) : [],
    value: select?.value ?? null
  };
})()
"""


class Browser:
    def __init__(self, session: str, timeout: float = 120):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            raise ValueError("Invalid browser session name")
        self.session = session
        self.timeout = timeout

    async def _run(self, *args: str) -> dict[str, Any]:
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
            raise BrowserError("Browser command failed; stop the service and run login to inspect")
        return result.get("data", {})

    async def command(self, *args: str) -> dict[str, Any]:
        return await self._run("--restore", "--headed", "false", "--idle-timeout", "0", *args)

    async def start(self) -> None:
        # Capture the live session BEFORE changing launch flags. Legacy headed
        # sessions may not have restore configured; relaunching first loses auth.
        info = await self._run("session", "info")
        state = None
        try:
            if info.get("active"):
                fd, state = tempfile.mkstemp(prefix="poe-headless-", suffix=".json")
                os.fchmod(fd, 0o600)
                os.close(fd)
                await self._run("state", "save", state)
                await self._run("close")
            flags = ("--state", state) if state else ()
            await self.command(*flags, "open", "about:blank")
        finally:
            if state:
                os.unlink(state)

    async def close(self) -> None:
        # --restore saves auth outside the repository for the next service start.
        await self.command("close")

    async def evaluate(self, script: str) -> dict:
        result = await self.command("eval", script)
        value = result.get("result", result)
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise BrowserError("Poe page structure changed")
        return value

    async def state(self) -> dict:
        return await self.evaluate(READ_MESSAGE)

    async def configure_effort(self, effort: ReasoningEffort) -> None:
        # The data attribute and native select were observed in Poe's custom
        # inputs UI. Read-back is mandatory; never pretend a prompt sets effort.
        button = 'button[data-button-custom-ui="true"]'
        for _ in range(40):
            state = await self.evaluate(READ_EFFORT)
            if state.get("options"):
                break
            if state.get("button") and not state.get("open"):
                await self.command("click", button)
            await asyncio.sleep(0.25)
        else:
            raise BrowserError("Poe effort control unavailable; message was not sent")
        if effort not in state["options"]:
            raise BrowserError("Poe does not support the requested effort; message was not sent")
        await self.command("select", EFFORT_SELECT, effort)
        state = await self.evaluate(READ_EFFORT)
        if state.get("value") != effort:
            raise BrowserError("Poe effort setting was not acknowledged; message was not sent")
        if state.get("open"):
            await self.command("click", button)

    async def reset(self):
        # Leaving the page also stops reading/generating into the next request.
        # This is not a promise that Poe will refund points or cancel server work.
        await self.command("open", "about:blank")

    async def generate(
        self, model: str, prompt: str, turn: BrowserTurn | None = None
    ) -> AsyncGenerator[str, None]:
        turn = turn or BrowserTurn()
        expected_url, baseline = turn.chat_url, turn.reply_id
        with tempfile.TemporaryDirectory(prefix="poe-attachments-") as directory:
            async with asyncio.timeout(self.timeout):
                await self.command("open", expected_url or f"https://poe.com/{model}")
                for _ in range(80):
                    state = await self.state()
                    if state.get("login"):
                        raise BrowserError("Poe login required; authenticate the dedicated browser")
                    if state.get("ready"):
                        break
                    await asyncio.sleep(0.25)
                else:
                    raise BrowserError("Poe composer unavailable; check login and model access")
                if expected_url:
                    if (
                        state.get("url") != expected_url
                        or state.get("replyId") != baseline
                        or not state.get("complete")
                    ):
                        raise BrowserError(
                            "Poe chat changed externally; refusing to send into divergent history"
                        )
                elif state.get("count"):
                    raise BrowserError("Expected a new chat; refusing to reuse existing history")
                # Poe remembers custom inputs: explicitly restore Opus's observed
                # Medium default when omitted, so another session's Max cannot leak.
                effort = turn.reasoning_effort
                if effort is None and model == "Claude-Opus-5.5":
                    effort = "medium"
                if effort is not None:
                    await self.configure_effort(effort)
                await self.command("fill", "textarea", prompt)
                if turn.images:
                    paths = []
                    for image in turn.images:
                        path = Path(directory) / image.name
                        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "wb") as file:
                            file.write(image.data)
                        paths.append(str(path))
                    await self.command("upload", "input[type=file]", *paths)
                    for _ in range(160):
                        state = await self.state()
                        if state.get("canSend") and state.get("attachments") == len(paths):
                            break
                        await asyncio.sleep(0.25)
                    else:
                        raise BrowserError(
                            "Image upload did not become ready; message was not sent"
                        )
                # Set before invoking send: a command failure can still have sent it.
                turn.submitted = True
                await self.command("press", "Enter")
                previous = ""
                while True:
                    state = await self.state()
                    if state.get("login"):
                        raise BrowserError("Poe session expired")
                    url = state.get("url", "")
                    if re.fullmatch(r"https://poe\.com/chat/[A-Za-z0-9_-]+", url):
                        turn.chat_url = url
                    # Reopening an existing chat exposes its previous completed reply.
                    # Never return it as the response to the new message.
                    if not state.get("hasReply") or (baseline and state.get("replyId") == baseline):
                        await asyncio.sleep(0.3)
                        continue
                    text = state.get("text", "")
                    if not text.startswith(previous):
                        raise BrowserError("Poe revised streamed text; cannot safely append deltas")
                    if text != previous:
                        yield text[len(previous) :]
                        previous = text
                    if state.get("complete"):
                        if not previous:
                            raise BrowserError("Poe returned an empty response")
                        turn.reply_id = state.get("replyId")
                        return
                    await asyncio.sleep(0.3)
