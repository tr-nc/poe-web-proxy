from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from poe_web_proxy.cli import cli
from poe_web_proxy.paths import data_dir


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("poe_web_proxy.paths.sys.platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return tmp_path


def test_linux_defaults_to_user_data(home):
    assert data_dir() == home / ".local/share/poe-web-proxy"


def test_linux_uses_absolute_xdg_data_home(home, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "custom data"))
    assert data_dir() == home / "custom data/poe-web-proxy"


@pytest.mark.parametrize("value", ["", "relative/data"])
def test_relative_xdg_is_ignored(home, monkeypatch, value):
    monkeypatch.setenv("XDG_DATA_HOME", value)
    assert data_dir() == home / ".local/share/poe-web-proxy"


def test_existing_linux_bindings_are_not_abandoned(home, monkeypatch):
    old = home / "Library/Application Support/poe-web-proxy"
    old.mkdir(parents=True)
    (old / "chats.sqlite3").touch()
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "new-data"))
    assert data_dir() == old


def test_macos_location_is_unchanged(home, monkeypatch):
    monkeypatch.setattr("poe_web_proxy.paths.sys.platform", "darwin")
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "custom"))
    assert data_dir() == home / "Library/Application Support/poe-web-proxy"


def test_serve_stores_bindings_in_platform_directory(home, monkeypatch):
    monkeypatch.setenv("POE_PROXY_API_KEY", "test-local-secret")
    run = Mock()
    monkeypatch.setattr("poe_web_proxy.cli.uvicorn.run", run)
    result = CliRunner().invoke(cli, ["serve"])
    assert result.exit_code == 0, result.output
    database = home / ".local/share/poe-web-proxy/chats.sqlite3"
    assert database.is_file()
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.parent.stat().st_mode & 0o777 == 0o700
    assert run.call_args.kwargs["host"] == "127.0.0.1"
