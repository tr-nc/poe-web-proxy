"""Opt-in paid test: Pi -> proxy -> native Poe effort, all five levels.

Requires the managed service running the current code and updated Pi models.json.
Run when the browser is idle: uv run python tests/live_effort_smoke.py
A private HAR verifies actual sendMessageMutation parameters, then is deleted.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from poe_web_proxy.browser import READ_MESSAGE
from poe_web_proxy.sessions import SessionStore, digest

LEVELS = ("low", "medium", "high", "xhigh", "max")


def browser(*arguments):
    result = subprocess.run(
        [
            "agent-browser",
            "--session",
            "poe-web-proxy",
            "--restore",
            "--headed",
            "false",
            "--idle-timeout",
            "0",
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Browser recording command failed")


def verify_reader():
    # Exercise actual rendered DOM: exclude native thinking subtrees while
    # preserving identical words in the answer and paragraph boundaries.
    with tempfile.TemporaryDirectory(prefix="poe-reader-fixture-") as temporary:
        page = Path(temporary) / "fixture.html"
        page.write_text("""<textarea></textarea><div id="message-1" data-complete="true">
<div class="Message_leftSideMessageBubble__fixture"><div class="Message_selectableText__fixture">
<div class="MarkdownThinkingBlock_root__fixture" style="color: red"><button>思考中</button>
<p>Native reasoning must not become the answer</p></div>
<p>Answer includes 思考中 and Thinking</p><p>Second paragraph</p></div></div></div>""")
        command = ["agent-browser", "--session", "poe-effort-reader-check", "--json"]
        try:
            subprocess.run([*command, "open", page.as_uri()], capture_output=True, check=True)
            result = subprocess.run(
                [*command, "eval", READ_MESSAGE], capture_output=True, text=True, check=True
            )
            state = json.loads(result.stdout)["data"]["result"]
            state = json.loads(state) if isinstance(state, str) else state
            assert state["text"] == "Answer includes 思考中 and Thinking\n\nSecond paragraph"
            result = subprocess.run(
                [
                    *command,
                    "eval",
                    "document.querySelector('[class*=MarkdownThinkingBlock_root__]').getAttribute('style')",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assert json.loads(result.stdout)["data"]["result"] == "color: red"
            print(
                "PASS: real DOM reader separates native reasoning and preserves answer text",
                flush=True,
            )
        finally:
            subprocess.run([*command, "close"], capture_output=True, check=False)


def main():
    verify_reader()
    with tempfile.TemporaryDirectory(prefix="poe-native-effort-") as temporary:
        directory = Path(temporary)
        session = directory / "pi-session.jsonl"
        har = directory / "requests.har"
        store = SessionStore(
            Path.home() / "Library/Application Support/poe-web-proxy/chats.sqlite3"
        )
        chat_url = None
        browser("network", "har", "start")
        try:
            for level in LEVELS:
                marker = "EFFORT_" + level.upper() + "_OK"
                result = subprocess.run(
                    [
                        "pi",
                        "--offline",
                        "--no-extensions",
                        "--no-skills",
                        "--no-context-files",
                        "--no-prompt-templates",
                        "--no-tools",
                        "--session",
                        str(session),
                        "--provider",
                        "poe-web",
                        "--model",
                        "Claude-Opus-5.5",
                        "--thinking",
                        level,
                        "--print",
                        "Reply exactly: " + marker,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=220,
                    check=False,
                )
                if result.returncode or result.stdout.strip() != marker:
                    raise RuntimeError(
                        "Pi native effort test failed at "
                        + level
                        + "; exit="
                        + str(result.returncode)
                    )
                identity = json.loads(session.read_text().splitlines()[0])["id"]
                record = store.get(digest(identity))
                assert record and record["status"] == "ready"
                chat_url = chat_url or record["chat_url"]
                assert record["chat_url"] == chat_url
                print("PASS: Pi " + level + " completed in the same Poe chat", flush=True)
        finally:
            browser("network", "har", "stop", str(har))
            har.chmod(0o600)
        entries = json.loads(har.read_text())["log"]["entries"]
        sent = []
        for entry in entries:
            text = entry["request"].get("postData", {}).get("text", "")
            try:
                body = json.loads(text)
            except ValueError:
                continue
            if isinstance(body, dict) and body.get("queryName") == "sendMessageMutation":
                parameters = body["variables"].get("parameters") or "{}"
                parameters = json.loads(parameters) if isinstance(parameters, str) else parameters
                # Poe omits the field when the native select is at Medium.
                sent.append(parameters.get("effort", "medium"))
        assert sent == list(LEVELS), f"Native effort parameters did not match Pi selections: {sent}"
        print("PASS: all five real Poe sendMessageMutation effort parameters match Pi", flush=True)


if __name__ == "__main__":
    main()
