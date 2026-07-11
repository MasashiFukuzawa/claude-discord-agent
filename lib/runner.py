"""実行プレーン: claude -p --output-format stream-json をsubprocessで管理。

stderrも別スレッドで読み取り、rate limit検出に使用。
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Scheduler型は循環importを避けるためTYPE_CHECKING内でのみimport
from typing import TYPE_CHECKING, Any

from .db import Database

if TYPE_CHECKING:
    from .scheduler import Scheduler

logger = logging.getLogger("runner")

# worker-system-prompt.md のデフォルトパス
_DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "worker-system-prompt.md"


def _load_system_prompt(path: Path | None = None) -> str | None:
    """worker-system-prompt.md を読み込む。失敗時は None を返し warning ログ。"""
    target = path or _DEFAULT_SYSTEM_PROMPT_PATH
    try:
        return target.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not load system prompt from %s: %s", target, e)
        return None


# rate limit検出パターン（stderr/stdout両方で検査）
RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "overloaded",
]


def _is_rate_limit_error(text: str) -> bool:
    """テキストにrate limit関連のエラーが含まれるか。"""
    lower = text.lower()
    return any(pat in lower for pat in RATE_LIMIT_PATTERNS)


class RunnerError(Exception):
    """Runner操作のエラー。"""


class ClaudeProcess:
    """単一のclaude -pプロセスを管理する。"""

    def __init__(
        self,
        *,
        task: str,
        working_dir: str,
        model: str = "sonnet",
        system_prompt: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_complete: Callable[[int, str | None, bool], None] | None = None,
    ):
        self.task = task
        self.working_dir = working_dir
        self.model = model
        self.system_prompt = system_prompt
        self.on_event = on_event
        # on_complete(exit_code, result_text, is_rate_limited)
        self.on_complete = on_complete

        self.process: subprocess.Popen[str] | None = None
        self.pid: int | None = None
        self.last_event_time: float = 0.0
        self.started_at: float = 0.0
        self.finished_at: float | None = None
        self.exit_code: int | None = None
        self.output_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self.result_text: str = ""
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> int:
        """claudeプロセスを起動。PIDを返す。"""
        cmd = [
            "claude",
            "-p",
            self.task,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
        ]
        # worker-system-prompt を配送（R4 構造化結果契約を Worker に届ける）
        if self.system_prompt:
            cmd += ["--append-system-prompt", self.system_prompt]
        self.started_at = time.time()
        self.last_event_time = self.started_at

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.working_dir,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RunnerError("claude CLI not found. Is it installed and in PATH?") from exc

        self.pid = self.process.pid

        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

        return self.pid

    def _read_stdout(self) -> None:
        """stream-json stdoutを読み取り、イベントをディスパッチ。"""
        assert self.process and self.process.stdout

        for line in self.process.stdout:
            if self._stop_event.is_set():
                break
            line = line.strip()
            if not line:
                continue
            self.output_lines.append(line)
            self.last_event_time = time.time()

            try:
                event = json.loads(line)
                if self.on_event:
                    self.on_event(event)
                if event.get("type") == "result":
                    self.result_text = event.get("result", "")
            except json.JSONDecodeError:
                pass

        self.exit_code = self.process.wait()
        self.finished_at = time.time()

        # #2修正: stderrスレッドをjoinしてから判定（race防止）
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5.0)

        if not self.result_text:
            self._extract_result()

        is_rate_limited = self._check_rate_limit()

        if self.on_complete:
            self.on_complete(self.exit_code, self.result_text or None, is_rate_limited)

    def _read_stderr(self) -> None:
        """stderrを読み取って蓄積。"""
        assert self.process and self.process.stderr

        for line in self.process.stderr:
            if self._stop_event.is_set():
                break
            line = line.strip()
            if line:
                self.stderr_lines.append(line)

    def _check_rate_limit(self) -> bool:
        """stderr/stdoutからrate limitを検出。"""
        for line in self.stderr_lines:
            if _is_rate_limit_error(line):
                return True
        return any(_is_rate_limit_error(line) for line in self.output_lines[-10:])

    def _extract_result(self) -> None:
        """出力からresultを抽出する。"""
        for line in reversed(self.output_lines):
            try:
                event = json.loads(line)
                if event.get("type") == "result":
                    self.result_text = event.get("result", "")
                    return
            except (json.JSONDecodeError, TypeError):
                continue

    def stop(self, timeout: float = 10.0) -> None:
        """プロセスを停止。"""
        self._stop_event.set()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.exit_code = self.process.returncode
            self.finished_at = time.time()

    @property
    def is_alive(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None

    @property
    def elapsed(self) -> float:
        if self.started_at == 0:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def idle_seconds(self) -> float:
        """最後のイベントからの経過秒数。"""
        if self.last_event_time == 0:
            return 0.0
        return time.time() - self.last_event_time


class Runner:
    """複数のClaudeProcessを管理する実行プレーン。

    デーモンプロセス内で動作し、全子プロセスの生存期間を管理する。
    schedulerが設定されている場合、on_complete時にbackoff更新を行う。
    """

    def __init__(self, db: Database, scheduler: Scheduler | None = None):
        self._db = db
        self._scheduler = scheduler
        self._processes: dict[int, ClaudeProcess] = {}  # attempt_id -> process
        self._lock = threading.Lock()

    def run_job(
        self,
        job_id: int,
        session_id: int,
        *,
        model: str = "sonnet",
    ) -> int:
        """ジョブを実行する。attempt_idを返す。"""
        job = self._db.get_job(job_id)
        if job is None:
            raise RunnerError(f"Job {job_id} not found")

        repo_row = self._db.conn.execute(
            "SELECT * FROM repos WHERE id = ?", (job["repo_id"],)
        ).fetchone()
        if not repo_row:
            raise RunnerError(f"Repo {job['repo_id']} not found")
        repo = dict(repo_row)

        # working_dirは常にRegistry経由（#6: セキュリティ修正）
        working_dir = repo["path"]
        attempt_id = self._db.create_attempt(job_id, session_id)

        self._db.transition_job(job_id, "starting")

        def on_event(event: dict[str, Any]) -> None:
            event_type = event.get("type", "unknown")
            try:
                self._db.add_event(
                    attempt_id,
                    event_type,
                    json.dumps(event, ensure_ascii=False),
                )
            except Exception:
                pass  # イベント記録失敗はノンブロッキング

        def on_complete(exit_code: int, result: str | None, is_rate_limited: bool) -> None:
            # stale attempt guard: job + session両方でチェック
            current_attempts = self._db.get_attempts(job_id)
            if current_attempts and current_attempts[-1]["id"] != attempt_id:
                with self._lock:
                    self._processes.pop(attempt_id, None)
                return

            error_msg = None if exit_code == 0 else (result or "Unknown error")
            self._db.finish_attempt(attempt_id, exit_code=exit_code, error=error_msg)

            # R4-#2修正: CASでsession更新（current_attempt_idが一致する場合のみ）
            self._db.cas_update_session(
                session_id,
                attempt_id,
                state="idle",
                last_heartbeat=time.time(),
            )
            with self._lock:
                self._processes.pop(attempt_id, None)

            # 状態確認してから遷移（高速終了/watchdog介入でraceしても安全）
            current_job = self._db.get_job(job_id)
            if current_job is None or current_job["state"] not in ("starting", "running"):
                return

            # Scheduler経由でbackoff更新
            if exit_code == 0:
                if self._scheduler:
                    self._scheduler.on_success(job["repo_id"], job_id, result=result)
                else:
                    self._db.transition_job(job_id, "succeeded", result=result)
            elif is_rate_limited:
                if self._scheduler:
                    self._scheduler.on_rate_limit(job["repo_id"], job_id)
                else:
                    self._db.transition_job(job_id, "rate_limited")
            elif exit_code in (143, -15):
                # SIGTERM: watchdog/user kill — no auto-retry
                # 状態遷移のみ。Discord 通知は Supervisor._tick() が担う（R-B: 二重送信防止）
                try:
                    self._db.transition_job(
                        job_id, "failed_permanent", result="Killed by SIGTERM (no retry)"
                    )
                except ValueError:
                    pass
            else:
                self._db.transition_job(job_id, "failed_retryable")

        system_prompt = _load_system_prompt()
        proc = ClaudeProcess(
            task=job["task"],
            working_dir=working_dir,
            model=model,
            system_prompt=system_prompt,
            on_event=on_event,
            on_complete=on_complete,
        )

        # session更新（current_attempt_id設定）+ job遷移をstart()の前に行う
        self._db.update_session(
            session_id, state="busy", last_heartbeat=time.time(), current_attempt_id=attempt_id
        )
        self._db.transition_job(job_id, "running")

        try:
            pid = proc.start()
            self._db.update_session(session_id, pid=pid)
            with self._lock:
                self._processes[attempt_id] = proc
            return attempt_id
        except RunnerError:
            self._db.finish_attempt(
                attempt_id, exit_code=-1, error="Failed to start claude process"
            )
            self._db.update_session(session_id, state="idle")
            self._db.transition_job(job_id, "failed_retryable")
            raise

    def get_process(self, attempt_id: int) -> ClaudeProcess | None:
        with self._lock:
            return self._processes.get(attempt_id)

    def stop_job(self, attempt_id: int) -> bool:
        """実行中のジョブを停止。"""
        with self._lock:
            proc = self._processes.get(attempt_id)
        if proc is None:
            return False
        proc.stop()
        return True

    def active_processes(self) -> dict[int, ClaudeProcess]:
        """実行中のプロセス一覧。"""
        with self._lock:
            return {aid: p for aid, p in self._processes.items() if p.is_alive}

    def stop_all(self) -> int:
        """全プロセスを停止。停止数を返す。"""
        with self._lock:
            aids = list(self._processes.keys())
        count = 0
        for aid in aids:
            if self.stop_job(aid):
                count += 1
        return count
