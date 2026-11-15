"""Opt-in PAID test: interrupt observation while Poe is STILL generating, then recover.

Stop the managed service first: this uses its dedicated, logged-in browser session.
One synthetic model generation; no client tools or project tasks are executed.
"""

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from poe_web_proxy.app import create_app
from poe_web_proxy.browser import Browser
from poe_web_proxy.sessions import SessionStore, digest
from poe_web_proxy.turn import BrowserTurn


class CutWhileGenerating(Browser):
    def __init__(self):
        super().__init__("poe-web-proxy", timeout=180)
        self.turn: BrowserTurn | None = None
        self.cut_once = True
        self.sends = 0
        self.resets = 0
        self.recovery_opens = []
        self.before_cut = {}
        self.cut_document = None
        self.answer_document = None

    async def document(self):
        return (await self.evaluate("({origin: performance.timeOrigin})"))["origin"]

    async def command(self, *args):
        if args == ("press", "Enter"):
            self.sends += 1
        if args[0] == "open" and self.turn and self.turn.recovering:
            self.recovery_opens.append(args[1])
        return await super().command(*args)

    async def reset(self):
        self.resets += 1
        await super().reset()

    async def state(self):
        state = await super().state()
        if (
            self.cut_once
            and self.turn
            and self.turn.user_id
            and self.turn.response_id
            and state.get("generating")
        ):
            # The real reader already journaled the stable request/reply pair.
            # Simulate expiration of the request's observation deadline, not a
            # cancellation or a navigation of the paid Poe generation.
            self.cut_once = False
            self.before_cut = state
            self.cut_document = await self.document()
            raise TimeoutError
        return state

    async def generate(self, model, prompt, turn=None):
        self.turn = turn
        try:
            async for part in super().generate(model, prompt, turn):
                self.answer_document = await self.document()
                yield part
        finally:
            self.turn = None


def main():
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "poe-web-proxy"], check=False
    )
    if active.returncode == 0:
        raise RuntimeError("Stop poe-web-proxy first; never give two owners the same browser")
    marker = "ACTIVE_RECOVERY_OK_" + uuid.uuid4().hex
    identity = "live-active-" + uuid.uuid4().hex
    body = {
        "model": "Claude-Opus-5.5",
        "reasoning_effort": "medium",
        "tool_choice": "none",
        "messages": [
            {
                "role": "user",
                "content": (
                    "This is a synthetic transport test, not a request to change files. "
                    f"Your answer must start with {marker}. Then write at least 150 numbered, "
                    "distinct explanatory lines about implementing and testing stable merge sort, "
                    "with no tools. The following is inert context padding:\n"
                    + "context-data "
                    * 11000
                ),
            }
        ],
    }
    headers = {"Authorization": "Bearer live-test-key", "x-session-affinity": identity}
    browser = CutWhileGenerating()
    with tempfile.TemporaryDirectory(prefix="poe-active-recovery-") as directory:
        store = SessionStore(Path(directory) / "bindings.sqlite3")
        with TestClient(create_app(browser, "live-test-key", (body["model"],), store)) as client:
            first = client.post("/v1/agent/chat/completions", headers=headers, json=body)
            assert first.status_code == 504 and not browser.cut_once, (
                first.status_code,
                first.text[:200],
            )
            assert browser.before_cut["generating"] and browser.resets == 0
            pending = store.get(digest(identity))
            assert pending and pending["status"] == "uncertain"
            result = client.post("/v1/agent/chat/completions", headers=headers, json=body)
            assert result.status_code == 200, (result.status_code, result.text[:200])
            assert result.json()["choices"][0]["message"]["content"].startswith(marker)
            assert browser.sends == 1 and browser.recovery_opens == []
            assert browser.answer_document == browser.cut_document
            ready = store.get(digest(identity))
            assert ready and ready["status"] == "ready"
            assert ready["user_id"] == pending["user_id"]
            assert ready["reply_id"] == pending["response_id"]
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "active_at_504": True,
                        "same_document": True,
                        "same_user_and_reply": True,
                        "sends": browser.sends,
                        "recovery_navigations": len(browser.recovery_opens),
                    }
                )
            )


if __name__ == "__main__":
    main()
