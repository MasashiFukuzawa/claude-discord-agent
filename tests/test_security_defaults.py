from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.notifier import NotifyResult
from lib.paths import ensure_private_dir, secure_file
from lib.supervisor import Supervisor, _redact_result_preview


def test_private_directory_and_file_modes(tmp_path: Path) -> None:
    directory = ensure_private_dir(tmp_path / "state")
    secret = directory / "state.json"
    secret.write_text("data")
    secure_file(secret)

    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_fallback_preview_is_disabled_by_default() -> None:
    supervisor = Supervisor(object())  # type: ignore[arg-type]
    assert supervisor.fallback_result_preview is False


def test_result_preview_redacts_credentials_identity_and_home_path() -> None:
    address = "user" + "@" + "example.invalid"
    preview = _redact_result_preview(
        f"token=top-secret contact={address} path=/home/operator/project/file.txt"
    )
    assert "top-secret" not in preview
    assert address not in preview
    assert "/home/operator" not in preview
    assert preview.count("[REDACTED]") == 3


def test_fallback_message_omits_result_unless_opted_in() -> None:
    db = MagicMock()
    job = {
        "id": 7,
        "state": "succeeded",
        "notify_chat_id": "channel",
        "result": "token=top-secret completed",
    }
    with patch(
        "lib.supervisor.notify_discord_result", return_value=NotifyResult(success=True)
    ) as notify:
        Supervisor(db)._attempt_fallback_notify(job)
        assert "Result:" not in notify.call_args.args[1]

        Supervisor(db, fallback_result_preview=True)._attempt_fallback_notify(job)
        message = notify.call_args.args[1]
        assert "Result:" in message
        assert "top-secret" not in message
