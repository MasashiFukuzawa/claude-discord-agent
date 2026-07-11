#!/usr/bin/env python3
"""discord-agent Orchestrator CLI

4プレーン分離アーキテクチャの統合エントリポイント。
コントローラーはこのCLIを叩くだけ（O(1)操作の原則維持）。

Usage:
    orchestrator.py daemon start [--foreground]
    orchestrator.py daemon stop
    orchestrator.py create-repo <name> --path <path> [--model <model>] [--max-concurrency <n>]
    orchestrator.py list-repos
    orchestrator.py dispatch <repo> '<task>' [--priority <n>]
    orchestrator.py run-spec <spec_id> --repo <repo> [--var k=v ...] [--notify-chat-id X]
    orchestrator.py list-specs [--spec-dir <dir>]
    orchestrator.py collect <repo> [--job-id <id>] [--wait] [--json]
    orchestrator.py status [--repo <repo>] [--json]
    orchestrator.py kill <repo> [--job-id <id>]
    orchestrator.py watch [--once]
    orchestrator.py health [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# libディレクトリをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

from lib.daemon import Daemon, is_daemon_running
from lib.db import Database
from lib.registry import Registry, RegistryError, preflight_check
from lib.result_parser import parse_result
from lib.runner import Runner
from lib.scheduler import Scheduler
from lib.spec import SpecError, list_specs, load_spec
from lib.timeparse import format_enqueue_at, parse_enqueue_at
from lib.watchdog import Watchdog

# --db で上書きされるグローバルDBパス
_custom_db_path: str | None = None


def get_db() -> Database:
    return Database(_custom_db_path)


def _daemon_state_dir() -> Path:
    """現在のDB設定に対応するdaemon state dirを返す。"""
    db = get_db()
    state_dir = db._path.parent
    db.close()
    return state_dir


# ─── Commands ───────────────────────────────────────────


def cmd_daemon(args: argparse.Namespace) -> int:
    """デーモン管理。"""
    action = args.daemon_action
    if action == "start":
        db = get_db()
        daemon = Daemon(
            db,
            global_max_concurrency=args.global_concurrency,
            auto_wake=args.auto_wake is True,
        )
        return daemon.start(foreground=args.foreground)
    elif action == "stop":
        db = get_db()
        daemon = Daemon(db)
        return daemon.stop()
    elif action == "status":
        pid = is_daemon_running(_daemon_state_dir())
        if pid:
            print(f"Daemon running (PID {pid})")
        else:
            print("Daemon not running")
        return 0
    else:
        print(f"Unknown daemon action: {action}", file=sys.stderr)
        return 1


def cmd_create_repo(args: argparse.Namespace) -> int:
    """repoを登録する。"""
    db = get_db()
    registry = Registry(db)
    try:
        result = registry.register(
            args.name,
            args.path,
            aliases=args.alias or [],
            model=args.model,
            max_concurrency=args.max_concurrency,
            skip_preflight=args.skip_preflight,
        )
        print(f"✓ Repo '{args.name}' registered")
        print(f"  Path: {result['path']}")
        if result.get("git_root"):
            print(f"  Git root: {result['git_root']}")
        registry.export_json()
        return 0
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()


def cmd_update_repo(args: argparse.Namespace) -> int:
    """repo のパス・設定を更新する。"""
    db = get_db()
    registry = Registry(db)
    try:
        registry.resolve(args.name)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        db.close()
        return 1

    kwargs: dict[str, Any] = {}
    if args.path is not None:
        resolved = str(Path(args.path).resolve())
        if not args.skip_preflight:
            try:
                check = preflight_check(resolved)
                kwargs["path"] = resolved
                kwargs["expected_git_root"] = check["git_root"]
            except RegistryError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                db.close()
                return 1
        else:
            kwargs["path"] = resolved
        if args.expected_git_root is not None:
            kwargs["expected_git_root"] = str(Path(args.expected_git_root).resolve())
    if args.model is not None:
        kwargs["model"] = args.model
    if args.max_concurrency is not None:
        kwargs["max_concurrency"] = args.max_concurrency

    if not kwargs:
        print("Nothing to update. Specify at least one of: --path, --model, --max-concurrency")
        db.close()
        return 1

    db.update_repo(args.name, **kwargs)
    registry.export_json()
    db.close()

    db2 = get_db()
    repo2 = Registry(db2).resolve(args.name)
    db2.close()

    print(f"✓ Repo '{args.name}' updated")
    print(f"  Path: {repo2['path']}")
    if repo2.get("expected_git_root"):
        print(f"  Expected git root: {repo2['expected_git_root']}")
    return 0


def cmd_list_repos(args: argparse.Namespace) -> int:
    """登録済みrepo一覧。"""
    db = get_db()
    registry = Registry(db)
    repos = registry.list_all()
    if not repos:
        print("(no repos registered)")
        return 0

    for r in repos:
        aliases = ", ".join(r["aliases"]) if r["aliases"] else "-"
        print(f"  {r['name']}")
        print(f"    path: {r['path']}")
        print(f"    model: {r['model']}  concurrency: {r['max_concurrency']}")
        print(f"    aliases: {aliases}")
        try:
            preflight_check(r["path"])
            print("    status: ✓ valid")
        except RegistryError:
            print("    status: ✗ path invalid")
    db.close()
    return 0


def _enqueue_job(
    db: Database,
    repo: dict[str, Any],
    task: str,
    *,
    priority: int = 0,
    notify_chat_id: str | None = None,
    enqueue_at: float = 0,
) -> int:
    """ジョブを DB に enqueue して job_id を返す。"""
    job_id = db.create_job(
        repo["id"],
        task,
        priority=priority,
        notify_chat_id=notify_chat_id,
        enqueue_at=enqueue_at,
    )
    pid = is_daemon_running(_daemon_state_dir())
    if enqueue_at > time.time():
        print(
            f"✓ Job {job_id} scheduled (repo: {repo['name']}, at: {format_enqueue_at(enqueue_at)})"
        )
    else:
        print(f"✓ Job {job_id} queued (repo: {repo['name']})")
    if pid:
        print(f"  Daemon running (PID {pid}), job will be picked up automatically")
    else:
        print("  WARNING: Daemon not running. Start with: orchestrator.py daemon start")
    print(f"  Job ID: {job_id}")
    return job_id


def cmd_dispatch(args: argparse.Namespace) -> int:
    """タスクをジョブキューに投入。デーモンが起動していれば自動実行される。"""
    db = get_db()
    registry = Registry(db)

    # --chain-file モード
    if getattr(args, "chain_file", None):
        return _cmd_dispatch_chain_file(db, registry, args)

    # 通常dispatch: repo と task が必須
    if not args.repo or not args.task:
        print("ERROR: repo and task are required (or use --chain-file)", file=sys.stderr)
        db.close()
        return 1

    try:
        repo = registry.resolve(args.repo)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        db.close()
        return 1

    # #6修正: working_dirは常にRegistry経由（--working-dir廃止）
    notify_chat_id = getattr(args, "notify_chat_id", None) or os.environ.get(
        "DISCORD_NOTIFY_CHAT_ID"
    )
    if not notify_chat_id:
        logging.getLogger("orchestrator").warning(
            "No notify_chat_id set; job completion will not be notified to Discord. "
            "Use --notify-chat-id or set DISCORD_NOTIFY_CHAT_ID env var."
        )

    enqueue_at = 0.0
    if getattr(args, "enqueue_at", None):
        try:
            enqueue_at = parse_enqueue_at(args.enqueue_at)
        except ValueError as e:
            print(f"ERROR: --enqueue-at parse failed: {e}", file=sys.stderr)
            db.close()
            return 1

    idempotency_hint = (
        "冪等性: 同じ task name の branch (chore/foo-X) が既に存在する場合は"
        " git pull で取得して reuse、commit を amend or 追加。新規 branch を作らない。"
        " push --force-with-lease 可。\n"
        "PR が既に open 状態なら新規 PR を作らず既存に push。close されたものは新規 OK。\n"
        "---\n"
    )
    task_with_hint = idempotency_hint + args.task
    _enqueue_job(
        db,
        repo,
        task_with_hint,
        priority=args.priority,
        notify_chat_id=notify_chat_id or None,
        enqueue_at=enqueue_at,
    )
    db.close()
    return 0


def _build_chain_payload(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """chain.json の配列を linked list 形式に変換する。"""
    if not entries:
        return None
    if len(entries) == 1:
        result = dict(entries[0])
        result["next_dispatch_payload"] = None
        return result
    tail = _build_chain_payload(entries[1:])
    head = dict(entries[0])
    head["next_dispatch_payload"] = tail
    return head


def _cmd_dispatch_chain_file(db: Database, registry: Registry, args: argparse.Namespace) -> int:
    """--chain-file で指定された chain.json を読み込み最初のジョブを投入。"""
    chain_file = Path(args.chain_file)
    if not chain_file.exists():
        print(f"ERROR: chain file not found: {chain_file}", file=sys.stderr)
        db.close()
        return 1

    try:
        raw = chain_file.read_text(encoding="utf-8")
        entries = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: Failed to read chain file: {e}", file=sys.stderr)
        db.close()
        return 1

    if not isinstance(entries, list) or not entries:
        print("ERROR: chain file must be a non-empty JSON array", file=sys.stderr)
        db.close()
        return 1

    notify_chat_id_fallback = getattr(args, "notify_chat_id", None) or os.environ.get(
        "DISCORD_NOTIFY_CHAT_ID"
    )

    # 各エントリに notify_chat_id が未設定の場合は fallback を適用
    for entry in entries:
        if not entry.get("notify_chat_id"):
            entry["notify_chat_id"] = notify_chat_id_fallback

    chain_head = _build_chain_payload(entries)
    if chain_head is None:
        print("ERROR: chain is empty after build", file=sys.stderr)
        db.close()
        return 1

    # 先頭エントリを投入
    first_repo_name = chain_head.get("repo")
    first_task = chain_head.get("task")
    if not first_repo_name or not first_task:
        print("ERROR: first chain entry missing 'repo' or 'task'", file=sys.stderr)
        db.close()
        return 1

    try:
        repo = registry.resolve(first_repo_name)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        db.close()
        return 1

    next_payload = chain_head.get("next_dispatch_payload")
    next_payload_str = json.dumps(next_payload) if next_payload else None

    # enqueue_at: 先頭エントリに指定がある場合は解釈
    enqueue_at_raw = chain_head.get("enqueue_at")
    enqueue_at = 0.0
    if enqueue_at_raw:
        try:
            enqueue_at = parse_enqueue_at(str(enqueue_at_raw))
        except ValueError as e:
            print(f"ERROR: enqueue_at parse failed for chain head: {e}", file=sys.stderr)
            db.close()
            return 1

    job_id = db.create_job(
        repo["id"],
        first_task,
        priority=getattr(args, "priority", 0),
        notify_chat_id=chain_head.get("notify_chat_id"),
        next_dispatch_payload=next_payload_str,
        chain_depth=0,
        enqueue_at=enqueue_at,
    )

    print(f"✓ Chain job 1/{len(entries)} queued (repo: {first_repo_name}, job_id: {job_id})")
    if len(entries) > 1:
        print(f"  Remaining {len(entries) - 1} job(s) will auto-dispatch on success")

    pid = is_daemon_running(_daemon_state_dir())
    if pid:
        print(f"  Daemon running (PID {pid}), chain will execute automatically")
    else:
        print("  WARNING: Daemon not running. Start with: orchestrator.py daemon start")

    db.close()
    return 0


def cmd_run_spec(args: argparse.Namespace) -> int:
    """spec_id から prompt を組み立てて dispatch する。"""
    db = get_db()
    registry = Registry(db)

    # --var k=v をパース
    vars_dict: dict[str, str] = {}
    for kv in args.var or []:
        if "=" not in kv:
            print(f"ERROR: --var must be in k=v format, got: {kv!r}", file=sys.stderr)
            db.close()
            return 1
        k, v = kv.split("=", 1)
        if k in vars_dict:
            print(f"ERROR: duplicate --var key: {k!r}", file=sys.stderr)
            db.close()
            return 1
        vars_dict[k] = v

    # spec をロード
    spec_dir = Path(args.spec_dir) if args.spec_dir else None
    try:
        spec = load_spec(args.spec_id, search_dirs=[spec_dir] if spec_dir else None)
    except SpecError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        db.close()
        return 1

    # 変数補間
    try:
        task = spec.render(vars_dict)
    except SpecError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        db.close()
        return 1

    # repo 解決
    try:
        repo = registry.resolve(args.repo)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        db.close()
        return 1

    notify_chat_id = args.notify_chat_id or os.environ.get("DISCORD_NOTIFY_CHAT_ID")
    if not notify_chat_id:
        logging.getLogger("orchestrator").warning(
            "No notify_chat_id set; job completion will not be notified to Discord."
        )

    enqueue_at = 0.0
    if getattr(args, "enqueue_at", None):
        try:
            enqueue_at = parse_enqueue_at(args.enqueue_at)
        except ValueError as e:
            print(f"ERROR: --enqueue-at parse failed: {e}", file=sys.stderr)
            db.close()
            return 1

    print(f"Spec: {spec.id} — {spec.description}")
    _enqueue_job(
        db,
        repo,
        task,
        priority=args.priority,
        notify_chat_id=notify_chat_id or None,
        enqueue_at=enqueue_at,
    )
    db.close()
    return 0


def cmd_list_specs(args: argparse.Namespace) -> int:
    """利用可能な spec 一覧を表示。"""
    from lib.spec import SPECS_DIR

    spec_dir = Path(args.spec_dir) if args.spec_dir else SPECS_DIR
    specs = list_specs(search_dirs=[spec_dir])
    if not specs:
        print(f"(no specs found in {spec_dir})")
        return 0
    for s in specs:
        print(f"  {s['id']}")
        print(f"    {s['description']}")
        print(f"    file: {s['file']}")
    return 0


def cmd_chain_resume(args: argparse.Namespace) -> int:
    """chain haltした続きを指定job_idのnext_dispatch_payloadから再開する。"""
    db = get_db()
    job = db.get_job(args.job_id)
    if job is None:
        print(f"ERROR: Job {args.job_id} not found", file=sys.stderr)
        db.close()
        return 1

    next_payload_str = job.get("next_dispatch_payload")
    if not next_payload_str:
        print(
            f"ERROR: Job {args.job_id} has no next_dispatch_payload (no chain to resume)",
            file=sys.stderr,
        )
        db.close()
        return 1

    try:
        payload = json.loads(next_payload_str)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: Invalid next_dispatch_payload JSON: {e}", file=sys.stderr)
        db.close()
        return 1

    repo_name = payload.get("repo")
    task = payload.get("task")
    notify_chat_id = payload.get("notify_chat_id")
    next_next = payload.get("next_dispatch_payload")

    if not repo_name or not task:
        print("ERROR: next_dispatch_payload missing 'repo' or 'task'", file=sys.stderr)
        db.close()
        return 1

    registry = Registry(db)
    try:
        repo = registry.resolve(repo_name)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        db.close()
        return 1

    next_payload_str2 = json.dumps(next_next) if next_next else None
    current_depth = job.get("chain_depth") or 0

    # enqueue_at: resume 時点 now 基準で解釈 (option B)
    enqueue_at = 0.0
    enqueue_at_raw = payload.get("enqueue_at")
    if enqueue_at_raw:
        try:
            enqueue_at = parse_enqueue_at(str(enqueue_at_raw))
        except ValueError as e:
            print(f"ERROR: enqueue_at parse failed: {e}", file=sys.stderr)
            db.close()
            return 1

    new_job_id = db.create_job(
        repo["id"],
        task,
        notify_chat_id=notify_chat_id,
        next_dispatch_payload=next_payload_str2,
        chain_depth=current_depth + 1,
        enqueue_at=enqueue_at,
    )
    print(f"✓ Chain resumed from job {args.job_id} → new job {new_job_id} (repo: {repo_name})")

    pid = is_daemon_running(_daemon_state_dir())
    if not pid:
        print("  WARNING: Daemon not running. Start with: orchestrator.py daemon start")

    db.close()
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """ジョブの結果を取得する。--waitで完了を待機。"""
    db = get_db()
    registry = Registry(db)

    try:
        repo = registry.resolve(args.repo)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        db.close()
        return 1

    # --wait: ジョブ完了までポーリング
    if args.wait:
        return _collect_wait(db, repo, args)

    return _collect_show(db, repo, args)


def _collect_wait(db: Database, repo: dict[str, Any], args: argparse.Namespace) -> int:
    """ジョブ完了を待って結果を表示する。"""
    job_id = args.job_id
    poll_interval = 3.0
    max_wait = 1800  # 30分

    start = time.time()
    print(f"Waiting for job completion (repo: {repo['name']})...")

    while time.time() - start < max_wait:
        if job_id:
            job = db.get_job(job_id)
            if job and job["state"] in ("succeeded", "failed_permanent", "needs_controller"):
                _print_job(job, json_output=getattr(args, "json_output", False))
                db.close()
                return 0
        else:
            all_jobs = db.list_jobs(repo_id=repo["id"])
            running = [j for j in all_jobs if j["state"] in ("queued", "starting", "running")]
            if not running:
                return _collect_show(db, repo, args)

        time.sleep(poll_interval)

    print("Timeout: job did not complete within 30 minutes", file=sys.stderr)
    db.close()
    return 1


def _collect_show(db: Database, repo: dict[str, Any], args: argparse.Namespace) -> int:
    """結果を表示する。"""
    if args.job_id:
        job = db.get_job(args.job_id)
        if job is None:
            print(f"ERROR: Job {args.job_id} not found", file=sys.stderr)
            db.close()
            return 1
        jobs = [job]
    else:
        all_jobs = db.list_jobs(repo_id=repo["id"])
        jobs = [
            j
            for j in all_jobs
            if j["state"]
            in ("succeeded", "failed_permanent", "failed_retryable", "needs_controller")
        ]

    if not jobs:
        print(f"[{args.repo}] No completed jobs found. Still running or queued.")
        db.close()
        return 0

    for job in jobs[:5]:
        _print_job(job, json_output=getattr(args, "json_output", False))

    db.close()
    return 0


def _print_job(job: dict[str, Any], *, json_output: bool = False) -> None:
    state = job["state"]
    result = job.get("result") or "(no result)"
    if json_output:
        raw_result = job.get("result") or ""
        parsed = parse_result(raw_result)
        print(
            json.dumps(
                {
                    "job_id": job["id"],
                    "state": state,
                    "task": job["task"][:100],
                    "result": result,
                    "parsed": parsed.to_dict() if parsed is not None else None,
                    "enqueue_at": job.get("enqueue_at") or 0,
                    # Supervisor wake 時に Controller が返信先を知るために必要
                    "notify_chat_id": job.get("notify_chat_id"),
                },
                ensure_ascii=False,
            )
        )
    else:
        status_mark = "✓" if state == "succeeded" else "✗"
        print(f"  {status_mark} Job {job['id']} [{state}]")
        if state == "queued":
            enqueue_at = job.get("enqueue_at") or 0
            import time as _time

            if enqueue_at > _time.time():
                print(f"    Scheduled at: {format_enqueue_at(enqueue_at)}")
        print(f"    Task: {job['task'][:80]}...")
        print(f"    Result: {result}")


def cmd_status(args: argparse.Namespace) -> int:
    """システム状態の表示。DB状態のみ参照（デーモンのprocess tableは見ない）。"""
    db = get_db()
    scheduler = Scheduler(db, global_max_concurrency=args.global_concurrency)
    # #4修正: Runner不要。WatchdogにはダミーRunnerを渡す（health_reportはDB-only）
    watchdog = Watchdog(db, Runner(db))

    sched_status = scheduler.status()
    health = watchdog.get_health_report()
    daemon_pid = is_daemon_running(_daemon_state_dir())

    if args.json_output:
        data = {
            "daemon_pid": daemon_pid,
            "scheduler": sched_status,
            "health": health,
        }
        print(json.dumps(data, indent=2, ensure_ascii=False))
        db.close()
        return 0

    print("=== Orchestrator Status ===")
    print(f"  Daemon: {'running (PID ' + str(daemon_pid) + ')' if daemon_pid else 'not running'}")
    print(
        f"  Global running: {sched_status['global_running']}/{sched_status['global_max_concurrency']}"
    )
    print(f"  Queued jobs: {sched_status['queued_jobs']}")
    print()

    repos_to_show = (
        {args.repo: sched_status["repos"].get(args.repo, {})}
        if args.repo
        else sched_status["repos"]
    )

    if repos_to_show:
        for name, info in repos_to_show.items():
            print(f"  [{name}]")
            print(f"    running: {info.get('running', 0)}/{info.get('max_concurrency', 1)}")
            if info.get("backed_off"):
                print(f"    ⚠ backed off ({info.get('backoff_wait_seconds', 0)}s remaining)")
            try:
                registry = Registry(db)
                repo = registry.resolve(name)
                jobs = db.list_jobs(repo_id=repo["id"], limit=10)
                active = [j for j in jobs if j["state"] in ("queued", "starting", "running")]
                if active:
                    print("    active jobs:")
                    for j in active:
                        print(f"      job {j['id']}: {j['state']} - {j['task'][:60]}")
            except RegistryError:
                pass
    else:
        print("  (no repos registered)")

    print()
    print(f"  Health: {health['healthy']} healthy, {health['stale']} stale, {health['dead']} dead")

    db.close()
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    """ジョブまたはrepoの全ジョブを停止する。デーモン経由でkill。"""
    db = get_db()

    if args.job_id:
        job = db.get_job(args.job_id)
        if job is None:
            print(f"ERROR: Job {args.job_id} not found", file=sys.stderr)
            db.close()
            return 1
        # デーモンにkillコマンドを送信
        db.enqueue_command("kill_job", {"job_id": args.job_id})
        print(f"✓ Kill command sent for job {args.job_id}")
    else:
        registry = Registry(db)
        try:
            repo = registry.resolve(args.repo)
        except RegistryError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            db.close()
            return 1
        db.enqueue_command("kill_repo", {"repo_id": repo["id"]})
        print(f"✓ Kill command sent for repo '{args.repo}'")

    if not is_daemon_running(_daemon_state_dir()):
        print("  WARNING: Daemon not running. Kill will be processed when daemon starts.")

    db.close()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Watchdog状態のポーリング表示。

    #4修正: 実際のWatchdog監視はデーモン内で実行される。
    このCLIコマンドはDB状態を定期的に表示するだけ。
    """
    db = get_db()

    pid = is_daemon_running(_daemon_state_dir())
    if not pid:
        print(
            "WARNING: Daemon not running. Watchdog monitoring requires the daemon.", file=sys.stderr
        )
        print("  Start with: orchestrator.py daemon start", file=sys.stderr)

    print("Watching job states (Ctrl+C to stop)...")

    try:
        while True:
            running = db.list_jobs(state="running")
            starting = db.list_jobs(state="starting")
            queued = db.list_jobs(state="queued")
            failed = db.list_jobs(state="failed_retryable")
            rate_limited = db.list_jobs(state="rate_limited")

            ts = time.strftime("%H:%M:%S")
            parts = []
            if running:
                parts.append(f"running={len(running)}")
            if starting:
                parts.append(f"starting={len(starting)}")
            if queued:
                parts.append(f"queued={len(queued)}")
            if failed:
                parts.append(f"failed_retryable={len(failed)}")
            if rate_limited:
                parts.append(f"rate_limited={len(rate_limited)}")
            summary = ", ".join(parts) if parts else "idle"
            print(f"  [{ts}] {summary}")

            if args.once:
                break
            time.sleep(5.0)
    except KeyboardInterrupt:
        print("\nStopped.")

    db.close()
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """ヘルスレポート。DB状態のみ参照。"""
    db = get_db()
    watchdog = Watchdog(db, Runner(db))
    report = watchdog.get_health_report()

    if args.json_output:
        print(json.dumps(report, indent=2))
    else:
        print("=== Health Report ===")
        print(f"  Daemon: {'running' if is_daemon_running(_daemon_state_dir()) else 'not running'}")
        print(f"  Sessions: {report['total_sessions']}")
        print(f"  Healthy: {report['healthy']}")
        print(f"  Stale: {report['stale']}")
        print(f"  Dead: {report['dead']}")
        print(f"  Running jobs: {report['running_jobs']}")

    db.close()
    return 0



def cmd_register_pane(args: argparse.Namespace) -> int:
    """現在の TMUX_PANE を controller.pane ファイルに保存する。"""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        print("ERROR: TMUX_PANE が設定されていません。tmux 内で実行してください。", file=sys.stderr)
        return 1
    if not re.match(r'^%\d+$', pane):
        print(f"ERROR: TMUX_PANE の形式が不正です: {pane!r}", file=sys.stderr)
        return 1
    pane_file = _daemon_state_dir() / "controller.pane"
    pane_file.parent.mkdir(parents=True, exist_ok=True)
    pane_file.write_text(pane)
    print(f"OK: registered pane {pane}")
    return 0


def cmd_clear_session(args: argparse.Namespace) -> int:
    """Controller の tmux pane に /clear<Enter> を送信する。"""
    pane_file = _daemon_state_dir() / "controller.pane"
    if not pane_file.exists():
        print(
            "ERROR: controller pane が登録されていません。"
            " tmux 内で 'python3 orchestrator.py register-pane' を実行してください。",
            file=sys.stderr,
        )
        return 1
    pane = pane_file.read_text().strip()
    if not re.match(r'^%\d+$', pane):
        print(f"ERROR: controller.pane の値が不正です: {pane!r}", file=sys.stderr)
        return 1

    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
    )
    if pane not in result.stdout.split():
        print(f"ERROR: pane {pane} は存在しません。register-pane を再実行してください。", file=sys.stderr)
        return 1

    subprocess.run(["tmux", "send-keys", "-t", pane, "/clear", "Enter"], check=True)
    print(f"OK: /clear sent to pane {pane}")
    return 0

def cmd_report_done(args: argparse.Namespace) -> int:
    """Controller が Discord への reply 完了後に呼ぶ。Supervisor の fallback 配送を抑止する。

    CAS で一度だけ reported_at を設定する。既に設定済みの場合は no-op（冪等）。
    Controller は reply に成功したら必ずこのコマンドを呼ぶこと。
    呼ばない場合、Supervisor が wake_grace_seconds 経過後に fallback push を行う。
    """
    db = get_db()
    now = time.time()
    ok = db.mark_reported(args.job_id, now)
    if ok:
        print(f"✓ Job {args.job_id} marked as reported (Supervisor fallback cancelled)")
    else:
        job = db.get_job(args.job_id)
        if job is None:
            print(f"ERROR: Job {args.job_id} not found", file=sys.stderr)
            db.close()
            return 1
        elif job.get("reported_at") is not None:
            print(f"  Job {args.job_id} already reported (no-op)")
        else:
            print(
                f"  Job {args.job_id} report-done no-op "
                f"(job state: {job.get('state')} — may not be in terminal state)"
            )
    db.close()
    return 0


# ─── Parser ─────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="discord-agent Orchestrator CLI",
    )
    parser.add_argument(
        "--global-concurrency",
        type=int,
        default=2,
        help="Global max concurrency (default: 2)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Custom database file path (e.g. /path/to/state.db)",
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # daemon
    p = sub.add_parser("daemon", help="Manage the orchestrator daemon")
    p.add_argument("daemon_action", choices=["start", "stop", "status"], help="Daemon action")
    p.add_argument("--foreground", action="store_true", help="Run in foreground (debug)")
    p.add_argument(
        "--auto-wake",
        action="store_true",
        help="Opt in to fail-closed tmux wake injection (disabled by default)",
    )

    # create-repo
    p = sub.add_parser("create-repo", help="Register a new repo")
    p.add_argument("name", help="Repo name")
    p.add_argument("--path", required=True, help="Repo path")
    p.add_argument("--model", default="sonnet", help="Model (default: sonnet)")
    p.add_argument("--max-concurrency", type=int, default=1, help="Max concurrency for this repo")
    p.add_argument("--alias", action="append", help="Aliases for this repo")
    p.add_argument("--skip-preflight", action="store_true", help="Skip git preflight check")

    # update-repo
    p = sub.add_parser("update-repo", help="Update path/settings for a registered repo")
    p.add_argument("name", help="Repo name")
    p.add_argument("--path", default=None, help="New repo path (absolute or relative)")
    p.add_argument(
        "--expected-git-root",
        dest="expected_git_root",
        default=None,
        help="Override expected_git_root (default: inferred from --path)",
    )
    p.add_argument("--model", default=None, help="Model override")
    p.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        dest="max_concurrency",
        help="Max concurrency override",
    )
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        dest="skip_preflight",
        help="Skip git preflight check",
    )

    # list-repos
    sub.add_parser("list-repos", help="List registered repos")

    # dispatch (#6: --working-dir removed, chain-dispatch: repo/task optional with --chain-file)
    p = sub.add_parser("dispatch", help="Dispatch a task to job queue")
    p.add_argument(
        "repo", nargs="?", default=None, help="Repo name or alias (omit with --chain-file)"
    )
    p.add_argument(
        "task", nargs="?", default=None, help="Task description (omit with --chain-file)"
    )
    p.add_argument("--priority", type=int, default=0, help="Priority (higher = first)")
    p.add_argument(
        "--notify-chat-id",
        dest="notify_chat_id",
        default=None,
        help="Discord channel ID to notify on completion (fallback: DISCORD_NOTIFY_CHAT_ID env)",
    )
    p.add_argument(
        "--chain-file",
        dest="chain_file",
        default=None,
        metavar="PATH",
        help="JSON array file defining a chain of jobs to auto-dispatch sequentially",
    )
    p.add_argument(
        "--enqueue-at",
        dest="enqueue_at",
        default=None,
        metavar="TIME",
        help="Delay pickup until TIME (ISO 8601, e.g. 2026-05-03T14:00:00, or relative: +7d, +1h, +30m, +60s)",
    )

    # run-spec
    p = sub.add_parser("run-spec", help="Dispatch a task via spec template")
    p.add_argument("spec_id", help="Spec ID (e.g. p4-pr-generic) or absolute path")
    p.add_argument("--repo", required=True, help="Repo name or alias")
    p.add_argument(
        "--var",
        action="append",
        metavar="k=v",
        help="Variable override (repeatable); e.g. --var branch=main",
    )
    p.add_argument("--priority", type=int, default=0, help="Priority (higher = first)")
    p.add_argument(
        "--notify-chat-id",
        dest="notify_chat_id",
        default=None,
        help="Discord channel ID to notify on completion",
    )
    p.add_argument(
        "--spec-dir",
        dest="spec_dir",
        default=None,
        metavar="DIR",
        help="Custom directory to search specs (default: specs/)",
    )
    p.add_argument(
        "--enqueue-at",
        dest="enqueue_at",
        default=None,
        metavar="TIME",
        help="Delay pickup until TIME (ISO 8601 or relative: +7d, +1h, +30m, +60s)",
    )

    # list-specs
    p = sub.add_parser("list-specs", help="List available specs")
    p.add_argument(
        "--spec-dir",
        dest="spec_dir",
        default=None,
        metavar="DIR",
        help="Custom directory to search specs (default: specs/)",
    )

    # chain-resume
    p = sub.add_parser("chain-resume", help="Resume a halted chain from a specific job")
    p.add_argument("job_id", type=int, help="Job ID whose next_dispatch_payload to resume from")

    # collect (#7: --wait implemented)
    p = sub.add_parser("collect", help="Collect job results")
    p.add_argument("repo", help="Repo name or alias")
    p.add_argument("--job-id", type=int, help="Specific job ID")
    p.add_argument("--json", dest="json_output", action="store_true", help="JSON output")
    p.add_argument("--wait", action="store_true", help="Wait for job completion (poll)")

    # status
    p = sub.add_parser("status", help="Show system status")
    p.add_argument("--repo", help="Filter by repo")
    p.add_argument("--json", dest="json_output", action="store_true", help="JSON output")

    # kill
    p = sub.add_parser("kill", help="Kill jobs (sends command to daemon)")
    p.add_argument("repo", help="Repo name or alias")
    p.add_argument("--job-id", type=int, help="Specific job ID")

    # watch
    p = sub.add_parser("watch", help="Start watchdog monitoring (standalone)")
    p.add_argument("--once", action="store_true", help="Run once and exit")

    # health
    p = sub.add_parser("health", help="Show health report")
    p.add_argument("--json", dest="json_output", action="store_true", help="JSON output")

    # register-pane
    sub.add_parser("register-pane", help="現在の TMUX_PANE を controller.pane ファイルに保存")

    # clear-session
    sub.add_parser("clear-session", help="Controller の tmux pane に /clear を送信")

    # report-done
    p = sub.add_parser(
        "report-done",
        help="Controller が Discord reply 完了後に呼ぶ。Supervisor の fallback 配送を抑止する。",
    )
    p.add_argument("job_id", type=int, help="完了を報告するジョブ ID")

    return parser


def main() -> int:
    global _custom_db_path

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # #7修正: --db でDBファイルパスを直接指定
    if args.db:
        _custom_db_path = args.db

    commands: dict[str, Any] = {
        "daemon": cmd_daemon,
        "create-repo": cmd_create_repo,
        "update-repo": cmd_update_repo,
        "list-repos": cmd_list_repos,
        "dispatch": cmd_dispatch,
        "run-spec": cmd_run_spec,
        "list-specs": cmd_list_specs,
        "collect": cmd_collect,
        "status": cmd_status,
        "kill": cmd_kill,
        "watch": cmd_watch,
        "health": cmd_health,
        "chain-resume": cmd_chain_resume,
        "register-pane": cmd_register_pane,
        "clear-session": cmd_clear_session,
        "report-done": cmd_report_done,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
