"""Repo Registry: repo_name → canonical_path の管理とpreflight検証。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .db import Database

CONFIG_DIR = Path(__file__).parent.parent / "config"
REPOS_JSON = CONFIG_DIR / "repos.json"


class RegistryError(Exception):
    """Registry操作のエラー。"""


def _run_git(path: str, *args: str) -> str:
    """指定パスでgitコマンドを実行。"""
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RegistryError(f"git {' '.join(args)} failed at {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def preflight_check(path: str, expected_git_root: str | None = None) -> dict[str, Any]:
    """パスの存在確認とgit root検証。"""
    p = Path(path)
    if not p.exists():
        raise RegistryError(f"Path does not exist: {path}")
    if not p.is_dir():
        raise RegistryError(f"Path is not a directory: {path}")

    try:
        actual_root = _run_git(path, "rev-parse", "--show-toplevel")
    except RegistryError as exc:
        raise RegistryError(f"Not a git repository: {path}") from exc

    if expected_git_root and Path(actual_root).resolve() != Path(expected_git_root).resolve():
        raise RegistryError(f"Git root mismatch: expected {expected_git_root}, got {actual_root}")

    branch = ""
    try:
        branch = _run_git(path, "rev-parse", "--abbrev-ref", "HEAD")
    except RegistryError:
        pass

    return {
        "path": path,
        "git_root": actual_root,
        "branch": branch,
        "valid": True,
    }


class Registry:
    """Repo Registryの管理。DBバックエンドを使用。"""

    def __init__(self, db: Database):
        self._db = db

    def register(
        self,
        name: str,
        path: str,
        *,
        aliases: list[str] | None = None,
        model: str = "sonnet",
        max_concurrency: int = 1,
        skip_preflight: bool = False,
    ) -> dict[str, Any]:
        """新しいrepoを登録する。"""
        existing = self._db.get_repo(name)
        if existing:
            raise RegistryError(f"Repo '{name}' already exists")

        resolved = str(Path(path).resolve())

        if not skip_preflight:
            check = preflight_check(resolved)
            git_root = check["git_root"]
        else:
            git_root = None

        repo_id = self._db.create_repo(
            name,
            resolved,
            aliases=aliases,
            expected_git_root=git_root,
            model=model,
            max_concurrency=max_concurrency,
        )
        return {
            "id": repo_id,
            "name": name,
            "path": resolved,
            "git_root": git_root,
        }

    def resolve(self, name_or_alias: str) -> dict[str, Any]:
        """名前またはエイリアスからrepoを解決する。"""
        repo = self._db.get_repo_by_alias(name_or_alias)
        if repo is None:
            raise RegistryError(f"Repo not found: {name_or_alias}")
        return repo

    def list_all(self) -> list[dict[str, Any]]:
        return self._db.list_repos()

    def unregister(self, name: str) -> bool:
        return self._db.delete_repo(name)

    def validate(self, name: str) -> dict[str, Any]:
        """登録済みrepoのpreflight検証を再実行。"""
        repo = self.resolve(name)
        return preflight_check(repo["path"], repo.get("expected_git_root"))

    def export_json(self) -> str:
        """repos.jsonにエクスポート。"""
        repos = self.list_all()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        for r in repos:
            data[r["name"]] = {
                "path": r["path"],
                "aliases": r["aliases"],
                "model": r["model"],
                "max_concurrency": r["max_concurrency"],
                "expected_git_root": r.get("expected_git_root"),
            }
        with open(REPOS_JSON, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return str(REPOS_JSON)

    def import_json(self, json_path: str | None = None) -> int:
        """repos.jsonからインポート。"""
        path = Path(json_path) if json_path else REPOS_JSON
        if not path.exists():
            raise RegistryError(f"File not found: {path}")
        with open(path) as f:
            data = json.load(f)
        count = 0
        for name, info in data.items():
            if self._db.get_repo(name):
                continue
            self._db.create_repo(
                name,
                info["path"],
                aliases=info.get("aliases", []),
                expected_git_root=info.get("expected_git_root"),
                model=info.get("model", "sonnet"),
                max_concurrency=info.get("max_concurrency", 1),
            )
            count += 1
        return count
