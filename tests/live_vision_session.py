"""Opt-in macOS/Pi vision + durable-session test; RESTARTS the managed service.

Uses actual Poe points. Run only when no other proxy request is active:
    uv run python tests/live_vision_session.py
"""

import json
import secrets
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from poe_web_proxy.paths import data_dir
from poe_web_proxy.sessions import SessionStore, digest


def main():
    with tempfile.TemporaryDirectory(prefix="poe-pi-vision-") as temporary:
        directory = Path(temporary)
        session = directory / "session.jsonl"
        image_path = directory / "sample.png"
        code = "".join(str(secrets.randbelow(10)) for _ in range(6))
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 150)

        def draw(value):
            image = Image.new("RGB", (1000, 300), "white")
            ImageDraw.Draw(image).text((80, 60), value, fill="black", font=font)
            image.save(image_path)

        def ask(prompt, *, attachment=False):
            arguments = [
                "pi",
                "--offline",
                "--no-extensions",
                "--no-skills",
                "--no-context-files",
                "--no-prompt-templates",
                "--tools",
                "read",
                "--session",
                str(session),
                "--provider",
                "poe-web",
                "--model",
                "Claude-Opus-5.5",
                "--thinking",
                "medium",
                "--print",
                prompt,
            ]
            if attachment:
                arguments.append("@" + str(image_path))
            result = subprocess.run(
                arguments, capture_output=True, text=True, timeout=260, check=False
            )
            if result.returncode:
                raise RuntimeError("Pi vision/session request failed: " + result.stderr[-800:])
            return result.stdout.strip()

        def binding():
            metadata = json.loads(session.read_text().splitlines()[0])
            store = SessionStore(data_dir() / "chats.sqlite3")
            record = store.get(digest(metadata["id"]))
            assert record and record["status"] == "ready"
            return record

        draw(code)
        assert (
            ask(
                "Read the six digits in the attached image. Reply with only those digits.",
                attachment=True,
            )
            == code
        )
        first = binding()
        print("PASS: Pi attached-image OCR; durable chat binding created", flush=True)
        assert ask("What were the six digits in my image? Reply with only the digits.") == code
        second = binding()
        assert second["chat_url"] == first["chat_url"] and second["reply_id"] != first["reply_id"]
        print("PASS: next turn uses the same Poe chat without reuploading the image", flush=True)

        subprocess.run([str(Path.home() / ".local/bin/poe-proxy"), "restart"], check=True)
        assert (
            ask("Again, recall the six digits in the image from earlier. Reply only the digits.")
            == code
        )
        third = binding()
        assert third["chat_url"] == first["chat_url"]
        print("PASS: Pi and proxy restart preserve the same Poe chat and image context", flush=True)

        # A read-tool image result must also reach the model, not merely user attachments.
        next_code = "".join(str(secrets.randbelow(10)) for _ in range(6))
        draw(next_code)
        assert (
            ask(
                f"Use read to open {image_path}. Read the six digits in this NEW image; "
                "reply with only those digits, not the old image's digits."
            )
            == next_code
        )
        assert binding()["chat_url"] == first["chat_url"]
        print("PASS: Pi read-tool image result uploaded into that same Poe chat", flush=True)


if __name__ == "__main__":
    main()
