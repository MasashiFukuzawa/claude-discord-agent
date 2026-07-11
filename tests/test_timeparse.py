"""lib.timeparse のテスト: 相対時刻 / ISO 8601 / epoch / エラー。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import UTC

from lib.timeparse import format_enqueue_at, parse_enqueue_at


class TestParseRelative(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000.0

    def test_seconds(self):
        result = parse_enqueue_at("+60s", now=self.now)
        self.assertAlmostEqual(result, self.now + 60, places=1)

    def test_minutes(self):
        result = parse_enqueue_at("+30m", now=self.now)
        self.assertAlmostEqual(result, self.now + 1800, places=1)

    def test_hours(self):
        result = parse_enqueue_at("+1h", now=self.now)
        self.assertAlmostEqual(result, self.now + 3600, places=1)

    def test_days(self):
        result = parse_enqueue_at("+7d", now=self.now)
        self.assertAlmostEqual(result, self.now + 7 * 86400, places=1)

    def test_weeks(self):
        result = parse_enqueue_at("+2w", now=self.now)
        self.assertAlmostEqual(result, self.now + 14 * 86400, places=1)

    def test_uppercase_unit(self):
        result = parse_enqueue_at("+1H", now=self.now)
        self.assertAlmostEqual(result, self.now + 3600, places=1)

    def test_decimal_amount(self):
        result = parse_enqueue_at("+1.5h", now=self.now)
        self.assertAlmostEqual(result, self.now + 5400, places=1)

    def test_leading_space_stripped(self):
        result = parse_enqueue_at("  +60s  ", now=self.now)
        self.assertAlmostEqual(result, self.now + 60, places=1)


class TestParseEpoch(unittest.TestCase):
    def test_epoch_string(self):
        val = 1_746_259_200.0
        result = parse_enqueue_at(str(val))
        self.assertAlmostEqual(result, val, places=1)

    def test_epoch_integer_string(self):
        val = 1_746_259_200
        result = parse_enqueue_at(str(val))
        self.assertAlmostEqual(result, float(val), places=1)


class TestParseISO8601(unittest.TestCase):
    def test_naive_datetime(self):
        from datetime import datetime

        expr = "2026-05-03T14:00:00"
        result = parse_enqueue_at(expr)
        dt = datetime.strptime(expr, "%Y-%m-%dT%H:%M:%S")
        expected = dt.astimezone().timestamp()
        self.assertAlmostEqual(result, expected, places=1)

    def test_aware_utc(self):
        from datetime import datetime

        expr = "2026-05-03T14:00:00+00:00"
        result = parse_enqueue_at(expr)
        dt = datetime(2026, 5, 3, 14, 0, 0, tzinfo=UTC)
        self.assertAlmostEqual(result, dt.timestamp(), places=1)

    def test_aware_offset(self):
        from datetime import datetime, timedelta, timezone

        expr = "2026-05-03T14:00:00+09:00"
        result = parse_enqueue_at(expr)
        jst = timezone(timedelta(hours=9))
        dt = datetime(2026, 5, 3, 14, 0, 0, tzinfo=jst)
        self.assertAlmostEqual(result, dt.timestamp(), places=1)

    def test_date_only(self):
        from datetime import datetime

        expr = "2026-05-03"
        result = parse_enqueue_at(expr)
        dt = datetime.strptime(expr, "%Y-%m-%d")
        expected = dt.astimezone().timestamp()
        self.assertAlmostEqual(result, expected, places=1)

    def test_aware_z_suffix(self):
        from datetime import datetime

        try:
            dt = datetime.strptime("2026-05-03T14:00:00Z", "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return  # Z suffix not supported on this Python version
        result = parse_enqueue_at("2026-05-03T14:00:00Z")
        self.assertAlmostEqual(result, dt.timestamp(), places=1)


class TestParseErrors(unittest.TestCase):
    def test_empty_string(self):
        with self.assertRaises(ValueError):
            parse_enqueue_at("")

    def test_invalid_string(self):
        with self.assertRaises(ValueError):
            parse_enqueue_at("next-monday")

    def test_small_number_not_epoch(self):
        with self.assertRaises(ValueError):
            parse_enqueue_at("12345")

    def test_missing_plus_prefix(self):
        with self.assertRaises(ValueError):
            parse_enqueue_at("7d")


class TestFormatEnqueueAt(unittest.TestCase):
    def test_zero_returns_immediate(self):
        self.assertEqual(format_enqueue_at(0), "immediate")

    def test_negative_returns_immediate(self):
        self.assertEqual(format_enqueue_at(-1.0), "immediate")

    def test_future_epoch_returns_string(self):
        epoch = time.time() + 3600
        result = format_enqueue_at(epoch)
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "immediate")
        self.assertRegex(result, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


if __name__ == "__main__":
    unittest.main()
