"""Discord通知: Bot API直接呼び出し（stdlib urllib のみ）。

トークンは環境変数または XDG 設定ディレクトリの env ファイルから読み込む。
notify_chat_id が None/空の場合は何もしない（ノーオペ）。

notify_discord_result() は失敗理由を構造化して返す（Supervisor が一時/恒久失敗を区別するため）。
"""

from __future__ import annotations

import json
import logging
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

from .paths import config_dir

logger = logging.getLogger("notifier")

_ENV_VAR = "DISCORD_BOT_TOKEN"
_ENV_FILE_VAR = "DISCORD_AGENT_ENV_FILE"
_API_BASE = "https://discord.com/api/v10"


def _env_file() -> Path:
    override = os.environ.get(_ENV_FILE_VAR)
    return Path(override).expanduser() if override else config_dir() / "env"


def _read_secure_env_file(path: Path) -> str | None:
    """Read an owner-controlled regular file without following symlinks."""
    try:
        if path.is_symlink():
            logger.warning("Refusing symlinked Discord credential file: %s", path)
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            logger.warning("Refusing non-regular Discord credential file: %s", path)
            return None
        if info.st_uid != os.getuid():
            logger.warning("Refusing Discord credential file owned by another user: %s", path)
            return None
        if stat.S_IMODE(info.st_mode) & 0o077:
            logger.warning("Refusing Discord credential file with group/other permissions: %s", path)
            return None
        with os.fdopen(fd, encoding="utf-8") as file:
            fd = -1
            return file.read()
    except (OSError, UnicodeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)

class NotifyResult:
    """notify_discord_result の構造化レスポンス。

    bool() は success を返すため既存の `if not result:` パターンと互換。
    """

    def __init__(
        self,
        *,
        success: bool,
        retry: bool = False,
        permanent: bool = False,
        reason: str = "",
    ) -> None:
        self.success = success
        # 一時的な失敗（5xx/network/429）→ 次 tick でリトライ
        self.retry = retry
        # 恒久的な失敗（4xx/token なし）→ undeliverable に移行
        self.permanent = permanent
        self.reason = reason

    def __bool__(self) -> bool:
        return self.success

    def __repr__(self) -> str:
        return (
            f"NotifyResult(success={self.success}, retry={self.retry}, "
            f"permanent={self.permanent}, reason={self.reason!r})"
        )


_STATE_ICONS: dict[str, str] = {
    "succeeded": "✅",
    "failed_permanent": "❌",
    "timed_out": "⏱️",
    "lost": "🔴",
    "needs_controller": "⚠️",
}


def _load_token() -> str | None:
    """Bot tokenを取得。環境変数 → .env ファイルの順で探索。"""
    tok = os.environ.get(_ENV_VAR)
    if tok:
        return tok.strip()
    env_file = _env_file()
    contents = _read_secure_env_file(env_file)
    if contents is not None:
        for line in contents.splitlines():
            line = line.strip()
            if line.startswith(f"{_ENV_VAR}="):
                return line[len(f"{_ENV_VAR}=") :].strip()
    return None


def notify_discord(chat_id: str, text: str) -> bool:
    """Discord channelにメッセージを送信。成功時True。後方互換 API。"""
    return bool(notify_discord_result(chat_id, text))


def notify_discord_result(chat_id: str, text: str) -> NotifyResult:
    """Discord channelにメッセージを送信し、構造化された結果を返す。

    Supervisor が一時失敗（リトライ可能）と恒久失敗（undeliverable）を区別するために使用。
    """
    token = _load_token()
    if not token:
        logger.warning("DISCORD_BOT_TOKEN not found; skipping Discord notification")
        return NotifyResult(success=False, permanent=True, reason="no token")

    url = f"{_API_BASE}/channels/{chat_id}/messages"
    payload = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                return NotifyResult(success=True)
            reason = f"HTTP {resp.status}"
            logger.warning("Discord notify failed: %s", reason)
            return NotifyResult(success=False, permanent=True, reason=reason)
    except urllib.error.HTTPError as e:
        reason = f"HTTP {e.code} {e.reason}"
        # 一時失敗: レート制限・サーバーエラー
        if e.code in (429, 500, 502, 503, 504):
            logger.warning("Discord notify temporarily failed: %s (will retry)", reason)
            return NotifyResult(success=False, retry=True, reason=reason)
        # 恒久失敗: 認証エラー・チャンネル不存在など
        logger.warning("Discord notify permanently failed: %s", reason)
        return NotifyResult(success=False, permanent=True, reason=reason)
    except Exception as e:
        # ネットワークエラーは一時失敗として扱う
        logger.warning("Discord notify failed (network): %s (will retry)", e)
        return NotifyResult(success=False, retry=True, reason=str(e))


def notify_job_state(
    chat_id: str | None,
    *,
    job_id: int,
    state: str,
    task: str = "",
    result: str | None = None,
) -> None:
    """ジョブ状態変化をDiscordに通知する。chat_idがNoneなら何もしない。"""
    if not chat_id:
        return

    icon = _STATE_ICONS.get(state, "🔔")
    task_short = task[:80] + ("..." if len(task) > 80 else "")
    lines = [f"{icon} **Job {job_id}** → `{state}`", f"Task: {task_short}"]
    if result:
        result_short = result[:200] + ("..." if len(result) > 200 else "")
        lines.append(f"Result: {result_short}")

    if not notify_discord(chat_id, "\n".join(lines)):
        logger.warning("Failed to notify Discord for job %d state %s", job_id, state)


def notify_chain_halt(
    chat_id: str | None,
    *,
    parent_job_id: int,
    reason: str,
) -> None:
    """Chain halt時のDiscord通知。chat_idがNoneならログのみ。"""
    if not chat_id:
        logger.warning(
            "Chain halted at job %d: %s (no chat_id for notification)",
            parent_job_id,
            reason,
        )
        return
    lines = [
        f"\U0001f517\u26d4 **Chain halted** after Job {parent_job_id}",
        f"Reason: {reason}",
    ]
    if not notify_discord(chat_id, "\n".join(lines)):
        logger.warning("Failed to notify Discord for chain halt at job %d", parent_job_id)
