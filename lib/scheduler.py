"""Scheduler: 同時実行制御、rate limitバックオフ（DB永続化）、ジョブキュー管理。"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from .db import VALID_TRANSITIONS, Database

logger = logging.getLogger("scheduler")

MAX_CHAIN_DEPTH = 50


class BackoffTracker:
    """Rate limit用の指数バックオフ + jitter。DB永続化対応。"""

    def __init__(
        self,
        db: Database | None = None,
        *,
        base_delay: float = 30.0,
        max_delay: float = 300.0,
        jitter: float = 0.3,
    ):
        self._db = db
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        # メモリキャッシュ（DBなしでも動作するため）
        self._cache: dict[int, tuple[int, float]] = {}

    def _load(self, repo_id: int) -> tuple[int, float]:
        """DBから読み込み、キャッシュに格納。"""
        if self._db is not None:
            until, count = self._db.get_backoff(repo_id)
            self._cache[repo_id] = (count, until)
            return (count, until)
        return self._cache.get(repo_id, (0, 0.0))

    def _save(self, repo_id: int, count: int, until: float) -> None:
        """DBに永続化。"""
        self._cache[repo_id] = (count, until)
        if self._db is not None:
            self._db.set_backoff(repo_id, until, count)

    def record_rate_limit(self, repo_id: int) -> float:
        """Rate limitを記録し、次回利用可能時刻を返す。"""
        count, _ = self._load(repo_id)
        count += 1
        delay = min(self.base_delay * (2 ** (count - 1)), self.max_delay)
        jitter_amount = delay * self.jitter * (2 * random.random() - 1)
        actual_delay = delay + jitter_amount
        next_available = time.time() + actual_delay
        self._save(repo_id, count, next_available)
        return next_available

    def is_backed_off(self, repo_id: int) -> bool:
        """バックオフ中かどうか。"""
        _, until = self._load(repo_id)
        return time.time() < until

    def get_wait_time(self, repo_id: int) -> float:
        """残りバックオフ時間（秒）。0ならすぐ実行可能。"""
        _, until = self._load(repo_id)
        return max(0.0, until - time.time())

    def clear(self, repo_id: int) -> None:
        """成功時にバックオフをリセット。"""
        self._save(repo_id, 0, 0.0)

    def clear_all(self) -> None:
        for repo_id in list(self._cache):
            self.clear(repo_id)


class Scheduler:
    """ジョブスケジューラ: 同時実行制限とバックオフを管理。

    デーモンメインループから tick() を呼ぶことで:
    - queuedジョブの起動
    - rate_limited/failed_retryableの自動リキュー
    を駆動する。
    """

    def __init__(
        self,
        db: Database,
        *,
        global_max_concurrency: int = 2,
        backoff: BackoffTracker | None = None,
    ):
        self._db = db
        self.global_max_concurrency = global_max_concurrency
        self.backoff = backoff or BackoffTracker(db)

    def _get_repo_by_id(self, repo_id: int) -> dict[str, Any] | None:
        """IDでrepoを取得する。"""
        row = self._db.conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        return dict(row) if row else None

    def can_run(self, repo_id: int) -> bool:
        """指定repoで新ジョブを開始できるか。"""
        if self.backoff.is_backed_off(repo_id):
            return False

        global_running = self._db.count_running_jobs()
        if global_running >= self.global_max_concurrency:
            return False

        repo = self._get_repo_by_id(repo_id)
        if repo is None:
            return False

        repo_running = self._db.count_running_jobs(repo_id)
        max_conc = repo.get("max_concurrency", 1)
        return repo_running < max_conc

    def next_job(self, repo_id: int | None = None) -> dict[str, Any] | None:
        """次に実行すべきqueuedジョブを取得する（優先度順）。enqueue_at が未来のジョブはスキップ。"""
        now = time.time()
        if repo_id is not None:
            if not self.can_run(repo_id):
                return None
            jobs = self._db.list_jobs(repo_id=repo_id, state="queued", limit=1, now=now)
        else:
            jobs = self._db.list_jobs(state="queued", limit=50, now=now)
            jobs = [j for j in jobs if self.can_run(j["repo_id"])]
            jobs = jobs[:1] if jobs else []
        return jobs[0] if jobs else None

    def on_rate_limit(self, repo_id: int, job_id: int) -> float:
        """Rate limit発生時。job状態遷移とbackoff記録を単一トランザクションで原子化。

        #1修正: 別操作だとdrive_recovery()が間に入りbackoff未設定のまま再dispatchされる。
        """
        with self._db.transaction():
            # トランザクション内で直接SQL実行（transition_jobはcommitするため使えない）
            job = self._db.get_job(job_id)
            if job is None:
                raise ValueError(f"Job {job_id} not found")
            from .db import VALID_TRANSITIONS

            if "rate_limited" not in VALID_TRANSITIONS.get(job["state"], set()):
                raise ValueError(f"Invalid transition: {job['state']} -> rate_limited")
            now = time.time()
            self._db.conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
                ("rate_limited", now, job_id),
            )
            # backoff記録も同一トランザクション内
            count, _ = self.backoff._load(repo_id)
            count += 1
            delay = min(self.backoff.base_delay * (2 ** (count - 1)), self.backoff.max_delay)
            jitter_amount = delay * self.backoff.jitter * (2 * random.random() - 1)
            next_available = now + delay + jitter_amount
            self._db.conn.execute(
                "UPDATE repos SET backoff_until = ?, backoff_count = ? WHERE id = ?",
                (next_available, count, repo_id),
            )
        # キャッシュ更新
        self.backoff._cache[repo_id] = (count, next_available)
        return next_available

    def on_success(self, repo_id: int, job_id: int, result: str | None = None) -> None:
        """ジョブ成功時。状態遷移 + 条件付きbackoffクリア。

        R4-#1修正: COUNT+clearをBEGIN IMMEDIATEで原子化。
        on_rate_limit()のトランザクションと直列化される。
        """
        notify_chat_id: str | None = None
        next_payload_str: str | None = None
        chain_depth: int = 0
        with self._db.transaction() as conn:
            # job succeeded遷移
            job = self._db.get_job(job_id)
            if job is None:
                return
            if "succeeded" not in VALID_TRANSITIONS.get(job["state"], set()):
                return
            notify_chat_id = job.get("notify_chat_id")
            next_payload_str = job.get("next_dispatch_payload")
            chain_depth = job.get("chain_depth") or 0
            now = time.time()
            conn.execute(
                # terminal_at = COALESCE で初回遷移時刻を保持（Supervisor の grace 計算基準）
                "UPDATE jobs SET state = 'succeeded', updated_at = ?, result = ?,"
                " terminal_at = COALESCE(terminal_at, ?) WHERE id = ?",
                (now, result, now, job_id),
            )
            # rate_limitedカウント確認 + clear を同一トランザクション内で実行
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE repo_id = ? AND state = 'rate_limited'",
                (repo_id,),
            ).fetchone()
            if row and row[0] == 0:
                conn.execute(
                    "UPDATE repos SET backoff_until = 0, backoff_count = 0 WHERE id = ?",
                    (repo_id,),
                )
                self.backoff._cache[repo_id] = (0, 0.0)
        # chain dispatch: 次のjobをenqueue（トランザクション外で実行）
        # 注: Discord 通知は Supervisor._tick() が担う（R-B: 二重送信防止）
        if next_payload_str:
            self._dispatch_chain_next(job_id, next_payload_str, chain_depth, notify_chat_id)

    def _dispatch_chain_next(
        self,
        parent_job_id: int,
        payload_str: str,
        current_depth: int,
        fallback_notify_chat_id: str | None,
    ) -> None:
        """次のchain jobをenqueueする。失敗時はログのみ（Discord通知はSupervisorが担う）。"""
        try:
            payload = json.loads(payload_str)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(
                "Chain halt for job %d: Invalid next_dispatch_payload JSON: %s",
                parent_job_id, e,
            )
            return

        new_depth = current_depth + 1
        if new_depth > MAX_CHAIN_DEPTH:
            logger.error(
                "Chain halt for job %d: depth limit (%d) exceeded",
                parent_job_id, MAX_CHAIN_DEPTH,
            )
            return

        repo_name = payload.get("repo")
        task = payload.get("task")
        notify_chat_id = payload.get("notify_chat_id") or fallback_notify_chat_id
        next_next = payload.get("next_dispatch_payload")

        if not repo_name or not task:
            logger.error(
                "Chain halt for job %d: missing 'repo' or 'task' field", parent_job_id
            )
            return

        repo = self._db.get_repo_by_alias(repo_name)
        if repo is None:
            logger.error(
                "Chain halt for job %d: repo '%s' not found in registry",
                parent_job_id, repo_name,
            )
            return

        next_payload_str = json.dumps(next_next) if next_next else None

        # enqueue_at: chain payload から取得し、now 基準で解釈 (option B: 前ジョブ完了時点)
        enqueue_at = 0.0
        enqueue_at_raw = payload.get("enqueue_at")
        if enqueue_at_raw:
            try:
                from .timeparse import parse_enqueue_at

                enqueue_at = parse_enqueue_at(str(enqueue_at_raw))
            except ValueError as e:
                logger.error(
                    "Chain halt for job %d: invalid enqueue_at in chain payload: %s",
                    parent_job_id, e,
                )
                return

        try:
            new_job_id = self._db.create_job(
                repo["id"],
                task,
                notify_chat_id=notify_chat_id,
                next_dispatch_payload=next_payload_str,
                chain_depth=new_depth,
                enqueue_at=enqueue_at,
            )
            logger.info(
                "Chain dispatch: parent_job=%d -> new_job=%d (depth=%d, repo=%s)",
                parent_job_id,
                new_job_id,
                new_depth,
                repo_name,
            )
        except Exception as e:
            logger.error(
                "Chain halt for job %d: failed to create next chain job: %s",
                parent_job_id, e,
            )

    def requeue_job(self, job_id: int) -> bool:
        """ジョブをキューに戻す（リトライ用）。max_retries超過時はfailed_permanent。"""
        job = self._db.get_job(job_id)
        if job is None:
            return False
        attempts = self._db.get_attempts(job_id)
        if len(attempts) >= job["max_retries"]:
            # 状態遷移のみ。Discord 通知は Supervisor._tick() が担う（R-B: 二重送信防止）
            self._db.transition_job(job_id, "failed_permanent", result="Max retries exceeded")
            return False
        current = job["state"]
        if "queued" in VALID_TRANSITIONS.get(current, set()):
            self._db.transition_job(job_id, "queued")
            return True
        return False

    def drive_recovery(self) -> list[dict[str, Any]]:
        """failed_retryable/rate_limitedジョブを自動リキュー。

        R4-#1修正: backoff clear判定もトランザクションで原子化。
        """
        actions: list[dict[str, Any]] = []

        # rate_limited → backoff完了後にrequeue
        rate_limited_jobs = self._db.list_jobs(state="rate_limited")
        for job in rate_limited_jobs:
            repo_id = job["repo_id"]
            if not self.backoff.is_backed_off(repo_id) and self.requeue_job(job["id"]):
                actions.append({"action": "requeue_rate_limited", "job_id": job["id"]})

        # rate_limitedが全て解消されたrepoのbackoffをトランザクション内でクリア
        if rate_limited_jobs:
            repo_ids = {j["repo_id"] for j in rate_limited_jobs}
            for repo_id in repo_ids:
                with self._db.transaction() as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE repo_id = ? AND state = 'rate_limited'",
                        (repo_id,),
                    ).fetchone()
                    if row and row[0] == 0:
                        conn.execute(
                            "UPDATE repos SET backoff_until = 0, backoff_count = 0 WHERE id = ?",
                            (repo_id,),
                        )
                        self.backoff._cache[repo_id] = (0, 0.0)

        # failed_retryable → 即座にrequeue
        for job in self._db.list_jobs(state="failed_retryable"):
            if self.requeue_job(job["id"]):
                actions.append({"action": "requeue_failed_retryable", "job_id": job["id"]})

        return actions

    def status(self) -> dict[str, Any]:
        """スケジューラの現在の状態をサマリーで返す。"""
        global_running = self._db.count_running_jobs()
        queued = len(self._db.list_jobs(state="queued"))
        repos = self._db.list_repos()
        repo_status: dict[str, Any] = {}
        for r in repos:
            running = self._db.count_running_jobs(r["id"])
            backed_off = self.backoff.is_backed_off(r["id"])
            wait = self.backoff.get_wait_time(r["id"])
            repo_status[r["name"]] = {
                "running": running,
                "max_concurrency": r["max_concurrency"],
                "backed_off": backed_off,
                "backoff_wait_seconds": round(wait, 1),
            }
        return {
            "global_running": global_running,
            "global_max_concurrency": self.global_max_concurrency,
            "queued_jobs": queued,
            "repos": repo_status,
        }
