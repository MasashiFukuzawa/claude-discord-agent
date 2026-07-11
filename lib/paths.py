"""XDG-compatible runtime paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def state_dir() -> Path:
    """Return the mutable state directory without creating it."""
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "claude-discord-agent"


def config_dir() -> Path:
    """Return the user configuration directory without creating it."""
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "claude-discord-agent"


def ensure_private_dir(path: Path, *, tighten_existing: bool = True) -> Path:
    """Create an owner-only directory and tighten an existing directory."""
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and (tighten_existing or not existed):
        path.chmod(0o700)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o700:
            raise PermissionError(f"Could not secure directory {path}: mode {mode:o}")
    return path


def secure_file(path: Path) -> Path:
    """Tighten an existing regular file to owner read/write on POSIX."""
    if os.name == "posix" and path.exists():
        if path.is_symlink() or not path.is_file():
            raise PermissionError(f"Refusing non-regular state file: {path}")
        path.chmod(0o600)
    return path
