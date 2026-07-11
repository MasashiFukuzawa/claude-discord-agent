"""Scheduler tests: 同時実行制御、バックオフ（DB永続化）、キュー管理、リカバリ駆動。"""

import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import Database
from lib.scheduler import BackoffTracker, Scheduler


class TestBackoffTracker(unittest.TestCase):
    def test_initial_no_backoff(self):
        bt = BackoffTracker(base_delay=10.0, jitter=0.0)
        self.assertFalse(bt.is_backed_off(1))
        self.assertEqual(bt.get_wait_time(1), 0.0)

    def test_record_and_backoff(self):
        bt = BackoffTracker(base_delay=10.0, jitter=0.0)
        next_time = bt.record_rate_limit(1)
        self.assertTrue(bt.is_backed_off(1))
        self.assertGreater(bt.get_wait_time(1), 0.0)
        self.assertGreater(next_time, time.time())

    def test_exponential_increase(self):
        bt = BackoffTracker(base_delay=10.0, max_delay=300.0, jitter=0.0)
        bt.record_rate_limit(1)
        wait1 = bt.get_wait_time(1)
        bt.record_rate_limit(1)
        wait2 = bt.get_wait_time(1)
        self.assertGreater(wait2, wait1)

    def test_max_delay_cap(self):
        bt = BackoffTracker(base_delay=100.0, max_delay=200.0, jitter=0.0)
        for _ in range(10):
            bt.record_rate_limit(1)
        wait = bt.get_wait_time(1)
        self.assertLessEqual(wait, 201.0)

    def test_clear_resets(self):
        bt = BackoffTracker(base_delay=10.0, jitter=0.0)
        bt.record_rate_limit(1)
        self.assertTrue(bt.is_backed_off(1))
        bt.clear(1)
        self.assertFalse(bt.is_backed_off(1))

    def test_jitter_adds_variance(self):
        results = set()
        for _ in range(20):
            bt2 = BackoffTracker(base_delay=100.0, jitter=0.5)
            bt2.record_rate_limit(1)
            results.add(round(bt2.get_wait_time(1), 1))
        self.assertGreater(len(results), 1)

    def test_per_repo_isolation(self):
        bt = BackoffTracker(base_delay=10.0, jitter=0.0)
        bt.record_rate_limit(1)
        self.assertTrue(bt.is_backed_off(1))
        self.assertFalse(bt.is_backed_off(2))

    def test_db_persistence(self):
        """バックオフ状態がDBに永続化される。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        repo_id = db.create_repo("bo-test", "/tmp/bo-test")

        bt = BackoffTracker(db, base_delay=10.0, jitter=0.0)
        bt.record_rate_limit(repo_id)

        # DB直接チェック
        until, count = db.get_backoff(repo_id)
        self.assertGreater(until, time.time())
        self.assertEqual(count, 1)

        # 別のBackoffTrackerで復元
        bt2 = BackoffTracker(db, base_delay=10.0, jitter=0.0)
        self.assertTrue(bt2.is_backed_off(repo_id))

        db.close()
        os.unlink(tmp.name)


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.scheduler = Scheduler(
            self.db,
            global_max_concurrency=2,
            backoff=BackoffTracker(self.db, base_delay=10.0, jitter=0.0),
        )

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def _create_repo(self, name="test", max_conc=1):
        return self.db.create_repo(name, f"/tmp/{name}", max_concurrency=max_conc)

    def test_can_run_empty(self):
        repo_id = self._create_repo()
        self.assertTrue(self.scheduler.can_run(repo_id))

    def test_can_run_repo_limit(self):
        repo_id = self._create_repo(max_conc=1)
        job_id = self.db.create_job(repo_id, "Task 1")
        self.db.transition_job(job_id, "starting")
        self.assertFalse(self.scheduler.can_run(repo_id))

    def test_can_run_global_limit(self):
        r1 = self._create_repo("r1", max_conc=2)
        r2 = self._create_repo("r2", max_conc=2)
        j1 = self.db.create_job(r1, "Task 1")
        j2 = self.db.create_job(r2, "Task 2")
        self.db.transition_job(j1, "starting")
        self.db.transition_job(j2, "starting")
        # global_max = 2, 2つ走っているので追加不可
        r3 = self._create_repo("r3", max_conc=2)
        self.assertFalse(self.scheduler.can_run(r3))

    def test_can_run_backed_off(self):
        repo_id = self._create_repo()
        self.scheduler.backoff.record_rate_limit(repo_id)
        self.assertFalse(self.scheduler.can_run(repo_id))

    def test_next_job(self):
        repo_id = self._create_repo()
        self.db.create_job(repo_id, "Low", priority=1)
        self.db.create_job(repo_id, "High", priority=10)
        job = self.scheduler.next_job(repo_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["task"], "High")

    def test_next_job_respects_limit(self):
        repo_id = self._create_repo(max_conc=1)
        j1 = self.db.create_job(repo_id, "Running")
        self.db.transition_job(j1, "starting")
        self.db.create_job(repo_id, "Queued")
        job = self.scheduler.next_job(repo_id)
        self.assertIsNone(job)

    def test_on_success_clears_backoff_when_no_rate_limited(self):
        """#3修正: rate_limitedジョブがなければbackoffクリア。"""
        repo_id = self._create_repo()
        self.scheduler.backoff.record_rate_limit(repo_id)
        self.assertTrue(self.scheduler.backoff.is_backed_off(repo_id))

        job_id = self.db.create_job(repo_id, "Task")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        # rate_limitedジョブがないので、on_successでbackoffクリアされる
        self.scheduler.on_success(repo_id, job_id, "Done!")
        self.assertFalse(self.scheduler.backoff.is_backed_off(repo_id))

    def test_on_rate_limit(self):
        repo_id = self._create_repo()
        job_id = self.db.create_job(repo_id, "Rate limited task")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        next_avail = self.scheduler.on_rate_limit(repo_id, job_id)
        self.assertGreater(next_avail, time.time())
        self.assertEqual(self.db.get_job(job_id)["state"], "rate_limited")

    def test_status(self):
        repo_id = self._create_repo()
        self.db.create_job(repo_id, "Task 1")
        status = self.scheduler.status()
        self.assertEqual(status["global_running"], 0)
        self.assertEqual(status["queued_jobs"], 1)
        self.assertIn("test", status["repos"])

    def test_drive_recovery_failed_retryable(self):
        """drive_recoveryがfailed_retryableをrequeuする。"""
        repo_id = self._create_repo()
        job_id = self.db.create_job(repo_id, "Retry me")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "failed_retryable")

        actions = self.scheduler.drive_recovery()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "requeue_failed_retryable")
        self.assertEqual(self.db.get_job(job_id)["state"], "queued")

    def test_drive_recovery_rate_limited_backoff_not_expired(self):
        """rate_limitedはバックオフ期間中requeueされない。"""
        repo_id = self._create_repo()
        job_id = self.db.create_job(repo_id, "Rate limited")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_rate_limit(repo_id, job_id)

        actions = self.scheduler.drive_recovery()
        # バックオフ中なのでリキューされない
        self.assertEqual(len(actions), 0)
        self.assertEqual(self.db.get_job(job_id)["state"], "rate_limited")

    def test_drive_recovery_max_retries_exceeded(self):
        """max_retries超過でfailed_permanentになる。"""
        repo_id = self._create_repo()
        job_id = self.db.create_job(repo_id, "Max retry", max_retries=1)
        sess_id = self.db.create_session(repo_id)

        # 1回目のattemptを作成（max_retries=1なのでこれで超過）
        self.db.create_attempt(job_id, sess_id)
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "failed_retryable")

        self.scheduler.drive_recovery()
        # requeuではなくfailed_permanentに
        self.assertEqual(self.db.get_job(job_id)["state"], "failed_permanent")

    def test_on_rate_limit_atomic(self):
        """#1回帰: rate_limited遷移とbackoff記録が原子的。"""
        repo_id = self._create_repo()
        job_id = self.db.create_job(repo_id, "Atomic rate limit test")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")

        # on_rate_limitを呼ぶ
        self.scheduler.on_rate_limit(repo_id, job_id)

        # job状態とbackoff状態が同時に設定されている
        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "rate_limited")
        self.assertTrue(self.scheduler.backoff.is_backed_off(repo_id))

        # DB上のbackoff_untilも設定されている
        until, count = self.db.get_backoff(repo_id)
        self.assertGreater(until, time.time())
        self.assertEqual(count, 1)

    def test_on_success_preserves_backoff_when_rate_limited_exists(self):
        """#3回帰: 別ジョブのsuccessがactive backoffを消さない。"""
        repo_id = self._create_repo(max_conc=2)

        # ジョブA: rate_limited
        job_a = self.db.create_job(repo_id, "Job A - will be rate limited")
        self.db.transition_job(job_a, "starting")
        self.db.transition_job(job_a, "running")
        self.scheduler.on_rate_limit(repo_id, job_a)
        self.assertTrue(self.scheduler.backoff.is_backed_off(repo_id))

        # ジョブB: success
        job_b = self.db.create_job(repo_id, "Job B - will succeed")
        self.db.transition_job(job_b, "starting")
        self.db.transition_job(job_b, "running")
        self.scheduler.on_success(repo_id, job_b, "Done!")

        # Aがまだrate_limitedなので、backoffは消えない
        self.assertTrue(self.scheduler.backoff.is_backed_off(repo_id))
        self.assertEqual(self.db.get_job(job_a)["state"], "rate_limited")

    def test_backoff_cleared_when_all_rate_limited_resolved(self):
        """#3回帰: rate_limitedジョブが全て解消されたらbackoffクリア。"""
        repo_id = self._create_repo()
        job_id = self.db.create_job(repo_id, "Will be rate limited then resolved")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_rate_limit(repo_id, job_id)

        # backoff期限を過去に設定（手動で期限切れにする）
        self.db.set_backoff(repo_id, time.time() - 1, 1)
        self.scheduler.backoff._cache[repo_id] = (1, time.time() - 1)

        # drive_recoveryでrequeueされる
        actions = self.scheduler.drive_recovery()
        self.assertGreater(len(actions), 0)
        # rate_limitedジョブがなくなったのでbackoffがクリアされる
        self.assertFalse(self.scheduler.backoff.is_backed_off(repo_id))

    def test_interleaved_success_rate_limit_race(self):
        """R5回帰: on_success()とon_rate_limit()の並行実行で
        backoffが不正にクリアされないことをSQLiteレベルで確認。

        別々のDatabaseインスタンス（同一DBファイル）+ threading.Eventで
        実際のinterleaving順序を制御する。
        """
        import threading

        repo_id = self._create_repo(max_conc=3)
        job_a = self.db.create_job(repo_id, "Job A")
        self.db.transition_job(job_a, "starting")
        self.db.transition_job(job_a, "running")

        job_b = self.db.create_job(repo_id, "Job B")
        self.db.transition_job(job_b, "starting")
        self.db.transition_job(job_b, "running")

        # 別DBインスタンス（同一ファイル）で別Schedulerを作成
        db_a = Database(self.tmp.name)
        db_b = Database(self.tmp.name)
        sched_a = Scheduler(db_a, backoff=BackoffTracker(db_a, base_delay=10.0, jitter=0.0))
        sched_b = Scheduler(db_b, backoff=BackoffTracker(db_b, base_delay=10.0, jitter=0.0))

        event_a_started = threading.Event()
        event_b_done = threading.Event()
        errors: list[Exception] = []

        def thread_a() -> None:
            """on_success(job_a): BEGIN IMMEDIATE → COUNT=0 → clear → COMMIT"""
            try:
                # on_successはBEGIN IMMEDIATEを使うため
                # Thread Bが先にBEGIN IMMEDIATEを取得している場合はブロック
                event_a_started.set()
                sched_a.on_success(repo_id, job_a, "Done!")
            except Exception as e:
                errors.append(e)

        def thread_b() -> None:
            """on_rate_limit(job_b): BEGIN IMMEDIATE → rate_limited + backoff → COMMIT"""
            try:
                event_a_started.wait(timeout=5)
                sched_b.on_rate_limit(repo_id, job_b)
                event_b_done.set()
            except Exception as e:
                errors.append(e)
                event_b_done.set()

        # Thread AとBを並行起動
        # SQLiteのBEGIN IMMEDIATEにより、どちらが先に獲得しても直列化される
        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Errors: {errors}")

        # 検証用に新しいDB接続で最終状態を確認
        db_check = Database(self.tmp.name)
        job_b_state = db_check.get_job(job_b)
        self.assertEqual(job_b_state["state"], "rate_limited")

        # backoffが最終的にセットされている
        # （on_successが先でもon_rate_limitが後にcommitするのでbackoffが残る）
        until, count = db_check.get_backoff(repo_id)
        self.assertGreater(count, 0, "Backoff count should be > 0 after rate limit")
        self.assertGreater(until, time.time(), "Backoff until should be in the future")

        db_a.close()
        db_b.close()
        db_check.close()

    def test_cas_session_stale_completion_race(self):
        """R5回帰: 旧on_complete()がsessionを上書きしないことを
        別DBインスタンスで確認。

        シナリオ:
        1. attempt_id=1がsessionに設定される
        2. watchdogがsessionのcurrent_attempt_idを2に更新
        3. 旧on_complete(attempt_id=1)がCAS更新を試みる → 失敗
        """
        repo_id = self._create_repo()
        sess_id = self.db.create_session(repo_id)

        # attempt_id=1をセット
        self.db.update_session(sess_id, state="busy", current_attempt_id=1)

        # 別DB接続でcurrent_attempt_id=2に更新（watchdogが新attemptを開始した状態）
        db2 = Database(self.tmp.name)
        db2.update_session(sess_id, state="busy", current_attempt_id=2)

        # 旧on_complete（attempt_id=1）がCASでidle化を試みる → 失敗するはず
        result = self.db.cas_update_session(sess_id, 1, state="idle")
        self.assertFalse(result, "Stale attempt should not be able to update session")

        # sessionはbusyのまま（attempt_id=2）
        sess = db2.get_session(sess_id)
        self.assertEqual(sess["state"], "busy")
        self.assertEqual(sess["current_attempt_id"], 2)

        # 正しいattempt_id=2でのCAS更新は成功
        result2 = db2.cas_update_session(sess_id, 2, state="idle")
        self.assertTrue(result2)
        sess = db2.get_session(sess_id)
        self.assertEqual(sess["state"], "idle")

        db2.close()

    def test_cas_session_basic(self):
        """CAS session更新の基本テスト。"""
        repo_id = self._create_repo()
        sess_id = self.db.create_session(repo_id)

        self.db.update_session(sess_id, current_attempt_id=100)

        # 一致するattempt_idで更新 → 成功
        result = self.db.cas_update_session(sess_id, 100, state="idle")
        self.assertTrue(result)

        # 不一致のattempt_idで更新 → 失敗
        self.db.update_session(sess_id, state="busy", current_attempt_id=200)
        result = self.db.cas_update_session(sess_id, 99, state="idle")
        self.assertFalse(result)
        sess = self.db.get_session(sess_id)
        self.assertEqual(sess["state"], "busy")

    def test_concurrent_dispatch(self):
        """複数ジョブを同時投入してもDBが壊れない。"""
        import threading

        repo_id = self._create_repo(max_conc=5)
        errors: list[Exception] = []
        ids: list[int] = []
        lock = threading.Lock()

        def create_and_check(n: int) -> None:
            try:
                jid = self.db.create_job(repo_id, f"Concurrent job {n}", priority=n)
                with lock:
                    ids.append(jid)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=create_and_check, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)

        # 優先度順で取得できること
        jobs = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(jobs), 20)
        self.assertEqual(jobs[0]["priority"], 19)  # 最も高い優先度


class TestSchedulerStateTransitions(unittest.TestCase):
    """Scheduler の状態遷移テスト（R-B 変更後）。

    Discord 通知は Supervisor._tick() が担うため、DB 状態遷移のみを検証する。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.scheduler = Scheduler(self.db)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def _make_running_job(self, notify_chat_id=None):
        repo_id = self.db.create_repo("notify-test", "/tmp/notify-test")
        sess_id = self.db.create_session(repo_id)
        job_id = self.db.create_job(repo_id, "Notify test task", notify_chat_id=notify_chat_id)
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        return repo_id, sess_id, job_id

    def test_on_success_transitions_to_succeeded(self):
        """on_success() は job を succeeded に遷移させる（通知は Supervisor が担う）。"""
        repo_id, _, job_id = self._make_running_job(notify_chat_id="123456789")
        self.scheduler.on_success(repo_id, job_id, result="Done")

        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["result"], "Done")

    def test_on_success_sets_terminal_at(self):
        """on_success() は terminal_at を設定する（Supervisor の配送基準）。"""
        repo_id, _, job_id = self._make_running_job(notify_chat_id="123456789")
        before = time.time()
        self.scheduler.on_success(repo_id, job_id, result="Done")
        after = time.time()

        job = self.db.get_job(job_id)
        self.assertIsNotNone(job.get("terminal_at"), "terminal_at が設定されること")
        self.assertGreaterEqual(job["terminal_at"], before)
        self.assertLessEqual(job["terminal_at"], after)

    def test_on_success_without_chat_id_still_transitions(self):
        """notify_chat_id がなくても succeeded に遷移する。"""
        repo_id, _, job_id = self._make_running_job(notify_chat_id=None)
        self.scheduler.on_success(repo_id, job_id, result="Done")

        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "succeeded")

    def test_requeue_job_transitions_to_failed_permanent_on_max_retries(self):
        """max_retries 超過で failed_permanent になる（通知は Supervisor が担う）。"""
        repo_id = self.db.create_repo("notify-retry", "/tmp/notify-retry")
        sess_id = self.db.create_session(repo_id)
        job_id = self.db.create_job(
            repo_id, "Retry task", max_retries=2, notify_chat_id="987654321"
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.create_attempt(job_id, sess_id)
        self.db.create_attempt(job_id, sess_id)
        self.db.transition_job(job_id, "failed_retryable")
        result = self.scheduler.requeue_job(job_id)

        self.assertFalse(result)
        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "failed_permanent")


class TestEnqueueAtScheduler(unittest.TestCase):
    """time-based trigger: next_job の enqueue_at フィルタと chain 連携。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.scheduler = Scheduler(self.db, global_max_concurrency=4)
        self.repo_id = self.db.create_repo("ea-sched", "/tmp/ea-sched")

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_next_job_skips_future_job(self):
        """enqueue_at が未来のジョブは next_job() で返されない。"""
        self.db.create_job(self.repo_id, "future task", enqueue_at=9_999_999_999.0)
        job = self.scheduler.next_job()
        self.assertIsNone(job)

    def test_next_job_returns_past_job(self):
        """enqueue_at が過去のジョブは next_job() で返される。"""
        self.db.create_job(self.repo_id, "ready task", enqueue_at=1_000_000.0)
        job = self.scheduler.next_job()
        self.assertIsNotNone(job)
        self.assertEqual(job["task"], "ready task")

    def test_next_job_returns_zero_enqueue_at(self):
        """enqueue_at = 0 (デフォルト) のジョブは即時返される。"""
        self.db.create_job(self.repo_id, "immediate task")
        job = self.scheduler.next_job()
        self.assertIsNotNone(job)
        self.assertEqual(job["task"], "immediate task")

    def test_next_job_prefers_earlier_enqueue_at(self):
        """同じ priority では enqueue_at が早いジョブが優先される。"""
        self.db.create_job(self.repo_id, "later", enqueue_at=2_000_000.0)
        self.db.create_job(self.repo_id, "earlier", enqueue_at=1_000_000.0)
        job = self.scheduler.next_job()
        self.assertIsNotNone(job)
        self.assertEqual(job["task"], "earlier")

    def test_chain_dispatch_with_enqueue_at(self):
        """chain payload に enqueue_at を含む場合、次ジョブに設定される。"""
        import json

        future_str = "+7d"
        next_payload = json.dumps(
            {
                "repo": "ea-sched",
                "task": "step 2 delayed",
                "enqueue_at": future_str,
                "next_dispatch_payload": None,
            }
        )
        job_id = self.db.create_job(
            self.repo_id,
            "step 1",
            next_dispatch_payload=next_payload,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")

        before = time.time()
        self.scheduler.on_success(self.repo_id, job_id, result="done")
        after = time.time()

        # step 2 が queued になっている
        jobs = self.db.list_jobs(repo_id=self.repo_id, state="queued")
        self.assertEqual(len(jobs), 1)
        job2 = jobs[0]
        self.assertEqual(job2["task"], "step 2 delayed")

        # enqueue_at が "今から +7d" の範囲内である
        expected_min = before + 7 * 86400
        expected_max = after + 7 * 86400
        self.assertGreaterEqual(job2["enqueue_at"], expected_min)
        self.assertLessEqual(job2["enqueue_at"], expected_max)

        # 未来なので next_job() では返されない
        job = self.scheduler.next_job()
        self.assertIsNone(job)

    def test_chain_dispatch_invalid_enqueue_at_halts_chain(self):
        """chain payload に無効な enqueue_at がある場合は chain halt になる（新 job なし）。"""
        import json

        next_payload = json.dumps(
            {
                "repo": "ea-sched",
                "task": "step 2 bad",
                "enqueue_at": "not-a-time",
                "next_dispatch_payload": None,
            }
        )
        job_id = self.db.create_job(
            self.repo_id,
            "step 1",
            next_dispatch_payload=next_payload,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_success(self.repo_id, job_id, result="done")

        # chain が halt されている (step 2 は queued されていない)
        jobs = self.db.list_jobs(repo_id=self.repo_id, state="queued")
        self.assertEqual(len(jobs), 0)

    def test_chain_dispatch_iso8601_enqueue_at(self):
        """chain payload に ISO 8601 absolute datetime を含む場合、正しく解釈される。"""
        import json
        from datetime import datetime, timedelta, timezone

        # 確実に未来の時刻を ISO 8601 aware 形式で指定
        future_dt = datetime.now(timezone(timedelta(hours=9))) + timedelta(days=3)
        future_iso = future_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        expected_epoch = future_dt.timestamp()

        next_payload = json.dumps(
            {
                "repo": "ea-sched",
                "task": "iso8601 delayed step",
                "enqueue_at": future_iso,
                "next_dispatch_payload": None,
            }
        )
        job_id = self.db.create_job(
            self.repo_id,
            "step 1",
            next_dispatch_payload=next_payload,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_success(self.repo_id, job_id, result="done")

        jobs = self.db.list_jobs(repo_id=self.repo_id, state="queued")
        self.assertEqual(len(jobs), 1)
        job2 = jobs[0]
        self.assertEqual(job2["task"], "iso8601 delayed step")
        # epoch が期待値から ±5 秒以内
        self.assertAlmostEqual(job2["enqueue_at"], expected_epoch, delta=5)

    def test_chain_dispatch_no_enqueue_at_is_immediate(self):
        """chain payload に enqueue_at キーが無い場合、次ジョブは即時 (enqueue_at=0) になる。"""
        import json

        next_payload = json.dumps(
            {
                "repo": "ea-sched",
                "task": "immediate step",
                "next_dispatch_payload": None,
            }
        )
        job_id = self.db.create_job(
            self.repo_id,
            "step 1",
            next_dispatch_payload=next_payload,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_success(self.repo_id, job_id, result="done")

        jobs = self.db.list_jobs(repo_id=self.repo_id, state="queued")
        self.assertEqual(len(jobs), 1)
        job2 = jobs[0]
        self.assertEqual(job2["task"], "immediate step")
        self.assertEqual(job2["enqueue_at"], 0.0)
        # 即時なので next_job() で返される
        job = self.scheduler.next_job()
        self.assertIsNotNone(job)


if __name__ == "__main__":
    unittest.main()
