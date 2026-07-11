"""状態プレーン: SQLiteでrepos/sessions/jobs/attempts/eventsを管理する。

スレッドセーフ: threading.local() で接続をスレッドごとに分離。
WALモードにより読み取りと書き込みの並行性を確保。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .paths import ensure_private_dir, secure_file, state_dir

DEFAULT_DB_DIR = state_dir()

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS repos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    path        TEXT    NOT NULL,
    aliases     TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    expected_git_root TEXT,
    model       TEXT    NOT NULL DEFAULT 'sonnet',
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    backoff_until REAL  NOT NULL DEFAULT 0,      -- rate limitバックオフ解除時刻
    backoff_count INTEGER NOT NULL DEFAULT 0,    -- 連続rate limit回数
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    pid         INTEGER,                         -- claude subprocess PID
    state       TEXT    NOT NULL DEFAULT 'idle',  -- idle / busy / dead
    current_attempt_id INTEGER,                  -- CAS用: 現在のattempt_id
    created_at  REAL    NOT NULL,
    last_heartbeat REAL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    task        TEXT    NOT NULL,
    state       TEXT    NOT NULL DEFAULT 'queued',
    priority    INTEGER NOT NULL DEFAULT 0,       -- 高い=優先
    max_retries INTEGER NOT NULL DEFAULT 2,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    result      TEXT,                             -- JSON: サマリー等
    notify_chat_id TEXT,                          -- Discord channel ID for completion notification
    next_dispatch_payload TEXT,                   -- JSON: 次のchain jobのpayload（NULLならchainなし）
    chain_depth INTEGER NOT NULL DEFAULT 0,       -- chainの深さ（無限再帰防止）
    enqueue_at  REAL    NOT NULL DEFAULT 0,       -- スケジュール実行時刻（0=即時）
    terminal_at REAL,                             -- 終端状態遷移の安定タイムスタンプ（Supervisor配送基準）
    wake_state  TEXT,                             -- NULL/'woken'/'fallback'/'undeliverable'
    wake_attempts INTEGER NOT NULL DEFAULT 0,     -- Controllerへのwake試行回数
    reported_at REAL                              -- Controller が report-done した時刻（CAS idempotency）
);

CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    session_id  INTEGER REFERENCES sessions(id),
    attempt_num INTEGER NOT NULL DEFAULT 1,
    started_at  REAL    NOT NULL,
    finished_at REAL,
    exit_code   INTEGER,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  INTEGER NOT NULL REFERENCES attempts(id),
    event_type  TEXT    NOT NULL,
    data        TEXT,                             -- JSON
    timestamp   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT    NOT NULL,                 -- "kill_job", "kill_repo", "shutdown"
    payload     TEXT,                             -- JSON
    created_at  REAL    NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_repo  ON jobs(repo_id, state);
CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo_id, state);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts(job_id);
CREATE INDEX IF NOT EXISTS idx_events_attempt ON events(attempt_id);
CREATE INDEX IF NOT EXISTS idx_daemon_commands_pending ON daemon_commands(processed, created_at);
"""

# v1→v2マイグレーション: 既存DBにカラム追加
MIGRATIONS = [
    # backoff永続化カラム
    "ALTER TABLE repos ADD COLUMN backoff_until REAL NOT NULL DEFAULT 0",
    "ALTER TABLE repos ADD COLUMN backoff_count INTEGER NOT NULL DEFAULT 0",
    # daemon_commandsテーブル
    """CREATE TABLE IF NOT EXISTS daemon_commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT NOT NULL,
        payload TEXT,
        created_at REAL NOT NULL,
        processed INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_daemon_commands_pending ON daemon_commands(processed, created_at)",
    # working_dir列の削除は不可能（SQLiteの制限）→ 無視するだけ
    # R4-#2: session CAS用カラム
    "ALTER TABLE sessions ADD COLUMN current_attempt_id INTEGER",
    # completion-notification: notify_chat_id列
    "ALTER TABLE jobs ADD COLUMN notify_chat_id TEXT",
    # chain-dispatch: next_dispatch_payload列 + chain_depth列
    "ALTER TABLE jobs ADD COLUMN next_dispatch_payload TEXT",
    "ALTER TABLE jobs ADD COLUMN chain_depth INTEGER NOT NULL DEFAULT 0",
    # R6: 時間ベース trigger (0 = 即時)
    "ALTER TABLE jobs ADD COLUMN enqueue_at REAL NOT NULL DEFAULT 0",
    # Supervisor配送終局保証カラム群
    "ALTER TABLE jobs ADD COLUMN terminal_at REAL",
    "ALTER TABLE jobs ADD COLUMN wake_state TEXT",
    "ALTER TABLE jobs ADD COLUMN wake_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN reported_at REAL",
]

# Supervisor が配送トリガとする終端状態（timed_out/lost は中間状態なので含まない）
DELIVERY_TERMINAL_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed_permanent", "needs_controller"}
)

# ジョブの有効な状態遷移マップ
JOB_STATES = {
    "queued",
    "starting",
    "running",
    "succeeded",
    "failed_retryable",
    "failed_permanent",
    "rate_limited",
    "needs_controller",
    "timed_out",
    "lost",
}

VALID_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"starting", "failed_permanent"},
    "starting": {"running", "failed_retryable", "failed_permanent", "timed_out", "lost"},
    "running": {
        "succeeded",
        "failed_retryable",
        "failed_permanent",
        "rate_limited",
        "needs_controller",
        "timed_out",
        "lost",
    },
    "rate_limited": {"queued", "failed_permanent"},
    "failed_retryable": {"queued", "failed_permanent"},
    "timed_out": {"queued", "failed_permanent"},
    "lost": {"queued", "failed_permanent"},
    "needs_controller": {"queued", "failed_permanent", "succeeded"},
    # Terminal states — no transitions out
    "succeeded": set(),
    "failed_permanent": set(),
}


class Database:
    """SQLiteデータベースのラッパー。

    スレッドセーフ: threading.local() で接続をスレッドごとに分離。
    WALモードにより複数スレッドからの同時読み書きをサポート。
    """

    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            ensure_private_dir(DEFAULT_DB_DIR)
            db_path = DEFAULT_DB_DIR / "state.db"
            secure_parent = True
        else:
            secure_parent = False
        self._secure_parent = secure_parent
        self._path = Path(db_path)
        if secure_parent:
            ensure_private_dir(self._path.parent)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        # メインスレッドで初期化（スキーマ作成）
        self._init_schema()
        secure_file(self._path)

    def _make_conn(self) -> sqlite3.Connection:
        """新しいSQLite接続を作成する。"""
        conn = sqlite3.connect(str(self._path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        secure_file(self._path)
        return conn

    def _init_schema(self) -> None:
        """スキーマ初期化（メインスレッドで1回だけ）。"""
        conn = self._make_conn()
        conn.executescript(SCHEMA_SQL)
        # マイグレーション実行（既存DBへのカラム追加等）
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # カラム/テーブルが既に存在
        conn.commit()
        conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        """スレッドローカルな接続を取得する。"""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._make_conn()
            self._local.conn = c
        return c

    def close(self) -> None:
        """現在のスレッドの接続を閉じる。"""
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextmanager
    def transaction(self):
        """明示的トランザクション（書き込みロック付き）。"""
        with self._lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise

    # ─── Repos ──────────────────────────────────────────────

    def create_repo(
        self,
        name: str,
        path: str,
        *,
        aliases: list[str] | None = None,
        expected_git_root: str | None = None,
        model: str = "sonnet",
        max_concurrency: int = 1,
    ) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO repos (name, path, aliases, expected_git_root, model, max_concurrency, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, path, json.dumps(aliases or []), expected_git_root, model, max_concurrency, now),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def get_repo(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM repos WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["aliases"] = json.loads(d["aliases"])
        return d

    def get_repo_by_alias(self, alias: str) -> dict[str, Any] | None:
        """名前またはエイリアスでrepoを検索する。"""
        repo = self.get_repo(alias)
        if repo:
            return repo
        rows = self.conn.execute("SELECT * FROM repos").fetchall()
        for row in rows:
            d = dict(row)
            aliases = json.loads(d["aliases"])
            if alias in aliases:
                d["aliases"] = aliases
                return d
        return None

    def list_repos(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM repos ORDER BY name").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["aliases"] = json.loads(d["aliases"])
            result.append(d)
        return result

    def update_repo(self, name: str, **kwargs: Any) -> bool:
        repo = self.get_repo(name)
        if not repo:
            return False
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k == "aliases":
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return True
        vals.append(name)
        self.conn.execute(f"UPDATE repos SET {', '.join(sets)} WHERE name = ?", vals)
        self.conn.commit()
        return True

    def delete_repo(self, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM repos WHERE name = ?", (name,))
        self.conn.commit()
        return cur.rowcount > 0

    # ─── Sessions ───────────────────────────────────────────

    def create_session(self, repo_id: int, pid: int | None = None) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO sessions (repo_id, pid, state, created_at, last_heartbeat)"
            " VALUES (?, ?, 'idle', ?, ?)",
            (repo_id, pid, now, now),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(
        self, repo_id: int | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if repo_id is not None:
            sql += " AND repo_id = ?"
            params.append(repo_id)
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def update_session(self, session_id: int, **kwargs: Any) -> bool:
        sets = []
        vals = []
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return True
        vals.append(session_id)
        cur = self.conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", vals)
        self.conn.commit()
        return cur.rowcount > 0

    def cas_update_session(
        self,
        session_id: int,
        expected_attempt_id: int,
        **kwargs: Any,
    ) -> bool:
        """Compare-And-Set: current_attempt_idが一致する場合のみ更新。

        R4-#2修正: 旧attemptからのsession上書きを防止する。
        """
        sets = []
        vals = []
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return True
        vals.extend([session_id, expected_attempt_id])
        cur = self.conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ? AND current_attempt_id = ?",
            vals,
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_session(self, session_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ─── Jobs ───────────────────────────────────────────────

    def create_job(
        self,
        repo_id: int,
        task: str,
        *,
        priority: int = 0,
        max_retries: int = 2,
        notify_chat_id: str | None = None,
        next_dispatch_payload: str | None = None,
        chain_depth: int = 0,
        enqueue_at: float = 0,
    ) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO jobs (repo_id, task, state, priority, max_retries, created_at, updated_at, notify_chat_id, next_dispatch_payload, chain_depth, enqueue_at)"
            " VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo_id,
                task,
                priority,
                max_retries,
                now,
                now,
                notify_chat_id,
                next_dispatch_payload,
                chain_depth,
                enqueue_at,
            ),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def transition_job(self, job_id: int, new_state: str, *, result: str | None = None) -> bool:
        """ジョブの状態遷移。無効な遷移はValueErrorを送出。"""
        if new_state not in JOB_STATES:
            raise ValueError(f"Invalid state: {new_state}")
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")
        current = job["state"]
        if new_state not in VALID_TRANSITIONS.get(current, set()):
            raise ValueError(
                f"Invalid transition: {current} -> {new_state} "
                f"(allowed: {VALID_TRANSITIONS.get(current, set())})"
            )
        now = time.time()
        updates = "state = ?, updated_at = ?"
        params: list[Any] = [new_state, now]
        if result is not None:
            updates += ", result = ?"
            params.append(result)
        # 配送対象の終端状態に初回遷移した時刻を記録（Supervisor の grace 計算基準）
        if new_state in DELIVERY_TERMINAL_STATES:
            updates += ", terminal_at = COALESCE(terminal_at, ?)"
            params.append(now)
        # requeue 時は配送状態をリセット（新しい attempt のクリーンスタート）
        elif new_state == "queued":
            updates += (
                ", terminal_at = NULL, wake_state = NULL, wake_attempts = 0, reported_at = NULL"
            )
        params.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {updates} WHERE id = ?", params)
        self.conn.commit()
        return True

    def list_jobs(
        self,
        repo_id: int | None = None,
        state: str | None = None,
        limit: int = 50,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """ジョブ一覧取得。now を渡すと enqueue_at <= now のジョブのみ返す。"""
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if repo_id is not None:
            sql += " AND repo_id = ?"
            params.append(repo_id)
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        if now is not None:
            sql += " AND enqueue_at <= ?"
            params.append(now)
        sql += " ORDER BY priority DESC, enqueue_at ASC, created_at ASC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def count_running_jobs(self, repo_id: int | None = None) -> int:
        """running状態のジョブ数をカウント。"""
        if repo_id is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('starting', 'running') AND repo_id = ?",
                (repo_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('starting', 'running')"
            ).fetchone()
        return row[0] if row else 0

    # ─── Attempts ───────────────────────────────────────────

    def create_attempt(self, job_id: int, session_id: int | None = None) -> int:
        # attempt_numを自動計算
        row = self.conn.execute(
            "SELECT COALESCE(MAX(attempt_num), 0) FROM attempts WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        attempt_num = row[0] + 1
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO attempts (job_id, session_id, attempt_num, started_at)"
            " VALUES (?, ?, ?, ?)",
            (job_id, session_id, attempt_num, now),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_attempt(
        self, attempt_id: int, *, exit_code: int | None = None, error: str | None = None
    ) -> bool:
        now = time.time()
        cur = self.conn.execute(
            "UPDATE attempts SET finished_at = ?, exit_code = ?, error = ? WHERE id = ?",
            (now, exit_code, error, attempt_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_attempts(self, job_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM attempts WHERE job_id = ? ORDER BY attempt_num", (job_id,)
            ).fetchall()
        ]

    # ─── Events ─────────────────────────────────────────────

    def add_event(self, attempt_id: int, event_type: str, data: str | None = None) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO events (attempt_id, event_type, data, timestamp) VALUES (?, ?, ?, ?)",
            (attempt_id, event_type, data, now),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def get_latest_event(self, attempt_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE attempt_id = ? ORDER BY timestamp DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
        return dict(row) if row else None

    # ─── Backoff永続化 ─────────────────────────────────────

    def set_backoff(self, repo_id: int, backoff_until: float, backoff_count: int) -> None:
        """バックオフ状態をDBに永続化。"""
        self.conn.execute(
            "UPDATE repos SET backoff_until = ?, backoff_count = ? WHERE id = ?",
            (backoff_until, backoff_count, repo_id),
        )
        self.conn.commit()

    def clear_backoff(self, repo_id: int) -> None:
        """バックオフをクリア。"""
        self.conn.execute(
            "UPDATE repos SET backoff_until = 0, backoff_count = 0 WHERE id = ?",
            (repo_id,),
        )
        self.conn.commit()

    def get_backoff(self, repo_id: int) -> tuple[float, int]:
        """バックオフ状態を取得: (backoff_until, backoff_count)。"""
        row = self.conn.execute(
            "SELECT backoff_until, backoff_count FROM repos WHERE id = ?",
            (repo_id,),
        ).fetchone()
        if row is None:
            return (0.0, 0)
        return (row[0] or 0.0, row[1] or 0)

    # ─── Daemon Commands ───────────────────────────────────

    def enqueue_command(self, command: str, payload: dict[str, Any] | None = None) -> int:
        """デーモンへのコマンドをキューに投入。"""
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO daemon_commands (command, payload, created_at) VALUES (?, ?, ?)",
            (command, json.dumps(payload) if payload else None, now),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    # ─── Supervisor 配送終局保証 ──────────────────────────────

    def mark_reported(self, job_id: int, ts: float) -> bool:
        """Controller が Discord reply 完了後に呼ぶ。CAS で一度だけ設定。

        rowcount==1 なら Controller が配送権獲得（Supervisor の fallback は no-op になる）。
        rowcount==0 なら既に reported_at 設定済み（no-op）。
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE jobs SET reported_at = ? WHERE id = ? AND reported_at IS NULL",
                (ts, job_id),
            )
            return cur.rowcount == 1

    def claim_for_fallback(self, job_id: int) -> bool:
        """Supervisor fallback が配送権を CAS で獲得。

        rowcount==1 なら fallback が配送権獲得（Controller の report-done は no-op になる）。
        rowcount==0 なら既に fallback claim 済み or reported 済み（no-op）。
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE jobs SET wake_state = 'fallback'"
                " WHERE id = ? AND reported_at IS NULL AND (wake_state IS NULL OR wake_state != 'fallback')",
                (job_id,),
            )
            return cur.rowcount == 1

    def mark_woken(self, job_id: int) -> None:
        """Controller への wake send-keys 送信後に記録。"""
        self.conn.execute(
            "UPDATE jobs SET wake_state = 'woken' WHERE id = ? AND wake_state IS NULL",
            (job_id,),
        )
        self.conn.commit()

    def incr_wake_attempts(self, job_id: int) -> int:
        """wake_attempts をインクリメントして新しい値を返す。"""
        self.conn.execute(
            "UPDATE jobs SET wake_attempts = wake_attempts + 1 WHERE id = ?", (job_id,)
        )
        self.conn.commit()
        row = self.conn.execute("SELECT wake_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row[0] if row else 0

    def set_undeliverable(self, job_id: int) -> None:
        """恒久失敗でこれ以上配送を試みない状態にする（無限リトライ防止）。"""
        self.conn.execute("UPDATE jobs SET wake_state = 'undeliverable' WHERE id = ?", (job_id,))
        self.conn.commit()

    def list_undelivered_terminal_jobs(self) -> list[dict[str, Any]]:
        """未配送の終端ジョブ一覧 (reported_at IS NULL)。terminal_at 昇順。"""
        states = list(DELIVERY_TERMINAL_STATES)
        placeholders = ",".join("?" * len(states))
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE state IN ({placeholders}) AND reported_at IS NULL"
            " ORDER BY COALESCE(terminal_at, updated_at) ASC",
            states,
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Daemon Commands ───────────────────────────────────

    def poll_commands(self, limit: int = 10) -> list[dict[str, Any]]:
        """未処理コマンドを取得してprocessed=1にマーク。"""
        rows = self.conn.execute(
            "SELECT * FROM daemon_commands WHERE processed = 0 ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("payload"):
                d["payload"] = json.loads(d["payload"])
            self.conn.execute(
                "UPDATE daemon_commands SET processed = 1 WHERE id = ?",
                (d["id"],),
            )
            result.append(d)
        if result:
            self.conn.commit()
        return result
