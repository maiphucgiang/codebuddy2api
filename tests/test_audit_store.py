"""Audit regressions: isolated temporary databases, no app/server imports."""
import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.audit_store import SCHEMA_VERSION, AuditStore, _CLEANUP_BATCH


class ArchiveAndStorageStatsTests(unittest.TestCase):
    """Archive exports old rows before deleting them; storage reports reclaimable pages."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "audit.sqlite3"
        self.store = AuditStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, id, started_at, duration_ms=100):
        return {"id": id, **self.store.ticket(), "started_at": started_at,
                "public_model": "m", "upstream_model": "u", "profile": "p",
                "credential": "c", "protocol": "chat", "status_code": 200,
                "duration_ms": duration_ms, "outcome": "success"}

    def test_archive_exports_then_deletes_and_keeps_recent_rows(self):
        old = time.time() - 10 * 86400
        self.store.record_request(self.record("old", old, duration_ms=200))
        self.store.record_request(self.record("new", time.time(), duration_ms=100))
        self.store.event("runtime", "rotate", {"note": "old"})
        self.assertEqual(self.store.dashboard(30)["summary"]["p95_duration_ms"], 200)
        result = self.store.archive(7)
        self.assertTrue(result["ok"])
        self.assertEqual(result["archived_requests"], 1)
        self.assertEqual(result["archived_events"], 0)  # the event is fresh
        archive = self.path.parent / "archives" / result["file"]
        lines = archive.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], "old")
        self.assertEqual(oct(archive.stat().st_mode & 0o777), "0o600")
        self.assertIsNone(self.store.get_request("old"))
        self.assertIsNotNone(self.store.get_request("new"))
        # Aggregates survive, replay dedup stays intact, and partial detail samples hide P95.
        replay = self.store.record_request(self.record("old", old, duration_ms=200))
        self.assertEqual(replay, {"ok": True, "recorded": False, "reason": "duplicate"})
        summary = self.store.dashboard(30)["summary"]
        self.assertEqual(summary["requests"], 2)
        self.assertIsNone(summary["p95_duration_ms"])

    def test_archive_empty_window_is_a_noop_without_a_file(self):
        self.store.record_request(self.record("fresh", time.time()))
        result = self.store.archive(7)
        self.assertTrue(result["ok"])
        self.assertEqual(result["archived_requests"], 0)
        self.assertIsNone(result["file"])
        self.assertFalse((self.path.parent / "archives").exists())

    def test_archive_rejects_invalid_windows(self):
        for days in (0, -1, 3651, 1.5, "30", True):
            with self.assertRaises(ValueError):
                self.store.archive(days)

    def test_storage_reports_reclaimable_bytes(self):
        storage = self.store.storage()
        self.assertIn("reclaimable_bytes", storage)
        self.assertGreaterEqual(storage["reclaimable_bytes"], 0)
        self.assertGreater(storage["page_count"], 0)
        for _ in range(50):
            self.store.record_request(self.record(f"bulk-{_}", time.time()))
        self.store.clear("details")
        self.assertGreater(self.store.storage()["reclaimable_bytes"], 0)


class AuditStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "audit.sqlite3"
        self.store = AuditStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, id="r1", **values):
        return {"id": id, **self.store.ticket(), "started_at": time.time(),
                "public_model": "test-model", "upstream_model": "upstream-model",
                "profile": "test-profile", "credential": "account-fingerprint",
                "protocol": "chat", "status_code": 200, "outcome": "success",
                **values}

    def test_zero_and_unknown_are_distinct_and_no_double_count(self):
        self.assertTrue(self.store.record_request(self.record())['ok'])
        self.store.record_request(self.record("r2", input_tokens=10, output_tokens=5,
                                             total_tokens=15, cache_read_tokens=8,
                                             reasoning_tokens=3, credit=0))
        first = self.store.get_request("r1")
        self.assertIsNone(first["credit"])
        self.assertIsNone(first["total_tokens"])
        summary = self.store.dashboard()["summary"]
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["credit"], 0)
        self.assertEqual(summary["credit_known"], 1)
        self.assertEqual(summary["total_tokens"], 15)
        self.assertEqual(summary["cache_read_tokens"], 8)
        self.assertEqual(summary["reasoning_tokens"], 3)
        self.assertIsNone(self.store.get_request("r2")["first_token_ms"])

    def test_dedup_survives_clear_details_and_restart(self):
        record = self.record()
        self.store.record_request(record)
        self.store.clear("details")
        self.assertIsNone(self.store.get_request("r1"))
        self.store.close()
        self.store = AuditStore(self.path)
        self.assertEqual(self.store.record_request(record)["reason"], "duplicate")
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 1)
        self.assertEqual(self.store.storage()["ingest_count"], 1)

    def test_inflight_details_clear_keeps_only_aggregate(self):
        pending = self.record("pending")
        self.store.clear("details")
        result = self.store.record_request(pending)
        self.assertTrue(result["recorded"])
        self.assertFalse(result["details"])
        self.assertIsNone(self.store.get_request("pending"))
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 1)
        self.store.record_request(self.record("new"))
        self.assertIsNotNone(self.store.get_request("new"))

    def test_epoch_only_compatibility_clear_watermark(self):
        pending = self.record("pending")
        pending.pop("detail_generation")
        self.store.clear()
        self.store.record_request(pending)
        self.assertIsNone(self.store.get_request("pending"))
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 1)

    def test_all_clear_rejects_old_inflight_and_advances_epoch(self):
        pending = self.record()
        self.store.record_request(self.record("before"))
        result = self.store.clear("all")
        self.assertEqual(result["epoch"], pending["epoch"] + 1)
        self.assertEqual(self.store.record_request(pending)["reason"], "stale_epoch")
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 0)
        self.assertEqual(self.store.storage()["ingest_count"], 0)
        self.store.record_request(self.record("r1"))
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 1)

    def test_all_dimensions_and_periods_persist_without_details(self):
        self.store.record_request(self.record(input_tokens=0))
        self.store.clear("details")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            for table in ("stats_hourly", "stats_daily", "stats_totals"):
                rows = db.execute(f"SELECT dimension,payload FROM {table}").fetchall()
                self.assertEqual({r[0] for r in rows}, {"global", "model", "profile", "credential"})
                self.assertTrue(all(json.loads(r[1])["requests"] == 1 for r in rows))

    def test_atomic_rollback_and_visible_failure(self):
        with patch.object(self.store, "_aggregate", side_effect=sqlite3.OperationalError("sensitive-secret")):
            result = self.store.record_request(self.record())
        self.assertFalse(result["ok"])
        self.assertIsNone(self.store.get_request("r1"))
        health = self.store.storage()
        self.assertTrue(health["degraded"])
        self.assertEqual(health["failure_count"], 1)
        self.assertEqual(health["dropped_records"], 1)
        self.assertEqual(health["last_error"], "OperationalError")
        self.assertEqual(health["ingest_count"], 0)
        self.assertTrue(self.store.record_request(self.record())["recorded"])
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 1)

    def test_sqlite_busy_has_bounded_wait_and_no_partial_ingest(self):
        record = self.record()
        with closing(sqlite3.connect(self.path)) as other:
            other.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            result = self.store.record_request(record)
            elapsed = time.monotonic() - start
        self.assertFalse(result["ok"])
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.store.storage()["ingest_count"], 0)
        self.assertTrue(self.store.storage()["degraded"])

    def test_thread_lock_timeout_is_transient_not_degraded(self):
        # Contention is expected under load; it must never latch the 24h banner.
        record = self.record()
        with self.store._lock:
            start = time.monotonic()
            result = self.store.record_request(record)
        self.assertLess(time.monotonic() - start, 1)
        self.assertFalse(result["ok"])
        health = self.store.storage()
        self.assertFalse(health["degraded"])
        self.assertEqual(health["transient_contention"], 1)
        self.assertEqual(health["dropped_records"], 1)
        self.assertIsNone(health["last_error"])

    def test_transient_fault_degrades_only_within_window(self):
        with patch.object(self.store, "_aggregate", side_effect=sqlite3.OperationalError("boom")):
            self.assertFalse(self.store.record_request(self.record())["ok"])
        health = self.store.storage()
        self.assertTrue(health["degraded"])
        self.assertEqual(health["recent_failures"], 1)
        self.assertEqual(health["failure_count"], 1)
        self.assertGreater(health["last_failure_at"], 0)
        # 窗口之外：横幅消失，但终身计数与最近异常仍如实保留。
        self.store.failure_events = [time.monotonic() - 25 * 3600]
        health = self.store.storage()
        self.assertFalse(health["degraded"])
        self.assertEqual(health["recent_failures"], 0)
        self.assertEqual(health["failure_count"], 1)
        self.assertEqual(health["dropped_records"], 1)
        self.assertEqual(health["last_error"], "OperationalError")

    def test_reads_do_not_queue_behind_write_transactions(self):
        # The WebUI fires parallel dashboard/log/storage reads while inference
        # writes run; reads take a WAL snapshot, never the write lock.
        for i in range(3):
            self.assertTrue(self.store.record_request(
                self.record(f"r{i}", started_at=time.time() - i, duration_ms=100 + i))["recorded"])
        with self.store._lock:  # a write transaction in flight
            self.assertEqual(self.store.ticket()["epoch"], 0)
            self.assertEqual(self.store.dashboard(30)["summary"]["requests"], 3)
            self.assertEqual(self.store.get_request("r0")["id"], "r0")
            self.assertEqual(len(self.store.list_records("request", 10)["items"]), 3)
            self.assertFalse(self.store.storage()["degraded"])
        # Writers and readers share no lock; a fresh record commits immediately.
        self.assertTrue(self.store.record_request(
            self.record("w0", started_at=time.time()))["recorded"])
        self.assertFalse(self.store.storage()["degraded"])

    def test_dashboard_view_storm_does_not_drop_traffic_records(self):
        # Regression: parallel dashboard queries used to steal the 250ms write
        # lock budget of live traffic and latch a day of false storage alarms.
        now = time.time()
        payload = json.dumps({"id": "seed", "duration_ms": 5})
        with self.store._lock:
            self.store._db.execute("PRAGMA synchronous=OFF")  # bulk seed only
            self.store._db.execute("BEGIN IMMEDIATE")
            self.store._db.executemany(
                "INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(f"s{i}", now - i, "m", "p", "c", "success", 200, payload, 64, None, 5.0)
                 for i in range(20000)])
            self.store._db.execute("COMMIT")
            self.store._db.execute("PRAGMA synchronous=FULL")

        def traffic(tag):
            dropped = 0
            for i in range(20):
                if not self.store.record_request(
                        self.record(f"{tag}{i}", started_at=time.time()))["ok"]:
                    dropped += 1
            return dropped

        with concurrent.futures.ThreadPoolExecutor(5) as pool:
            futures = [pool.submit(self.store.dashboard, 30),
                       pool.submit(self.store.dashboard, 1, "hour"),
                       pool.submit(self.store.storage), pool.submit(traffic, "a"),
                       pool.submit(traffic, "b")]
            dashboards = [futures[0].result(), futures[1].result()]
            dropped = futures[3].result() + futures[4].result()
        self.assertIsNone(dashboards[0].get("degraded"))
        self.assertIsNone(dashboards[1].get("degraded"))
        self.assertEqual(dropped, 0)
        health = self.store.storage()
        self.assertFalse(health["degraded"])
        self.assertEqual(health["dropped_records"], 0)

    def test_v3_duration_column_backfills_and_p95_uses_covered_index(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audit.sqlite3"
            store = AuditStore(path)
            base = time.time() - 3 * 86400
            for i in range(5):
                self.assertTrue(store.record_request(
                    self.record(f"r{i}", started_at=base + i, duration_ms=100 + i))["recorded"])
            store.close()
            with closing(sqlite3.connect(str(path))) as db:
                db.execute("DROP INDEX requests_duration")
                db.execute("ALTER TABLE requests DROP COLUMN duration_ms")
                db.execute("PRAGMA user_version=3")
                db.commit()
            migrated = AuditStore(path)
            try:
                with closing(sqlite3.connect(str(path))) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                    rows = [r[0] for r in db.execute("SELECT duration_ms FROM requests ORDER BY id").fetchall()]
                self.assertEqual(rows, [100.0, 101.0, 102.0, 103.0, 104.0])
                with closing(migrated._open_read()) as plan_conn:
                    plan = plan_conn.execute(
                        "EXPLAIN QUERY PLAN SELECT duration_ms FROM requests WHERE started_at>=? AND started_at<=?",
                        (0, time.time())).fetchall()
                self.assertIn("COVERING INDEX requests_duration", plan[0][3])
                summary = migrated.dashboard(30)["summary"]
                self.assertEqual(summary["requests"], 5)
                self.assertEqual(summary["p95_duration_ms"], 104)
            finally:
                migrated.close()
    def test_v1_ingest_table_migrates_with_backfilled_timestamps(self):
        """Migrate v1 ingest timestamps and indexes while preserving retained deduplication."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audit.sqlite3"
            with closing(sqlite3.connect(str(path))) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE ingest (id TEXT PRIMARY KEY, kind TEXT NOT NULL)")  # Legacy schema
                db.execute("INSERT INTO ingest VALUES('legacy-req', 'request')")
                db.execute("PRAGMA user_version=1")
                db.execute("COMMIT")
            store = AuditStore(path)
            try:
                with closing(sqlite3.connect(str(path))) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                    columns = [row[1] for row in db.execute("PRAGMA table_info(ingest)").fetchall()]
                    self.assertIn("created_at", columns)
                    self.assertGreater(db.execute(
                        "SELECT created_at FROM ingest WHERE id='legacy-req'").fetchone()[0], 0)
                self.assertTrue(store.record_request(self.record("fresh"))["recorded"])
                self.assertEqual(store.record_request(self.record("legacy-req"))["reason"], "duplicate")
            finally:
                store.close()

    def test_concurrent_duplicate_ingest_exactly_once(self):
        record = self.record()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(self.store.record_request, [record] * 20))
        self.assertEqual(sum(result["recorded"] for result in results), 1)
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 1)

    def test_logical_budget_and_retention_never_delete_aggregate(self):
        self.store.configure(max_bytes=0)
        self.store.record_request(self.record())
        self.assertIsNone(self.store.get_request("r1"))
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 1)
        self.assertEqual(self.store.storage()["logical_bytes"], 0)
        self.store.configure(max_bytes=100000)
        self.store.record_request(self.record("old", started_at=time.time() - 31 * 86400))
        self.assertIsNone(self.store.get_request("old"))
        self.assertEqual(self.store.dashboard(90)["summary"]["requests"], 2)
        # Deduplication expires with request details rather than blocking IDs permanently.
        self.assertTrue(self.store.record_request(self.record("old"))["recorded"])
        health = self.store.storage()
        for name in ("db_bytes", "wal_bytes", "shm_bytes"):
            self.assertIsInstance(health[name], int)
        self.assertGreater(health["db_bytes"], 0)

    def test_permissions_and_symlink_refusal(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        for suffix in ("-wal", "-shm"):
            self.assertEqual(stat.S_IMODE(Path(str(self.path) + suffix).stat().st_mode), 0o600)
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            AuditStore(link / "new.sqlite3")
        file_link = self.path.parent / "linked.sqlite3"
        file_link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            AuditStore(file_link)
        target = self.path.parent / "sidecar.sqlite3"
        Path(str(target) + "-wal").symlink_to(self.path)
        with self.assertRaises(ValueError):
            AuditStore(target)

    def test_existing_corrupt_or_future_schema_is_not_reset(self):
        corrupt = self.path.parent / "corrupt.sqlite3"
        corrupt.write_bytes(b"not-a-sqlite-database")
        with self.assertRaises(sqlite3.DatabaseError):
            AuditStore(corrupt)
        self.assertEqual(corrupt.read_bytes(), b"not-a-sqlite-database")
        future = self.path.parent / "future.sqlite3"
        with closing(sqlite3.connect(future)) as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError):
            AuditStore(future)
        with closing(sqlite3.connect(future)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_whitelist_excludes_bodies_tokens_and_unbounded_attempts(self):
        secret = "sk-synthetic-secret"
        self.store.record_request(self.record(token=secret, body=secret, headers={"Authorization": secret},
                                             error_code=secret,
                                             attempts=[{"stage": "send", "body": secret, "token": secret,
                                                        "error_code": secret, "status_code": 429}] * 100))
        record = self.store.get_request("r1")
        self.assertNotIn(secret, json.dumps(record))
        self.assertNotIn("body", record)
        self.assertLessEqual(len(record["attempts"]), 32)
        self.assertIsNone(record["error_code"])
        self.store.event("admin", "credential_upload", {"body": secret, "session": secret, "token": secret,
                                                        "status_code": 200, "event_id": "evt1"})
        self.assertNotIn(secret, json.dumps(self.store.list_records("admin")))
        self.store.clear("details")
        self.assertEqual(self.store.event("admin", "credential_upload", {"event_id": "evt1"})["reason"], "duplicate")

    def test_credit_changes_keep_signed_deltas_and_filter_by_account_and_type(self):
        self.store.event("credit", "credit.income", {"credential": "a" * 64, "delta": 500.0, "balance": 530,
                                                     "source": "体验积分", "package_code": "trial",
                                                     "expires_at": 2e9, "token": "secret-token"})
        self.store.event("credit", "credit.consume", {"credential": "b" * 64, "delta": -70.5, "balance": 30,
                                                      "source": "Bearer sk-leak"})
        income, consume = sorted(self.store.list_records("credit")["items"], key=lambda item: item["action"])[::-1]
        self.assertEqual(income["details"], {"credential": "a" * 64, "package_code": "trial", "source": "体验积分",
                                             "delta": 500.0, "balance": 530, "expires_at": 2e9})
        self.assertEqual(consume["details"]["delta"], -70.5)
        self.assertNotIn("source", consume["details"])
        by_account = self.store.list_records("credit", credential="b" * 64)["items"]
        self.assertEqual([item["action"] for item in by_account], ["credit.consume"])
        self.assertEqual([item["action"] for item in self.store.list_records("credit", type="income")["items"]],
                         ["credit.income"])
        self.assertEqual(self.store.list_records("admin")["items"], [])
        with self.assertRaises(ValueError):
            self.store.list_records("credit", type="refund")

    def test_event_tickets_cannot_revive_cleared_details(self):
        pending = {"event_id": "pending-event", **self.store.ticket()}
        self.store.clear("details")
        self.assertEqual(self.store.event("runtime", "finished", pending)["reason"], "details_cleared")
        self.assertEqual(self.store.list_records("runtime")["items"], [])
        self.store.clear("all")
        self.assertEqual(self.store.event("runtime", "finished", pending)["reason"], "stale_epoch")
        self.assertEqual(self.store.storage()["ingest_count"], 0)

    def test_cursor_filters_and_search(self):
        for index in range(5):
            self.store.record_request(self.record(f"r{index}", started_at=time.time(),
                                                 outcome="error" if index % 2 else "success"))
        first = self.store.list_records(limit=2)
        second = self.store.list_records(limit=2, cursor=first["next_cursor"])
        third = self.store.list_records(limit=2, cursor=second["next_cursor"])
        ids = [r["id"] for page in (first, second, third) for r in page["items"]]
        self.assertEqual(len(set(ids)), 5)
        self.assertFalse(third["has_more"])
        self.assertEqual(len(self.store.list_records(status="error")["items"]), 2)
        self.assertEqual(len(self.store.list_records(search="test-model")["items"]), 5)
        self.assertEqual(len(self.store.list_records(search="%'")["items"]), 0)
        with self.assertRaises(ValueError):
            self.store.list_records(cursor="garbage")

    def test_idle_expiry_removes_details_without_changing_totals(self):
        now = time.time()
        self.store.record_request(self.record(started_at=now, attempts=[{"stage": "send"}]))
        self.store.event("runtime", "test_event")
        with patch("app.audit_store.time.time", return_value=now + 31 * 86400):
            self.assertEqual(self.store.list_records()["items"], [])
            self.assertEqual(self.store.list_records("runtime")["items"], [])
            self.assertEqual(self.store.storage()["logical_bytes"], 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
            total = db.execute("SELECT payload FROM stats_totals WHERE dimension='global'").fetchone()[0]
            self.assertEqual(json.loads(total)["requests"], 1)

    def test_failure_after_aggregate_rolls_back_entire_request(self):
        with patch.object(self.store, "_prune", side_effect=sqlite3.OperationalError("synthetic")):
            self.assertFalse(self.store.record_request(self.record())["ok"])
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 0)
        self.assertEqual(self.store.storage()["ingest_count"], 0)
        self.assertIsNone(self.store.get_request("r1"))

    def test_configure_failure_does_not_publish_settings(self):
        original = self.store.max_bytes
        with patch.object(self.store, "_prune", side_effect=sqlite3.OperationalError("synthetic")):
            self.assertFalse(self.store.configure(max_bytes=0)["ok"])
        self.assertEqual(self.store.max_bytes, original)
        self.assertTrue(self.store.configure(preview_limit=0)["ok"])
        self.store.record_request(self.record(attempts=[{"stage": "send"}]))
        self.assertEqual(self.store.get_request("r1")["attempts"], [])

    def test_budget_evicts_oldest_across_event_and_request_details(self):
        now = time.time()
        self.store.record_request(self.record("old", started_at=now - 2))
        self.store.event("runtime", "event")
        self.store.record_request(self.record("new", started_at=now + 1))
        with closing(sqlite3.connect(self.path)) as db:
            newest_cost = db.execute("SELECT logical_bytes FROM requests WHERE id='new'").fetchone()[0]
        self.store.configure(max_bytes=newest_cost)
        self.assertIsNotNone(self.store.get_request("new"))
        self.assertIsNone(self.store.get_request("old"))
        self.assertEqual(self.store.list_records("runtime")["items"], [])
        self.assertEqual(self.store.storage()["logical_bytes"], newest_cost)

    def assert_accounting(self):
        with closing(sqlite3.connect(self.path)) as db:
            actual = db.execute("SELECT logical_bytes,request_count,event_count,ingest_count FROM detail_accounting").fetchone()
            expected = db.execute("SELECT COALESCE((SELECT SUM(logical_bytes) FROM requests),0)+COALESCE((SELECT SUM(logical_bytes) FROM events),0),(SELECT COUNT(*) FROM requests),(SELECT COUNT(*) FROM events),(SELECT COUNT(*) FROM ingest)").fetchone()
            self.assertEqual(actual, expected)
        return actual

    def aggregate_snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {table: db.execute(f"SELECT * FROM {table} ORDER BY bucket,dimension,dimension_key").fetchall()
                    for table in ("stats_hourly", "stats_daily", "stats_totals")}

    def test_accounting_triggers_rollback_clear_and_restart(self):
        self.store.record_request(self.record(attempts=[{"stage": "send"}] * 32))
        self.store.event("admin", "test", {"id": "evt"})
        before = self.assert_accounting()
        prune = self.store._prune
        def fail_after_cleanup():
            prune(max_bytes=0)
            raise sqlite3.OperationalError("synthetic")
        with patch.object(self.store, "_prune", side_effect=fail_after_cleanup):
            self.assertFalse(self.store.record_request(self.record("rollback"))["ok"])
        self.assertEqual(self.assert_accounting(), before)
        self.store.close()
        self.store = AuditStore(self.path)
        self.assertEqual(self.assert_accounting(), before)
        self.store.clear("details")
        self.assertEqual(self.assert_accounting(), (0, 0, 0, 2))
        self.store.clear("all")
        self.assertEqual(self.assert_accounting(), (0, 0, 0, 0))

    def test_legacy_v1_accounting_bootstrap_is_once_and_preserves_data(self):
        self.store.record_request(self.record(attempts=[{"stage": "send"}]))
        self.store.event("runtime", "test", {"id": "evt"})
        expected = self.assert_accounting()
        aggregates = self.aggregate_snapshot()
        self.store.close()
        # Recreate the exact pre-accounting v1 layout, only in this temp DB.
        with closing(sqlite3.connect(self.path)) as db:
            names = db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
            for (name,) in names:
                db.execute(f'DROP TRIGGER "{name}"')
            db.execute("DROP TABLE detail_accounting")
            db.commit()
        self.store = AuditStore(self.path)
        self.assertEqual(self.assert_accounting(), expected)
        self.assertEqual(self.aggregate_snapshot(), aggregates)
        self.assertEqual(self.store.record_request(self.record())["reason"], "duplicate")
        self.store.close()
        statements = []
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch("app.audit_store.sqlite3.connect", side_effect=traced_connect):
            self.store = AuditStore(self.path)
        self.assertFalse(any("SUM(" in sql.upper() for sql in statements))
        self.assertEqual(self.assert_accounting(), expected)

    def test_large_budget_reduction_commits_bounded_batches_and_preserves_stats(self):
        count = _CLEANUP_BATCH * 4 + 5
        for index in range(count):
            self.assertTrue(self.store.record_request(self.record(
                f"r{index}", input_tokens=0 if index % 2 else None,
                attempts=[{"stage": "send"}] * 4))["ok"])
            if index % 8 == 0:
                self.assertTrue(self.store.event("runtime", "test", {"id": f"evt{index}"})["ok"])
        aggregates = self.aggregate_snapshot()
        before = self.assert_accounting()
        result = self.store.configure(max_bytes=0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["pending_cleanup"])
        self.assertEqual(self.store.max_bytes, 0)
        after = self.assert_accounting()
        self.assertGreater(before[1] + before[2], after[1] + after[2])
        self.assertLessEqual(before[1] + before[2] - after[1] - after[2], _CLEANUP_BATCH)
        # Target and counters survive restart; callers reapply configured limits.
        self.store.close()
        self.store = AuditStore(self.path, max_bytes=0)
        remaining = after[1] + after[2]
        for _ in range(count):
            health = self.store.storage()
            current = health["request_count"] + health["event_count"]
            self.assertLess(current, remaining)
            self.assertLessEqual(remaining - current, _CLEANUP_BATCH)
            self.assertFalse(health["degraded"])
            self.assertEqual(health["dropped_records"], 0)
            remaining = current
            if not health["pending_cleanup"]:
                break
        else:
            self.fail("incremental cleanup did not converge")
        self.assertEqual(health["logical_bytes"], 0)
        self.assertEqual(self.aggregate_snapshot(), aggregates)
        self.assertEqual(self.assert_accounting(), (0, 0, 0, before[3]))
        self.assertEqual(self.store.record_request(self.record("r0"))["reason"], "duplicate")
        self.assertTrue(self.store.record_request(self.record("new", input_tokens=0))["ok"])
        summary = self.store.dashboard()["summary"]
        self.assertEqual(summary["requests"], count + 1)
        self.assertEqual(summary["input_tokens_known"], count // 2 + 1)
        self.assertEqual(summary["input_tokens"], 0)
        self.assertIsNone(summary["credit"])
        self.assertFalse(self.store.storage()["degraded"])

    def test_incremental_retention_hides_expired_details_and_preserves_stats(self):
        count = _CLEANUP_BATCH * 4 + 1
        now = time.time()
        for index in range(count):
            self.assertTrue(self.store.record_request(self.record(
                f"r{index}", started_at=now - 2 * 86400,
                attempts=[{"stage": "send"}]))["ok"])
        aggregates = self.aggregate_snapshot()
        configured = self.store.configure(retention_days=1)
        self.assertTrue(configured["ok"])
        self.assertTrue(configured["pending_cleanup"])
        self.assertEqual(self.store.list_records()["items"], [])
        self.assertIsNone(self.store.get_request(f"r{count - 1}"))
        for _ in range(count):
            health = self.store.storage()
            if not health["pending_cleanup"]:
                break
        self.assertFalse(health["pending_cleanup"])
        # Deduplication expires with details without affecting aggregate statistics.
        self.assertEqual(self.assert_accounting(), (0, 0, 0, 0))
        self.assertEqual(self.aggregate_snapshot(), aggregates)
        self.assertFalse(health["degraded"])

    def test_capacity_headroom_avoids_eviction_on_every_full_budget_write(self):
        for index in range(100):
            self.assertTrue(self.store.record_request(self.record(f"r{index:04d}"))["ok"])
        budget = self.store.storage()["logical_bytes"]
        self.assertTrue(self.store.configure(max_bytes=budget)["ok"])
        self.assertTrue(self.store.record_request(self.record("r0100"))["ok"])
        first = self.store.storage()
        self.assertLess(first["request_count"], 99)
        self.assertLessEqual(first["logical_bytes"], budget)
        with patch.object(self.store, "_oldest", wraps=self.store._oldest) as oldest:
            for index in range(101, 105):
                self.assertTrue(self.store.record_request(self.record(f"r{index:04d}"))["ok"])
            # Below budget, only indexed retention probes, not capacity scans.
            self.assertTrue(all(call.args and call.args[0] is not None for call in oldest.call_args_list))
        final = self.store.storage()
        self.assertEqual(final["request_count"], first["request_count"] + 4)
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 105)
        self.assert_accounting()

    def test_cleanup_time_slice_commits_progress_instead_of_rolling_back(self):
        for index in range(20):
            self.assertTrue(self.store.record_request(self.record(f"r{index}"))["ok"])
        aggregates = self.aggregate_snapshot()
        db = self.store._db
        clock = time.monotonic
        class SlowDeleteConnection:
            elapsed = 0
            def __getattr__(self, name):
                return getattr(db, name)
            def execute(self, sql, *args):
                result = db.execute(sql, *args)
                if sql.startswith("DELETE FROM requests WHERE id="):
                    self.elapsed += 0.02  # Simulate 20ms per indexed deletion.
                return result
        proxy = SlowDeleteConnection()
        self.store._db = proxy
        try:
            with patch("app.audit_store.time.monotonic", side_effect=lambda: clock() + proxy.elapsed):
                result = self.store.configure(max_bytes=0)
        finally:
            self.store._db = db
        self.assertTrue(result["ok"])
        self.assertTrue(result["pending_cleanup"])
        remaining = self.assert_accounting()[1]
        self.assertGreater(remaining, 0)
        self.assertLess(remaining, 20)
        self.assertLessEqual(20 - remaining, 3)
        self.assertEqual(self.store.max_bytes, 0)
        health = self.store.storage()
        self.assertFalse(health["pending_cleanup"])
        self.assertFalse(health["degraded"])
        self.assertEqual(health["logical_bytes"], 0)
        self.assertEqual(self.aggregate_snapshot(), aggregates)

    def test_sql_deadline_still_interrupts_and_rolls_back(self):
        def expensive_cleanup():
            self.store._db.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n").fetchone()
        start = time.monotonic()
        with patch.object(self.store, "_prune", side_effect=expensive_cleanup):
            self.assertFalse(self.store.record_request(self.record())["ok"])
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.assert_accounting(), (0, 0, 0, 0))
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 0)
        health = self.store.storage()
        self.assertEqual(health["dropped_records"], 1)
        self.assertEqual(health["last_error"], "OperationalError")
        self.assertEqual(health["sql_deadline_ms"], 1000)

    def test_normal_write_work_does_not_scale_with_detail_population(self):
        # Count VM progress callbacks as a deterministic small benchmark, while
        # preserving the real deadline callback. Wall times are diagnostic only.
        samples = []
        statements = []
        db = self.store._db
        class CountingConnection:
            steps = 0
            def __getattr__(self, name):
                return getattr(db, name)
            def set_progress_handler(self, callback, interval):
                def counted():
                    self.steps += 1
                    return callback()
                # Finer sampling avoids 1000-opcode quantization at this scale;
                # it checks the production deadline more often, never less.
                db.set_progress_handler(counted if callback else None, min(interval, 100))
        proxy = CountingConnection()
        stamp = time.time()
        for population in (100, 10000):
            # Synthetic details avoid measuring fixture aggregation/setup. Their
            # accounting still exercises the production triggers.
            with closing(sqlite3.connect(self.path)) as fixture:
                first = samples[-1][0] if samples else 0
                for index in range(first, population):
                    fixture.execute(
                        "INSERT INTO requests(id,started_at,model,profile,credential,outcome,status_code,payload,logical_bytes) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"seed{index}", stamp, None, None, None, "success", 200, "{}", 256))
                    fixture.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (
                        f"event{index}", stamp, "runtime", "test", "{}", 128))
                fixture.commit()
            proxy.steps = 0
            statements.clear()
            self.store._db = proxy
            db.set_trace_callback(statements.append)
            start = time.monotonic()
            try:
                for index in range(20):
                    self.assertTrue(self.store.record_request(self.record(f"measured{population}-{index}"))["ok"])
                    self.assertTrue(self.store.event("runtime", "measured")["ok"])
            finally:
                db.set_trace_callback(None)
                self.store._db = db
            samples.append((population, proxy.steps, time.monotonic() - start))
            for sql in statements:
                normalized = sql.upper()
                self.assertNotIn("SUM(", normalized)
                self.assertNotIn(" OVER ", normalized)
                self.assertNotIn("NOT IN", normalized)
            for table in ("requests", "events"):
                for where, args in (("", ()), (" WHERE started_at<?", (stamp - 86400,))):
                    plan = db.execute(f"EXPLAIN QUERY PLAN SELECT started_at,id,logical_bytes FROM {table}{where} ORDER BY started_at,id LIMIT ?", (*args, _CLEANUP_BATCH)).fetchall()
                    description = " ".join(row[3] for row in plan)
                    self.assertIn(f"{table}_time", description)
                    self.assertNotIn("TEMP B-TREE", description)
        self.assertLessEqual(samples[1][1], samples[0][1] * 1.2 + 2, samples)
        self.assertEqual(self.store.dashboard()["summary"]["requests"], 40)
        self.assertFalse(self.store.storage()["degraded"])
        accounting = self.assert_accounting()
        print("\naudit write benchmark (population/table, VM callbacks/100 opcodes, seconds/40 writes):", samples)
        aggregates = self.aggregate_snapshot()
        remaining = accounting[1] + accounting[2]
        durations = []
        start = time.monotonic()
        result = self.store.configure(max_bytes=0)
        durations.append(time.monotonic() - start)
        self.assertTrue(result["ok"])
        self.assertTrue(result["pending_cleanup"])
        for _ in range(remaining):
            start = time.monotonic()
            health = self.store.storage()
            durations.append(time.monotonic() - start)
            current = health["request_count"] + health["event_count"]
            self.assertLess(current, remaining)
            self.assertFalse(health["degraded"])
            remaining = current
            if not health["pending_cleanup"]:
                break
        self.assertFalse(health["pending_cleanup"])
        self.assertEqual(self.assert_accounting()[:3], (0, 0, 0))
        self.assertEqual(self.aggregate_snapshot(), aggregates)
        print("audit reduction benchmark (calls, max seconds/call, total seconds):",
              (len(durations), max(durations), sum(durations)))

    def test_closed_store_degrades_instead_of_throwing(self):
        record = self.record()
        self.store.close()
        self.store.close()
        self.assertFalse(self.store.record_request(record)["ok"])
        self.assertTrue(self.store.storage()["degraded"])

    def test_client_ip_and_agent_are_sanitized_persisted_and_filterable(self):
        good = self.record(id="meta-1", client_ip="2001:db8::1", user_agent="synthetic-agent/1.2")
        self.assertTrue(self.store.record_request(good)["ok"])
        dirty = self.record(id="meta-2", client_ip="fe80::1%eth0",
                            user_agent="evil\nBearer sk-secret\r\nX")
        self.assertTrue(self.store.record_request(dirty)["ok"])
        items = {item["id"]: item for item in self.store.list_records()["items"]}
        self.assertEqual(items["meta-1"]["client_ip"], "2001:db8::1")
        self.assertEqual(items["meta-1"]["user_agent"], "synthetic-agent/1.2")
        self.assertIsNone(items["meta-2"]["client_ip"])
        self.assertIsNone(items["meta-2"]["user_agent"])
        self.assertEqual(self.store.list_records(client_ip="2001:db8::1")["items"][0]["id"], "meta-1")
        self.assertEqual(self.store.list_records(client_ip="198.51.100.1")["items"], [])

    def test_v2_database_gains_client_ip_without_losing_rows(self):
        legacy_path = Path(self.temp.name) / "private" / "v2.sqlite3"
        raw = sqlite3.connect(legacy_path)
        with raw:
            raw.execute("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL,"
                        " detail_generation INTEGER NOT NULL, cleared_at REAL NOT NULL)")
            raw.execute("INSERT INTO state VALUES(1,0,0,0)")
            raw.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, started_at REAL NOT NULL, model TEXT,"
                        " profile TEXT, credential TEXT, outcome TEXT, status_code INTEGER,"
                        " payload TEXT NOT NULL, logical_bytes INTEGER NOT NULL)")
            raw.execute(
                "INSERT INTO requests VALUES('legacy-1', ?, 'm', NULL, NULL, 'success', 200, ?, 128)",
                (time.time(), json.dumps({"id": "legacy-1", "model": "m", "outcome": "success",
                                          "status_code": 200, "attempts": []})))
            raw.execute("PRAGMA user_version=2")
        raw.close()
        reopened = AuditStore(legacy_path)
        self.addCleanup(reopened.close)
        self.assertIn("client_ip",
                      [c[1] for c in reopened._db.execute("PRAGMA table_info(requests)")])
        indexes = [row[1] for row in reopened._db.execute("PRAGMA index_list(requests)")]
        self.assertIn("requests_client", indexes)
        items = reopened.list_records()["items"]
        self.assertEqual([item["id"] for item in items], ["legacy-1"])
        self.assertIsNone(items[0].get("client_ip"))
        self.assertTrue(reopened.record_request(
            {"id": "meta-3", **reopened.ticket(), "started_at": time.time(),
             "public_model": "test-model", "client_ip": "203.0.113.9",
             "protocol": "chat", "status_code": 200, "outcome": "success"})["ok"])
        self.assertEqual(reopened.list_records(client_ip="203.0.113.9")["items"][0]["id"], "meta-3")
        accounting = reopened._db.execute("SELECT request_count FROM detail_accounting").fetchone()[0]
        raw_count = reopened._db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        self.assertEqual(accounting, raw_count)

    def test_attempts_are_stored_once_in_the_request_payload(self):
        attempts = [{"stage": "upstream_attempt", "profile": "p"}, {"stage": "upstream_http", "status_code": 200}]
        self.assertTrue(self.store.record_request(self.record(attempts=attempts))["recorded"])
        with closing(sqlite3.connect(self.path)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("attempts", tables)
            payload, logical_bytes = db.execute("SELECT payload,logical_bytes FROM requests").fetchone()
        self.assertEqual(logical_bytes, len(payload.encode()) + 256)
        self.assertEqual(self.store.get_request("r1")["attempts"], attempts)
        self.assertEqual(self.store.list_records()["items"][0]["attempts"], attempts)
        self.assert_accounting()
        self.store.clear("details")
        self.assertEqual(self.assert_accounting(), (0, 0, 0, 1))

    def legacy_attempts_database(self, path, count=3):
        """Build the pre-change layout: a per-row attempts table, its delete cascade and double-counted bytes."""
        store = AuditStore(path)
        for index in range(count):
            self.assertTrue(store.record_request(self.record(
                f"legacy-{index}", started_at=time.time() - index,
                attempts=[{"stage": "upstream_attempt", "profile": "p"},
                          {"stage": "upstream_http", "status_code": 200}]))["recorded"])
        store.close()
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE attempts (request_id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(request_id,ordinal))")
            db.execute("DROP TRIGGER audit_requests_delete")
            db.execute("CREATE TRIGGER audit_requests_delete AFTER DELETE ON requests BEGIN UPDATE detail_accounting SET logical_bytes=logical_bytes-OLD.logical_bytes,request_count=request_count-1 WHERE id=1; DELETE FROM attempts WHERE request_id=OLD.id; END")
            for request_id, payload in db.execute("SELECT id,payload FROM requests").fetchall():
                attempts = json.loads(payload)["attempts"]
                for ordinal, attempt in enumerate(attempts):
                    db.execute("INSERT INTO attempts VALUES(?,?,?)", (request_id, ordinal, json.dumps(attempt)))
                db.execute("UPDATE requests SET logical_bytes=? WHERE id=?", (
                    len(payload.encode()) + sum(len(json.dumps(a).encode()) + 32 for a in attempts) + 256,
                    request_id))
            db.commit()

    def test_legacy_attempts_table_is_dropped_and_bytes_recounted(self):
        path = Path(self.temp.name) / "private" / "legacy-attempts.sqlite3"
        self.legacy_attempts_database(path)
        with closing(sqlite3.connect(path)) as db:
            doubled = db.execute("SELECT SUM(logical_bytes) FROM requests").fetchone()[0]
        store = AuditStore(path)
        self.addCleanup(store.close)
        with closing(sqlite3.connect(path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='attempts'").fetchone())
            self.assertNotIn("attempts", db.execute(
                "SELECT sql FROM sqlite_master WHERE name='audit_requests_delete'").fetchone()[0])
            for payload, logical_bytes in db.execute("SELECT payload,logical_bytes FROM requests"):
                self.assertEqual(logical_bytes, len(payload.encode()) + 256)
            fixed = db.execute("SELECT SUM(logical_bytes) FROM requests").fetchone()[0]
            self.assertLess(fixed, doubled)
            self.assertEqual(db.execute("SELECT logical_bytes,request_count FROM detail_accounting").fetchone(),
                             (fixed, 3))
        self.assertEqual(len(store.get_request("legacy-0")["attempts"]), 2)
        # Deletes through the rebuilt trigger keep the accounting exact.
        self.assertTrue(store.record_request(self.record("fresh"))["recorded"])
        store.configure(max_bytes=0)
        store.storage()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT logical_bytes,request_count FROM detail_accounting").fetchone(), (0, 0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_attempts_migration_is_idempotent_and_leaves_clear_working(self):
        path = Path(self.temp.name) / "private" / "legacy-attempts.sqlite3"
        self.legacy_attempts_database(path, count=2)
        AuditStore(path).close()
        statements = []
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch("app.audit_store.sqlite3.connect", side_effect=traced_connect):
            store = AuditStore(path)
        self.addCleanup(store.close)
        self.assertFalse(any("UPDATE requests SET logical_bytes" in sql for sql in statements), statements)
        with closing(sqlite3.connect(path)) as db:
            expected = db.execute("SELECT SUM(logical_bytes) FROM requests").fetchone()[0]
            self.assertEqual(db.execute("SELECT logical_bytes FROM detail_accounting").fetchone()[0], expected)
        self.assertTrue(store.clear("details")["ok"])
        self.assertEqual(store.storage()["logical_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
