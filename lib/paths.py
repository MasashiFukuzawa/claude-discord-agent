"""XDG-compatible runtime paths."""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Return the mutable state directory without creating it."""
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "claude-discord-agent"


def config_dir() -> Path:
    """Return the user configuration directory without creating it."""
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "claude-discord-agent"
