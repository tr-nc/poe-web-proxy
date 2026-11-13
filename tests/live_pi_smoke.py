"""Opt-in live Pi/read round trip. Requires installed Pi provider and running service.

Run: uv run python tests/live_pi_smoke.py
Uses Poe points for two model turns; only the read tool is enabled.
"""

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path


def main():
    fd, path = tempfile.mkstemp(prefix="poe-pi-read-", suffix=".txt")
    os.fchmod(fd, 0o600)
    marker = "PI_READ_VERIFIED_" + secrets.token_hex(6)
    with os.fdopen(fd, "w") as file:
        file.write(marker + "\n")
    try:
        command = [
            "pi",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--tools",
            "read",
            "--no-session",
            "--provider",
            "poe-web",
            "--model",
            "Claude-Opus-5.5",
            "--thinking",
            "off",
            "--mode",
            "json",
            "--print",
            (
                f"Use the read tool to read {path}. Then reply with exactly its contents, "
                "without any other words. Do not guess the contents."
            ),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=240, check=False)
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        executions = [
            event.get("toolName") for event in events if event.get("type") == "tool_execution_start"
        ]
        replies = [
            event["message"]
            for event in events
            if event.get("type") == "message_end"
            and event.get("message", {}).get("role") == "assistant"
        ]
        answer = "".join(
            block.get("text", "")
            for block in (replies[-1].get("content", []) if replies else [])
            if block.get("type") == "text"
        )
        if result.returncode or "read" not in executions or answer.strip() != marker:
            raise RuntimeError(
                "Pi tool round trip failed; check poe-proxy status and provider setup"
            )
        print("PASS: real Pi -> Opus 5.5 -> read -> tool result -> exact final answer")
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
