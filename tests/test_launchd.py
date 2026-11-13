import plistlib
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from poe_web_proxy.cli import cli
from poe_web_proxy.launchd import LABEL, Service


def service(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "poe_web_proxy.launchd.shutil.which", lambda _: "/opt/node/bin/agent-browser"
    )
    value = Service()
    value.launchctl = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
    value.start = Mock()
    return value


def test_install_keeps_key_outside_plist_and_preserves_it(tmp_path, monkeypatch):
    manager = service(tmp_path, monkeypatch)
    manager.install()
    key = manager.key.read_text()
    assert len(key.strip()) >= 32
    assert manager.key.stat().st_mode & 0o777 == 0o600
    document = plistlib.loads(manager.plist.read_bytes())
    assert document["Label"] == LABEL
    assert document["RunAtLoad"] and document["KeepAlive"]
    assert document["ProgramArguments"][1:] == ["-m", "poe_web_proxy.launchd"]
    assert "/opt/node/bin" in document["EnvironmentVariables"]["PATH"]
    assert key.strip() not in manager.plist.read_text()
    assert "POE_PROXY_API_KEY" not in document["EnvironmentVariables"]
    assert manager.link.resolve() == manager.script
    manager.install()
    assert manager.key.read_text() == key
    assert isinstance(manager.start, Mock)
    assert manager.start.call_count == 2


def test_install_preserves_unrelated_shortcut(tmp_path, monkeypatch):
    manager = service(tmp_path, monkeypatch)
    manager.link.parent.mkdir(parents=True)
    manager.link.write_text("user file")
    with pytest.raises(RuntimeError, match="Refusing to replace"):
        manager.install()
    assert manager.link.read_text() == "user file"
    assert not manager.key.exists()


def test_stop_unloads_only_our_agent(tmp_path, monkeypatch):
    manager = service(tmp_path, monkeypatch)
    manager.launchctl = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),  # old registration still unloading
            subprocess.CompletedProcess([], 1, "", ""),
        ]
    )
    monkeypatch.setattr("poe_web_proxy.launchd.time.sleep", lambda _: None)
    manager.stop()
    assert [c.args for c in manager.launchctl.call_args_list] == [
        ("print", manager.target),
        ("bootout", manager.target),
        ("print", manager.target),
        ("print", manager.target),
    ]


def test_start_is_idempotent_and_never_force_kills(tmp_path, monkeypatch):
    manager = service(tmp_path, monkeypatch)
    manager.plist.parent.mkdir(parents=True)
    manager.plist.touch()
    manager.healthy = Mock(return_value=True)
    manager.launchctl = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    Service.start(manager)
    assert manager.launchctl.call_args_list[-1].args == ("kickstart", manager.target)
    assert not any("bootout" in c.args or "-k" in c.args for c in manager.launchctl.call_args_list)


def test_service_help_and_unknown_command():
    runner = CliRunner()
    result = runner.invoke(cli, ["service", "--help"])
    assert result.exit_code == 0
    for command in ["install", "start", "stop", "restart", "status", "uninstall"]:
        assert command in result.output
        assert runner.invoke(cli, ["service", command, "--help"]).exit_code == 0
    assert runner.invoke(cli, ["service", "wrong-command"]).exit_code == 2
