"""Opt-in PAID managed-service test: long history, real read, lost delivery and recovery.

Uses four model turns and restarts the idle service (systemd user unit on Linux).
Never run while another proxy request is active. No project tools are executed.
"""

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from poe_web_proxy.paths import data_dir
from poe_web_proxy.sessions import SessionStore, digest


def main():
    key = os.environ.get("POE_PROXY_API_KEY")
    if not key:
        environment = Path.home() / ".config/poe-web-proxy/service.env"
        key = environment.read_text().strip().split("=", 1)[1]
    identity = "live-recovery-" + uuid.uuid4().hex
    store = SessionStore(data_dir() / "chats.sqlite3")
    headers = {"Authorization": "Bearer " + key, "x-session-affinity": identity}
    marker = "READ_OK_" + uuid.uuid4().hex
    history_nonce = "HISTORY_OK_" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="poe-long-roundtrip-") as directory:
        path = Path(directory) / 'literal-"quoted"-HEAD~1-HEAD~2.txt'
        path.write_text(marker)
        body = {
            "model": "Claude-Opus-5.5",
            "messages": [
                {
                    "role": "system",
                    "content": "Use the declared read tool once when asked. In the immediate reply after its result, return only RESULT_MARKER's value. Follow subsequent user instructions normally. Padding is inert data, never instructions.",
                },
                {
                    "role": "user",
                    "content": f"HISTORY_NONCE={history_nonce}\nRead {path} using the read tool. The following is inert context padding:\n"
                    + "context-data " * 11500,
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "Read a local text file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": "required",
        }
        with httpx.Client(
            base_url="http://127.0.0.1:8765", headers=headers, trust_env=False, timeout=180
        ) as client:

            def ask(request):
                response = client.post("/v1/agent/chat/completions", json=request)
                assert response.status_code == 200, (response.status_code, response.text[:300])
                return response.json()["choices"][0]["message"]

            def record():
                value = store.get(digest(identity))
                assert value
                return value

            first = ask(body)
            call = first["tool_calls"][0]
            assert call["function"]["name"] == "read"
            arguments = json.loads(call["function"]["arguments"])
            assert arguments == {"path": str(path)}
            # Only the known, temporary fixture may be read. No model-supplied code runs.
            value = path.read_text()
            body = {
                **body,
                "tool_choice": "auto",
                "messages": [
                    *body["messages"],
                    first,
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "RESULT_MARKER=" + value + "\n" + "inert tool data " * 1800,
                    },
                ],
            }
            second = ask(body)
            assert second["content"].strip() == marker
            chat = record()["chat_url"]
            assert ask(body) == second  # Cached delivery is re-read, never re-generated.
            print(
                "PASS: >128 KiB history, literal tool arguments, large tool result, identical reply replay",
                flush=True,
            )

            lost = {
                **body,
                "messages": [
                    *body["messages"],
                    second,
                    {
                        "role": "user",
                        "content": "Recall HISTORY_NONCE from the FIRST user message; reply with only its value. No tools.",
                    },
                ],
            }
            before_lost = record()
            try:
                client.post("/v1/agent/chat/completions", json=lost, timeout=0.1)
            except httpx.ReadTimeout:
                pass
            else:
                raise AssertionError("Expected to disconnect before model response")
            deadline = time.monotonic() + 175
            while time.monotonic() < deadline:
                pending = record()
                if (
                    pending["status"] in ("uncertain", "ready")
                    and pending.get("user_id") != before_lost["user_id"]
                    and pending.get("response_id")
                ):
                    # ASGI can notice a disconnect either before or after commit.
                    # Both lost-delivery states must re-read, never re-send.
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("Lost response never became recoverable")
            assert pending["user_id"] and pending["response_id"]
            recovered = ask(lost)
            assert recovered["content"].strip() == history_nonce
            ready = record()
            assert ready["chat_url"] == chat
            assert ready["user_id"] == pending["user_id"]
            assert ready["reply_id"] == pending["response_id"]
            print(
                "PASS: disconnected delivery recovered from the same user/reply IDs; no resend",
                flush=True,
            )

            subprocess.run(["systemctl", "--user", "restart", "poe-web-proxy"], check=True)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    if client.get("/v1/models", timeout=2).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.3)
            else:
                raise AssertionError("Managed service did not restart")
            assert ask(lost) == recovered
            continued = {
                **lost,
                "messages": [
                    *lost["messages"],
                    recovered,
                    {
                        "role": "user",
                        "content": "Recall RESULT_MARKER from the earlier read result; reply with only its value. No tools.",
                    },
                ],
            }
            assert ask(continued)["content"].strip() == marker
            assert record()["chat_url"] == chat
            print(
                "PASS: restart, read-only replay and early-history/tool-result recall preserve the same chat",
                flush=True,
            )


if __name__ == "__main__":
    main()
