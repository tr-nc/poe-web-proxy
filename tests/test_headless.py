import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from poe_web_proxy.app import create_app
from poe_web_proxy.browser import Browser, BrowserError
from poe_web_proxy.cli import cli


@pytest.mark.parametrize("active", [True, False])
def test_start_preserves_existing_session_before_switching_mode(active):
    async def run():
        browser = Browser("dedicated-test")
        calls = []
        saved = []

        async def command(*args, input_text=None):
            assert input_text is None
            calls.append(args)
            if args == ("session", "info"):
                return {"active": active}
            if args[:2] == ("state", "save"):
                path = Path(args[2])
                assert path.stat().st_mode & 0o777 == 0o600
                saved.append(path)
            return {}

        browser._run = command
        await browser.start()
        assert calls[0] == ("session", "info")
        if active:
            assert calls[1][:2] == ("state", "save")
            assert calls[2] == ("close",)
            assert "--state" in calls[3]
            assert not saved[0].exists()
        else:
            assert len(calls) == 2
            assert "--state" not in calls[-1]
        launch = calls[-1]
        assert launch[:5] == ("--restore", "--headed", "false", "--idle-timeout", "0")
        assert launch[-2:] == ("open", "about:blank")

    asyncio.run(run())


def test_switch_failure_cleans_private_state():
    async def run():
        browser = Browser("dedicated-test")
        saved = []

        async def command(*args, input_text=None):
            assert input_text is None
            if args == ("session", "info"):
                return {"active": True}
            if args[:2] == ("state", "save"):
                saved.append(Path(args[2]))
                raise BrowserError("snapshot failed")
            raise AssertionError("Must not close or relaunch after a failed snapshot")

        browser._run = command
        with pytest.raises(BrowserError):
            await browser.start()
        assert not saved[0].exists()

    asyncio.run(run())


def test_all_runtime_commands_explicitly_force_headless_and_restore(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_HEADED", "true")

    async def run():
        browser = Browser("dedicated-test")
        command = AsyncMock(return_value={})
        browser._run = command
        await browser.reset()
        await browser.close()
        assert command.call_args_list[0].args == (
            "--restore",
            "--headed",
            "false",
            "--idle-timeout",
            "0",
            "open",
            "about:blank",
        )
        assert command.call_args_list[1].args == (
            "--restore",
            "--headed",
            "false",
            "--idle-timeout",
            "0",
            "close",
        )

    asyncio.run(run())


def test_login_remains_visible_and_persistent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "poe_web_proxy.cli.subprocess.run", lambda args, **kwargs: calls.append(args)
    )
    result = CliRunner().invoke(cli, ["login", "--session", "dedicated-test"])
    assert result.exit_code == 0
    assert calls == [
        [
            "agent-browser",
            "--session",
            "dedicated-test",
            "--restore",
            "--headed",
            "true",
            "open",
            "https://poe.com/GPT-6.1-Sol",
        ]
    ]


def test_service_owns_browser_lifetime():
    class Fake:
        def __init__(self):
            self.events = []

        async def start(self):
            self.events.append("start")

        async def reset(self):
            self.events.append("reset")

        async def close(self):
            self.events.append("close")

        async def generate(self, model, prompt, turn=None):
            yield "hello"

    fake = Fake()
    with TestClient(create_app(fake, "test-key", ("GPT-6.1-Sol",))):
        assert fake.events == ["start"]
    assert fake.events == ["start", "reset", "close"]
