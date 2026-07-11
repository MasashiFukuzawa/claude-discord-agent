"""監視プレーン: heartbeat監視、タイムアウト検出、復旧ポリシー。"""

from __future__ import annotations

import time
from typing import Any

from .db import Database
from .runner import Runner


class RecoveryAction:
    """復旧アクションの結果。"""

    def __init__(
        self,
        action: str,
        job_id: int,
        detail: str = "",
    ):
        self.action = (
            action  # "retry_same_session", "fork_session", "report_lost", "backoff", "escalate"
        )
        self.job_id = job_id
        self.detail = detail

    def __repr__(self) -> str:
        return f"RecoveryAction({self.action}, job={self.job_id}, {self.detail})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "job_id": self.job_id,
            "detail": self.detail,
        }


class Watchdog:
    """ワーカーのheartbeat監視とタイムアウト検出。"""

    def __init__(
        self,
        db: Database,
        runner: Runner,
        *,
        heartbeat_timeout: float = 600.0,  # 10分
        job_timeout: float = 3600.0,  # 60分
        check_interval: float = 30.0,  # 30秒ごとにチェック
    ):
        self._db = db
        self._runner = runner
        self.heartbeat_timeout = heartbeat_timeout
        self.job_timeout = job_timeout
        self.check_interval = check_interval

    def check_all(self) -> list[RecoveryAction]:
        """全アクティブジョブのヘルスチェック。必要な復旧アクションのリストを返す。"""
        actions: list[RecoveryAction] = []

        running_jobs = self._db.list_jobs(state="running")
        starting_jobs = self._db.list_jobs(state="starting")

        for job in running_jobs + starting_jobs:
            action = self._check_job(job)
            if action:
                actions.append(action)

        return actions

    def _check_job(self, job: dict[str, Any]) -> RecoveryAction | None:
        """個別ジョブのヘルスチェック。

        タイムアウト判定は attempt.started_at 基準（#4修正: キュー滞留時間を除外）。
        """
        job_id = job["id"]
        now = time.time()

        attempts = self._db.get_attempts(job_id)
        if not attempts:
            return None

        latest_attempt = attempts[-1]
        attempt_id = latest_attempt["id"]

        # attempt.started_at 基準でのタイムアウト判定
        attempt_elapsed = now - latest_attempt["started_at"]
        if attempt_elapsed > self.job_timeout:
            return self._handle_timeout(job)

        proc = self._runner.get_process(attempt_id)

        if proc is None:
            # プロセスが見つからない = lost
            if latest_attempt.get("finished_at") is None:
                return self._handle_lost(job, latest_attempt)
            return None

        if not proc.is_alive:
            return None

        # Heartbeat（最後のイベント時刻）チェック
        if proc.idle_seconds > self.heartbeat_timeout:
            return self._handle_timeout(job)

        return None

    def _handle_timeout(self, job: dict[str, Any]) -> RecoveryAction:
        """タイムアウト時の復旧判断。"""
        job_id = job["id"]
        attempts = self._db.get_attempts(job_id)
        attempt_count = len(attempts)

        # 同一セッションで1回目のタイムアウト → リトライ
        if attempt_count <= 1:
            try:
                # 状態遷移のみ。Discord 通知は Supervisor._tick() が担う（R-B: 二重送信防止）
                self._db.transition_job(job_id, "timed_out")
            except ValueError:
                pass
            return RecoveryAction(
                "retry_same_session",
                job_id,
                f"Timed out after {attempt_count} attempt(s), will retry on same session",
            )

        # 2回目以降 → 新セッションでfork
        try:
            self._db.transition_job(job_id, "timed_out")
        except ValueError:
            pass
        return RecoveryAction(
            "fork_session",
            job_id,
            f"Timed out after {attempt_count} attempt(s), will fork to new session",
        )

    def _handle_lost(self, job: dict[str, Any], attempt: dict[str, Any]) -> RecoveryAction:
        """Lost検出時の復旧判断。"""
        job_id = job["id"]
        try:
            # 状態遷移のみ。Discord 通知は Supervisor._tick() が担う（R-B: 二重送信防止）
            self._db.transition_job(job_id, "lost")
        except ValueError:
            pass
        return RecoveryAction(
            "report_lost",
            job_id,
            f"Process lost (attempt {attempt.get('attempt_num', '?')}), reporting to controller",
        )

    def _cleanup_running_process(self, job_id: int) -> None:
        """実行中プロセスの停止 → reader thread join → attempt完了 → session更新。

        #2修正: proc.stop()後にreader threadをjoinし、遅延on_complete()の
        暴走を防止する。joinが完了するまでsession解放しない。
        """
        attempts = self._db.get_attempts(job_id)
        if not attempts:
            return
        latest = attempts[-1]
        attempt_id = latest["id"]

        # プロセス停止 + reader thread join
        proc = self._runner.get_process(attempt_id)
        if proc is not None:
            proc.stop()
            # reader threadをjoinして完全に停止を待つ
            if proc._stdout_thread is not None:
                proc._stdout_thread.join(timeout=5.0)
            if proc._stderr_thread is not None:
                proc._stderr_thread.join(timeout=5.0)

        # attempt完了（on_completeが先に呼ばれていれば既にfinished_at設定済み）
        fresh = self._db.get_attempts(job_id)
        if fresh:
            latest = fresh[-1]
            if latest.get("finished_at") is None:
                self._db.finish_attempt(attempt_id, exit_code=-1, error="Stopped by watchdog")

        # session更新
        if latest.get("session_id") is not None:
            self._db.update_session(latest["session_id"], state="idle")

    def apply_recovery(self, action: RecoveryAction) -> dict[str, Any]:
        """復旧アクションを実行する。

        フルステップ: 子プロセスkill → attempt完了 → session解放 → requeue。
        #1修正: timed_out/lostでもcleanup実行（二重実行+スロットリーク防止）。
        """
        job = self._db.get_job(action.job_id)
        if job is None:
            return {"error": f"Job {action.job_id} not found"}

        # 子プロセスkill → attempt完了 → session解放
        # timed_out/lost状態でも実行する（_handle_timeoutが先に遷移するため）
        if job["state"] in ("running", "starting", "timed_out", "lost"):
            self._cleanup_running_process(action.job_id)

        if action.action == "retry_same_session":
            try:
                self._db.transition_job(action.job_id, "queued")
                return {"status": "requeued", "job_id": action.job_id}
            except ValueError as e:
                return {"error": str(e)}

        elif action.action == "fork_session":
            # #3修正: 旧sessionをdead化し、新sessionを作成
            old_session_id = self._mark_old_session_dead(action.job_id)
            new_session_id = self._db.create_session(job["repo_id"])
            try:
                self._db.transition_job(action.job_id, "queued")
                return {
                    "status": "requeued_for_new_session",
                    "job_id": action.job_id,
                    "old_session_id": old_session_id,
                    "new_session_id": new_session_id,
                }
            except ValueError as e:
                try:
                    # 状態遷移のみ。Discord 通知は Supervisor._tick() が担う（R-B: 二重送信防止）
                    self._db.transition_job(
                        action.job_id,
                        "failed_permanent",
                        result="Max retries exceeded after fork",
                    )
                except ValueError:
                    pass
                return {"status": "failed_permanent", "job_id": action.job_id, "reason": str(e)}

        elif action.action == "report_lost":
            try:
                self._db.transition_job(
                    action.job_id,
                    "needs_controller",
                    result="Process lost, needs manual intervention",
                )
            except ValueError:
                pass
            return {"status": "needs_controller", "job_id": action.job_id}

        elif action.action == "backoff":
            try:
                self._db.transition_job(action.job_id, "queued")
                return {"status": "requeued_with_backoff", "job_id": action.job_id}
            except ValueError as e:
                return {"error": str(e)}

        elif action.action == "escalate":
            try:
                # 状態遷移のみ。Discord 通知は Supervisor._tick() が担う（R-B: 二重送信防止）
                self._db.transition_job(
                    action.job_id,
                    "failed_permanent",
                    result="Escalated: " + action.detail,
                )
            except ValueError:
                pass
            return {"status": "escalated", "job_id": action.job_id}

        return {"error": f"Unknown action: {action.action}"}

    def _mark_old_session_dead(self, job_id: int) -> int | None:
        """ジョブの最新attemptに紐づくsessionをdead化する。"""
        attempts = self._db.get_attempts(job_id)
        if not attempts:
            return None
        latest = attempts[-1]
        session_id = latest.get("session_id")
        if session_id is not None:
            self._db.update_session(session_id, state="dead")
        return session_id

    def get_health_report(self) -> dict[str, Any]:
        """全体のヘルスレポートを生成。

        #4修正: DB状態のみで判断。Runner.active_processes()はデーモン内でしか
        有効でないため、CLI経由では使用しない。
        """
        sessions = self._db.list_sessions()

        healthy = 0
        stale = 0
        dead = 0

        for session in sessions:
            if session["state"] == "dead":
                dead += 1
            elif session["state"] == "idle":
                healthy += 1
            elif session["state"] == "busy":
                # DB上のheartbeat時刻で判断
                hb = session.get("last_heartbeat") or session["created_at"]
                if time.time() - hb > self.heartbeat_timeout:
                    stale += 1
                else:
                    healthy += 1

        # running/startingジョブ数はDB直接カウント
        running_jobs = self._db.count_running_jobs()

        return {
            "total_sessions": len(sessions),
            "healthy": healthy,
            "stale": stale,
            "dead": dead,
            "running_jobs": running_jobs,
            "timestamp": time.time(),
        }
