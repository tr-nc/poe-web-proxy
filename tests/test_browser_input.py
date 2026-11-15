"""Exercise composer input at the advertised size, including Linux argv limits."""

import asyncio
import json
import os
import uuid

import pytest

from poe_web_proxy.browser import Browser, BrowserError


def test_composer_data_uses_stdin_not_process_arguments(monkeypatch):
    text = "私有上下文" * 50000
    calls = []

    class Process:
        returncode = 0

        async def communicate(self, data=None):
            assert isinstance(data, bytes)
            script = data.decode()
            assert json.dumps(text, ensure_ascii=False) in script
            return b'{"success":true,"data":{"result":{"filled":true}}}', b""

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        assert all(len(arg.encode()) < 1024 for arg in args)
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert args[-2:] == ("eval", "--stdin")
        return Process()

    monkeypatch.setattr("poe_web_proxy.browser.asyncio.create_subprocess_exec", spawn)
    asyncio.run(Browser("test").command("fill", "textarea", text))
    assert len(calls) == 1


def test_process_start_failure_is_a_browser_error(monkeypatch):
    async def spawn(*args, **kwargs):
        raise OSError("private diagnostic")

    monkeypatch.setattr("poe_web_proxy.browser.asyncio.create_subprocess_exec", spawn)
    with pytest.raises(BrowserError, match="Unable to start browser command"):
        asyncio.run(Browser("test").command("open", "about:blank"))


@pytest.mark.skipif(
    os.environ.get("POE_PROXY_BROWSER_TESTS") != "1",
    reason="opt-in local browser test; no Poe account or points required",
)
@pytest.mark.parametrize(
    "text",
    ["x" * 199000, "中🙂" * 65000 + '\n"quoted" \\path `code`'],
    ids=["near-limit-ascii", "multibyte"],
)
def test_large_composer_input_is_exact_and_dispatches_input(tmp_path, text):
    async def run():
        browser = Browser("poe-input-" + uuid.uuid4().hex[:12])
        page = tmp_path / "composer.html"
        page.write_text(
            "<textarea></textarea><script>window.observed = null;"
            'document.querySelector("textarea").addEventListener("input", e => {'
            "window.observed = e.target.value;});</script>"
        )
        try:
            await browser.start()
            await browser.command("open", page.as_uri())
            await browser.command("fill", "textarea", text)
            state = await browser.evaluate(
                '({value:document.querySelector("textarea").value,observed:window.observed,'
                'focused:document.activeElement===document.querySelector("textarea")})'
            )
            assert state == {"value": text, "observed": text, "focused": True}
        finally:
            await browser.close()

    asyncio.run(run())
