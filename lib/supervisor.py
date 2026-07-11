"""Supervisor: 配送終局保証（Controller wake + fallback Discord push）。

非LLM のライフサイクル監視層。job の終端遷移を検知し:
1. Controller (tmux pane) に wake メッセージを send-keys して起床させ、
   Controller が collect → reply → report-done で配送を担う（主経路）。
2. wake_grace_seconds 以内に report-done がなければ daemon 直 push で
   Discord に確実に返信する（終局保証フォールバック）。

CAS により主経路と fallback が競合しても 1 回だけ送信される。

Idle-state allowlist:
  Controller の tmux pane に send-keys するのは、通常入力待ちを positive allowlist で
  確認できた場合だけ。不明な画面は常に busy とみなし、fallback 配送に委ねる。
  自動 wake は既定で無効。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .db import Database
from .notifier import notify_discord_result
from .paths import state_dir

logger = logging.getLogger("supervisor")

# Controller pane のデフォルト保存先（register-pane が書くファイル）
_DEFAULT_PANE_FILE = state_dir() / "controller.pane"

# Full-line prompts that positively identify a normal, idle input state.
# A UI change intentionally disables wake until this allowlist is reviewed.
IDLE_PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^claude>\s*$"),
)


class Supervisor:
    """ジョブの配送終局保証を担う非LLM Supervisor。

    daemon._tick() から5秒ごとに tick() が呼ばれる。
    """

    def __init__(
        self,
        db: Database,
        *,
        wake_grace_seconds: float = 90.0,
        max_wake_attempts: int = 1,
        pane_file: Path | None = None,
        auto_wake: bool = False,
    ) -> None:
        self._db = db
        self.wake_grace_seconds = wake_grace_seconds
        self.max_wake_attempts = max_wake_attempts
        self._pane_file = pane_file or _DEFAULT_PANE_FILE
        self.auto_wake = auto_wake

    def tick(self) -> None:
        """未配送の terminal ジョブを走査し、配送を試みる。"""
        jobs = self._db.list_undelivered_terminal_jobs()
        for job in jobs:
            try:
                self._drive_delivery(job)
            except Exception:
                logger.exception("Error driving delivery for job %d", job["id"])

    # ─── 内部: 配送ドライバ ───────────────────────────────────

    def _drive_delivery(self, job: dict[str, Any]) -> None:
        """1ジョブの配送ステートマシン。"""
        now = time.time()
        wake_state = job.get("wake_state")

        # 既に reported → スキップ（reported_at はリストから除外されているはずだが念のため）
        if job.get("reported_at") is not None:
            return

        # 恒久失敗 → スキップ（無限リトライ防止）
        if wake_state == "undeliverable":
            return

        # fallback 配送権を既に claim 済み → 通知をリトライ
        if wake_state == "fallback":
            self._attempt_fallback_notify(job)
            return

        # Default-safe mode: never inject keystrokes into tmux. Direct fallback
        # is both safer and faster than waiting for a wake path that is disabled.
        if not self.auto_wake:
            self._claim_and_fallback(job)
            return

        # grace 期間の判定（fallback の唯一のトリガー）
        terminal_at = job.get("terminal_at") or job.get("updated_at", now)
        grace_expired = (now - terminal_at) > self.wake_grace_seconds

        # grace 超過 → fallback に移行
        # （max_wake_attempts 到達後でも grace まで待つ。early fallback は二重送信を招く）
        if grace_expired:
            self._claim_and_fallback(job)
            return

        # wake 試行上限未達なら Controller 起床を試みる（デフォルト1回だけ）
        # 上限到達後は grace まで待機（do nothing = 次 tick で grace チェック）
        # 理由: 再 wake は Controller の進行中ターン（collect→reply）に別メッセージを送り込み
        #       二重 reply を引き起こす。wake は1回だけ送り、後は fallback に委ねるのが安全。
        wake_attempts = job.get("wake_attempts") or 0
        if wake_attempts < self.max_wake_attempts:
            self._try_wake(job)
        # else: max に達した → 何もしない、次 tick で grace_expired が True になったら fallback

    def _try_wake(self, job: dict[str, Any]) -> bool:
        """Controller pane に wake メッセージを send-keys。成功時 True。

        pane が未登録/消失/busy の場合:
          - 未登録/消失 → 即 fallback
          - busy       → 何もしない（次 tick で再試行 or grace 超過で fallback）
        """
        job_id = job["id"]

        if not self.auto_wake:
            logger.debug("automatic controller wake disabled for job %d", job_id)
            return False

        pane = self._load_pane()
        if pane is None:
            logger.warning(
                "controller.pane not registered; falling back immediately for job %d", job_id
            )
            self._claim_and_fallback(job)
            return False

        if not self._pane_exists(pane):
            logger.warning(
                "controller pane %s does not exist; falling back for job %d", pane, job_id
            )
            self._claim_and_fallback(job)
            return False

        if self._is_pane_busy(pane):
            # Controller が処理中 → 送らない。次 tick で再試行。
            logger.debug("controller pane busy, deferring wake for job %d", job_id)
            return False

        # wake メッセージ組み立て（単一行）
        repo_row = self._db.conn.execute(
            "SELECT name FROM repos WHERE id = ?", (job["repo_id"],)
        ).fetchone()
        repo_name = repo_row[0] if repo_row else "?"
        chat_id = job.get("notify_chat_id") or ""
        state = job["state"]

        msg = (
            f"[discord-agent] Job {job_id} -> {state}. "
            f"collect {repo_name} --job-id {job_id} --json ; "
            f"reply to chat_id {chat_id} ; "
            f"then report-done {job_id}"
        )

        try:
            # 2段階送信: テキスト(-l でリテラル) → Enter
            subprocess.run(
                ["tmux", "send-keys", "-t", pane, "-l", "--", msg],
                check=True,
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", pane, "Enter"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            self._db.incr_wake_attempts(job_id)
            self._db.mark_woken(job_id)
            logger.info("Woke controller for job %d (pane %s)", job_id, pane)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning("Failed to send-keys for job %d: %s", job_id, e)
            return False

    def _claim_and_fallback(self, job: dict[str, Any]) -> None:
        """fallback 配送権を CAS で獲得し、Discord に直送する。"""
        job_id = job["id"]
        if not self._db.claim_for_fallback(job_id):
            # 別の tick が先に claim 済み（または既に reported）
            return
        # claim 成功 → job を再取得して最新 result を使う
        fresh = self._db.get_job(job_id)
        self._attempt_fallback_notify(fresh or job)

    def _attempt_fallback_notify(self, job: dict[str, Any]) -> None:
        """fallback 通知を試みる（claim_for_fallback 済みの前提）。"""
        job_id = job["id"]
        chat_id = job.get("notify_chat_id")

        if not chat_id:
            # chat_id 未設定 → fallback も送れない
            logger.warning(
                "Fallback push impossible for job %d: no notify_chat_id set", job_id
            )
            self._db.set_undeliverable(job_id)
            return

        state = job["state"]
        result_text = job.get("result") or ""
        result_short = result_text[:200] + ("..." if len(result_text) > 200 else "")

        msg_parts = [
            f"⚠️ (自動配送) **Job {job_id}** → `{state}`",
            "Controller 無応答のため daemon が直接通知しました。",
        ]
        if result_short:
            msg_parts.append(f"Result: {result_short}")
        msg = "\n".join(msg_parts)

        result = notify_discord_result(chat_id, msg)
        if result.success:
            self._db.mark_reported(job_id, time.time())
            logger.info("Fallback push succeeded for job %d", job_id)
        elif result.permanent:
            logger.error(
                "Fallback push permanently failed for job %d: %s", job_id, result.reason
            )
            self._db.set_undeliverable(job_id)
        else:
            # 一時失敗 → 次 tick でリトライ（wake_state='fallback' のまま）
            logger.warning(
                "Fallback push temporarily failed for job %d: %s (will retry)", job_id, result.reason
            )

    # ─── 内部: tmux ユーティリティ ───────────────────────────

    def _load_pane(self) -> str | None:
        """controller.pane ファイルから pane ID を読む。不正/不在なら None。"""
        try:
            pane = self._pane_file.read_text().strip()
            if re.match(r"^%\d+$", pane):
                return pane
            logger.warning("Invalid pane ID in controller.pane: %r", pane)
        except OSError:
            pass
        return None

    def _pane_exists(self, pane: str) -> bool:
        """pane が現在の tmux セッションに存在するか。"""
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return pane in result.stdout.split()
        except Exception:
            return False

    def _is_pane_busy(self, pane: str) -> bool:
        """明確な通常入力待ち以外は True（fail-closed）。

        copy-mode でなく、末尾の非空行が positive idle-state allowlist に
        完全一致する場合だけ False を返す。
        """
        try:
            # copy-mode チェック（#{pane_in_mode} == 1）
            result = subprocess.run(
                ["tmux", "display-message", "-t", pane, "-p", "#{pane_in_mode}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            pane_mode = result.stdout.strip()
            if pane_mode != "0":
                return True

            # 末尾 5 行から最後の非空行を positive allowlist と照合
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", pane, "-S", "-5"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if not lines:
                return True
            last_line = lines[-1]
            idle = any(pattern.fullmatch(last_line) for pattern in IDLE_PROMPT_PATTERNS)
            if not idle:
                logger.debug("Pane %s is not in a positively identified idle state", pane)
            return not idle
        except Exception as e:
            # 判定不能 → busy として扱う（安全バイアス）
            logger.debug("Could not check pane %s busy state: %s; assuming busy", pane, e)
            return True
