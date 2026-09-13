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
from app.audit_store import AuditStore, _CLEANUP_BATCH


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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
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

    def test_thread_lock_timeout_is_visible(self):
        record = self.record()
        with self.store._lock:
            start = time.monotonic()
            result = self.store.record_request(record)
        self.assertLess(time.monotonic() - start, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.storage()["last_error"], "TimeoutError")

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
        self.assertEqual(self.store.record_request(self.record("old"))["reason"], "duplicate")
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
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
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
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts WHERE request_id NOT IN (SELECT id FROM requests)").fetchone()[0], 0)
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
        self.assertEqual(self.assert_accounting(), (0, 0, 0, count))
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
            # accounting and attempts still exercise the production triggers.
            with closing(sqlite3.connect(self.path)) as fixture:
                first = samples[-1][0] if samples else 0
                for index in range(first, population):
                    fixture.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)", (
                        f"seed{index}", stamp, None, None, None, "success", 200, "{}", 256))
                    fixture.execute("INSERT INTO attempts VALUES(?,?,?)", (f"seed{index}", 0, "{}"))
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


if __name__ == "__main__":
    unittest.main()
