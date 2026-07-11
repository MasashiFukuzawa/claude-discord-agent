"""Notifier tests: Discord Bot API呼び出し、トークン読み込み、notify_job_state。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.notifier import _load_token, notify_discord, notify_job_state


class TestLoadToken(unittest.TestCase):
    """トークン読み込みテスト。"""

    def test_env_var_takes_priority(self):
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "env-token"}):
            self.assertEqual(_load_token(), "env-token")

    def test_reads_from_env_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("DISCORD_BOT_TOKEN=file-token\n")
            f.write("OTHER_VAR=other\n")
            env_path = Path(f.name)
        try:
            with patch.dict(
                os.environ,
                {"DISCORD_AGENT_ENV_FILE": str(env_path)},
                clear=True,
            ):
                tok = _load_token()
            self.assertEqual(tok, "file-token")
        finally:
            env_path.unlink(missing_ok=True)

    def test_returns_none_when_not_found(self):
        with patch.dict(
            os.environ,
            {"DISCORD_AGENT_ENV_FILE": "/nonexistent/.env"},
            clear=True,
        ):
            tok = _load_token()
        self.assertIsNone(tok)

    def test_rejects_group_or_world_readable_env_file(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("DISCORD_BOT_TOKEN=unsafe-token\n")
            env_path = Path(f.name)
        try:
            env_path.chmod(0o644)
            with patch.dict(
                os.environ,
                {"DISCORD_AGENT_ENV_FILE": str(env_path)},
                clear=True,
            ):
                self.assertIsNone(_load_token())
        finally:
            env_path.unlink(missing_ok=True)

    def test_rejects_symlink_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "credentials")
            target.write_text("DISCORD_BOT_TOKEN=unsafe-token\n")
            target.chmod(0o600)
            link = Path(directory, "env")
            link.symlink_to(target)
            with patch.dict(
                os.environ,
                {"DISCORD_AGENT_ENV_FILE": str(link)},
                clear=True,
            ):
                self.assertIsNone(_load_token())

    def test_rejects_env_file_owned_by_another_user(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("DISCORD_BOT_TOKEN=unsafe-token\n")
            env_path = Path(f.name)
        try:
            env_path.chmod(0o600)
            with (
                patch.dict(
                    os.environ,
                    {"DISCORD_AGENT_ENV_FILE": str(env_path)},
                    clear=True,
                ),
                patch("lib.notifier.os.getuid", return_value=os.getuid() + 1),
            ):
                self.assertIsNone(_load_token())
        finally:
            env_path.unlink(missing_ok=True)


class TestNotifyDiscord(unittest.TestCase):
    """notify_discord HTTPリクエストテスト。"""

    def test_returns_false_when_no_token(self):
        with patch("lib.notifier._load_token", return_value=None):
            result = notify_discord("123456789", "test message")
        self.assertFalse(result)

    @patch("lib.notifier.urllib.request.urlopen")
    def test_sends_correct_request(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__ = lambda s: mock_resp
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        with patch("lib.notifier._load_token", return_value="test-token"):
            result = notify_discord("111222333", "hello world")

        self.assertTrue(result)
        req = mock_urlopen.call_args[0][0]
        self.assertIn("111222333", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bot test-token")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        body = json.loads(req.data.decode())
        self.assertEqual(body["content"], "hello world")

    @patch("lib.notifier.urllib.request.urlopen")
    def test_returns_false_on_http_error(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=403, msg="Forbidden", hdrs=None, fp=None
        )
        with patch("lib.notifier._load_token", return_value="test-token"):
            result = notify_discord("123", "msg")
        self.assertFalse(result)

    @patch("lib.notifier.urllib.request.urlopen")
    def test_returns_false_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("Connection refused")
        with patch("lib.notifier._load_token", return_value="test-token"):
            result = notify_discord("123", "msg")
        self.assertFalse(result)


class TestNotifyJobState(unittest.TestCase):
    """notify_job_state: メッセージ構成と呼び出し制御テスト。"""

    def test_noop_when_chat_id_none(self):
        with patch("lib.notifier.notify_discord") as mock_send:
            notify_job_state(None, job_id=42, state="succeeded")
        mock_send.assert_not_called()

    def test_noop_when_chat_id_empty(self):
        with patch("lib.notifier.notify_discord") as mock_send:
            notify_job_state("", job_id=42, state="succeeded")
        mock_send.assert_not_called()

    def test_succeeded_message_format(self):
        with patch("lib.notifier.notify_discord") as mock_send:
            mock_send.return_value = True
            notify_job_state(
                "1234567890", job_id=99, state="succeeded", task="Fix bug", result="Done"
            )

        mock_send.assert_called_once()
        chat_id, message = mock_send.call_args[0]
        self.assertEqual(chat_id, "1234567890")
        self.assertIn("✅", message)
        self.assertIn("Job 99", message)
        self.assertIn("succeeded", message)
        self.assertIn("Fix bug", message)
        self.assertIn("Done", message)

    def test_failed_permanent_message_format(self):
        with patch("lib.notifier.notify_discord") as mock_send:
            mock_send.return_value = True
            notify_job_state(
                "111", job_id=5, state="failed_permanent", task="Deploy", result="Error"
            )

        _, message = mock_send.call_args[0]
        self.assertIn("❌", message)
        self.assertIn("failed_permanent", message)

    def test_timed_out_message_format(self):
        with patch("lib.notifier.notify_discord") as mock_send:
            mock_send.return_value = True
            notify_job_state("111", job_id=7, state="timed_out", task="Long task")

        _, message = mock_send.call_args[0]
        self.assertIn("⏱️", message)
        self.assertIn("timed_out", message)

    def test_lost_message_format(self):
        with patch("lib.notifier.notify_discord") as mock_send:
            mock_send.return_value = True
            notify_job_state("111", job_id=8, state="lost", task="Lost task")

        _, message = mock_send.call_args[0]
        self.assertIn("🔴", message)

    def test_long_task_truncated(self):
        long_task = "A" * 200
        with patch("lib.notifier.notify_discord") as mock_send:
            mock_send.return_value = True
            notify_job_state("111", job_id=1, state="succeeded", task=long_task)

        _, message = mock_send.call_args[0]
        # 80文字+...に切り詰め
        self.assertIn("A" * 80, message)
        self.assertIn("...", message)

    def test_long_result_truncated(self):
        long_result = "R" * 500
        with patch("lib.notifier.notify_discord") as mock_send:
            mock_send.return_value = True
            notify_job_state("111", job_id=1, state="succeeded", task="t", result=long_result)

        _, message = mock_send.call_args[0]
        self.assertIn("R" * 200, message)
        self.assertIn("...", message)

    def test_no_result_section_when_result_none(self):
        with patch("lib.notifier.notify_discord") as mock_send:
            mock_send.return_value = True
            notify_job_state("111", job_id=1, state="succeeded", task="task")

        _, message = mock_send.call_args[0]
        self.assertNotIn("Result:", message)


if __name__ == "__main__":
    unittest.main()
