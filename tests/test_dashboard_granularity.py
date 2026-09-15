"""Dashboard bucket regressions using isolated persistent aggregates."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from app.audit_store import AuditStore


class GranularityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.start = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()
        self.enterContext(patch("app.audit_store.time.time", return_value=self.start + 12.5 * 3600))
        self.store = AuditStore(self.root / "logs.sqlite3")
        self.addCleanup(self.store.close)
        for hour in (2, 9):
            result = self.store.record_request({"id": f"hour-{hour}", **self.store.ticket(),
                "started_at": self.start + hour * 3600, "public_model": "synthetic-model",
                "profile": "cn-cli", "protocol": "chat", "status_code": 200, "outcome": "success"})
            self.assertTrue(result["ok"])

    def test_one_day_auto_uses_real_hours_without_changing_summary(self):
        hourly = self.store.dashboard(1)
        daily = self.store.dashboard(1, "day")
        self.assertEqual(hourly["range"]["granularity"], "hour")
        self.assertEqual(hourly["range"]["timezone"], "UTC")
        self.assertEqual(len(hourly["series"]), 13)
        self.assertEqual(len(daily["series"]), 1)
        self.assertEqual(hourly["summary"], daily["summary"])
        self.assertEqual([row["bucket"] for row in hourly["series"] if row["requests"]],
                         [self.start + 2 * 3600, self.start + 9 * 3600])
        self.assertEqual(sum(row["requests"] for row in hourly["series"]), 2)
        self.assertIsNone(hourly["series"][0]["credit"])
        self.assertEqual(hourly["series"][0]["requests"], 0)
        self.assertLessEqual(hourly["series"][-1]["bucket"], hourly["range"]["end"])

    def test_granularity_is_explicit_and_bounded(self):
        self.assertEqual(self.store.dashboard(7)["range"]["granularity"], "day")
        self.assertEqual(self.store.dashboard(7, "hour")["range"]["granularity"], "hour")
        self.assertLessEqual(len(self.store.dashboard(90, "hour")["series"]), 90 * 24)
        for grain in ("minute", "stats_hourly; DROP TABLE requests", None):
            with self.subTest(grain=grain), self.assertRaises(ValueError):
                self.store.dashboard(1, grain)
        with self.assertRaises(ValueError):
            self.store.dashboard(91, "hour")

    def test_detail_clear_and_restart_retain_hourly_stats(self):
        before = self.store.dashboard(1)
        self.store.clear("details")
        reopened = AuditStore(self.root / "logs.sqlite3")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.dashboard(1)["series"], before["series"])
        self.assertEqual(reopened.list_records()["items"], [])
        reopened.clear("all")
        self.assertEqual(sum(row["requests"] for row in reopened.dashboard(1)["series"]), 0)

    def test_missing_hourly_history_is_marked_incomplete_not_split_from_daily(self):
        self.store._db.execute("DELETE FROM stats_hourly")
        result = self.store.dashboard(1, "hour")
        self.assertTrue(result["range"]["partial"])
        self.assertEqual(result["series"], [])
        self.assertEqual(result["summary"]["requests"], 2)
        self.assertFalse(self.store.dashboard(1, "day")["range"]["partial"])


if __name__ == "__main__":
    unittest.main()
