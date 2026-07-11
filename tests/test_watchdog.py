"""Watchdog tests: heartbeat監視、タイムアウト検出、復旧ポリシー。"""

import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import Database
from lib.runner import Runner
from lib.watchdog import RecoveryAction, Watchdog


class TestRecoveryAction(unittest.TestCase):
    def test_to_dict(self):
        action = RecoveryAction("retry_same_session", 42, "test detail")
        d = action.to_dict()
        self.assertEqual(d["action"], "retry_same_session")
        self.assertEqual(d["job_id"], 42)
        self.assertEqual(d["detail"], "test detail")

    def test_repr(self):
        action = RecoveryAction("fork_session", 1, "detail")
        self.assertIn("fork_session", repr(action))


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.runner = Runner(self.db)
        self.watchdog = Watchdog(
            self.db,
            self.runner,
            heartbeat_timeout=5.0,  # テスト用短めのタイムアウト
            job_timeout=60.0,
        )

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_check_all_empty(self):
        """アクティブジョブなしならアクション不要。"""
        actions = self.watchdog.check_all()
        self.assertEqual(len(actions), 0)

    def test_check_all_queued_ignored(self):
        """queuedジョブは無視。"""
        repo_id = self.db.create_repo("test", "/tmp/test")
        self.db.create_job(repo_id, "Queued task")
        actions = self.watchdog.check_all()
        self.assertEqual(len(actions), 0)

    def test_timed_out_first_attempt_retries(self):
        """最初のタイムアウトは同一セッションでリトライ。

        #4修正: attempt.started_at基準でタイムアウト判定。
        """
        repo_id = self.db.create_repo("test", "/tmp/test")
        job_id = self.db.create_job(repo_id, "Timeout test")
        sess_id = self.db.create_session(repo_id)

        att_id = self.db.create_attempt(job_id, sess_id)

        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")

        # attempt.started_atを古くする（job.created_atではない）
        self.db.conn.execute(
            "UPDATE attempts SET started_at = ? WHERE id = ?",
            (time.time() - 120, att_id),
        )
        self.db.conn.commit()

        self.watchdog.job_timeout = 60.0
        actions = self.watchdog.check_all()

        self.assertGreater(len(actions), 0)
        self.assertEqual(actions[0].action, "retry_same_session")

    def test_queued_time_not_counted_as_timeout(self):
        """#4修正: キュー滞留時間はタイムアウト判定に含まない。

        job.created_atが古くても、attempt.started_atが新しければタイムアウトしない。
        プロセスが存在せず finished_at もない場合は lost 判定になるのは正しい動作。
        ここでは attempt が完了済み（finished_at設定）でタイムアウトが発生しないことを確認。
        """
        repo_id = self.db.create_repo("test2", "/tmp/test2")
        job_id = self.db.create_job(repo_id, "Queue wait test")
        sess_id = self.db.create_session(repo_id)

        # job.created_atを2分前にするが、attemptは今始まったばかり
        self.db.conn.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (time.time() - 120, job_id),
        )
        self.db.conn.commit()

        att_id = self.db.create_attempt(job_id, sess_id)
        self.db.conn.execute(
            "UPDATE attempts SET started_at = ? WHERE id = ?",
            (time.time(), att_id),
        )
        self.db.conn.commit()

        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")

        # タイムアウトは attempt.started_at 基準で判定
        # attempt_elapsed ≈ 0s < job_timeout = 60s なのでタイムアウトしない
        # ただしプロセスが存在しないため lost 検出される
        self.watchdog.job_timeout = 60.0
        actions = self.watchdog.check_all()

        # lost判定が出る（タイムアウトではない）
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "report_lost")
        # 重要: タイムアウト（retry_same_session/fork_session）ではないこと
        self.assertNotIn(actions[0].action, ("retry_same_session", "fork_session"))

    def test_timed_out_second_attempt_forks(self):
        """2回目のタイムアウトは新セッションにfork。"""
        repo_id = self.db.create_repo("test3", "/tmp/test3")
        job_id = self.db.create_job(repo_id, "Fork test")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")

        sess_id = self.db.create_session(repo_id)
        self.db.create_attempt(job_id, sess_id)
        att2 = self.db.create_attempt(job_id, sess_id)

        # 最新attemptのstarted_atを古くする
        self.db.conn.execute(
            "UPDATE attempts SET started_at = ? WHERE id = ?",
            (time.time() - 120, att2),
        )
        self.db.conn.commit()

        self.watchdog.job_timeout = 60.0
        actions = self.watchdog.check_all()
        self.assertGreater(len(actions), 0)
        self.assertEqual(actions[0].action, "fork_session")

    def test_apply_recovery_retry(self):
        """リトライアクションの適用。"""
        repo_id = self.db.create_repo("test", "/tmp/test")
        job_id = self.db.create_job(repo_id, "Retry test")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "timed_out")

        action = RecoveryAction("retry_same_session", job_id)
        result = self.watchdog.apply_recovery(action)
        self.assertEqual(result["status"], "requeued")
        self.assertEqual(self.db.get_job(job_id)["state"], "queued")

    def test_apply_recovery_lost(self):
        """Lost検出時のneeds_controller遷移。"""
        repo_id = self.db.create_repo("test", "/tmp/test")
        job_id = self.db.create_job(repo_id, "Lost test")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "lost")

        action = RecoveryAction("report_lost", job_id)
        result = self.watchdog.apply_recovery(action)
        self.assertEqual(result["status"], "needs_controller")

    def test_apply_recovery_timed_out_cleanup(self):
        """#1修正: timed_out状態でもcleanupが実行される。"""
        repo_id = self.db.create_repo("test4", "/tmp/test4")
        job_id = self.db.create_job(repo_id, "Timeout cleanup test")
        sess_id = self.db.create_session(repo_id)
        self.db.create_attempt(job_id, sess_id)

        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        # _handle_timeoutが先にtimed_outに遷移した状態をシミュレート
        self.db.transition_job(job_id, "timed_out")

        action = RecoveryAction("retry_same_session", job_id)
        result = self.watchdog.apply_recovery(action)

        # cleanupが実行されたことを確認
        self.assertEqual(result["status"], "requeued")
        # attemptが完了マークされている
        attempts = self.db.get_attempts(job_id)
        self.assertIsNotNone(attempts[-1]["finished_at"])
        # sessionがidleに戻っている
        session = self.db.get_session(sess_id)
        self.assertEqual(session["state"], "idle")

    def test_fork_session_creates_new_session(self):
        """#3修正: fork_sessionが新sessionレコードを作成する。"""
        repo_id = self.db.create_repo("test5", "/tmp/test5")
        job_id = self.db.create_job(repo_id, "Fork new session test")
        sess_id = self.db.create_session(repo_id)
        self.db.create_attempt(job_id, sess_id)

        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "timed_out")

        action = RecoveryAction("fork_session", job_id)
        result = self.watchdog.apply_recovery(action)

        self.assertEqual(result["status"], "requeued_for_new_session")
        # 旧sessionがdead
        old_session = self.db.get_session(sess_id)
        self.assertEqual(old_session["state"], "dead")
        # 新sessionが作成されている
        self.assertIn("new_session_id", result)
        new_session = self.db.get_session(result["new_session_id"])
        self.assertIsNotNone(new_session)
        self.assertEqual(new_session["state"], "idle")

    def test_cleanup_joins_reader_threads(self):
        """#2回帰: _cleanup_running_processがreader threadをjoinする。"""
        from unittest.mock import MagicMock

        repo_id = self.db.create_repo("test6", "/tmp/test6")
        job_id = self.db.create_job(repo_id, "Thread join test")
        sess_id = self.db.create_session(repo_id)
        att_id = self.db.create_attempt(job_id, sess_id)

        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")

        # MockプロセスをRunnerに直接注入
        mock_proc = MagicMock()
        mock_proc.is_alive = False
        mock_proc._stdout_thread = MagicMock()
        mock_proc._stderr_thread = MagicMock()
        with self.runner._lock:
            self.runner._processes[att_id] = mock_proc

        self.watchdog._cleanup_running_process(job_id)

        # stop() + join()が呼ばれたことを確認
        mock_proc.stop.assert_called_once()
        mock_proc._stdout_thread.join.assert_called_once_with(timeout=5.0)
        mock_proc._stderr_thread.join.assert_called_once_with(timeout=5.0)

    def test_health_report(self):
        """ヘルスレポートの生成（DB-only, #4修正）。"""
        repo_id = self.db.create_repo("test", "/tmp/test")
        self.db.create_session(repo_id)
        report = self.watchdog.get_health_report()
        self.assertIn("total_sessions", report)
        self.assertIn("healthy", report)
        self.assertIn("running_jobs", report)
        self.assertIn("timestamp", report)
        self.assertEqual(report["total_sessions"], 1)
        # active_processesはもう返さない（DB-only）
        self.assertNotIn("active_processes", report)


class TestWatchdogStateTransitions(unittest.TestCase):
    """Watchdog の状態遷移テスト（R-B 変更後）。

    Discord 通知は Supervisor._tick() が担うため、watchdog は DB 状態遷移のみ検証する。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.runner = Runner(self.db)
        self.watchdog = Watchdog(
            self.db,
            self.runner,
            heartbeat_timeout=5.0,
            job_timeout=60.0,
        )

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def _make_running_job_with_old_attempt(self, notify_chat_id=None):
        repo_id = self.db.create_repo("wdnotify", "/tmp/wdnotify")
        job_id = self.db.create_job(repo_id, "WD notify task", notify_chat_id=notify_chat_id)
        sess_id = self.db.create_session(repo_id)
        att_id = self.db.create_attempt(job_id, sess_id)
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        # Force attempt started_at to be old enough to trigger timeout
        self.db.conn.execute(
            "UPDATE attempts SET started_at = ? WHERE id = ?",
            (time.time() - 120, att_id),
        )
        self.db.conn.commit()
        return repo_id, job_id, sess_id, att_id

    def test_handle_timeout_transitions_to_timed_out(self):
        """タイムアウト時に job が timed_out に遷移する（通知は Supervisor が担う）。"""
        _, job_id, _, _ = self._make_running_job_with_old_attempt(notify_chat_id="111222333")
        self.watchdog.job_timeout = 60.0
        actions = self.watchdog.check_all()

        self.assertGreater(len(actions), 0)
        # job は timed_out に遷移している
        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "timed_out")

    def test_handle_timeout_no_chat_id_still_transitions(self):
        """notify_chat_id なしでも timed_out に遷移する（通知先なしでも動作する）。"""
        _, job_id, _, _ = self._make_running_job_with_old_attempt(notify_chat_id=None)
        self.watchdog.job_timeout = 60.0
        self.watchdog.check_all()

        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "timed_out")

    def test_handle_lost_transitions_to_lost(self):
        """プロセス消失検出時に job が lost に遷移する（通知は Supervisor が担う）。"""
        repo_id = self.db.create_repo("wdlost", "/tmp/wdlost")
        job_id = self.db.create_job(repo_id, "Lost task", notify_chat_id="444555666")
        sess_id = self.db.create_session(repo_id)
        att_id = self.db.create_attempt(job_id, sess_id)
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        # attempt started_at is recent → no timeout; but no live process → lost
        self.db.conn.execute(
            "UPDATE attempts SET started_at = ? WHERE id = ?",
            (time.time(), att_id),
        )
        self.db.conn.commit()

        self.watchdog.job_timeout = 300.0  # won't time out
        actions = self.watchdog.check_all()

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "report_lost")
        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "lost")


if __name__ == "__main__":
    unittest.main()
