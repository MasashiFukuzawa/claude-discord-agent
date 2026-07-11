"""時刻表現パーサー: ISO 8601 / unix epoch / 相対時刻 (+7d, +1h, +30m, +60s)。

naive ISO 8601 はローカルタイムゾーンとして解釈する。
aware ISO 8601 (+09:00 / Z) は UTC 変換して epoch を返す。
DB には常に unix epoch (float) で保存する。
"""

from __future__ import annotations

import re
import time
from datetime import datetime

_RELATIVE_RE = re.compile(r"^\+(\d+(?:\.\d+)?)(w|d|h|m|s)$", re.IGNORECASE)

_UNIT_SECONDS: dict[str, float] = {
    "w": 7 * 24 * 3600,
    "d": 24 * 3600,
    "h": 3600,
    "m": 60,
    "s": 1,
}

_ISO_AWARE_FMTS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
)

_ISO_NAIVE_FMTS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def parse_enqueue_at(expr: str, *, now: float | None = None) -> float:
    """時刻表現を unix epoch (float) に変換する。

    サポート形式:
    - 相対: +7d, +1h, +30m, +60s, +2w (now 基準で加算)
    - ISO 8601 aware: 2026-05-03T14:00:00+09:00 / 2026-05-03T14:00:00Z
    - ISO 8601 naive: 2026-05-03T14:00:00 (ローカルTZ扱い)
    - unix epoch 数値文字列: "1746259200.0"

    Parameters
    ----------
    expr:
        時刻表現文字列
    now:
        テスト用の現在時刻上書き (省略時は time.time())

    Returns
    -------
    float: unix epoch

    Raises
    ------
    ValueError: パース不能
    """
    if now is None:
        now = time.time()

    expr = expr.strip()

    # 相対表現: +7d, +1h, +30m, +60s, +2w
    m = _RELATIVE_RE.match(expr)
    if m:
        amount = float(m.group(1))
        unit = m.group(2).lower()
        return now + amount * _UNIT_SECONDS[unit]

    # unix epoch (数値文字列)
    try:
        val = float(expr)
        if val > 1_000_000_000:
            return val
    except ValueError:
        pass

    # ISO 8601 aware (タイムゾーン付き)
    for fmt in _ISO_AWARE_FMTS:
        try:
            dt = datetime.strptime(expr, fmt)
            return dt.timestamp()
        except ValueError:
            pass

    # ISO 8601 naive → ローカルTZ として解釈
    for fmt in _ISO_NAIVE_FMTS:
        try:
            dt = datetime.strptime(expr, fmt)
            return dt.astimezone().timestamp()
        except ValueError:
            pass

    raise ValueError(f"Cannot parse time expression: {expr!r}")


def format_enqueue_at(epoch: float) -> str:
    """unix epoch を人間可読な文字列に変換する (ローカルTZ)。

    epoch <= 0 の場合は "immediate" を返す。
    """
    if epoch <= 0:
        return "immediate"
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
