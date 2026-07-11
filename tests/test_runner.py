"""Runner tests: ClaudeProcess管理、stream-json解析、stderr rate limit検出。

注: 実際のclaudeプロセス起動はモックで対応。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import Database
from lib.runner import ClaudeProcess, Runner, RunnerError, _is_rate_limit_error


class TestRateLimitDetection(unittest.TestCase):
    """stderrベースのrate limit検出テスト。"""

    def test_rate_limit_patterns(self):
        self.assertTrue(_is_rate_limit_error("Error: rate limit exceeded"))
        self.assertTrue(_is_rate_limit_error("429 Too Many Requests"))
        self.assertTrue(_is_rate_limit_error("rate_limit_error"))
        self.assertTrue(_is_rate_limit_error("API is overloaded"))
        self.assertFalse(_is_rate_limit_error("normal error message"))
        self.assertFalse(_is_rate_limit_error(""))

    def test_check_rate_limit_from_stderr(self):
        proc = ClaudeProcess(task="test", working_dir="/tmp")
        proc.stderr_lines = ["Error: rate limit exceeded for model"]
        proc.output_lines = []
        self.assertTrue(proc._check_rate_limit())

    def test_check_rate_limit_from_stdout(self):
        proc = ClaudeProcess(task="test", working_dir="/tmp")
        proc.stderr_lines = []
        proc.output_lines = ['{"error":"429 Too Many Requests"}']
        self.assertTrue(proc._check_rate_limit())

    def test_no_rate_limit(self):
        proc = ClaudeProcess(task="test", working_dir="/tmp")
        proc.stderr_lines = ["some other error"]
        proc.output_lines = ['{"type":"result","result":"done"}']
        self.assertFalse(proc._check_rate_limit())


class TestClaudeProcess(unittest.TestCase):
    """ClaudeProcessの基本テスト（プロセス起動なし）。"""

    def test_initial_state(self):
        proc = ClaudeProcess(
            task="test task",
            working_dir="/tmp",
            model="sonnet",
        )
        self.assertIsNone(proc.process)
        self.assertIsNone(proc.pid)
        self.assertFalse(proc.is_alive)
        self.assertEqual(proc.elapsed, 0.0)
        self.assertEqual(proc.idle_seconds, 0.0)

    def test_result_extraction(self):
        proc = ClaudeProcess(task="test", working_dir="/tmp")
        proc.output_lines = [
            '{"type":"assistant","message":{"text":"working..."}}',
            '{"type":"result","result":"Task completed successfully"}',
        ]
        proc._extract_result()
        self.assertEqual(proc.result_text, "Task completed successfully")

    def test_result_extraction_no_result(self):
        proc = ClaudeProcess(task="test", working_dir="/tmp")
        proc.output_lines = [
            '{"type":"assistant","message":{"text":"working..."}}',
            "not a json line",
        ]
        proc._extract_result()
        self.assertEqual(proc.result_text, "")

    def test_event_callback(self):
        events: list[dict] = []
        proc = ClaudeProcess(
            task="test",
            working_dir="/tmp",
            on_event=lambda e: events.append(e),
        )
        proc.on_event({"type": "start"})
        proc.on_event({"type": "assistant", "message": {"text": "hello"}})
        self.assertEqual(len(events), 2)


class TestRunner(unittest.TestCase):
    """Runner管理テスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.runner = Runner(self.db)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_run_job_not_found(self):
        with self.assertRaises(RunnerError):
            self.runner.run_job(999, 1)

    def test_run_job_repo_not_found(self):
        repo_id = self.db.create_repo("test", "/tmp/test")
        job_id = self.db.create_job(repo_id, "Test task")
        self.db.conn.execute("PRAGMA foreign_keys=OFF")
        self.db.conn.execute("UPDATE jobs SET repo_id = 999 WHERE id = ?", (job_id,))
        self.db.conn.commit()
        self.db.conn.execute("PRAGMA foreign_keys=ON")
        with self.assertRaises(RunnerError):
            self.runner.run_job(job_id, 1)

    def test_active_processes_empty(self):
        self.assertEqual(len(self.runner.active_processes()), 0)

    def test_stop_nonexistent(self):
        self.assertFalse(self.runner.stop_job(999))

    def test_stop_all_empty(self):
        self.assertEqual(self.runner.stop_all(), 0)

    @patch("lib.runner.subprocess.Popen")
    def test_run_job_creates_attempt(self, mock_popen: MagicMock) -> None:
        """ジョブ実行時にattemptが作成される。"""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        repo_id = self.db.create_repo("test", "/tmp/test")
        sess_id = self.db.create_session(repo_id)
        job_id = self.db.create_job(repo_id, "Test task")

        with patch("lib.runner.ClaudeProcess._read_stdout"):
            attempt_id = self.runner.run_job(job_id, sess_id)
        self.assertIsNotNone(attempt_id)

        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "running")

        attempts = self.db.get_attempts(job_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["session_id"], sess_id)

        # #5修正: sessionがbusyになっている
        session = self.db.get_session(sess_id)
        self.assertEqual(session["state"], "busy")

    @patch("lib.runner.subprocess.Popen")
    def test_run_job_uses_repo_path_not_working_dir(self, mock_popen: MagicMock) -> None:
        """#6修正: working_dirは常にrepo pathから取得。"""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        repo_id = self.db.create_repo("test", "/tmp/safe-repo-path")
        sess_id = self.db.create_session(repo_id)
        job_id = self.db.create_job(repo_id, "Test task")

        with patch("lib.runner.ClaudeProcess._read_stdout"):
            self.runner.run_job(job_id, sess_id)

        # Popenのcwd引数がrepo pathであること
        call_kwargs = mock_popen.call_args
        self.assertEqual(
            call_kwargs.kwargs.get("cwd") or call_kwargs[1].get("cwd"), "/tmp/safe-repo-path"
        )

    @patch("lib.runner.subprocess.Popen")
    @patch("lib.runner._load_system_prompt")
    def test_run_job_passes_system_prompt(
        self, mock_load_prompt: MagicMock, mock_popen: MagicMock
    ) -> None:
        """R4 回帰ガード: worker-system-prompt が --append-system-prompt として argv に渡される。

        このテストが落ちたら runner.py の system-prompt 配線が壊れている。
        """
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0  # exit code を整数に固定（MagicMock 型エラー防止）
        mock_popen.return_value = mock_proc

        # _load_system_prompt が固定文字列を返すようにモック
        mock_load_prompt.return_value = "WORKER_SYSTEM_PROMPT_CONTENT"

        repo_id = self.db.create_repo("prompt-test", "/tmp/prompt-test")
        sess_id = self.db.create_session(repo_id)
        job_id = self.db.create_job(repo_id, "Test task with prompt")

        with patch("lib.runner.ClaudeProcess._read_stdout"):
            self.runner.run_job(job_id, sess_id)

        # Popen に渡された argv を検証
        call_args = mock_popen.call_args
        argv = call_args[0][0] if call_args[0] else call_args.kwargs.get("args", [])

        # 必須フラグの存在確認
        self.assertIn("claude", argv, "claude CLI がコマンドに含まれること")
        self.assertIn("-p", argv, "print モードフラグが含まれること")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("--model", argv, "model フラグが含まれること")

        # --append-system-prompt フラグの存在確認（R4 回帰ガード）
        self.assertIn(
            "--append-system-prompt",
            argv,
            "--append-system-prompt フラグが含まれること（worker-system-prompt.md の配送）",
        )

        # --append-system-prompt の次引数がプロンプト本文であること
        idx = argv.index("--append-system-prompt")
        self.assertEqual(
            argv[idx + 1],
            "WORKER_SYSTEM_PROMPT_CONTENT",
            "--append-system-prompt の値が worker-system-prompt.md の内容であること",
        )

    @patch("lib.runner.subprocess.Popen")
    @patch("lib.runner._load_system_prompt")
    def test_run_job_without_system_prompt_still_starts(
        self, mock_load_prompt: MagicMock, mock_popen: MagicMock
    ) -> None:
        """system-prompt 読み込み失敗時も worker が起動する（ハードフェイルしない）。"""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0  # exit code を整数に固定（MagicMock 型エラー防止）
        mock_popen.return_value = mock_proc

        # _load_system_prompt が None を返す（読み込み失敗）
        mock_load_prompt.return_value = None

        repo_id = self.db.create_repo("no-prompt-test", "/tmp/no-prompt-test")
        sess_id = self.db.create_session(repo_id)
        job_id = self.db.create_job(repo_id, "Test task without prompt")

        with patch("lib.runner.ClaudeProcess._read_stdout"):
            attempt_id = self.runner.run_job(job_id, sess_id)
        self.assertIsNotNone(attempt_id)

        # system-prompt なしでも Popen は呼ばれる
        mock_popen.assert_called_once()

        # --append-system-prompt は含まれない（None なので追加しない）
        call_args = mock_popen.call_args
        argv = call_args[0][0] if call_args[0] else call_args.kwargs.get("args", [])
        self.assertNotIn("--append-system-prompt", argv)


class TestStreamJsonParsing(unittest.TestCase):
    """stream-json出力のパーステスト。"""

    def test_parse_result_event(self):
        line = json.dumps({"type": "result", "result": "Task done"})
        event = json.loads(line)
        self.assertEqual(event["type"], "result")
        self.assertEqual(event["result"], "Task done")

    def test_parse_assistant_event(self):
        line = json.dumps({"type": "assistant", "message": {"text": "Working on it..."}})
        event = json.loads(line)
        self.assertEqual(event["type"], "assistant")

    def test_parse_tool_use_event(self):
        line = json.dumps({"type": "tool_use", "tool": "bash", "input": {"command": "ls"}})
        event = json.loads(line)
        self.assertEqual(event["type"], "tool_use")


if __name__ == "__main__":
    unittest.main()
