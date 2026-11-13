"""Local-only service and a separate interactive browser login command."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from .app import create_app
from .browser import Browser

cli = typer.Typer(no_args_is_help=True)
DEFAULT_SESSION = "poe-web-proxy"


@cli.command()
def login(
    session: str = DEFAULT_SESSION,
    cookies: Annotated[
        Path | None, typer.Option(help="Optional exported Poe cookie JSON; kept outside Git")
    ] = None,
):
    """Open a dedicated browser; log in manually or import your own exported cookies."""
    Browser(session)  # Validate before passing session to the CLI.
    args = ["agent-browser", "--session", session, "--restore", "--headed", "true"]
    state = None
    try:
        if cookies:
            raw = json.loads(cookies.read_text())
            if not isinstance(raw, list):
                raise typer.BadParameter("Expected a browser-exported cookie array")
            normalized = []
            for cookie in raw:
                if cookie.get("domain", "").lstrip(".") != "poe.com":
                    continue
                item = {
                    key: cookie[key]
                    for key in ("name", "value", "domain", "path", "httpOnly", "secure")
                    if key in cookie
                }
                item["expires"] = cookie.get("expirationDate", -1)
                item["sameSite"] = {"strict": "Strict", "lax": "Lax", "no_restriction": "None"}.get(
                    cookie.get("sameSite"), "Lax"
                )
                normalized.append(item)
            fd, state = tempfile.mkstemp(prefix="poe-login-", suffix=".json")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump({"cookies": normalized, "origins": []}, file)
            args += ["--state", state]
        subprocess.run([*args, "open", "https://poe.com/GPT-6.1-Sol"], check=True)
    finally:
        if state:
            os.unlink(state)


@cli.command()
def serve(
    session: str = DEFAULT_SESSION,
    port: int = 8765,
    timeout: Annotated[
        float, typer.Option(min=1, help="Upstream generation deadline in seconds")
    ] = 120,
):
    """Run a headless browser and loopback API. Requires POE_PROXY_API_KEY."""
    key = os.environ.get("POE_PROXY_API_KEY", "")
    models = tuple(
        filter(
            None, (s.strip() for s in os.environ.get("POE_PROXY_MODELS", "GPT-6.1-Sol").split(","))
        )
    )
    if not key:
        raise typer.BadParameter("Set POE_PROXY_API_KEY to a strong local client secret")
    if not models or any(
        not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", m) for m in models
    ):
        raise typer.BadParameter("POE_PROXY_MODELS must contain comma-separated Poe bot handles")
    app = create_app(Browser(session, timeout), key, models)
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


def main():
    cli()
