"""chain-dispatch 機能のテスト: 正常系・失敗halt・depth limit・migration idempotency・chain-resume。

R-B 変更後: scheduler から notify_* を撤去したため、テストもパッチなし・DB 状態ベースに変更。
Discord 通知は Supervisor._tick() が担う。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import Database
from lib.scheduler import MAX_CHAIN_DEPTH, Scheduler


class TestChainDispatchNormal(unittest.TestCase):
    """chain dispatch の正常系: job 成功後に次の job が自動 enqueue される。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.scheduler = Scheduler(self.db, global_max_concurrency=4)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def _create_repo(self, name="test-chain"):
        return self.db.create_repo(name, f"/tmp/{name}")

    def test_on_success_enqueues_next_job(self):
        """job 成功後に next_dispatch_payload から次の job が queued になる。"""
        repo_id = self._create_repo("chain-repo")

        next_payload = json.dumps(
            {
                "repo": "chain-repo",
                "task": "step 2 task",
                "notify_chat_id": "99999",
                "next_dispatch_payload": None,
            }
        )

        job_id = self.db.create_job(
            repo_id,
            "step 1 task",
            notify_chat_id="99999",
            next_dispatch_payload=next_payload,
            chain_depth=0,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")

        self.scheduler.on_success(repo_id, job_id, result="step1 done")

        # job 1 が succeeded
        job1 = self.db.get_job(job_id)
        self.assertEqual(job1["state"], "succeeded")

        # job 2 が queued されている
        jobs = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(jobs), 1)
        job2 = jobs[0]
        self.assertEqual(job2["task"], "step 2 task")
        self.assertEqual(job2["notify_chat_id"], "99999")
        self.assertEqual(job2["chain_depth"], 1)
        self.assertIsNone(job2["next_dispatch_payload"])

    def test_three_job_chain(self):
        """3 ジョブ chain が順番に enqueue される。"""
        repo_id = self._create_repo("chain3")

        payload_3 = json.dumps(
            {
                "repo": "chain3",
                "task": "step 3",
                "next_dispatch_payload": None,
            }
        )
        payload_2 = json.dumps(
            {
                "repo": "chain3",
                "task": "step 2",
                "next_dispatch_payload": json.loads(payload_3),
            }
        )

        job1_id = self.db.create_job(
            repo_id,
            "step 1",
            next_dispatch_payload=payload_2,
            chain_depth=0,
        )
        self.db.transition_job(job1_id, "starting")
        self.db.transition_job(job1_id, "running")
        self.scheduler.on_success(repo_id, job1_id, result="done1")

        # job2 がキューに入った
        queued = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(queued), 1)
        job2 = queued[0]
        self.assertEqual(job2["task"], "step 2")
        self.assertEqual(job2["chain_depth"], 1)
        # job2 の next_dispatch_payload は step 3
        next_p = json.loads(job2["next_dispatch_payload"])
        self.assertEqual(next_p["task"], "step 3")

        # job2 を succeeded にする
        self.db.transition_job(job2["id"], "starting")
        self.db.transition_job(job2["id"], "running")
        self.scheduler.on_success(repo_id, job2["id"], result="done2")

        # job3 がキューに入った
        queued2 = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(queued2), 1)
        job3 = queued2[0]
        self.assertEqual(job3["task"], "step 3")
        self.assertEqual(job3["chain_depth"], 2)
        self.assertIsNone(job3["next_dispatch_payload"])

    def test_no_chain_on_success_without_next_payload(self):
        """next_dispatch_payload が NULL の job は chain しない。"""
        repo_id = self._create_repo("no-chain")
        job_id = self.db.create_job(repo_id, "simple task")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_success(repo_id, job_id, result="done")

        queued = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(queued), 0)


class TestChainDispatchHalt(unittest.TestCase):
    """chain halt 条件のテスト。

    R-B 変更後: Discord 通知は Supervisor が担うため、chain halt 時の挙動は
    「新 job が enqueue されない」「ログにエラーが出る」の2点で検証する。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.scheduler = Scheduler(self.db, global_max_concurrency=4)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_halt_on_invalid_json(self):
        """next_dispatch_payload が不正 JSON の場合 chain halt になる（新 job なし）。"""
        repo_id = self.db.create_repo("halt-test", "/tmp/halt-test")
        job_id = self.db.create_job(
            repo_id,
            "step 1",
            notify_chat_id="12345",
            next_dispatch_payload="NOT VALID JSON",
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_success(repo_id, job_id)

        # 新 job が enqueue されない
        queued = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(queued), 0)

    def test_halt_on_missing_repo(self):
        """next payload の repo が存在しない場合 chain halt になる（新 job なし）。"""
        repo_id = self.db.create_repo("halt-repo", "/tmp/halt-repo")
        next_payload = json.dumps({"repo": "nonexistent-repo", "task": "step 2"})
        job_id = self.db.create_job(
            repo_id,
            "step 1",
            notify_chat_id="12345",
            next_dispatch_payload=next_payload,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_success(repo_id, job_id)

        queued = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(queued), 0)

    def test_halt_on_missing_task_field(self):
        """next payload に task フィールドがない場合 chain halt になる（新 job なし）。"""
        repo_id = self.db.create_repo("halt-task", "/tmp/halt-task")
        next_payload = json.dumps({"repo": "halt-task"})  # task が欠落
        job_id = self.db.create_job(
            repo_id,
            "step 1",
            notify_chat_id="12345",
            next_dispatch_payload=next_payload,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_success(repo_id, job_id)

        queued = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(queued), 0)

    def test_halt_on_max_retries_exceeded(self):
        """max_retries 超過で failed_permanent になった job は chain しない。"""
        repo_id = self.db.create_repo("halt-retry", "/tmp/halt-retry")
        sess_id = self.db.create_session(repo_id)
        next_payload = json.dumps({"repo": "halt-retry", "task": "step 2"})
        job_id = self.db.create_job(
            repo_id,
            "step 1",
            max_retries=1,
            notify_chat_id="12345",
            next_dispatch_payload=next_payload,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        # 1 回 attempt を作成 (max_retries=1 なのでこれで超過)
        self.db.create_attempt(job_id, sess_id)
        self.db.transition_job(job_id, "failed_retryable")

        result = self.scheduler.requeue_job(job_id)

        # requeue は失敗し failed_permanent になる
        self.assertFalse(result)
        self.assertEqual(self.db.get_job(job_id)["state"], "failed_permanent")
        # 新 job は enqueue されない（chain halt）
        queued = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(queued), 0)


class TestChainDepthLimit(unittest.TestCase):
    """chain depth limit のテスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.scheduler = Scheduler(self.db, global_max_concurrency=4)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_halt_at_depth_limit(self):
        """chain_depth が MAX_CHAIN_DEPTH を超えると halt する（新 job なし）。"""
        repo_id = self.db.create_repo("depth-test", "/tmp/depth-test")
        next_payload = json.dumps({"repo": "depth-test", "task": "next step"})

        # chain_depth = MAX_CHAIN_DEPTH の job を作成 (次のジョブは MAX+1 になる → halt)
        job_id = self.db.create_job(
            repo_id,
            "deep step",
            notify_chat_id="12345",
            next_dispatch_payload=next_payload,
            chain_depth=MAX_CHAIN_DEPTH,
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.scheduler.on_success(repo_id, job_id)

        # 新しい job は enqueue されない
        queued = self.db.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(queued), 0)

    def test_max_chain_depth_constant(self):
        """MAX_CHAIN_DEPTH は 50 であること。"""
        self.assertEqual(MAX_CHAIN_DEPTH, 50)


class TestMigrationIdempotency(unittest.TestCase):
    """マイグレーションの idempotency テスト。"""

    def test_migration_runs_twice_without_error(self):
        """migration を 2 回実行してもエラーにならない。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            # 1 回目
            db1 = Database(tmp.name)
            db1.close()
            # 2 回目 (同じ DB ファイルを開く)
            db2 = Database(tmp.name)
            # next_dispatch_payload と chain_depth 列が存在することを確認
            row = db2.conn.execute("PRAGMA table_info(jobs)").fetchall()
            col_names = [r[1] for r in row]
            self.assertIn("next_dispatch_payload", col_names)
            self.assertIn("chain_depth", col_names)
            # Supervisor 列も存在することを確認
            self.assertIn("terminal_at", col_names)
            self.assertIn("wake_state", col_names)
            self.assertIn("wake_attempts", col_names)
            self.assertIn("reported_at", col_names)
            db2.close()
        finally:
            os.unlink(tmp.name)

    def test_create_job_with_chain_fields(self):
        """create_job() が新列を正しく保存する。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            db = Database(tmp.name)
            repo_id = db.create_repo("migration-test", "/tmp/migration-test")
            payload = json.dumps({"repo": "migration-test", "task": "step 2"})
            job_id = db.create_job(
                repo_id,
                "step 1",
                next_dispatch_payload=payload,
                chain_depth=3,
            )
            job = db.get_job(job_id)
            self.assertEqual(job["next_dispatch_payload"], payload)
            self.assertEqual(job["chain_depth"], 3)
            db.close()
        finally:
            os.unlink(tmp.name)


class TestChainResumeCLI(unittest.TestCase):
    """chain-resume CLI コマンドのテスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_chain_resume_enqueues_next_job(self):
        """chain-resume が next_dispatch_payload から新 job を enqueue する。"""
        import argparse

        db = Database(self.tmp.name)
        repo_id = db.create_repo("resume-repo", "/tmp/resume-repo")

        # halt した job (next_dispatch_payload あり)
        next_payload = json.dumps(
            {
                "repo": "resume-repo",
                "task": "resumed step",
                "notify_chat_id": "77777",
                "next_dispatch_payload": None,
            }
        )
        failed_job_id = db.create_job(
            repo_id,
            "failed step",
            notify_chat_id="77777",
            next_dispatch_payload=next_payload,
            chain_depth=2,
        )
        db.transition_job(failed_job_id, "starting")
        db.transition_job(failed_job_id, "running")
        db.transition_job(failed_job_id, "failed_retryable")
        db.transition_job(failed_job_id, "failed_permanent")
        db.close()

        # orchestrator.py の cmd_chain_resume を呼ぶ
        import orchestrator

        # --db フラグで一時 DB を指定
        sys.argv = ["orchestrator", "--db", self.tmp.name, "chain-resume", str(failed_job_id)]
        orchestrator._custom_db_path = self.tmp.name

        args = argparse.Namespace(job_id=failed_job_id)

        with unittest.mock.patch("orchestrator.get_db") as mock_get_db:
            db2 = Database(self.tmp.name)
            mock_get_db.return_value = db2
            with unittest.mock.patch("orchestrator.is_daemon_running", return_value=None):
                ret = orchestrator.cmd_chain_resume(args)

        self.assertEqual(ret, 0)

        # 新 job が queued されている
        db3 = Database(self.tmp.name)
        jobs = db3.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(jobs), 1)
        new_job = jobs[0]
        self.assertEqual(new_job["task"], "resumed step")
        self.assertEqual(new_job["chain_depth"], 3)
        self.assertEqual(new_job["notify_chat_id"], "77777")
        db3.close()

    def test_chain_resume_preserves_nested_payload(self):
        """chain-resume が3ステップ chain の中間失敗から再開する際、残りの chain を保持する。"""
        import argparse

        db = Database(self.tmp.name)
        repo_id = db.create_repo("multi-resume-repo", "/tmp/multi-resume-repo")

        # step3 用のペイロード（chain の末尾）
        step3_payload = {
            "repo": "multi-resume-repo",
            "task": "step3",
            "next_dispatch_payload": None,
        }
        # step2 用のペイロード（next に step3 を持つ）
        step2_payload = json.dumps(
            {
                "repo": "multi-resume-repo",
                "task": "step2",
                "notify_chat_id": "99999",
                "next_dispatch_payload": step3_payload,
            }
        )

        # step1 が halt した job（next_dispatch_payload = step2 → step3）
        failed_job_id = db.create_job(
            repo_id,
            "step1",
            notify_chat_id="99999",
            next_dispatch_payload=step2_payload,
            chain_depth=0,
        )
        db.transition_job(failed_job_id, "starting")
        db.transition_job(failed_job_id, "running")
        db.transition_job(failed_job_id, "failed_retryable")
        db.transition_job(failed_job_id, "failed_permanent")
        db.close()

        import orchestrator

        args = argparse.Namespace(job_id=failed_job_id)
        with unittest.mock.patch("orchestrator.get_db") as mock_get_db:
            db2 = Database(self.tmp.name)
            mock_get_db.return_value = db2
            with unittest.mock.patch("orchestrator.is_daemon_running", return_value=None):
                ret = orchestrator.cmd_chain_resume(args)

        self.assertEqual(ret, 0)

        # step2 job が queued され、step3 のペイロードを保持している
        db3 = Database(self.tmp.name)
        jobs = db3.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(jobs), 1)
        new_job = jobs[0]
        self.assertEqual(new_job["task"], "step2")
        self.assertEqual(new_job["chain_depth"], 1)
        # nested next_dispatch_payload（step3）が保持されていること
        self.assertIsNotNone(new_job["next_dispatch_payload"])
        nested = json.loads(new_job["next_dispatch_payload"])
        self.assertEqual(nested["task"], "step3")
        db3.close()

    def test_chain_resume_no_payload_returns_error(self):
        """next_dispatch_payload が NULL の job に chain-resume するとエラー。"""
        import argparse

        db = Database(self.tmp.name)
        repo_id = db.create_repo("no-payload-repo", "/tmp/no-payload-repo")
        job_id = db.create_job(repo_id, "lone job")
        db.transition_job(job_id, "starting")
        db.transition_job(job_id, "running")
        db.transition_job(job_id, "failed_retryable")
        db.transition_job(job_id, "failed_permanent")
        db.close()

        import orchestrator

        args = argparse.Namespace(job_id=job_id)

        with unittest.mock.patch("orchestrator.get_db") as mock_get_db:
            db2 = Database(self.tmp.name)
            mock_get_db.return_value = db2
            ret = orchestrator.cmd_chain_resume(args)

        self.assertEqual(ret, 1)

    def test_chain_resume_with_enqueue_at(self):
        """chain-resume が enqueue_at を持つ payload を正しく次ジョブに伝播させる。"""
        import argparse

        db = Database(self.tmp.name)
        repo_id = db.create_repo("resume-ea-repo", "/tmp/resume-ea-repo")

        # halt した job: next payload に enqueue_at: "+7d" を含む
        next_payload = json.dumps(
            {
                "repo": "resume-ea-repo",
                "task": "delayed resumed step",
                "enqueue_at": "+7d",
                "next_dispatch_payload": None,
            }
        )
        failed_job_id = db.create_job(
            repo_id,
            "failed step",
            next_dispatch_payload=next_payload,
            chain_depth=0,
        )
        db.transition_job(failed_job_id, "starting")
        db.transition_job(failed_job_id, "running")
        db.transition_job(failed_job_id, "failed_retryable")
        db.transition_job(failed_job_id, "failed_permanent")
        db.close()

        import time

        import orchestrator

        args = argparse.Namespace(job_id=failed_job_id)
        before = time.time()
        with unittest.mock.patch("orchestrator.get_db") as mock_get_db:
            db2 = Database(self.tmp.name)
            mock_get_db.return_value = db2
            with unittest.mock.patch("orchestrator.is_daemon_running", return_value=None):
                ret = orchestrator.cmd_chain_resume(args)
        after = time.time()

        self.assertEqual(ret, 0)

        db3 = Database(self.tmp.name)
        jobs = db3.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(jobs), 1)
        new_job = jobs[0]
        self.assertEqual(new_job["task"], "delayed resumed step")

        # enqueue_at が "resume 時点から +7d" の範囲内
        expected_min = before + 7 * 86400
        expected_max = after + 7 * 86400
        self.assertGreaterEqual(new_job["enqueue_at"], expected_min)
        self.assertLessEqual(new_job["enqueue_at"], expected_max)
        db3.close()

    def test_chain_resume_invalid_enqueue_at_returns_error(self):
        """chain-resume で不正な enqueue_at がある場合はエラーを返す。"""
        import argparse

        db = Database(self.tmp.name)
        repo_id = db.create_repo("resume-bad-ea-repo", "/tmp/resume-bad-ea-repo")

        next_payload = json.dumps(
            {
                "repo": "resume-bad-ea-repo",
                "task": "bad enqueue step",
                "enqueue_at": "junk",
                "next_dispatch_payload": None,
            }
        )
        failed_job_id = db.create_job(
            repo_id,
            "failed step",
            next_dispatch_payload=next_payload,
            chain_depth=0,
        )
        db.transition_job(failed_job_id, "starting")
        db.transition_job(failed_job_id, "running")
        db.transition_job(failed_job_id, "failed_retryable")
        db.transition_job(failed_job_id, "failed_permanent")
        db.close()

        import orchestrator

        args = argparse.Namespace(job_id=failed_job_id)
        with unittest.mock.patch("orchestrator.get_db") as mock_get_db:
            db2 = Database(self.tmp.name)
            mock_get_db.return_value = db2
            ret = orchestrator.cmd_chain_resume(args)

        self.assertEqual(ret, 1)

        # job は queued されていない
        db3 = Database(self.tmp.name)
        jobs = db3.list_jobs(repo_id=repo_id, state="queued")
        self.assertEqual(len(jobs), 0)
        db3.close()


class TestBuildChainPayload(unittest.TestCase):
    """_build_chain_payload() のユニットテスト。"""

    def setUp(self):
        import orchestrator

        self.build = orchestrator._build_chain_payload

    def test_single_entry(self):
        entries = [{"repo": "r1", "task": "t1"}]
        result = self.build(entries)
        self.assertEqual(result["repo"], "r1")
        self.assertIsNone(result["next_dispatch_payload"])

    def test_two_entries(self):
        entries = [
            {"repo": "r1", "task": "t1"},
            {"repo": "r2", "task": "t2"},
        ]
        result = self.build(entries)
        self.assertEqual(result["repo"], "r1")
        self.assertIsNotNone(result["next_dispatch_payload"])
        self.assertEqual(result["next_dispatch_payload"]["repo"], "r2")
        self.assertIsNone(result["next_dispatch_payload"]["next_dispatch_payload"])

    def test_three_entries(self):
        entries = [
            {"repo": "r1", "task": "t1"},
            {"repo": "r2", "task": "t2"},
            {"repo": "r3", "task": "t3"},
        ]
        result = self.build(entries)
        self.assertEqual(result["repo"], "r1")
        second = result["next_dispatch_payload"]
        self.assertEqual(second["repo"], "r2")
        third = second["next_dispatch_payload"]
        self.assertEqual(third["repo"], "r3")
        self.assertIsNone(third["next_dispatch_payload"])

    def test_empty_entries(self):
        result = self.build([])
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
