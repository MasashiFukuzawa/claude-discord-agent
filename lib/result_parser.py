"""Worker output parser for structured result extraction.

Worker stdout may contain free-form prose followed by a structured JSON block:

    ... 自由作文 ...
    <<<DISCORD_AGENT_RESULT>>>
    {"status": "succeeded", ...}
    <<<END>>>

`parse_result()` extracts and deserializes the JSON block.
`validate()` returns a list of schema violations (empty = valid).

If no delimiter is found, returns None (backward-compatible).
The *last* occurrence of the opening delimiter is used (rfind) so that
workers safely mention the delimiter string in their prose section.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

DELIMITER_OPEN = "<<<DISCORD_AGENT_RESULT>>>"
DELIMITER_CLOSE = "<<<END>>>"

StatusLiteral = Literal["succeeded", "halt", "needs_controller"]


@dataclass
class AdvisorRound:
    round: int
    verdict: Literal["approved", "with_changes"]
    key_findings: list[str] = field(default_factory=list)


@dataclass
class Result:
    status: str
    merge_sha: str | None
    pr_url: str | None
    files_changed: list[str]
    advisor_rounds: list[AdvisorRound]
    next_action_hint: str | None
    unresolved: list[str]
    error_summary: str | None

    def to_dict(self) -> dict:
        return asdict(self)


_REQUIRED_KEYS = {
    "status",
    "merge_sha",
    "pr_url",
    "files_changed",
    "advisor_rounds",
    "next_action_hint",
    "unresolved",
    "error_summary",
}
_VALID_STATUSES = {"succeeded", "halt", "needs_controller"}


def parse_result(text: str, strict: bool = False) -> Result | None:
    """Extract structured Result from worker stdout.

    Uses rfind so that worker prose mentioning the delimiter is safe.
    Returns None when no delimiter block is found (backward-compatible).
    Raises ValueError only when strict=True and JSON/schema is invalid.
    """
    open_pos = text.rfind(DELIMITER_OPEN)
    if open_pos == -1:
        return None

    after_open = text[open_pos + len(DELIMITER_OPEN) :]
    close_pos = after_open.find(DELIMITER_CLOSE)
    if close_pos == -1:
        json_text = after_open.strip()
    else:
        json_text = after_open[:close_pos].strip()

    if not json_text:
        if strict:
            raise ValueError("Empty JSON block between delimiters")
        return None

    try:
        raw = json.loads(json_text)
    except json.JSONDecodeError as exc:
        if strict:
            raise ValueError(f"Invalid JSON in result block: {exc}") from exc
        return None

    if not isinstance(raw, dict):
        if strict:
            raise ValueError("Result block must be a JSON object")
        return None

    errors = _validate_raw(raw)
    if errors and strict:
        raise ValueError("Schema validation failed: " + "; ".join(errors))

    advisor_rounds = []
    for item in raw.get("advisor_rounds") or []:
        if isinstance(item, dict):
            advisor_rounds.append(
                AdvisorRound(
                    round=int(item.get("round", 0)),
                    verdict=item.get("verdict", "approved"),
                    key_findings=list(item.get("key_findings") or []),
                )
            )

    def _to_list(val) -> list:
        return list(val) if isinstance(val, list) else []

    return Result(
        status=raw.get("status", ""),
        merge_sha=raw.get("merge_sha"),
        pr_url=raw.get("pr_url"),
        files_changed=_to_list(raw.get("files_changed")),
        advisor_rounds=advisor_rounds,
        next_action_hint=raw.get("next_action_hint"),
        unresolved=_to_list(raw.get("unresolved")),
        error_summary=raw.get("error_summary"),
    )


def validate(result: Result) -> list[str]:
    """Return a list of schema violation messages (empty = valid)."""
    return _validate_raw(result.to_dict())


def _validate_raw(raw: dict) -> list[str]:
    errors: list[str] = []

    missing = _REQUIRED_KEYS - raw.keys()
    for key in sorted(missing):
        errors.append(f"missing required field: {key!r}")

    status = raw.get("status", "")
    if status not in _VALID_STATUSES:
        errors.append(f"invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}")

    for field_name in ("files_changed", "advisor_rounds", "unresolved"):
        val = raw.get(field_name)
        if val is not None and not isinstance(val, list):
            errors.append(f"{field_name!r} must be a list")

    for i, ar in enumerate(raw.get("advisor_rounds") or []):
        if not isinstance(ar, dict):
            errors.append(f"advisor_rounds[{i}] must be an object")
            continue
        if "round" not in ar:
            errors.append(f"advisor_rounds[{i}] missing 'round'")
        if ar.get("verdict") not in ("approved", "with_changes"):
            errors.append(f"advisor_rounds[{i}] invalid verdict {ar.get('verdict')!r}")

    return errors
