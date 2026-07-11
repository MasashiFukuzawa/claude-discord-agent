"""Tests for lib/result_parser.py — structured worker output parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.result_parser import (
    DELIMITER_CLOSE,
    DELIMITER_OPEN,
    AdvisorRound,
    parse_result,
    validate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_json(**overrides) -> str:
    base = {
        "status": "succeeded",
        "merge_sha": "abc1234",
        "pr_url": "https://github.com/example/repo/pull/1",
        "files_changed": ["lib/foo.py"],
        "advisor_rounds": [{"round": 1, "verdict": "approved", "key_findings": ["OK"]}],
        "next_action_hint": None,
        "unresolved": [],
        "error_summary": None,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def _wrap(json_str: str, prose: str = "作業完了しました。") -> str:
    return f"{prose}\n{DELIMITER_OPEN}\n{json_str}\n{DELIMITER_CLOSE}\n"


# ---------------------------------------------------------------------------
# Normal cases
# ---------------------------------------------------------------------------


class TestParseResultNormal:
    def test_basic_succeeded(self):
        text = _wrap(_make_json())
        result = parse_result(text)
        assert result is not None
        assert result.status == "succeeded"
        assert result.merge_sha == "abc1234"
        assert result.pr_url == "https://github.com/example/repo/pull/1"
        assert result.files_changed == ["lib/foo.py"]
        assert len(result.advisor_rounds) == 1
        assert result.advisor_rounds[0].round == 1
        assert result.advisor_rounds[0].verdict == "approved"
        assert result.advisor_rounds[0].key_findings == ["OK"]
        assert result.next_action_hint is None
        assert result.unresolved == []
        assert result.error_summary is None

    def test_halt_status(self):
        result = parse_result(_wrap(_make_json(status="halt")))
        assert result is not None
        assert result.status == "halt"

    def test_needs_controller_status(self):
        result = parse_result(_wrap(_make_json(status="needs_controller")))
        assert result is not None
        assert result.status == "needs_controller"

    def test_null_fields(self):
        result = parse_result(_wrap(_make_json(merge_sha=None, pr_url=None)))
        assert result is not None
        assert result.merge_sha is None
        assert result.pr_url is None

    def test_empty_advisor_rounds(self):
        result = parse_result(_wrap(_make_json(advisor_rounds=[])))
        assert result is not None
        assert result.advisor_rounds == []

    def test_multiple_advisor_rounds(self):
        rounds = [
            {"round": 1, "verdict": "with_changes", "key_findings": ["issue A"]},
            {"round": 2, "verdict": "approved", "key_findings": ["all good"]},
        ]
        result = parse_result(_wrap(_make_json(advisor_rounds=rounds)))
        assert result is not None
        assert len(result.advisor_rounds) == 2
        assert result.advisor_rounds[0].verdict == "with_changes"
        assert result.advisor_rounds[1].verdict == "approved"

    def test_files_changed_multiple(self):
        files = ["lib/a.py", "lib/b.py", "tests/test_a.py"]
        result = parse_result(_wrap(_make_json(files_changed=files)))
        assert result is not None
        assert result.files_changed == files

    def test_to_dict_roundtrip(self):
        text = _wrap(_make_json())
        result = parse_result(text)
        assert result is not None
        d = result.to_dict()
        assert d["status"] == "succeeded"
        assert "advisor_rounds" in d
        assert isinstance(d["advisor_rounds"], list)

    def test_japanese_content(self):
        text = _wrap(_make_json(next_action_hint="次はテストを実行してください"))
        result = parse_result(text)
        assert result is not None
        assert result.next_action_hint == "次はテストを実行してください"

    def test_unresolved_list(self):
        result = parse_result(_wrap(_make_json(unresolved=["懸念A", "懸念B"])))
        assert result is not None
        assert result.unresolved == ["懸念A", "懸念B"]


# ---------------------------------------------------------------------------
# Delimiter handling
# ---------------------------------------------------------------------------


class TestDelimiterHandling:
    def test_no_delimiter_returns_none(self):
        assert parse_result("作業完了しました。結果はありません。") is None

    def test_no_delimiter_strict_returns_none(self):
        # strict=True only raises on malformed JSON/schema, not on missing delimiter
        assert parse_result("no delimiter here", strict=True) is None

    def test_open_without_close_uses_rest(self):
        """If <<<END>>> is missing, use everything after the opening delimiter."""
        json_str = _make_json()
        text = f"prose\n{DELIMITER_OPEN}\n{json_str}\n"
        result = parse_result(text)
        assert result is not None
        assert result.status == "succeeded"

    def test_rfind_uses_last_occurrence(self):
        """When delimiter appears twice, use the last one."""
        json_str_first = json.dumps(
            {
                "status": "halt",
                "merge_sha": None,
                "pr_url": None,
                "files_changed": [],
                "advisor_rounds": [],
                "next_action_hint": None,
                "unresolved": [],
                "error_summary": None,
            }
        )
        json_str_last = _make_json()
        text = (
            f"In this prose I mention {DELIMITER_OPEN} as an example.\n"
            f"{DELIMITER_OPEN}\n{json_str_first}\n{DELIMITER_CLOSE}\n"
            f"More prose\n"
            f"{DELIMITER_OPEN}\n{json_str_last}\n{DELIMITER_CLOSE}\n"
        )
        result = parse_result(text)
        assert result is not None
        # Must use the LAST occurrence → "succeeded"
        assert result.status == "succeeded"

    def test_delimiter_in_prose_is_safe(self):
        """Worker mentioning the delimiter string in prose should not confuse parser."""
        prose = f"作業完了。なお {DELIMITER_OPEN} という区切り文字列をこのジョブで実装しました。"
        text = f"{prose}\n{DELIMITER_OPEN}\n{_make_json()}\n{DELIMITER_CLOSE}\n"
        result = parse_result(text)
        assert result is not None
        assert result.status == "succeeded"

    def test_empty_json_block_lenient(self):
        text = f"prose\n{DELIMITER_OPEN}\n\n{DELIMITER_CLOSE}\n"
        assert parse_result(text) is None

    def test_empty_json_block_strict(self):
        text = f"prose\n{DELIMITER_OPEN}\n\n{DELIMITER_CLOSE}\n"
        with pytest.raises(ValueError, match="Empty JSON"):
            parse_result(text, strict=True)


# ---------------------------------------------------------------------------
# Invalid JSON
# ---------------------------------------------------------------------------


class TestInvalidJson:
    def test_invalid_json_lenient(self):
        text = f"prose\n{DELIMITER_OPEN}\nnot valid json\n{DELIMITER_CLOSE}\n"
        assert parse_result(text) is None

    def test_invalid_json_strict(self):
        text = f"prose\n{DELIMITER_OPEN}\nnot valid json\n{DELIMITER_CLOSE}\n"
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_result(text, strict=True)

    def test_json_array_instead_of_object_lenient(self):
        text = f"prose\n{DELIMITER_OPEN}\n[1, 2, 3]\n{DELIMITER_CLOSE}\n"
        assert parse_result(text) is None

    def test_json_array_strict(self):
        text = f"prose\n{DELIMITER_OPEN}\n[1, 2, 3]\n{DELIMITER_CLOSE}\n"
        with pytest.raises(ValueError, match="JSON object"):
            parse_result(text, strict=True)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_valid_result_no_errors(self):
        result = parse_result(_wrap(_make_json()))
        assert result is not None
        errors = validate(result)
        assert errors == []

    def test_missing_fields_lenient(self):
        # Lenient mode: parse succeeds, missing fields get defaults
        incomplete = {"status": "succeeded"}
        text = f"prose\n{DELIMITER_OPEN}\n{json.dumps(incomplete)}\n{DELIMITER_CLOSE}\n"
        result = parse_result(text)
        assert result is not None
        assert result.status == "succeeded"
        assert result.merge_sha is None
        assert result.files_changed == []

    def test_missing_fields_strict(self):
        # Strict mode: missing required fields → ValueError
        incomplete = {"status": "succeeded"}
        text = f"prose\n{DELIMITER_OPEN}\n{json.dumps(incomplete)}\n{DELIMITER_CLOSE}\n"
        with pytest.raises(ValueError, match="Schema validation failed"):
            parse_result(text, strict=True)

    def test_invalid_status_caught_by_validate(self):
        result = parse_result(_wrap(_make_json(status="invalid_status")))
        assert result is not None
        errors = validate(result)
        assert any("invalid status" in e for e in errors)

    def test_invalid_status_strict(self):
        text = _wrap(_make_json(status="invalid_status"))
        with pytest.raises(ValueError, match="Schema validation failed"):
            parse_result(text, strict=True)

    def test_invalid_advisor_round_verdict(self):
        rounds = [{"round": 1, "verdict": "unknown_verdict", "key_findings": []}]
        result = parse_result(_wrap(_make_json(advisor_rounds=rounds)))
        assert result is not None
        errors = validate(result)
        assert any("verdict" in e for e in errors)

    def test_advisor_round_missing_round_field(self):
        # Strict mode catches missing 'round' in raw dict before Result construction
        rounds = [{"verdict": "approved", "key_findings": []}]
        text = _wrap(_make_json(advisor_rounds=rounds))
        with pytest.raises(ValueError, match="Schema validation failed"):
            parse_result(text, strict=True)
        # Lenient: defaults round to 0
        result = parse_result(text)
        assert result is not None
        assert result.advisor_rounds[0].round == 0

    def test_files_changed_not_list_strict(self):
        # Strict mode catches non-list files_changed in raw dict
        incomplete = json.dumps(
            {
                "status": "succeeded",
                "merge_sha": None,
                "pr_url": None,
                "files_changed": "should_be_list",
                "advisor_rounds": [],
                "next_action_hint": None,
                "unresolved": [],
                "error_summary": None,
            }
        )
        text = f"prose\n{DELIMITER_OPEN}\n{incomplete}\n{DELIMITER_CLOSE}\n"
        with pytest.raises(ValueError, match="Schema validation failed"):
            parse_result(text, strict=True)

    def test_files_changed_not_list_lenient(self):
        # Lenient mode: non-list files_changed falls back to [] (not coerced to chars)
        incomplete = json.dumps(
            {
                "status": "succeeded",
                "merge_sha": None,
                "pr_url": None,
                "files_changed": "should_be_list",
                "advisor_rounds": [],
                "next_action_hint": None,
                "unresolved": [],
                "error_summary": None,
            }
        )
        text = f"prose\n{DELIMITER_OPEN}\n{incomplete}\n{DELIMITER_CLOSE}\n"
        result = parse_result(text)
        assert result is not None
        # non-list falls back to [] instead of silently becoming char list
        assert result.files_changed == []


# ---------------------------------------------------------------------------
# Backward compatibility (old worker output — no delimiter)
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_old_free_form_output(self):
        old_output = "バグを修正しました。3ファイル変更。テスト全通過。"
        assert parse_result(old_output) is None

    def test_old_markdown_output(self):
        old_output = "## 結果\n- ファイル修正: `lib/foo.py`\n- テスト: 全通過\n"
        assert parse_result(old_output) is None

    def test_old_output_does_not_raise(self):
        # Must not raise even in strict mode — backward compat requires None not exception
        old_output = "修正完了しました。"
        result = parse_result(old_output, strict=True)
        assert result is None


# ---------------------------------------------------------------------------
# AdvisorRound dataclass
# ---------------------------------------------------------------------------


class TestAdvisorRound:
    def test_defaults(self):
        ar = AdvisorRound(round=1, verdict="approved")
        assert ar.key_findings == []

    def test_to_dict_via_result(self):
        rounds = [{"round": 1, "verdict": "with_changes", "key_findings": ["fix X"]}]
        result = parse_result(_wrap(_make_json(advisor_rounds=rounds)))
        assert result is not None
        d = result.to_dict()
        assert d["advisor_rounds"][0]["verdict"] == "with_changes"
        assert d["advisor_rounds"][0]["key_findings"] == ["fix X"]
