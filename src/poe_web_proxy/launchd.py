"""macOS service ownership. Secrets are read by the child, never placed in a plist."""

import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import typer

LABEL = "io.github.tr-nc.poe-web-proxy"
URL = "http://127.0.0.1:8765/v1"
cli = typer.Typer(no_args_is_help=True, help="Manage the macOS login-time headless service.")


class Service:
    def __init__(self):
        self.home = Path.home()
        self.data = self.home / "Library/Application Support/poe-web-proxy"
        self.key = self.data / "api-key"
        self.logs = self.home / "Library/Logs/poe-web-proxy"
        self.plist = self.home / "Library/LaunchAgents" / (LABEL + ".plist")
        self.link = self.home / ".local/bin/poe-proxy"
        self.script = Path(__file__).resolve().parents[2] / "scripts/poe-proxy"
        self.domain = f"gui/{os.getuid()}"
        self.target = self.domain + "/" + LABEL

    def launchctl(self, *args):
        return subprocess.run(
            ["/bin/launchctl", *args], check=False, capture_output=True, text=True, timeout=100
        )

    def loaded(self):
        return self.launchctl("print", self.target).returncode == 0

    def healthy(self):
        try:
            request = urllib.request.Request(
                URL + "/models", headers={"Authorization": "Bearer " + self.key.read_text().strip()}
            )
            # Loopback health must not be sent through environment HTTP proxies.
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=2
            ) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def install(self):
        browser = shutil.which("agent-browser")
        if not browser:
            raise RuntimeError(
                "agent-browser is missing from PATH; install it and retry poe-proxy install"
            )
        if not self.script.is_file():
            raise RuntimeError(
                "Install from a repository checkout: uv sync; uv run poe-web-proxy service install"
            )
        if (self.link.exists() or self.link.is_symlink()) and (
            not self.link.is_symlink() or self.link.resolve() != self.script
        ):
            raise RuntimeError(f"Refusing to replace unrelated command: {self.link}")
        # Validate before stopping a working service or replacing its plist.
        if self.key.exists() and not self.key.read_text().strip():
            raise RuntimeError(f"Local API key is empty; repair {self.key} before reinstalling")
        self.stop()
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data.chmod(0o700)
        if not self.key.exists():
            fd = os.open(self.key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(secrets.token_urlsafe(32) + "\n")
        self.key.chmod(0o600)
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs.chmod(0o700)
        for name in ("stdout.log", "stderr.log"):
            fd = os.open(self.logs / name, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            os.close(fd)
            (self.logs / name).chmod(0o600)
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        path = list(
            dict.fromkeys(
                [
                    str(Path(browser).parent),
                    str(Path(sys.executable).parent),
                    str(self.home / ".local/bin"),
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                ]
            )
        )
        document = {
            "Label": LABEL,
            "ProgramArguments": [
                str(Path(sys.executable).absolute()),
                "-m",
                "poe_web_proxy.launchd",
            ],
            "WorkingDirectory": str(self.home),
            "EnvironmentVariables": {
                "HOME": str(self.home),
                "PATH": ":".join(path),
                "PYTHONUNBUFFERED": "1",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 15,
            "ExitTimeOut": 90,
            "Umask": 0o077,
            "StandardOutPath": str(self.logs / "stdout.log"),
            "StandardErrorPath": str(self.logs / "stderr.log"),
        }
        # Atomic replacement prevents launchd observing a partially written file.
        temporary = self.plist.with_suffix(".plist.tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as file:
                plistlib.dump(document, file)
            temporary.chmod(0o600)
            temporary.replace(self.plist)
        finally:
            temporary.unlink(missing_ok=True)
        self.link.parent.mkdir(parents=True, exist_ok=True)
        if not self.link.is_symlink():
            self.link.symlink_to(self.script)
        self.start()

    def start(self):
        if not self.plist.exists():
            raise RuntimeError("Not installed. Run: uv run poe-web-proxy service install")
        if not self.loaded():
            self.launchctl("enable", self.target)
            result = self.launchctl("bootstrap", self.domain, str(self.plist))
            if result.returncode:
                raise RuntimeError(f"LaunchAgent bootstrap failed. Inspect {self.logs}/stderr.log")
        # Idempotent; never kill a request just to run 'start' again.
        result = self.launchctl("kickstart", self.target)
        if result.returncode:
            raise RuntimeError(f"LaunchAgent start failed. Inspect {self.logs}/stderr.log")
        for _ in range(180):
            if self.healthy():
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"API did not become ready. Run poe-proxy status; inspect {self.logs}/stderr.log"
        )

    def stop(self):
        if self.loaded():
            result = self.launchctl("bootout", self.target)
            if result.returncode:
                raise RuntimeError("LaunchAgent stop failed. Run: poe-proxy status")
            # bootout can return before the old job disappears from the domain.
            # Never let a following start kickstart that departing registration.
            for _ in range(900):
                if not self.loaded():
                    return
                time.sleep(0.1)
            raise RuntimeError("LaunchAgent is still unloading; retry poe-proxy status")

    def status(self):
        result = self.launchctl("print", self.target)
        loaded = result.returncode == 0
        ready = loaded and self.healthy()
        pid = re.search(r"\bpid = (\d+)", result.stdout)
        typer.echo(
            f"LaunchAgent: {'loaded' if loaded else 'stopped'}"
            + (f" (PID {pid[1]})" if pid else "")
        )
        typer.echo(f"API: {'ready' if ready else 'not ready'} — {URL}")
        typer.echo(f"Key file: {self.key}\nLogs: {self.logs}")
        return ready


def managed() -> Service:
    if sys.platform != "darwin":
        raise typer.BadParameter(
            "LaunchAgent management requires macOS; use poe-web-proxy serve elsewhere"
        )
    return Service()


def execute(action):
    try:
        action()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@cli.command()
def install():
    """Enable login-time startup, preserve/create a local key, and start now."""
    execute(managed().install)
    typer.echo("Installed and ready. Use: poe-proxy start | stop | restart | status")


@cli.command()
def start():
    """Start the installed agent; repeated calls do not interrupt active requests."""
    execute(managed().start)
    typer.echo(f"Ready: {URL}")


@cli.command()
def stop():
    """Stop until manually started or next login; also stop before running login."""
    execute(managed().stop)
    typer.echo("Stopped. Start again: poe-proxy start")


@cli.command()
def restart():
    """Stop and start the installed service (interrupts active requests)."""
    service = managed()
    execute(service.stop)
    execute(service.start)
    typer.echo(f"Ready: {URL}")


@cli.command()
def status():
    """Show launchd/API readiness and credential/log paths, never the key itself."""
    if not managed().status():
        raise typer.Exit(1)


@cli.command()
def uninstall():
    """Remove autostart and the shortcut; retain credentials, logs and browser auth."""
    service = managed()

    def remove():
        service.stop()
        service.plist.unlink(missing_ok=True)
        if service.link.is_symlink() and service.link.resolve() == service.script:
            service.link.unlink()

    execute(remove)
    typer.echo("Autostart removed; credentials and logs retained.")


def run():
    """LaunchAgent entry point; no credential is present in argv or the plist."""
    from .cli import serve

    service = Service()
    os.environ["POE_PROXY_API_KEY"] = service.key.read_text().strip()
    serve()


if __name__ == "__main__":
    run()
