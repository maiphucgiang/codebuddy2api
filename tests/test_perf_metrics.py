"""Process counters, routing instrumentation and the read-only management view."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import converter
from app import perf_metrics
from app import admin_account
from app.admin_api import install_admin
from app.control_store import ControlStore
from tests.admin_session import credentials


class PerfMetricsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(perf_metrics, "_COUNTERS", {}))
        self.enterContext(patch.object(perf_metrics, "_TIMINGS", {}))

    def test_counters_and_timings_accumulate(self):
        perf_metrics.inc("candidate_checks")
        perf_metrics.inc("candidate_checks", 4)
        perf_metrics.observe("route_pick_duration", 0.004)
        perf_metrics.observe("route_pick_duration", 0.002)
        with perf_metrics.timed("route_pick_duration"):
            pass
        snapshot = perf_metrics.snapshot()
        self.assertEqual(snapshot["counters"], {"candidate_checks": 5})
        timing = snapshot["timings"]["route_pick_duration"]
        self.assertEqual(timing["count"], 3)
        self.assertAlmostEqual(timing["total_ms"], 6.0, places=1)
        self.assertGreaterEqual(timing["max_ms"], 4.0)
        self.assertLessEqual(timing["min_ms"], 2.0)

    def test_malformed_sample_is_ignored_and_reset_clears(self):
        perf_metrics.observe("pool_lock_wait", "not-a-number")
        self.assertEqual(perf_metrics.snapshot()["timings"], {})
        perf_metrics.inc("catalog_fetch_failures")
        perf_metrics.reset()
        self.assertEqual(perf_metrics.snapshot()["counters"], {})


class RoutingInstrumentationTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": temporary}))
        self.enterContext(patch.object(perf_metrics, "_COUNTERS", {}))
        self.enterContext(patch.object(perf_metrics, "_TIMINGS", {}))
        self.root = Path(temporary)
        self.path = self.root / "account.info"
        self.path.write_text(json.dumps({"account": {"uid": "instrumented"}, "auth": {
            "accessToken": "synthetic-token", "domain": "www.codebuddy.cn",
            "expiresAt": (time.time() + 3600) * 1000}}), encoding="utf-8")
        self.pool = converter.CredentialPool([self.path])
        self.enterContext(patch.dict(converter.CONFIG, {"cred_pool": self.pool, "log_path": None,
                                                        "control_store": None, "model_cache": None,
                                                        "account_catalogs": None}))

    def test_pick_records_duration_lock_wait_and_candidate_work(self):
        self.assertIsNotNone(self.pool.pick("session", "glm-5.3-flash"))
        snapshot = perf_metrics.snapshot()
        self.assertGreaterEqual(snapshot["counters"]["candidate_checks"], 1)
        self.assertGreaterEqual(snapshot["counters"]["credential_summary_calls"], 1)
        self.assertGreaterEqual(snapshot["counters"]["credential_stat_calls"], 1)
        self.assertEqual(snapshot["timings"]["pool_lock_wait"]["count"], 1)
        self.assertEqual(snapshot["timings"]["route_pick_duration"]["count"], 1)

    def test_capacity_refusal_is_counted(self):
        with patch.dict(converter.CONFIG, {"max_inflight_per_account": 1}):
            self.pool.pick("session", "glm-5.3-flash")
            entry = self.pool._entries[0]
            self.pool._capacity.acquire(self.pool._capacity_key(entry), 1, entry["cm"], entry["cm"]._generation)
            with self.assertRaises(converter.HTTPException):
                self.pool.pick("session", "glm-5.3-flash", with_capacity=True)
        self.assertGreaterEqual(perf_metrics.snapshot()["counters"]["capacity_acquire_failures"], 1)



class AdminPerformanceViewTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = ControlStore(root / "control.sqlite3")
        self.addCleanup(self.store.close)
        admin_account.save(self.store, admin_account.new("admin", "synthetic-key", "user"))
        self.config = {"api_key": "synthetic-key", "control_store": self.store, "max_images": 16,
                       "session_path": root / "admin-sessions.json"}
        self.audit = Mock()
        self.audit.storage.return_value = {}
        self.config["audit_store"] = self.audit
        self.gateway = SimpleNamespace(managed_auth_dir=Mock(return_value=root))
        self.app = FastAPI()
        install_admin(self.app, self.config, self.gateway)
        self.client = self.enterContext(TestClient(self.app))

    def test_requires_admin_login_and_returns_counters_only(self):
        self.assertEqual(self.client.get("/admin/performance").status_code, 401)
        self.client.post("/admin/session", json=credentials("synthetic-key"),
                         headers={"Origin": "http://testserver"})
        perf_metrics.inc("route_pick_duration_marker")
        result = self.client.get("/admin/performance")
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(set(body), {"uptime_seconds", "counters", "timings", "scheduler"})
        self.assertGreaterEqual(body["counters"]["route_pick_duration_marker"], 1)
        self.assertNotIn("synthetic-key", result.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
