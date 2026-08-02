from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.daemon import _log_file_for, is_daemon_running

REPO_ROOT = Path(__file__).resolve().parent.parent


def _start_detached(state_dir: Path) -> subprocess.CompletedProcess[str]:
    """Start the daemon through a pipe, as a wrapper script would."""
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "orchestrator.py"), "daemon", "start"],
        cwd=REPO_ROOT,
        env={**os.environ, "XDG_STATE_HOME": str(state_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _stop(state_dir: Path) -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "orchestrator.py"), "daemon", "stop"],
        cwd=REPO_ROOT,
        env={**os.environ, "XDG_STATE_HOME": str(state_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(os.name != "posix", reason="daemon detach is POSIX-only")
def test_daemon_start_releases_the_callers_pipe(tmp_path: Path) -> None:
    """capture_output blocks until every writer closes the pipe.

    Rebinding sys.stdout leaves fd 1/2 attached to the caller, so a wrapper
    that reads our output hangs for the daemon's whole lifetime. Reaching the
    assertions at all is the regression signal.
    """
    state_dir = tmp_path / "state"
    try:
        result = _start_detached(state_dir)
        assert result.returncode == 0, result.stderr
        assert "Daemon started" in result.stdout

        agent_state = state_dir / "claude-discord-agent"
        deadline = time.time() + 10
        while time.time() < deadline and not is_daemon_running(agent_state):
            time.sleep(0.2)
        assert is_daemon_running(agent_state)
    finally:
        _stop(state_dir)


@pytest.mark.skipif(os.name != "posix", reason="daemon detach is POSIX-only")
def test_foreground_daemon_logs_to_stderr(tmp_path: Path) -> None:
    """Foreground mode is what a process supervisor runs, so it needs a trail.

    Nothing redirects fd 2 here, so the supervisor's captured stderr is the
    only place a crash can be explained.
    """
    state_dir = tmp_path / "state"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "orchestrator.py"),
            "daemon",
            "start",
            "--foreground",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "XDG_STATE_HOME": str(state_dir)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not is_daemon_running(
            state_dir / "claude-discord-agent"
        ):
            time.sleep(0.2)
        assert is_daemon_running(state_dir / "claude-discord-agent")
    finally:
        proc.terminate()
        _, stderr = proc.communicate(timeout=30)

    assert "Daemon started" in stderr
    assert "Daemon stopped" in stderr


@pytest.mark.skipif(os.name != "posix", reason="daemon detach is POSIX-only")
def test_daemon_log_is_owner_only_and_records_startup(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    agent_state = state_dir / "claude-discord-agent"
    try:
        assert _start_detached(state_dir).returncode == 0

        log_path = _log_file_for(agent_state)
        deadline = time.time() + 10
        while time.time() < deadline and not log_path.read_text():
            time.sleep(0.2)

        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
        assert "Daemon started" in log_path.read_text()
    finally:
        _stop(state_dir)
