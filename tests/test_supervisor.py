"""Supervisor テスト: fail-closed wake・fallback・CAS 二重送信防止。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import Database
from lib.notifier import NotifyResult
from lib.supervisor import Supervisor


class TestSupervisorTickNoop(unittest.TestCase):
    """terminal ジョブがない / 全て reported 済みの場合は何もしない。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.sup = Supervisor(self.db, wake_grace_seconds=90.0)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_tick_no_jobs(self):
        """ジョブが存在しない場合 tick は何もしない。"""
        self.sup.tick()  # should not raise

    def test_tick_already_reported(self):
        """reported_at 設定済みジョブはスキップされる。"""
        repo_id = self.db.create_repo("rp", "/tmp/rp")
        job_id = self.db.create_job(repo_id, "task", notify_chat_id="123")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded", result="done")
        self.db.mark_reported(job_id, time.time())

        with patch.object(self.sup, "_drive_delivery") as mock_drive:
            self.sup.tick()
        mock_drive.assert_not_called()

    def test_tick_undeliverable_skipped(self):
        """undeliverable ジョブはスキップされる。"""
        repo_id = self.db.create_repo("ud", "/tmp/ud")
        job_id = self.db.create_job(repo_id, "task", notify_chat_id="123")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "failed_permanent", result="err")
        self.db.set_undeliverable(job_id)

        with patch.object(self.sup, "_drive_delivery") as mock_drive:
            self.sup.tick()
        # undeliverable は _drive_delivery に渡されるが内部でスキップ
        mock_drive.assert_called_once()


class TestSupervisorWake(unittest.TestCase):
    """Controller wake (tmux send-keys) 動作テスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.pane_file = Path(tempfile.mktemp(suffix=".pane"))
        self.sup = Supervisor(
            self.db,
            wake_grace_seconds=90.0,
            max_wake_attempts=2,
            pane_file=self.pane_file,
            auto_wake=True,
        )

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)
        self.pane_file.unlink(missing_ok=True)

    def _make_terminal_job(self, state="succeeded", notify_chat_id="ch1"):
        repo_id = self.db.create_repo("wr", "/tmp/wr")
        job_id = self.db.create_job(repo_id, "test task", notify_chat_id=notify_chat_id)
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, state, result="done")
        return job_id

    def test_try_wake_no_pane_file_goes_fallback(self):
        """controller.pane 未登録の場合は即 fallback に移行する。"""
        # pane_file は存在しない
        job_id = self._make_terminal_job()

        with patch.object(self.sup, "_attempt_fallback_notify") as mock_fb:
            self.sup._drive_delivery(self.db.get_job(job_id))

        mock_fb.assert_called_once()

    def test_try_wake_pane_not_exists_goes_fallback(self):
        """pane が tmux に存在しない場合は即 fallback に移行する。"""
        self.pane_file.write_text("%42")
        job_id = self._make_terminal_job()

        with patch("lib.supervisor.subprocess.run") as mock_run:
            # list-panes: pane 不在
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            with patch.object(self.sup, "_claim_and_fallback") as mock_fb:
                self.sup._try_wake(self.db.get_job(job_id))

        mock_fb.assert_called_once()

    def test_try_wake_unknown_pane_state_no_send(self):
        """未知の画面は busy とみなし send-keys しない。"""
        self.pane_file.write_text("%42")
        job_id = self._make_terminal_job()

        def mock_subprocess(cmd, **kwargs):
            result = MagicMock()
            if "list-panes" in cmd:
                result.stdout = "%42\n"
            elif "display-message" in cmd:
                result.stdout = "0"  # not in copy-mode
            elif "capture-pane" in cmd:
                result.stdout = "A newly introduced confirmation UI\n"
            else:
                result.stdout = ""
            return result

        with patch("lib.supervisor.subprocess.run", side_effect=mock_subprocess):
            result = self.sup._try_wake(self.db.get_job(job_id))

        # send-keys は呼ばれず False を返す
        self.assertFalse(result)
        # wake_attempts は増えていない
        job = self.db.get_job(job_id)
        self.assertEqual(job.get("wake_attempts") or 0, 0)

    def test_try_wake_permission_prompt_no_send(self):
        """権限promptに文字列やEnterを注入しない。"""
        self.pane_file.write_text("%42")
        job_id = self._make_terminal_job()
        send_calls = []

        def mock_subprocess(cmd, **kwargs):
            result = MagicMock(stdout="")
            if "list-panes" in cmd:
                result.stdout = "%42\n"
            elif "display-message" in cmd:
                result.stdout = "0"
            elif "capture-pane" in cmd:
                result.stdout = "Do you want to proceed?\n  Yes\n  No\n"
            elif "send-keys" in cmd:
                send_calls.append(cmd)
            return result

        with patch("lib.supervisor.subprocess.run", side_effect=mock_subprocess):
            self.assertFalse(self.sup._try_wake(self.db.get_job(job_id)))
        self.assertEqual(send_calls, [])

    def test_auto_wake_disabled_by_default_uses_fallback_without_tmux(self):
        """既定設定は tmux を操作せず即fallbackする。"""
        job_id = self._make_terminal_job()
        safe_sup = Supervisor(self.db, pane_file=self.pane_file)
        with (
            patch("lib.supervisor.subprocess.run") as mock_run,
            patch.object(safe_sup, "_claim_and_fallback") as mock_fallback,
        ):
            safe_sup._drive_delivery(self.db.get_job(job_id))
        mock_run.assert_not_called()
        mock_fallback.assert_called_once()

    def test_try_wake_pane_idle_sends_message(self):
        """pane が idle の場合は send-keys で wake メッセージを送信する。"""
        self.pane_file.write_text("%42")
        job_id = self._make_terminal_job()

        send_calls = []

        def mock_subprocess(cmd, **kwargs):
            result = MagicMock()
            if "list-panes" in cmd:
                result.stdout = "%42\n"
            elif "display-message" in cmd:
                result.stdout = "0"  # not in copy-mode
            elif "capture-pane" in cmd:
                result.stdout = "claude> "  # no modal
            elif "send-keys" in cmd:
                send_calls.append(cmd)
                result.returncode = 0
            else:
                result.stdout = ""
            return result

        with patch("lib.supervisor.subprocess.run", side_effect=mock_subprocess):
            result = self.sup._try_wake(self.db.get_job(job_id))

        # 成功 → send-keys が 2 回呼ばれる（テキスト + Enter）
        self.assertTrue(result)
        self.assertEqual(len(send_calls), 2)

        # wake_attempts が 1 になっている
        job = self.db.get_job(job_id)
        self.assertEqual(job.get("wake_attempts") or 0, 1)
        self.assertEqual(job.get("wake_state"), "woken")


class TestSupervisorFallback(unittest.TestCase):
    """Supervisor フォールバック配送と CAS テスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.pane_file = Path(tempfile.mktemp(suffix=".pane"))
        self.sup = Supervisor(
            self.db,
            wake_grace_seconds=90.0,
            max_wake_attempts=2,
            pane_file=self.pane_file,
        )

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)
        self.pane_file.unlink(missing_ok=True)

    def _make_terminal_job(self, state="succeeded", notify_chat_id="ch1"):
        repo_id = self.db.create_repo("fb_test", "/tmp/fb_test")
        job_id = self.db.create_job(repo_id, "test task", notify_chat_id=notify_chat_id)
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, state, result="done")
        return job_id

    def test_fallback_sends_discord_on_grace_expiry(self):
        """grace 期間超過後に fallback が Discord に送信される。"""
        job_id = self._make_terminal_job(notify_chat_id="ch-fallback")

        # terminal_at を遠い過去に設定してgrace超過を再現
        self.db.conn.execute(
            "UPDATE jobs SET terminal_at = ? WHERE id = ?",
            (time.time() - 200, job_id),
        )
        self.db.conn.commit()

        with patch("lib.supervisor.notify_discord_result") as mock_notify:
            from lib.notifier import NotifyResult
            mock_notify.return_value = NotifyResult(success=True)
            self.sup._drive_delivery(self.db.get_job(job_id))

        # Discord に1回送信された
        mock_notify.assert_called_once()
        args = mock_notify.call_args[0]
        self.assertEqual(args[0], "ch-fallback")
        self.assertIn("⚠️", args[1])

        # reported_at が設定されている
        job = self.db.get_job(job_id)
        self.assertIsNotNone(job.get("reported_at"))

    def test_cas_prevents_double_send(self):
        """report-done と fallback が競合しても Discord 送信は 1 回だけ。"""
        job_id = self._make_terminal_job(notify_chat_id="ch-cas")

        # terminal_at を過去に設定
        self.db.conn.execute(
            "UPDATE jobs SET terminal_at = ? WHERE id = ?",
            (time.time() - 200, job_id),
        )
        self.db.conn.commit()

        # Controller が先に report-done を打つ
        self.db.mark_reported(job_id, time.time())

        with patch("lib.supervisor.notify_discord_result") as mock_notify:
            from lib.notifier import NotifyResult
            mock_notify.return_value = NotifyResult(success=True)
            self.sup._drive_delivery(self.db.get_job(job_id))

        # reported_at 設定済みなのでスキップ → Discord 送信なし
        mock_notify.assert_not_called()

    def test_fallback_no_chat_id_sets_undeliverable(self):
        """chat_id が設定されていない場合は undeliverable になる。"""
        job_id = self._make_terminal_job(notify_chat_id=None)

        # terminal_at を過去に設定して grace 超過
        self.db.conn.execute(
            "UPDATE jobs SET terminal_at = ? WHERE id = ?",
            (time.time() - 200, job_id),
        )
        self.db.conn.commit()

        with patch("lib.supervisor.notify_discord_result") as mock_notify:
            self.sup._drive_delivery(self.db.get_job(job_id))

        # Discord 送信なし
        mock_notify.assert_not_called()
        # undeliverable になっている
        job = self.db.get_job(job_id)
        self.assertEqual(job.get("wake_state"), "undeliverable")

    def test_fallback_temporary_failure_retries_next_tick(self):
        """一時失敗（network error）は undeliverable にならず次 tick でリトライする。"""
        job_id = self._make_terminal_job(notify_chat_id="ch-retry")

        # fallback を claim 済みにする
        self.db.claim_for_fallback(job_id)

        with patch("lib.supervisor.notify_discord_result") as mock_notify:
            from lib.notifier import NotifyResult
            # 一時失敗
            mock_notify.return_value = NotifyResult(success=False, retry=True, reason="network")
            self.sup._attempt_fallback_notify(self.db.get_job(job_id))

        # reported_at は設定されていない（次 tick でリトライ）
        job = self.db.get_job(job_id)
        self.assertIsNone(job.get("reported_at"))
        # undeliverable にもなっていない
        self.assertNotEqual(job.get("wake_state"), "undeliverable")

    def test_fallback_permanent_failure_sets_undeliverable(self):
        """恒久失敗（4xx/token なし）は undeliverable になる（無限リトライ防止）。"""
        job_id = self._make_terminal_job(notify_chat_id="ch-perm")

        # fallback を claim 済みにする
        self.db.claim_for_fallback(job_id)

        with patch("lib.supervisor.notify_discord_result") as mock_notify:
            from lib.notifier import NotifyResult
            mock_notify.return_value = NotifyResult(success=False, permanent=True, reason="HTTP 403")
            self.sup._attempt_fallback_notify(self.db.get_job(job_id))

        job = self.db.get_job(job_id)
        self.assertEqual(job.get("wake_state"), "undeliverable")

    def test_claim_for_fallback_is_exclusive(self):
        """claim_for_fallback は CAS。2回目は False を返す。"""
        repo_id = self.db.create_repo("cas_test", "/tmp/cas_test")
        job_id = self.db.create_job(repo_id, "cas task", notify_chat_id="123")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded", result="ok")

        first = self.db.claim_for_fallback(job_id)
        second = self.db.claim_for_fallback(job_id)

        self.assertTrue(first, "1回目は True（配送権獲得）")
        self.assertFalse(second, "2回目は False（既に claim 済み）")

    def test_mark_reported_cancels_fallback_claim(self):
        """report-done（mark_reported）が成功すると claim_for_fallback は False。"""
        repo_id = self.db.create_repo("rd_test", "/tmp/rd_test")
        job_id = self.db.create_job(repo_id, "rd task", notify_chat_id="123")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded", result="ok")

        # Controller が先に report-done
        ok = self.db.mark_reported(job_id, time.time())
        self.assertTrue(ok)

        # fallback は CAS で取れない
        claimed = self.db.claim_for_fallback(job_id)
        self.assertFalse(claimed, "Controller が先に報告済みなので fallback は発火しない")


class TestSupervisorWakeThenGraceFallback(unittest.TestCase):
    """wake 試行後 grace 期間内は fallback しない（二重 reply 防止の核心テスト）。

    デフォルト max_wake_attempts=1: wake は1回だけ送り、後は fallback に委ねる。
    理由: 再 wake は Controller の進行中ターン（collect→reply~30-60s）に
    別メッセージを送り込み、二重 reply を引き起こす。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.pane_file = Path(tempfile.mktemp(suffix=".pane"))
        self.pane_file.write_text("%99")

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)
        self.pane_file.unlink(missing_ok=True)

    def _make_terminal_job(self):
        repo_id = self.db.create_repo("wg", "/tmp/wg")
        job_id = self.db.create_job(repo_id, "task", notify_chat_id="ch")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded", result="done")
        return job_id

    def _make_idle_pane_subprocess(self):
        def mock_subprocess(cmd, **kwargs):
            result = MagicMock()
            if "list-panes" in cmd:
                result.stdout = "%99\n"
            elif "display-message" in cmd:
                result.stdout = "0"
            elif "capture-pane" in cmd:
                result.stdout = "claude> "  # idle
            elif "send-keys" in cmd:
                result.returncode = 0
            return result
        return mock_subprocess

    def test_single_wake_then_no_rewake_until_grace(self):
        """wake #1 の後、grace 未経過の ticks は wake も fallback もしないこと。

        これが「二重 reply なし」の保証の核心。
        Controller が collect→reply 中の ~45s 間に追加メッセージが届かない。
        """
        sup = Supervisor(
            self.db,
            wake_grace_seconds=90.0,
            max_wake_attempts=1,  # デフォルト。1回だけ wake。
            pane_file=self.pane_file,
            auto_wake=True,
        )
        job_id = self._make_terminal_job()

        with patch("lib.supervisor.subprocess.run", side_effect=self._make_idle_pane_subprocess()):
            with patch("lib.supervisor.notify_discord_result") as mock_notify:
                # tick 1: wake #1 (attempts → 1 = max_wake_attempts)
                sup._drive_delivery(self.db.get_job(job_id))
                self.assertEqual(self.db.get_job(job_id).get("wake_attempts", 0), 1)
                mock_notify.assert_not_called()

                # tick 2: attempts >= max → do nothing (grace not expired)
                sup._drive_delivery(self.db.get_job(job_id))
                mock_notify.assert_not_called()  # 二度目の wake も fallback も発火しない

                # tick 3: 同上
                sup._drive_delivery(self.db.get_job(job_id))
                mock_notify.assert_not_called()

                # wake_state は 'woken'（fallback でも undeliverable でもない）
                job = self.db.get_job(job_id)
                self.assertEqual(job.get("wake_state"), "woken")
                self.assertIsNone(job.get("reported_at"))

    def test_fallback_fires_only_after_grace(self):
        """pane で1回 wake した後、grace 超過後に fallback が1回だけ発火すること。"""
        sup = Supervisor(
            self.db,
            wake_grace_seconds=90.0,
            max_wake_attempts=1,
            pane_file=self.pane_file,
            auto_wake=True,
        )
        job_id = self._make_terminal_job()

        with patch("lib.supervisor.subprocess.run", side_effect=self._make_idle_pane_subprocess()):
            # wake #1
            sup._drive_delivery(self.db.get_job(job_id))
            self.assertEqual(self.db.get_job(job_id).get("wake_attempts", 0), 1)

        # terminal_at を grace 超過に設定
        self.db.conn.execute(
            "UPDATE jobs SET terminal_at = ? WHERE id = ?",
            (time.time() - 200, job_id),
        )
        self.db.conn.commit()

        fallback_calls = []
        with patch("lib.supervisor.notify_discord_result",
                   side_effect=lambda c, t: fallback_calls.append(c) or NotifyResult(success=True)):
            sup._drive_delivery(self.db.get_job(job_id))

        # grace 超過後に fallback が1回発火
        self.assertEqual(len(fallback_calls), 1, "grace 超過後に fallback が1回だけ発火すること")
        # 以降は reported_at 設定済みで二重発火しない
        sup._drive_delivery(self.db.get_job(job_id))
        self.assertEqual(len(fallback_calls), 1, "2回目の tick でも fallback は発火しない（CAS）")


class TestSupervisorTerminalAt(unittest.TestCase):
    """terminal_at が正しく設定・リセットされるかテスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_terminal_at_set_on_succeeded(self):
        """succeeded 遷移時に terminal_at が設定される。"""
        repo_id = self.db.create_repo("ta_test", "/tmp/ta_test")
        job_id = self.db.create_job(repo_id, "task")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")

        before = time.time()
        self.db.transition_job(job_id, "succeeded")
        after = time.time()

        job = self.db.get_job(job_id)
        self.assertIsNotNone(job.get("terminal_at"))
        self.assertGreaterEqual(job["terminal_at"], before)
        self.assertLessEqual(job["terminal_at"], after)

    def test_terminal_at_set_on_failed_permanent(self):
        """failed_permanent 遷移時に terminal_at が設定される。"""
        repo_id = self.db.create_repo("ta_fp", "/tmp/ta_fp")
        job_id = self.db.create_job(repo_id, "task")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "failed_permanent")

        job = self.db.get_job(job_id)
        self.assertIsNotNone(job.get("terminal_at"))

    def test_terminal_at_reset_on_requeue(self):
        """requeue（→ queued）時に terminal_at がリセットされる。"""
        repo_id = self.db.create_repo("ta_rq", "/tmp/ta_rq")
        job_id = self.db.create_job(repo_id, "task")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "timed_out")  # 中間状態
        # timed_out は DELIVERY_TERMINAL_STATES に含まれないので terminal_at はなし
        job = self.db.get_job(job_id)
        self.assertIsNone(job.get("terminal_at"))

        # requeue
        self.db.transition_job(job_id, "queued")
        job = self.db.get_job(job_id)
        self.assertIsNone(job.get("terminal_at"))
        self.assertIsNone(job.get("wake_state"))
        self.assertEqual(job.get("wake_attempts") or 0, 0)
        self.assertIsNone(job.get("reported_at"))


if __name__ == "__main__":
    unittest.main()
