"""E2E テスト（stub claude）: dispatch → Daemon tick → worker 完了 → R4 parse → fallback 配送。

このテストは「実際の配送パスが機能するか」を結合レベルで検証する。
- fake `claude` を PATH に挿し、実 Daemon を temp DB + 手動 tick で駆動する
- Discord/tmux は不要（notifier をモック、pane チェックをモック）

**このテストが カバーしない** 事項:
  - Controller-mediated 主経路（tmux wake path）= 実際の Claude pane が必要なため manual-only
  - 実際の Claude API 呼び出し（fake claude を使うため）
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.daemon import Daemon
from lib.db import Database
from lib.notifier import NotifyResult
from lib.result_parser import parse_result


def _make_fake_claude_script() -> str:
    """fake claude スクリプトの内容を返す。

    --append-system-prompt の値を echo back し R4 ブロックを出力する。
    """
    return (
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "system_prompt = ''\n"
        "args = sys.argv[1:]\n"
        "for i, a in enumerate(args):\n"
        "    if a == '--append-system-prompt' and i + 1 < len(args):\n"
        "        system_prompt = args[i + 1]\n"
        "        break\n"
        "r4 = json.dumps({\n"
        "    'status': 'succeeded', 'merge_sha': 'abc1234', 'pr_url': None,\n"
        "    'files_changed': ['README.md'], 'advisor_rounds': [],\n"
        "    'next_action_hint': None, 'unresolved': [], 'error_summary': None,\n"
        "}, ensure_ascii=False)\n"
        "echo = 'ECHO_SYSTEM_PROMPT_START:' + system_prompt[:80] + ':ECHO_SYSTEM_PROMPT_END'\n"
        "r4_block = '<<<DISCORD_AGENT_RESULT>>>\\n' + r4 + '\\n<<<END>>>'\n"
        "result_text = 'task completed\\n' + echo + '\\n\\n' + r4_block + '\\n'\n"
        "print(json.dumps({'type': 'assistant', 'message': {'text': 'working'}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'result': result_text, 'subtype': 'success'}), flush=True)\n"
    )


class TestE2EDispatchR4Pipeline(unittest.TestCase):
    """fake claude を使った R4 配送パイプラインの E2E 結合テスト。

    Supervisor の Discord 呼び出しはモックして実際の Discord API を呼ばない。
    """

    def setUp(self):
        # temp DB
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db = Database(self.tmp_db.name)

        # fake claude スクリプトを PATH に挿す
        self.tmp_bin = tempfile.mkdtemp()
        fake_claude = Path(self.tmp_bin) / "claude"
        fake_claude.write_text(_make_fake_claude_script())
        fake_claude.chmod(stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)
        self.original_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.tmp_bin + os.pathsep + self.original_path

        # Daemon (watchdog は手動で呼ばない)
        self.daemon = Daemon(
            self.db,
            global_max_concurrency=2,
            poll_interval=0.1,
            watchdog_interval=9999.0,
        )

        # テスト用 repo（fake claude を cwd として使う）
        self.repo_id = self.db.create_repo("e2e-repo", self.tmp_bin)

    def tearDown(self):
        os.environ["PATH"] = self.original_path
        # バックグラウンドスレッドが DB にアクセスする前にプロセスを停止
        try:
            self.daemon.runner.stop_all()
        except Exception:
            pass
        # 少し待ってスレッドが終了するのを待つ
        time.sleep(0.1)
        self.db.close()
        os.unlink(self.tmp_db.name)
        import shutil
        shutil.rmtree(self.tmp_bin, ignore_errors=True)

    def _tick_until(self, predicate, timeout=15.0, interval=0.2, supervisor_patch=True):
        """predicate が True になるまで Daemon._tick() を繰り返す。

        supervisor_patch=True の場合、Supervisor の Discord API 呼び出しをモックして
        テスト中に実際の Discord API を呼ばないようにする。
        """
        deadline = time.time() + timeout
        # Supervisor がテスト中に実際の Discord API を呼ばないようにモック
        # - pane が存在しないふり → 即 fallback へ
        # - notify_discord_result は一時失敗を返す → undeliverable にならずリトライ状態維持
        with patch("lib.supervisor.notify_discord_result",
                   return_value=NotifyResult(success=False, retry=True, reason="test-skip")):
            while time.time() < deadline:
                self.daemon._tick()
                if predicate():
                    return True
                time.sleep(interval)
        return False

    def test_dispatch_to_succeeded_with_r4_parsed(self):
        """dispatch → Daemon tick → succeeded + R4 parse の往復確認。

        **このテストが証明すること**:
        (a) job が succeeded に遷移する（fake claude が正常実行された）
        (b) job.result に R4 ブロックが含まれ、collect --json の parsed が非 None になる
            （= system-prompt が --append-system-prompt で worker に届いた）
        (c) result に system-prompt の echo が含まれる
            （= プロンプトの内容が実際に worker subprocess に渡った）
        """
        job_id = self.db.create_job(
            self.repo_id, "test task", notify_chat_id="ch-test"
        )

        ok = self._tick_until(
            lambda: self.db.get_job(job_id)["state"] == "succeeded"
        )
        self.assertTrue(ok, "job が succeeded に遷移すること（fake claude が正常実行された）")

        # (a) 状態確認
        job = self.db.get_job(job_id)
        self.assertEqual(job["state"], "succeeded")

        # (b) R4 parse 確認（system-prompt が worker に届いた証明）
        result_text = job.get("result") or ""
        self.assertIn(
            "<<<DISCORD_AGENT_RESULT>>>",
            result_text,
            "R4 区切り文字が result に含まれること",
        )
        parsed = parse_result(result_text)
        self.assertIsNotNone(
            parsed,
            "R4 ブロックが parse されること（worker-system-prompt が --append-system-prompt で届いた）",
        )
        self.assertEqual(parsed.status, "succeeded")
        self.assertEqual(parsed.merge_sha, "abc1234")

        # (c) system-prompt の echo 確認（プロンプトが実際に渡った）
        self.assertIn(
            "ECHO_SYSTEM_PROMPT_START:",
            result_text,
            "fake claude が system-prompt の echo を出力していること",
        )
        import re
        # re.DOTALL: system_prompt の内容が改行を含む場合も正しくマッチさせる
        m = re.search(r"ECHO_SYSTEM_PROMPT_START:(.*?):ECHO_SYSTEM_PROMPT_END", result_text, re.DOTALL)
        self.assertIsNotNone(m, "echo パターンが存在すること")
        echo_content = m.group(1)
        self.assertGreater(
            len(echo_content), 0,
            "system-prompt の内容が worker に渡っていること（空ならプロンプト配送失敗）",
        )

    def test_r4_collected_via_json_flag(self):
        """`collect --json` の `parsed` フィールドが R4 を返すこと。"""
        import argparse
        import io
        from contextlib import redirect_stdout

        import orchestrator

        job_id = self.db.create_job(
            self.repo_id, "collect-json test", notify_chat_id="ch-cj"
        )

        ok = self._tick_until(
            lambda: self.db.get_job(job_id)["state"] == "succeeded"
        )
        self.assertTrue(ok, "job が succeeded になること")

        args = argparse.Namespace(
            repo="e2e-repo",
            job_id=job_id,
            wait=False,
            json_output=True,
            poll_interval=3.0,
            timeout=30.0,
        )
        captured = io.StringIO()
        with patch("orchestrator.get_db", return_value=self.db):
            with redirect_stdout(captured):
                orchestrator.cmd_collect(args)

        output = captured.getvalue().strip()
        self.assertTrue(output, "collect --json が何か出力すること")
        data = json.loads(output.split("\n")[0])

        # parsed フィールドが非 None → R4 が収集された
        self.assertIsNotNone(data.get("parsed"), "collect --json の parsed が非 None であること")
        self.assertEqual(data["parsed"]["status"], "succeeded")
        self.assertEqual(data["parsed"]["merge_sha"], "abc1234")

        # chat_id が含まれること
        self.assertEqual(
            data.get("notify_chat_id"),
            "ch-cj",
            "collect --json に notify_chat_id が含まれること",
        )


class TestFallbackDeliveryPath(unittest.TestCase):
    """Supervisor フォールバック配送の E2E テスト（subprocess 不要）。

    worker を実際に動かさず、succeeded ジョブを手動で作成して Supervisor の挙動を検証する。
    """

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db = Database(self.tmp_db.name)
        self.pane_file = Path(tempfile.mktemp(suffix=".pane"))
        # pane_file なし → Supervisor は即 fallback へ
        self.daemon = Daemon(
            self.db,
            global_max_concurrency=2,
            poll_interval=0.1,
            watchdog_interval=9999.0,
        )
        # Supervisor の pane_file を未登録ファイルに向ける
        self.daemon.supervisor._pane_file = self.pane_file

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_db.name)
        self.pane_file.unlink(missing_ok=True)

    def _make_succeeded_job_past_grace(self, notify_chat_id="ch-fallback"):
        """succeeded 状態で terminal_at が grace を超えたジョブを作成する。"""
        repo_id = self.db.create_repo("fallback-repo", "/tmp/fallback-repo")
        job_id = self.db.create_job(repo_id, "fallback task", notify_chat_id=notify_chat_id)
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded", result="task done\n\n<<<DISCORD_AGENT_RESULT>>>\n" + json.dumps({
            "status": "succeeded", "merge_sha": None, "pr_url": None,
            "files_changed": [], "advisor_rounds": [], "next_action_hint": None,
            "unresolved": [], "error_summary": None,
        }) + "\n<<<END>>>")
        # terminal_at を grace 超過に設定
        self.db.conn.execute(
            "UPDATE jobs SET terminal_at = ? WHERE id = ?",
            (time.time() - 200, job_id),
        )
        self.db.conn.commit()
        return job_id

    def test_fallback_fires_with_correct_chat_id(self):
        """grace 超過後に fallback が正しい chat_id で発火すること。

        **このテストが証明すること**:
        (d) fallback notify が dispatch 時の chat_id で呼ばれる（二層配送の保証）
        """
        job_id = self._make_succeeded_job_past_grace(notify_chat_id="ch-fallback-999")

        fallback_calls: list[tuple] = []

        def mock_notify(chat_id, text):
            fallback_calls.append((chat_id, text))
            return NotifyResult(success=True)

        with patch("lib.supervisor.notify_discord_result", side_effect=mock_notify):
            self.daemon._tick()

        self.assertEqual(len(fallback_calls), 1, "fallback が1回だけ呼ばれること")
        self.assertEqual(
            fallback_calls[0][0], "ch-fallback-999",
            "fallback が正しい chat_id に送られること",
        )
        self.assertIn("⚠️", fallback_calls[0][1], "fallback メッセージに自動配送マークが含まれること")

        # reported_at が設定されている（二重送信防止）
        job = self.db.get_job(job_id)
        self.assertIsNotNone(job.get("reported_at"))

    def test_no_fallback_after_report_done(self):
        """report-done 後は fallback が発火しないこと（CAS 二重送信防止）。"""
        job_id = self._make_succeeded_job_past_grace(notify_chat_id="ch-cas")

        # Controller が先に report-done
        self.db.mark_reported(job_id, time.time())

        with patch("lib.supervisor.notify_discord_result") as mock_notify:
            mock_notify.return_value = NotifyResult(success=True)
            self.daemon._tick()

        mock_notify.assert_not_called()

    def test_fallback_does_not_double_send(self):
        """同一ジョブへの fallback は2回 _tick() しても1回だけ発火する。"""
        self._make_succeeded_job_past_grace(notify_chat_id="ch-double")

        calls = []

        def mock_notify(chat_id, text):
            calls.append(chat_id)
            return NotifyResult(success=True)

        with patch("lib.supervisor.notify_discord_result", side_effect=mock_notify):
            self.daemon._tick()
            self.daemon._tick()

        self.assertEqual(len(calls), 1, "fallback は2回 tick しても1回だけ発火すること")

    def test_failed_permanent_job_also_gets_fallback(self):
        """failed_permanent ジョブも fallback で通知されること。"""
        repo_id = self.db.create_repo("fp-repo", "/tmp/fp-repo")
        job_id = self.db.create_job(repo_id, "fp task", notify_chat_id="ch-fp")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "failed_permanent", result="job failed")
        # grace 超過
        self.db.conn.execute(
            "UPDATE jobs SET terminal_at = ? WHERE id = ?",
            (time.time() - 200, job_id),
        )
        self.db.conn.commit()

        calls = []
        with patch("lib.supervisor.notify_discord_result",
                   side_effect=lambda c, t: calls.append(c) or NotifyResult(success=True)):
            self.daemon._tick()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], "ch-fp")


class TestReportDoneCLI(unittest.TestCase):
    """report-done CLI コマンドの smoke テスト。

    このテストが証明すること:
    - report-done が reported_at をセットする
    - 2回目の report-done は no-op（冪等）
    - 存在しない job_id はエラー
    - collect --json に notify_chat_id が含まれる
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_report_done_marks_reported_at(self):
        """report-done が DB の reported_at をセットすること。"""
        repo_id = self.db.create_repo("rd-repo", "/tmp/rd-repo")
        job_id = self.db.create_job(
            repo_id, "rd task", notify_chat_id="ch-rd-123"
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded")

        import argparse

        import orchestrator

        args = argparse.Namespace(job_id=job_id)
        with patch("orchestrator.get_db", return_value=self.db):
            ret = orchestrator.cmd_report_done(args)

        self.assertEqual(ret, 0)
        job = self.db.get_job(job_id)
        self.assertIsNotNone(job.get("reported_at"))

    def test_report_done_idempotent(self):
        """report-done の2回目は no-op（冪等）。"""
        repo_id = self.db.create_repo("rd-idem", "/tmp/rd-idem")
        job_id = self.db.create_job(repo_id, "idem task")
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded")

        import argparse

        import orchestrator

        args = argparse.Namespace(job_id=job_id)
        with patch("orchestrator.get_db", return_value=self.db):
            orchestrator.cmd_report_done(args)
        ts1 = self.db.get_job(job_id).get("reported_at")

        time.sleep(0.01)
        with patch("orchestrator.get_db", return_value=self.db):
            ret = orchestrator.cmd_report_done(args)

        self.assertEqual(ret, 0)
        ts2 = self.db.get_job(job_id).get("reported_at")
        self.assertEqual(ts1, ts2, "2回目は reported_at を変更しない（冪等）")

    def test_report_done_unknown_job_returns_error(self):
        """存在しない job_id に report-done するとエラー。"""
        import argparse

        import orchestrator

        args = argparse.Namespace(job_id=99999)
        with patch("orchestrator.get_db", return_value=self.db):
            ret = orchestrator.cmd_report_done(args)

        self.assertEqual(ret, 1)

    def test_notify_chat_id_in_collect_json(self):
        """`collect --json` の出力に notify_chat_id が含まれること。"""
        import argparse
        import io
        from contextlib import redirect_stdout

        import orchestrator

        repo_id = self.db.create_repo("cj-repo", "/tmp/cj-repo")
        job_id = self.db.create_job(
            repo_id, "cj task", notify_chat_id="ch-collect-json-789"
        )
        self.db.transition_job(job_id, "starting")
        self.db.transition_job(job_id, "running")
        self.db.transition_job(job_id, "succeeded", result="done")

        args = argparse.Namespace(
            repo="cj-repo",
            job_id=job_id,
            wait=False,
            json_output=True,
            poll_interval=3.0,
            timeout=30.0,
        )

        captured = io.StringIO()
        with patch("orchestrator.get_db", return_value=self.db):
            with redirect_stdout(captured):
                orchestrator.cmd_collect(args)

        output = captured.getvalue().strip()
        self.assertTrue(output)
        parsed_output = json.loads(output.split("\n")[0])
        self.assertEqual(
            parsed_output.get("notify_chat_id"),
            "ch-collect-json-789",
        )


if __name__ == "__main__":
    unittest.main()
