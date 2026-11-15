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

from .agent import AgentCompletion
from .app import create_app
from .browser import Browser
from .launchd import cli as service_cli
from .paths import data_dir
from .sessions import SessionConflict, SessionStore, digest

cli = typer.Typer(no_args_is_help=True)
cli.add_typer(service_cli, name="service")
DEFAULT_SESSION = "poe-web-proxy"
DEFAULT_MODELS = "GPT-6.1-Sol,Claude-Opus-5.5"


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
            None, (s.strip() for s in os.environ.get("POE_PROXY_MODELS", DEFAULT_MODELS).split(","))
        )
    )
    if not key:
        raise typer.BadParameter("Set POE_PROXY_API_KEY to a strong local client secret")
    if not models or any(
        not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", m) for m in models
    ):
        raise typer.BadParameter("POE_PROXY_MODELS must contain comma-separated Poe bot handles")
    store = SessionStore(data_dir() / "chats.sqlite3")
    app = create_app(Browser(session, timeout), key, models, sessions=store)
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


@cli.command()
def rebuild(
    identity: Annotated[str, typer.Option(help="Existing Pi/session-affinity ID")],
    request: Annotated[
        Path, typer.Option(help="Private JSON AgentCompletion; final user input only is sent")
    ],
    database: Annotated[
        Path | None, typer.Option(help="Binding database; defaults to service data directory")
    ] = None,
    expected: Annotated[
        str | None, typer.Option(help="Revision hash from the read-only inspection")
    ] = None,
    accept_context_loss: Annotated[
        bool, typer.Option(help="Archive old binding and arm the reviewed new chat")
    ] = False,
    service_stopped: Annotated[
        bool, typer.Option(help="Confirm no service or browser request is running")
    ] = False,
):
    """Inspect/arm an explicit new chat without deleting Pi history or generating.

    Stop the service first. Inspect with --identity ID --request FILE; then repeat
    with --expected HASH --accept-context-loss --service-stopped. The request must
    have tool_choice=none and end with a user message: a reviewed summary/new task.
    On the next identical HTTP request only that final message and current context
    are sent, not earlier history. The new chat loses old upstream context and costs
    points. Old tools may already have run; this is NOT permission to run them again.
    Evidence is archived privately in the same database. Pi files are never edited.
    """
    try:
        body = AgentCompletion.model_validate_json(request.read_text())
        if body.tool_choice != "none" or body.messages[-1].role != "user":
            raise ValueError("Rebuild requires tool_choice=none and a final user message")
        store = SessionStore(database or data_dir() / "chats.sqlite3")
        key = digest(identity)
        old = store.get(key)
        if old is None:
            raise ValueError("No existing binding for this identity")
        if not accept_context_loss:
            typer.echo(
                json.dumps(
                    {
                        "revision": digest(old),
                        "status": old["status"],
                        "model": old["model"],
                        "sends": 0,
                    }
                )
            )
            return
        if not service_stopped or not expected:
            raise ValueError(
                "Stop the service; inspect without --accept-context-loss, then repeat with "
                "--expected HASH --accept-context-loss --service-stopped"
            )
        store.detach(key, expected, body)
        typer.echo("Rebuild armed; no generation. Restart service and send the reviewed request.")
    except (OSError, ValueError, SessionConflict):
        # Request files may contain sensitive transcript/schema values. Never echo
        # Pydantic's validation details or file contents into terminal/log output.
        typer.echo(
            "Rebuild refused: require an existing unchanged binding, a valid private request "
            "ending in a user message with tool_choice=none, and both confirmations. "
            "Inspect: poe-web-proxy rebuild --identity ID --request FILE. "
            "Apply after stopping service: add --expected HASH --accept-context-loss "
            "--service-stopped. See poe-web-proxy rebuild --help.",
            err=True,
        )
        raise typer.Exit(2) from None


def main():
    cli()
