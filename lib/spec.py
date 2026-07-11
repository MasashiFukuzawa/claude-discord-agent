"""Spec プレーン: dispatch prompt template の読み込み・検証・レンダリング。

フォーマット: Markdown body + TOML frontmatter（Python 3.11+ stdlib tomllib）
補間: string.Template ${var} スタイル（stdlib、外部依存なし）
設計判断: PyYAML / jinja2 を使わない（pyproject.toml の dependencies=[] 制約を維持するため）

spec ファイル構造:
    ---toml
    id = "my-spec"
    description = "説明"
    required_vars = ["branch", "pr_number"]
    optional_vars = ["extra_context"]
    ---
    タスク指示の本文。${branch} のように変数を埋め込む。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from string import Template
from typing import Any

SPECS_DIR = Path(__file__).parent.parent / "specs"

_FRONTMATTER_RE = re.compile(r"^---toml\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


class SpecError(Exception):
    """Spec 読み込み・検証・レンダリングエラー。"""


class Spec:
    """読み込み済みの spec エントリ。"""

    def __init__(
        self,
        *,
        spec_id: str,
        description: str,
        task_template: str,
        required_vars: list[str],
        optional_vars: list[str],
        raw_meta: dict[str, Any],
    ) -> None:
        self.id = spec_id
        self.description = description
        self.task_template = task_template
        self.required_vars = required_vars
        self.optional_vars = optional_vars
        self.raw_meta = raw_meta

    def render(self, vars: dict[str, str]) -> str:
        """変数を補間してタスク文字列を返す。

        required_vars が不足している場合は SpecError を送出。
        render 後に未置換の ${...} が残る場合も SpecError。
        """
        missing = [v for v in self.required_vars if v not in vars]
        if missing:
            raise SpecError(f"Missing required vars: {', '.join(missing)}")

        # optional_vars が未指定の場合は空文字で置換
        filled: dict[str, str] = {}
        for v in self.optional_vars:
            filled[v] = vars.get(v, "")
        filled.update(vars)

        try:
            result = Template(self.task_template).substitute(filled)
        except (KeyError, ValueError) as e:
            raise SpecError(f"Template substitution failed: {e}") from e

        # 未置換の ${...} が残っていないか確認
        remaining = re.findall(r"\$\{[^}]+\}", result)
        if remaining:
            raise SpecError(f"Unresolved template vars after render: {remaining}")

        return result

    def validate(self) -> None:
        """構造検証: 必須フィールドの存在チェック。"""
        if not self.id:
            raise SpecError("Spec 'id' is required")
        if not self.description:
            raise SpecError("Spec 'description' is required")
        if not self.task_template:
            raise SpecError("Spec 'task_template' (body) is required")
        if not isinstance(self.required_vars, list):
            raise SpecError("Spec 'required_vars' must be a list")
        if not isinstance(self.optional_vars, list):
            raise SpecError("Spec 'optional_vars' must be a list")


def _parse_spec_file(path: Path) -> Spec:
    """spec ファイルをパースして Spec を返す。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SpecError(f"Cannot read spec file {path}: {e}") from e

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise SpecError(
            f"Spec file {path} has no TOML frontmatter. "
            "Expected format: ---toml\\n...\\n---\\n<body>"
        )

    toml_src = m.group(1)
    body = m.group(2).strip()

    try:
        meta: dict[str, Any] = tomllib.loads(toml_src)
    except Exception as e:
        raise SpecError(f"TOML parse error in {path}: {e}") from e

    spec_id = meta.get("id", "")
    description = meta.get("description", "")
    required_vars: list[str] = meta.get("required_vars", [])
    optional_vars: list[str] = meta.get("optional_vars", [])

    spec = Spec(
        spec_id=spec_id,
        description=description,
        task_template=body,
        required_vars=required_vars,
        optional_vars=optional_vars,
        raw_meta=meta,
    )
    spec.validate()
    return spec


def load_spec(spec_id: str, search_dirs: list[Path] | None = None) -> Spec:
    """spec_id から spec を検索・ロードして返す。

    検索順:
    1. spec_id が絶対パスなら直接読む
    2. search_dirs（指定なければ SPECS_DIR）を順に検索
       - <dir>/<spec_id>.md
       - <dir>/<spec_id> (拡張子なし)
    """
    if Path(spec_id).is_absolute():
        return _parse_spec_file(Path(spec_id))

    dirs = search_dirs if search_dirs is not None else [SPECS_DIR]
    candidates: list[Path] = []
    for d in dirs:
        candidates.append(d / f"{spec_id}.md")
        candidates.append(d / spec_id)

    for c in candidates:
        if c.exists():
            return _parse_spec_file(c)

    searched = ", ".join(str(c) for c in candidates)
    raise SpecError(f"Spec '{spec_id}' not found. Searched: {searched}")


def list_specs(search_dirs: list[Path] | None = None) -> list[dict[str, str]]:
    """利用可能な spec 一覧を返す。"""
    dirs = search_dirs if search_dirs is not None else [SPECS_DIR]
    results: list[dict[str, str]] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                spec = _parse_spec_file(p)
                results.append(
                    {
                        "id": spec.id,
                        "description": spec.description,
                        "file": str(p),
                    }
                )
            except SpecError:
                pass
    return results
