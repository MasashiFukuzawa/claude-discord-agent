"""Database layer tests: スキーマ、CRUD、状態遷移、スレッドセーフ。"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import JOB_STATES, VALID_TRANSITIONS, Database


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    # ─── Repos ──────────────────────────────────────────────

    def test_create_and_get_repo(self):
        repo_id = self.db.create_repo("test-repo", "/tmp/test", model="opus")
        self.assertIsNotNone(repo_id)
        repo = self.db.get_repo("test-repo")
        self.assertIsNotNone(repo)
        self.assertEqual(repo["name"], "test-repo")
        self.assertEqual(repo["path"], "/tmp/test")
        self.assertEqual(repo["model"], "opus")
        self.assertEqual(repo["max_concurrency"], 1)

    def test_create_repo_duplicate(self):
        self.db.create_repo("dup", "/tmp/dup")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_repo("dup", "/tmp/dup2")

    def test_repo_aliases(self):
        self.db.create_repo("main-repo", "/tmp/main", aliases=["mr", "main"])
        repo = self.db.get_repo_by_alias("mr")
        self.assertIsNotNone(repo)
        self.assertEqual(repo["name"], "main-repo")

    def test_list_repos(self):
        self.db.create_repo("a", "/tmp/a")
        self.db.create_repo("b", "/tmp/b")
        repos = self.db.list_repos()
        self.assertEqual(len(repos), 2)
        names = [r["name"] for r in repos]
        self.assertIn("a", names)
        self.assertIn("b", names)

    def test_update_repo(self):
        self.db.create_repo("upd", "/tmp/upd")
        self.db.update_repo("upd", model="opus", max_concurrency=3)
        repo = self.db.get_repo("upd")
        self.assertEqual(repo["model"], "opus")
        self.assertEqual(repo["max_concurrency"], 3)

    def test_delete_repo(self):
        self.db.create_repo("del", "/tmp/del")
        self.assertTrue(self.db.delete_repo("del"))
        self.assertIsNone(self.db.get_repo("del"))

    # ─── Sessions ───────────────────────────────────────────

    def test_create_and_get_session(self):
        repo_id = self.db.create_repo("sess-repo", "/tmp/sess")
        sess_id = self.db.create_session(repo_id, pid=12345)
        sess = self.db.get_session(sess_id)
        self.assertIsNotNone(sess)
        self.assertEqual(sess["repo_id"], repo_id)
        self.assertEqual(sess["state"], "idle")
        self.assertEqual(sess["pid"], 12345)

    def test_list_sessions(self):
        repo_id = self.db.create_repo("ls-repo", "/tmp/ls")
        self.db.create_session(repo_id)
        self.db.create_session(repo_id)
        sessions = self.db.list_sessions(repo_id=repo_id)
        self.assertEqual(len(sessions), 2)

    def test_update_session(self):
        repo_id = self.db.create_repo("us-repo", "/tmp/us")
        sess_id = self.db.create_session(repo_id)
        self.db.update_session(sess_id, state="busy", pid=99999)
        sess = self.db.get_session(sess_id)
        self.assertEqual(sess["state"], "busy")
        self.assertEqual(sess["pid"], 99999)

    # ─── Jobs ───────────────────────────────────────────────

    def test_create_and_get_job(self):
        repo_id = self.db.create_repo("job-repo", "/tmp/job")
        job_id = self.db.create_job(repo_id, "Do something", priority=5)
        job = self.db.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["priority"], 5)
        self.assertEqual(job["task"], "Do something")

    def test_job_state_transitions(self):
        repo_id = self.db.create_repo("trans-repo", "/tmp/trans")
        job_id = self.db.create_job(repo_id, "Transition test")

        # queued -> starting
        self.db.transition_job(job_id, "starting")
        self.assertEqual(self.db.get_job(job_id)["state"], "starting")

        # starting -> running
        self.db.transition_job(job_id, "running")
        self.assertEqual(self.db.get_job(job_id)["state"], "running")

        # running -> succeeded
        self.db.transition_job(job_id, "succeeded", result="Done!")
        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["result"], "Done!")

    def test_invalid_transition(self):
        repo_id = self.db.create_repo("inv-repo", "/tmp/inv")
        job_id = self.db.create_job(repo_id, "Invalid test")

        # queued -> running (invalid: must go through starting)
        with self.assertRaises(ValueError):
            self.db.transition_job(job_id, "running")

    def test_terminal_states_no_transition(self):
        repo_id = self.db.create_repo("term-repo", "/tmp/term")
        job_id = self.db.create_job(repo_id, "Terminal test")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded")

        # succeeded -> anything should fail
        with self.assertRaises(ValueError):
            self.db.transition_job(job_id, "queued")

    def test_rate_limit_recovery(self):
        repo_id = self.db.create_repo("rl-repo", "/tmp/rl")
        job_id = self.db.create_job(repo_id, "Rate limit test")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "rate_limited")
        self.db.transition_job(job_id, "queued")
        self.assertEqual(self.db.get_job(job_id)["state"], "queued")

    def test_count_running_jobs(self):
        repo_id = self.db.create_repo("count-repo", "/tmp/count")
        j1 = self.db.create_job(repo_id, "Job 1")
        j2 = self.db.create_job(repo_id, "Job 2")
        self.assertEqual(self.db.count_running_jobs(), 0)

        self.db.transition_job(j1, "starting")
        self.assertEqual(self.db.count_running_jobs(), 1)

        self.db.transition_job(j1, "running")
        self.db.transition_job(j2, "starting")
        self.assertEqual(self.db.count_running_jobs(), 2)
        self.assertEqual(self.db.count_running_jobs(repo_id), 2)

    def test_list_jobs_priority(self):
        repo_id = self.db.create_repo("prio-repo", "/tmp/prio")
        self.db.create_job(repo_id, "Low", priority=1)
        self.db.create_job(repo_id, "High", priority=10)
        self.db.create_job(repo_id, "Med", priority=5)
        jobs = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(jobs[0]["task"], "High")
        self.assertEqual(jobs[1]["task"], "Med")
        self.assertEqual(jobs[2]["task"], "Low")

    # ─── Attempts ───────────────────────────────────────────

    def test_create_attempts(self):
        repo_id = self.db.create_repo("att-repo", "/tmp/att")
        job_id = self.db.create_job(repo_id, "Attempt test")
        sess_id = self.db.create_session(repo_id)

        self.db.create_attempt(job_id, sess_id)
        self.db.create_attempt(job_id, sess_id)

        attempts = self.db.get_attempts(job_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["attempt_num"], 1)
        self.assertEqual(attempts[1]["attempt_num"], 2)

    def test_finish_attempt(self):
        repo_id = self.db.create_repo("fin-repo", "/tmp/fin")
        job_id = self.db.create_job(repo_id, "Finish test")
        sess_id = self.db.create_session(repo_id)
        att_id = self.db.create_attempt(job_id, sess_id)
        self.db.finish_attempt(att_id, exit_code=0)
        attempts = self.db.get_attempts(job_id)
        self.assertEqual(attempts[0]["exit_code"], 0)
        self.assertIsNotNone(attempts[0]["finished_at"])

    # ─── Events ─────────────────────────────────────────────

    def test_events(self):
        repo_id = self.db.create_repo("evt-repo", "/tmp/evt")
        job_id = self.db.create_job(repo_id, "Event test")
        sess_id = self.db.create_session(repo_id)
        att_id = self.db.create_attempt(job_id, sess_id)

        self.db.add_event(att_id, "start", '{"msg":"starting"}')
        self.db.add_event(att_id, "output", '{"text":"hello"}')
        self.db.add_event(att_id, "end", None)

        latest = self.db.get_latest_event(att_id)
        self.assertEqual(latest["event_type"], "end")

    # ─── Valid transitions map consistency ──────────────────

    def test_all_states_in_transitions(self):
        """全状態がVALID_TRANSITIONSに定義されている。"""
        for state in JOB_STATES:
            self.assertIn(state, VALID_TRANSITIONS)

    def test_transition_targets_are_valid(self):
        """遷移先が全て有効な状態。"""
        for source, targets in VALID_TRANSITIONS.items():
            for target in targets:
                self.assertIn(
                    target, JOB_STATES, f"{source} -> {target}: {target} is not a valid state"
                )


class TestTransaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_transaction_commit(self):
        with self.db.transaction():
            self.db.conn.execute(
                "INSERT INTO repos (name, path, aliases, model, max_concurrency, created_at)"
                " VALUES (?, ?, '[]', 'sonnet', 1, ?)",
                ("tx-repo", "/tmp/tx", 1000.0),
            )
        repo = self.db.get_repo("tx-repo")
        self.assertIsNotNone(repo)

    def test_transaction_rollback(self):
        try:
            with self.db.transaction():
                self.db.conn.execute(
                    "INSERT INTO repos (name, path, aliases, model, max_concurrency, created_at)"
                    " VALUES (?, ?, '[]', 'sonnet', 1, ?)",
                    ("rb-repo", "/tmp/rb", 1000.0),
                )
                raise RuntimeError("Intentional")
        except RuntimeError:
            pass
        repo = self.db.get_repo("rb-repo")
        self.assertIsNone(repo)


class TestThreadSafety(unittest.TestCase):
    """スレッドセーフなDB操作テスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.db.create_repo("thread-repo", "/tmp/thread")

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_concurrent_job_creation(self):
        """複数スレッドからの同時ジョブ作成。"""
        repo = self.db.get_repo("thread-repo")
        repo_id = repo["id"]
        errors: list[Exception] = []
        ids: list[int] = []
        lock = threading.Lock()

        def create_job(n: int) -> None:
            try:
                jid = self.db.create_job(repo_id, f"Thread job {n}")
                with lock:
                    ids.append(jid)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=create_job, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(len(ids), 10)
        # 全IDがユニーク
        self.assertEqual(len(set(ids)), 10)


class TestBackoffPersistence(unittest.TestCase):
    """バックオフのDB永続化テスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_set_and_get_backoff(self):
        repo_id = self.db.create_repo("bo-repo", "/tmp/bo")
        self.db.set_backoff(repo_id, 9999.0, 3)
        until, count = self.db.get_backoff(repo_id)
        self.assertEqual(until, 9999.0)
        self.assertEqual(count, 3)

    def test_clear_backoff(self):
        repo_id = self.db.create_repo("bo2-repo", "/tmp/bo2")
        self.db.set_backoff(repo_id, 9999.0, 5)
        self.db.clear_backoff(repo_id)
        until, count = self.db.get_backoff(repo_id)
        self.assertEqual(until, 0.0)
        self.assertEqual(count, 0)


class TestDaemonCommands(unittest.TestCase):
    """daemon_commandsのCRUDテスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_enqueue_and_poll(self):
        self.db.enqueue_command("kill_job", {"job_id": 42})
        self.db.enqueue_command("shutdown")
        cmds = self.db.poll_commands()
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0]["command"], "kill_job")
        self.assertEqual(cmds[0]["payload"]["job_id"], 42)
        self.assertEqual(cmds[1]["command"], "shutdown")

    def test_poll_marks_processed(self):
        self.db.enqueue_command("test_cmd")
        cmds1 = self.db.poll_commands()
        self.assertEqual(len(cmds1), 1)
        cmds2 = self.db.poll_commands()
        self.assertEqual(len(cmds2), 0)


class TestEnqueueAt(unittest.TestCase):
    """enqueue_at 列: migration 冪等 / create / list_jobs フィルタ。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.repo_id = self.db.create_repo("ea-repo", "/tmp/ea")

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_migration_idempotent(self):
        """同じ DB に Database() を 2 度作成しても例外が出ない。"""
        db2 = Database(self.tmp.name)
        db2.close()  # 再初期化が冪等であること

    def test_default_enqueue_at_zero(self):
        """enqueue_at を指定しない場合はデフォルト 0 (即時)。"""
        job_id = self.db.create_job(self.repo_id, "immediate job")
        job = self.db.get_job(job_id)
        self.assertEqual(job["enqueue_at"], 0)

    def test_create_job_with_enqueue_at(self):
        """enqueue_at を指定して create_job すると保存される。"""
        future = 9_999_999_999.0
        job_id = self.db.create_job(self.repo_id, "future job", enqueue_at=future)
        job = self.db.get_job(job_id)
        self.assertAlmostEqual(job["enqueue_at"], future, places=1)

    def test_list_jobs_now_filter_excludes_future(self):
        """now < enqueue_at のジョブは list_jobs(now=...) で除外される。"""
        past = 1_000_000.0
        future = 9_999_999_999.0
        now = 2_000_000.0
        self.db.create_job(self.repo_id, "past job", enqueue_at=past)
        self.db.create_job(self.repo_id, "future job", enqueue_at=future)
        jobs = self.db.list_jobs(repo_id=self.repo_id, state="queued", now=now)
        tasks = [j["task"] for j in jobs]
        self.assertIn("past job", tasks)
        self.assertNotIn("future job", tasks)

    def test_list_jobs_no_now_returns_all(self):
        """now を渡さない場合は全件返す (後方互換)。"""
        future = 9_999_999_999.0
        self.db.create_job(self.repo_id, "future job", enqueue_at=future)
        jobs = self.db.list_jobs(repo_id=self.repo_id, state="queued")
        self.assertEqual(len(jobs), 1)

    def test_list_jobs_order_by_enqueue_at(self):
        """同じ priority では enqueue_at ASC 順にソートされる。"""
        self.db.create_job(self.repo_id, "later", enqueue_at=2_000_000.0)
        self.db.create_job(self.repo_id, "earlier", enqueue_at=1_000_000.0)
        now = 5_000_000.0
        jobs = self.db.list_jobs(repo_id=self.repo_id, state="queued", now=now)
        self.assertEqual(jobs[0]["task"], "earlier")
        self.assertEqual(jobs[1]["task"], "later")



class TestStateMachineFix(unittest.TestCase):
    """state-machine fix: rate_limited → failed_permanent 許可確認。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_rate_limited_to_failed_permanent(self):
        """rate_limited → failed_permanent が許可されている (daemon クラッシュ防止)。"""
        repo_id = self.db.create_repo("sm-repo", "/tmp/sm")
        job_id = self.db.create_job(repo_id, "state-machine test")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "rate_limited")
        self.db.transition_job(job_id, "failed_permanent", result="max retries exceeded")
        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "failed_permanent")
        self.assertEqual(job["result"], "max retries exceeded")

    def test_starting_to_lost(self):
        """starting → lost が許可されている (watchdog silent fail 防止)。"""
        repo_id = self.db.create_repo("sm-repo2", "/tmp/sm2")
        job_id = self.db.create_job(repo_id, "starting lost test")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "lost")
        self.assertEqual(self.db.get_job(job_id)["state"], "lost")


if __name__ == "__main__":
    unittest.main()
