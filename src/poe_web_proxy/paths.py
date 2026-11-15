"""Platform storage locations; preserve existing chat bindings on upgrade."""

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    legacy = Path.home() / "Library/Application Support/poe-web-proxy"
    if sys.platform == "darwin" or (legacy / "chats.sqlite3").exists():
        # Never silently abandon bindings created by an older Linux install.
        return legacy
    configured = Path(os.environ.get("XDG_DATA_HOME", ""))
    base = configured if configured.is_absolute() else Path.home() / ".local/share"
    return base / "poe-web-proxy"
