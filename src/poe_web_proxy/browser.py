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
(async () => {
  const messages = [...document.querySelectorAll('[id^="message-"][data-complete]')];
  const replies = messages.filter(e => e.querySelector('[class*="Message_leftSideMessageBubble__"]'));
  const reply = replies.at(-1);
  const input = document.querySelector('textarea');
  const selectable = reply?.querySelector('[class*="Message_selectableText__"]');
  const user = messages.filter(e => e.querySelector('[class*="Message_rightSideMessageBubble__"]')).at(-1);
  const userText = user?.querySelector('[class*="Message_selectableText__"]')?.textContent ?? '';
  const userHash = user ? [...new Uint8Array(await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(userText)))].map(b => b.toString(16).padStart(2, '0')).join('') : null;
  const marker = userText.match(/POE-PROXY-REQUEST:([a-f0-9]{64})\s*$/);
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
  // Agent envelopes must come from a sole JSON code block, never rendered prose:
  // Markdown consumes JSON backslashes and interprets tool-code punctuation.
  let agentText = null;
  if (selectable && reply?.getAttribute('data-complete') === 'true') {
    const content = selectable.cloneNode(true);
    content.querySelectorAll('[class*="MarkdownThinkingBlock_root__"]').forEach(e => e.remove());
    const codes = content.querySelectorAll('pre code');
    const code = codes.length === 1 ? codes[0] : null;
    const block = code?.closest('[class*="MarkdownCodeBlock_container__"]');
    if (code?.classList.contains('language-json') && block) {
      const source = code.textContent;
      block.remove();
      if (!content.textContent.trim()) agentText = source;
    }
  }
  const login = [...document.querySelectorAll('a,button')].some(e =>
    /^(Log in|Sign in|登录|登入)$/i.test(e.textContent.trim()));
  return {
    login,
    userId: user?.id ?? null,
    userHash,
    requestMarker: marker?.[1] ?? null,
    replyAfterUser: !!user && !!reply && messages.indexOf(reply) > messages.indexOf(user),
    generating: [...document.querySelectorAll('button')].some(e =>
      /^(Stop message|Stop generating|停止生成|停止回答)$/i.test(e.getAttribute('aria-label') ?? '') && !e.disabled),
    thinking: thinking.length > 0,
    url: location.href,
    replyId: reply?.id ?? null,
    attachments: document.querySelectorAll('[class*="ChatMessageInputAttachments_attachment__"]').length,
    canSend: [...document.querySelectorAll('button')].some(e =>
      /发送信息|Send message|^Send$/i.test(e.getAttribute('aria-label') ?? '') && !e.disabled),
    ready: !!input,
    count: messages.length,
    text,
    agentText,
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

    async def _run(self, *args: str, input_text: str | None = None) -> dict[str, Any]:
        try:
            process = await asyncio.create_subprocess_exec(
                "agent-browser",
                "--session",
                self.session,
                "--json",
                *args,
                stdin=asyncio.subprocess.PIPE
                if input_text is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise BrowserError(
                "Unable to start browser command; check agent-browser installation"
            ) from None
        try:
            out, _ = await asyncio.wait_for(
                process.communicate(input_text.encode() if input_text is not None else None),
                timeout=35,
            )
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
        flags = ("--restore", "--headed", "false", "--idle-timeout", "0")
        if len(args) == 3 and args[:2] == ("fill", "textarea"):
            # Linux caps a SINGLE argv string near 128 KiB, below our prompt limit.
            # Stream all composer data through stdin, never argv or temporary files.
            # Use the native setter so React's controlled-input tracker sees the change.
            script = f"""
(() => {{
  const inputs = document.querySelectorAll('textarea');
  if (inputs.length !== 1) throw new Error('Expected one composer');
  const input = inputs[0];
  if (input.disabled || input.readOnly) throw new Error('Composer is not editable');
  const text = {json.dumps(args[2], ensure_ascii=False)};
  input.focus();
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, text);
  input.dispatchEvent(new Event('input', {{bubbles: true}}));
  if (input.value !== text) throw new Error('Composer did not retain exact input');
  return {{filled: true}};
}})()
"""
            return await self._run(*flags, "eval", "--stdin", input_text=script)
        return await self._run(*flags, *args)

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

    @staticmethod
    def _matches_user(state: dict, turn: BrowserTurn) -> bool:
        if not turn.request_key:
            return True
        if not state.get("userId"):
            return False
        if turn.marker_required and state.get("requestMarker") != turn.request_key:
            return False
        if turn.user_id and state["userId"] != turn.user_id:
            return False
        if turn.user_hash and state.get("userHash") != turn.user_hash:
            return False
        return bool(turn.user_id or state["userId"] != turn.base_user_id)

    async def _prepare(self, model: str, prompt: str, turn: BrowserTurn, directory: str):
        expected_url, baseline = turn.chat_url, turn.reply_id
        if turn.recovering and not expected_url:
            raise BrowserError("Cannot locate the original Poe send; refusing to send again")
        await self.command("open", expected_url or f"https://poe.com/{model}")
        for _ in range(80):
            state = await self.state()
            if state.get("login"):
                raise BrowserError("Poe login required; authenticate the dedicated browser")
            if state.get("ready") and (not turn.recovering or self._matches_user(state, turn)):
                break
            await asyncio.sleep(0.25)
        else:
            raise BrowserError(
                "Poe composer or original request identity unavailable; nothing was sent"
            )
        if turn.recovering:
            # This path cannot configure, upload, fill, press Enter, or create a chat.
            if state.get("url") != expected_url:
                raise BrowserError("Poe chat changed during read-only recovery")
            return
        if expected_url:
            if (
                state.get("url") != expected_url
                or state.get("replyId") != baseline
                or not state.get("complete")
                or state.get("generating")
            ):
                raise BrowserError(
                    "Poe chat changed externally; refusing to send into divergent history"
                )
        elif state.get("count"):
            raise BrowserError("Expected a new chat; refusing to reuse existing history")
        turn.base_user_id = state.get("userId")
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
                raise BrowserError("Image upload did not become ready; message was not sent")
        # Persist the possibility of a send BEFORE invoking Enter, including process crashes.
        turn.submitted = True
        turn.checkpoint()
        await self.command("press", "Enter")

    async def generate(
        self, model: str, prompt: str, turn: BrowserTurn | None = None
    ) -> AsyncGenerator[str, None]:
        turn = turn or BrowserTurn()
        baseline = turn.reply_id
        with tempfile.TemporaryDirectory(prefix="poe-attachments-") as directory:
            async with asyncio.timeout(self.timeout):
                await self._prepare(model, prompt, turn, directory)
                previous = ""
                terminal = None
                identity = None
                while True:
                    state = await self.state()
                    if state.get("login"):
                        raise BrowserError("Poe session expired")
                    if not self._matches_user(state, turn):
                        if turn.user_id:
                            raise BrowserError("Poe request changed; refusing another turn's reply")
                        await asyncio.sleep(0.3)
                        continue
                    url = state.get("url", "")
                    valid_url = bool(re.fullmatch(r"https://poe\.com/chat/[A-Za-z0-9_-]+", url))
                    if turn.request_key and not valid_url:
                        await asyncio.sleep(0.3)
                        continue
                    if valid_url:
                        turn.chat_url = url
                    turn.checkpoint()
                    if (
                        not state.get("hasReply")
                        or (baseline and state.get("replyId") == baseline)
                        or (turn.request_key and not state.get("replyAfterUser"))
                    ):
                        await asyncio.sleep(0.3)
                        continue
                    observed = state.get("replyId")
                    if turn.request_key and not turn.user_id:
                        # Poe first mounts optimistic user/reply IDs, then replaces
                        # them with server IDs. Journal only a stable pair in a real
                        # chat, not the temporary client-side message identity.
                        current_identity = (state.get("userId"), state.get("userHash"), observed)
                        if current_identity != identity:
                            identity = current_identity
                            await asyncio.sleep(0.3)
                            continue
                        turn.user_id = state.get("userId")
                        turn.user_hash = state.get("userHash")
                    if turn.response_id and observed != turn.response_id:
                        raise BrowserError(
                            "Poe reply changed during recovery; refusing a different response"
                        )
                    turn.response_id = observed
                    turn.checkpoint()
                    source = state.get("agentText") if turn.structured else state.get("text")
                    if source is None:
                        source = state.get("text", "")
                    candidate = (
                        (observed, source)
                        if state.get("complete")
                        and not state.get("generating")
                        and isinstance(source, str)
                        and source.strip()
                        else None
                    )
                    settled = candidate is not None and candidate == terminal
                    terminal = candidate
                    if turn.structured:
                        if not settled:
                            await asyncio.sleep(0.3)
                            continue
                        turn.plain_text = state.get("agentText") is None
                        turn.reply_id = observed
                        turn.reply_received = True
                        yield source
                        return
                    text = state.get("text", "")
                    if not text.startswith(previous):
                        raise BrowserError("Poe revised streamed text; cannot safely append deltas")
                    if text != previous:
                        yield text[len(previous) :]
                        previous = text
                    if settled:
                        turn.reply_id = observed
                        turn.reply_received = True
                        return
                    await asyncio.sleep(0.3)
