"""常駐デーモン: Runner/Watchdog/Schedulerを単一プロセスに統合。

- PIDファイルで二重起動防止
- メインループでジョブキュー駆動、Watchdog監視、リカバリ実行
- daemon_commandsテーブル経由でCLIからコマンド受信（kill等）
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from .db import DEFAULT_DB_DIR, Database
from .paths import ensure_private_dir, secure_file
from .runner import Runner
from .scheduler import BackoffTracker, Scheduler
from .supervisor import Supervisor
from .watchdog import Watchdog

logger = logging.getLogger("daemon")


def _state_dir_for_db(db: Database) -> Path:
    """DBパスからPID/logのディレクトリを決定する。

    #6修正: --dbで切り替えてもdaemon lifecycle fileが追従する。
    """
    return db._path.parent


def _pid_file_for(state_dir: Path) -> Path:
    return state_dir / "daemon.pid"


def _log_file_for(state_dir: Path) -> Path:
    return state_dir / "daemon.log"


def is_daemon_running(state_dir: Path | None = None) -> int | None:
    """デーモンが起動中ならPIDを返す。"""
    if state_dir is None:
        state_dir = DEFAULT_DB_DIR
    pf = _pid_file_for(state_dir)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)  # プロセス存在チェック (signal 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pf.unlink(missing_ok=True)
        return None


class Daemon:
    """Orchestratorデーモン。"""

    def __init__(
        self,
        db: Database,
        *,
        global_max_concurrency: int = 2,
        poll_interval: float = 5.0,
        watchdog_interval: float = 30.0,
        auto_wake: bool = False,
        fallback_result_preview: bool = False,
    ):
        self.db = db
        self.poll_interval = poll_interval
        self.watchdog_interval = watchdog_interval
        self._running = False
        self._state_dir = _state_dir_for_db(db)

        self.backoff = BackoffTracker(db)
        self.scheduler = Scheduler(
            db,
            global_max_concurrency=global_max_concurrency,
            backoff=self.backoff,
        )
        # #2修正: RunnerにSchedulerを渡してon_completeからbackoff更新
        self.runner = Runner(db, scheduler=self.scheduler)
        self.watchdog = Watchdog(db, self.runner)
        # Supervisor: direct fallback by default; tmux wake is explicit opt-in.
        self.supervisor = Supervisor(
            db,
            auto_wake=auto_wake,
            fallback_result_preview=fallback_result_preview,
        )

        self._last_watchdog_check = 0.0

    def _write_pid(self) -> None:
        ensure_private_dir(self._state_dir, tighten_existing=self.db._secure_parent)
        pid_file = _pid_file_for(self._state_dir)
        pid_file.write_text(str(os.getpid()))
        secure_file(pid_file)

    def _remove_pid(self) -> None:
        _pid_file_for(self._state_dir).unlink(missing_ok=True)

    def _setup_signals(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        logger.info("Signal %d received, shutting down...", signum)
        self._running = False

    def start(self, *, foreground: bool = False) -> int:
        """デーモンを起動する。

        foreground=True: フォアグラウンドで実行（テスト・デバッグ用）
        foreground=False: バックグラウンドでfork
        """
        existing = is_daemon_running(self._state_dir)
        if existing:
            print(f"Daemon already running (PID {existing})", file=sys.stderr)
            return 1

        if foreground:
            return self._run()

        # バックグラウンドfork
        pid = os.fork()
        if pid > 0:
            print(f"Daemon started (PID {pid})")
            return 0

        # 子プロセス
        os.setsid()
        log_path = _log_file_for(self._state_dir)
        sys.stdin = open(os.devnull)
        sys.stdout = open(log_path, "a")
        sys.stderr = sys.stdout
        return self._run()

    def _run(self) -> int:
        """メインループ。"""
        self._write_pid()
        self._setup_signals()
        self._running = True
        logger.info("Daemon started (PID %d)", os.getpid())

        try:
            while self._running:
                self._tick()
                time.sleep(self.poll_interval)
        except Exception:
            logger.exception("Daemon crashed")
            return 1
        finally:
            logger.info("Daemon stopping, cleaning up...")
            self.runner.stop_all()
            self._remove_pid()
            logger.info("Daemon stopped")

        return 0

    def _tick(self) -> None:
        """1サイクルの処理。"""
        # 1. コマンド処理
        self._process_commands()

        # 2. リカバリ駆動（failed_retryable, rate_limited → requeue）
        self.scheduler.drive_recovery()

        # 3. キューからジョブを起動
        self._dispatch_queued_jobs()

        # 4. Watchdogチェック（間引き）
        now = time.time()
        if now - self._last_watchdog_check >= self.watchdog_interval:
            self._run_watchdog()
            self._last_watchdog_check = now

        # 5. Supervisor: 終局保証配送（Controller wake + fallback push）
        self.supervisor.tick()

    def _dispatch_queued_jobs(self) -> None:
        """キュー内のジョブを実行可能なら起動。"""
        while True:
            job = self.scheduler.next_job()
            if job is None:
                break

            repo_id = job["repo_id"]
            # セッション取得または作成
            sessions = self.db.list_sessions(repo_id=repo_id, state="idle")
            if not sessions:
                session_id = self.db.create_session(repo_id)
            else:
                session_id = sessions[0]["id"]

            repo_row = self.db.conn.execute(
                "SELECT model FROM repos WHERE id = ?", (repo_id,)
            ).fetchone()
            model = repo_row[0] if repo_row else "sonnet"

            try:
                attempt_id = self.runner.run_job(job["id"], session_id, model=model)
                logger.info(
                    "Started job %d (attempt %d, session %d)", job["id"], attempt_id, session_id
                )
            except Exception:
                logger.exception("Failed to start job %d", job["id"])

    def _run_watchdog(self) -> None:
        """Watchdogヘルスチェック + リカバリ適用。"""
        actions = self.watchdog.check_all()
        for action in actions:
            logger.info("Watchdog recovery: %s", action)
            result = self.watchdog.apply_recovery(action)
            logger.info("Recovery result: %s", result)

    def _process_commands(self) -> None:
        """daemon_commandsテーブルからコマンドを読み取り実行。"""
        commands = self.db.poll_commands()
        for cmd in commands:
            command = cmd["command"]
            payload = cmd.get("payload") or {}
            logger.info("Processing command: %s %s", command, payload)

            if command == "kill_job":
                self._cmd_kill_job(payload.get("job_id"))
            elif command == "kill_repo":
                self._cmd_kill_repo(payload.get("repo_id"))
            elif command == "shutdown":
                self._running = False
            else:
                logger.warning("Unknown command: %s", command)

    def _cmd_kill_job(self, job_id: int | None) -> None:
        if job_id is None:
            return
        attempts = self.db.get_attempts(job_id)
        for attempt in reversed(attempts):
            if self.runner.stop_job(attempt["id"]):
                break
        try:
            # 状態遷移のみ。Discord 通知は Supervisor._tick() が担う（R-B: 二重送信防止）
            self.db.transition_job(job_id, "failed_permanent", result="Killed by user")
        except ValueError:
            pass

    def _cmd_kill_repo(self, repo_id: int | None) -> None:
        if repo_id is None:
            return
        jobs = self.db.list_jobs(repo_id=repo_id)
        for job in jobs:
            if job["state"] in ("queued", "starting", "running"):
                self._cmd_kill_job(job["id"])

    def stop(self) -> int:
        """デーモンを停止。"""
        pid = is_daemon_running(self._state_dir)
        if pid is None:
            print("Daemon is not running")
            return 1
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not is_daemon_running(self._state_dir):
                print("Daemon stopped")
                return 0
            time.sleep(0.5)
        print(f"Daemon (PID {pid}) did not stop in time", file=sys.stderr)
        return 1
