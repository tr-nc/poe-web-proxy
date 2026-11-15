"""Explicit fallback retains the Pi session and archives evidence, never replays history."""

import json
import sqlite3

from fastapi.testclient import TestClient
from test_recovery import BODY, MODEL, Interrupted, request
from typer.testing import CliRunner

from poe_web_proxy.app import create_app
from poe_web_proxy.cli import cli
from poe_web_proxy.sessions import SessionStore, digest


def test_rebuild_is_reviewed_atomic_and_sends_only_selected_new_input(tmp_path):
    store = SessionStore(tmp_path / "private/chats.sqlite3")
    with TestClient(create_app(Interrupted(), "test-key", (MODEL,), store)) as client:
        assert request(client).status_code == 504
    old = store.get(digest("recover-me"))
    body = {
        **BODY,
        "tool_choice": "none",
        "messages": [
            *BODY["messages"],
            {"role": "user", "content": "Reviewed summary. Do not execute anything."},
        ],
    }
    file = tmp_path / "request.json"
    file.write_text(json.dumps(body))
    args = [
        "rebuild",
        "--identity",
        "recover-me",
        "--request",
        str(file),
        "--database",
        str(store.path),
    ]
    runner = CliRunner()
    inspected = runner.invoke(cli, args)
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.stdout)["revision"] == digest(old)
    assert store.get(digest("recover-me")) == old
    assert runner.invoke(cli, [*args, "--accept-context-loss"]).exit_code == 2
    assert (
        runner.invoke(
            cli, [*args, "--accept-context-loss", "--service-stopped", "--expected", "stale"]
        ).exit_code
        == 2
    )
    assert store.get(digest("recover-me")) == old
    applied = runner.invoke(
        cli, [*args, "--accept-context-loss", "--service-stopped", "--expected", digest(old)]
    )
    assert applied.exit_code == 0, applied.output
    with sqlite3.connect(store.path) as db:
        archived = db.execute("SELECT record FROM abandoned_chats").fetchone()
        assert archived and json.loads(archived[0]) == old
    assert file.read_text() == json.dumps(body)  # No local transcript mutation.

    class NewChat(Interrupted):
        async def generate(self, model, prompt, turn=None):
            assert turn and turn.chat_url is None
            assert not turn.recovering
            assert "private request" not in prompt
            assert "Reviewed summary" in prompt
            async for delta in super().generate(model, prompt, turn):
                yield delta

    page = NewChat()
    page.timeout = False
    with TestClient(create_app(page, "test-key", (MODEL,), store)) as client:
        # No silent unlock: only the pinned, tools-forbidden request may start it.
        assert request(client, {**body, "tool_choice": "auto"}).status_code == 409
        assert request(client, body).status_code == 200
        assert page.sends == 1
    assert b"Reviewed summary" not in store.path.read_bytes()


def test_rebuild_help_and_private_request_error_recovery(tmp_path):
    runner = CliRunner()
    for args in (["--help"], ["rebuild", "--help"]):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0 and "rebuild" in result.output
    help_text = runner.invoke(cli, ["rebuild", "--help"]).output
    for term in ("tool_choice=none", "points", "Pi files", "--expected", "--service-stopped"):
        assert term in help_text
    assert runner.invoke(cli, ["rebuild"]).exit_code == 2
    assert runner.invoke(cli, ["rebuild", "--unknown"]).exit_code == 2
    file = tmp_path / "bad.json"
    file.write_text('{"secret": "private transcript"}')
    result = runner.invoke(cli, ["rebuild", "--identity", "test", "--request", str(file)])
    assert result.exit_code == 2
    assert "private transcript" not in result.output
    assert "--expected HASH" in result.output and "rebuild --help" in result.output
