"""test_clear_session: register-pane / clear-session コマンドのテスト。"""

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import orchestrator


class TestRegisterPane(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.pane_file = Path(self.tmp_dir) / "controller.pane"

    def _make_args(self):
        args = MagicMock()
        return args

    def test_register_pane_no_tmux_env(self):
        """TMUX_PANE 未設定時はエラーを返す。"""
        env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(orchestrator, "Path"):
                exit_code = orchestrator.cmd_register_pane(self._make_args())
        self.assertEqual(exit_code, 1)

    def test_register_pane_invalid_format(self):
        """TMUX_PANE が ^%\\d+$ 形式でない場合はエラーを返す。"""
        with patch.dict(os.environ, {"TMUX_PANE": "invalid-pane"}, clear=False):
            exit_code = orchestrator.cmd_register_pane(self._make_args())
        self.assertEqual(exit_code, 1)

    def test_register_pane_valid(self):
        """正常系: TMUX_PANE=%3 のときファイルに書き出す。"""
        with patch.dict(os.environ, {"TMUX_PANE": "%3"}, clear=False):
            with patch("orchestrator._daemon_state_dir", return_value=Path(self.tmp_dir)):
                exit_code = orchestrator.cmd_register_pane(self._make_args())
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.pane_file.read_text(), "%3")


class TestClearSession(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def _make_args(self):
        args = MagicMock()
        return args

    def test_clear_session_no_pane_file(self):
        """controller.pane ファイルが存在しない場合はエラーを返す。"""
        with patch("orchestrator._daemon_state_dir", return_value=Path(self.tmp_dir)):
            exit_code = orchestrator.cmd_clear_session(self._make_args())
        self.assertEqual(exit_code, 1)

    def test_clear_session_invalid_pane_content(self):
        """controller.pane の内容が ^%\\d+$ でない場合はエラーを返す。"""
        Path(self.tmp_dir, "controller.pane").write_text("bad-value")
        with patch("orchestrator._daemon_state_dir", return_value=Path(self.tmp_dir)):
            exit_code = orchestrator.cmd_clear_session(self._make_args())
        self.assertEqual(exit_code, 1)

    def test_clear_session_pane_not_in_tmux(self):
        """tmux list-panes に pane が存在しない場合はエラーを返す。"""
        Path(self.tmp_dir, "controller.pane").write_text("%5")
        with patch("orchestrator._daemon_state_dir", return_value=Path(self.tmp_dir)):
            with patch("orchestrator.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="%1\n%2\n%3\n")
                exit_code = orchestrator.cmd_clear_session(self._make_args())
        self.assertEqual(exit_code, 1)
        mock_run.assert_called_once()

    def test_clear_session_success(self):
        """正常系: pane が存在するとき send-keys が呼ばれる。"""
        Path(self.tmp_dir, "controller.pane").write_text("%5")
        with patch("orchestrator._daemon_state_dir", return_value=Path(self.tmp_dir)):
            with patch("orchestrator.subprocess.run") as mock_run:
                # 1回目: list-panes (capture_output=True)  2回目: send-keys
                mock_run.side_effect = [
                    MagicMock(stdout="%3\n%5\n%7\n"),
                    MagicMock(returncode=0),
                ]
                exit_code = orchestrator.cmd_clear_session(self._make_args())
        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_run.call_count, 2)
        send_keys_call = mock_run.call_args_list[1]
        self.assertIn("/clear", send_keys_call[0][0])
        self.assertIn("Enter", send_keys_call[0][0])


if __name__ == "__main__":
    unittest.main()
