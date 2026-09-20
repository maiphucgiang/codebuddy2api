#!/usr/bin/env python3
"""Test the usage-snapshot cache: restart persistence, staleness and identity safety."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import converter
from app.usage_snapshots import (MAX_ACCOUNTS, MAX_BYTES, MAX_DAYS, MAX_MODELS, VERSION,
                                 UsageSnapshots)

IDENTITY = "a" * 64
OTHER_IDENTITY = "b" * 64
PATH = "/auth/account.info"
USAGE = {"by_day": {"2026-09-19": {"claude-sonnet-4": 12.5}}, "total_credits": 12.5,
         "requests": 7, "partial": False}


class UsageSnapshotStoreTests(unittest.TestCase):
    """Exercise the cache directly, with no credential files and no network access."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "usage-snapshots.json"

    def row(self, **overrides):
        row = {"identity": IDENTITY, "site": "domestic", "by_day": {"2026-09-19": {"m": 1.0}},
               "total_credits": 1.0, "requests": 1, "partial": False, "fetched_at": time.time()}
        row.update(overrides)
        return row

    def write(self, accounts):
        self.path.write_text(json.dumps({"version": VERSION, "accounts": accounts}), encoding="utf-8")

    def test_a_snapshot_survives_a_restart(self):
        first = UsageSnapshots(self.path)
        self.assertTrue(first.store(PATH, IDENTITY, "domestic", USAGE))
        restored = UsageSnapshots(self.path).accounts()[PATH]
        self.assertEqual(restored["total_credits"], 12.5)
        self.assertEqual(restored["by_day"]["2026-09-19"]["claude-sonnet-4"], 12.5)
        self.assertEqual(restored["site"], "domestic")

    def test_a_cached_row_keeps_its_identity_and_staleness_flags(self):
        store = UsageSnapshots(self.path)
        store.store(PATH, IDENTITY, "international", USAGE, partial=True)
        row = UsageSnapshots(self.path).accounts()[PATH]
        self.assertEqual(row["identity"], IDENTITY)
        self.assertTrue(row["partial"])

    def test_identity_mismatch_is_detectable(self):
        store = UsageSnapshots(self.path)
        store.store(PATH, IDENTITY, "domestic", USAGE)
        revived = UsageSnapshots(self.path)
        self.assertTrue(revived.identity_matches(PATH, IDENTITY))
        self.assertFalse(revived.identity_matches(PATH, OTHER_IDENTITY))
        self.assertFalse(revived.identity_matches("/auth/unknown.info", IDENTITY))

    def test_untrusted_snapshots_are_rejected_wholesale(self):
        payloads = {
            "not json": b"nope",
            "empty object": b"{}",
            "unsupported version": json.dumps({"version": 99, "accounts": {}}).encode(),
            "boolean version": json.dumps({"version": True, "accounts": {}}).encode(),
            "extra top-level field": json.dumps({"version": VERSION, "accounts": {}, "x": 1}).encode(),
            "extra row field": json.dumps({"version": VERSION,
                                           "accounts": {PATH: {**self.row(), "x": 1}}}).encode(),
            "non-hex identity": json.dumps({"version": VERSION,
                                            "accounts": {PATH: self.row(identity="zzz")}}).encode(),
            "unknown site": json.dumps({"version": VERSION,
                                        "accounts": {PATH: self.row(site="mars")}}).encode(),
            "duplicate keys": b'{"version":1,"version":1,"accounts":{}}',
            "infinity literal": ('{"version":1,"accounts":{"' + PATH + '":{"identity":"' + IDENTITY
                                 + '","site":"domestic","by_day":{},"total_credits":Infinity,'
                                 '"requests":0,"partial":false,"fetched_at":1}}}').encode(),
            "huge integer": json.dumps({"version": VERSION,
                                        "accounts": {PATH: self.row(total_credits=10 ** 400)}}).encode(),
            "negative credits": json.dumps({"version": VERSION,
                                            "accounts": {PATH: self.row(total_credits=-5.0)}}).encode(),
            "bool partial": json.dumps({"version": VERSION,
                                        "accounts": {PATH: self.row(partial=1)}}).encode(),
            "bad date": json.dumps({"version": VERSION,
                                    "accounts": {PATH: self.row(by_day={"19-09-2026": {"m": 1.0}})}}).encode(),
            "impossible date": json.dumps({"version": VERSION,
                                           "accounts": {PATH: self.row(by_day={"2026-02-31": {"m": 1.0}})}}).encode(),
            "fractional requests": json.dumps({"version": VERSION,
                                               "accounts": {PATH: self.row(requests=1.5)}}).encode(),
            "future timestamp": json.dumps({"version": VERSION,
                                            "accounts": {PATH: self.row(fetched_at=time.time() + 10 * 86400)}}).encode(),
            "oversized file": b"x" * (MAX_BYTES + 1),
        }
        for label, content in payloads.items():
            with self.subTest(payload=label):
                self.path.write_bytes(content)
                self.assertEqual(UsageSnapshots(self.path).accounts(), {})

    def test_oversized_tables_are_rejected_on_load(self):
        accounts = {f"/auth/{index}.info": self.row() for index in range(MAX_ACCOUNTS + 1)}
        self.write(accounts)
        self.assertEqual(UsageSnapshots(self.path).accounts(), {})
        self.write({PATH: self.row(by_day={f"2026-09-{day:02d}": {} for day in range(1, MAX_DAYS + 2)})})
        self.assertEqual(UsageSnapshots(self.path).accounts(), {})
        self.write({PATH: self.row(by_day={"2026-09-19": {f"m{index}": 1.0
                                                         for index in range(MAX_MODELS + 1)}})})
        self.assertEqual(UsageSnapshots(self.path).accounts(), {})

    def test_the_writer_never_produces_a_file_the_reader_rejects(self):
        """A rejected cache file loses every snapshot it held, not just the offending row."""
        store = UsageSnapshots(self.path)
        # October has 31 days, matching MAX_DAYS, so the fixtures are real calendar dates.
        days = [f"2026-10-{day:02d}" for day in range(1, MAX_DAYS + 1)]
        with store._lock:
            for index in range(MAX_ACCOUNTS):
                store._data[f"/auth/{index}.info"] = {
                    "identity": f"{index:064x}", "site": "domestic",
                    "by_day": {day: {f"model-name-{model:03d}": 1.5 for model in range(MAX_MODELS)}
                               for day in days},
                    "total_credits": 1.5, "requests": 1, "partial": False,
                    "fetched_at": time.time() - index}
            content, shed = store._serialize_locked()
            self.assertLessEqual(len(content), MAX_BYTES)
            self.assertTrue(shed)                   # Capacity really did force shedding.
            self.path.write_bytes(content)
        self.assertGreater(len(UsageSnapshots(self.path).accounts()), 0)

    def test_a_write_failure_is_reported_and_keeps_the_cache_in_memory(self):
        store = UsageSnapshots(self.path)
        store.store(PATH, IDENTITY, "domestic", USAGE)
        with patch("app.usage_snapshots.os.replace", side_effect=OSError("read-only")):
            self.assertFalse(store.store("/auth/other.info", IDENTITY, "domestic", USAGE))
        self.assertIsNotNone(store.last_error)
        self.assertIn("/auth/other.info", store.accounts())          # Still usable in memory.
        self.assertNotIn("/auth/other.info", json.loads(self.path.read_text())["accounts"])

    def test_prune_drops_stale_rows_only(self):
        store = UsageSnapshots(self.path)
        store.store("/auth/old.info", IDENTITY, "domestic", USAGE)
        store.store("/auth/new.info", IDENTITY, "domestic", USAGE)
        with store._lock:
            store._data["/auth/old.info"]["fetched_at"] = time.time() - 30 * 24 * 3600
        self.assertTrue(store.prune())
        self.assertEqual(set(UsageSnapshots(self.path).accounts()), {"/auth/new.info"})

    def test_forget_removes_one_snapshot(self):
        store = UsageSnapshots(self.path)
        store.store(PATH, IDENTITY, "domestic", USAGE)
        self.assertTrue(store.forget(PATH))
        self.assertEqual(UsageSnapshots(self.path).accounts(), {})

    def test_invalid_input_is_refused_rather_than_cached(self):
        store = UsageSnapshots(self.path)
        self.assertFalse(store.store(PATH, "not-a-hash", "domestic", USAGE))
        self.assertFalse(store.store(PATH, IDENTITY, "mars", USAGE))
        self.assertFalse(store.store(PATH, IDENTITY, "domestic", {"by_day": {"nope": {"m": 1.0}}}))
        self.assertEqual(store.accounts(), {})

    def test_malformed_usage_is_rejected_rather_than_normalized_to_zero(self):
        """A missing or bad field must not become a plausible zero."""
        store = UsageSnapshots(self.path)
        bad = {
            "missing by_day": {"total_credits": 1.0, "requests": 1},
            "missing total": {"by_day": {}, "requests": 1},
            "missing requests": {"by_day": {}, "total_credits": 1.0},
            "none by_day": {"by_day": None, "total_credits": 1.0, "requests": 1},
            "none total": {"by_day": {}, "total_credits": None, "requests": 1},
            "empty-string total": {"by_day": {}, "total_credits": "", "requests": 1},
            "false total": {"by_day": {}, "total_credits": False, "requests": 1},
            "string total": {"by_day": {}, "total_credits": "1.0", "requests": 1},
            "negative total": {"by_day": {}, "total_credits": -1.0, "requests": 1},
            "huge total": {"by_day": {}, "total_credits": 10 ** 400, "requests": 1},
            "nan total": {"by_day": {}, "total_credits": float("nan"), "requests": 1},
            "fractional requests": {"by_day": {}, "total_credits": 1.0, "requests": 1.5},
            "false requests": {"by_day": {}, "total_credits": 1.0, "requests": False},
            "negative credit day": {"by_day": {"2026-09-19": {"m": -1.0}}, "total_credits": 1.0,
                                    "requests": 1},
            "string day credit": {"by_day": {"2026-09-19": {"m": "1"}}, "total_credits": 1.0,
                                  "requests": 1},
            "impossible date": {"by_day": {"2026-02-31": {"m": 1.0}}, "total_credits": 1.0,
                                "requests": 1},
        }
        for label, usage in bad.items():
            with self.subTest(usage=label):
                self.assertFalse(store.store(PATH, IDENTITY, "domestic", usage))
        self.assertEqual(store.accounts(), {})

    def test_non_mapping_usage_and_partial_flag_are_rejected(self):
        store = UsageSnapshots(self.path)
        self.assertFalse(store.store(PATH, IDENTITY, "domestic", None))
        self.assertFalse(store.store(PATH, IDENTITY, "domestic", ["by_day"]))
        for partial in (1, 0, "true", None):
            with self.subTest(partial=partial):
                self.assertFalse(store.store(PATH, IDENTITY, "domestic", USAGE, partial=partial))
        self.assertEqual(store.accounts(), {})

    def test_the_stored_row_is_a_copy_the_caller_cannot_mutate(self):
        store = UsageSnapshots(self.path)
        usage = {"by_day": {"2026-09-19": {"m": 1.0}}, "total_credits": 1.0, "requests": 1,
                 "partial": False}
        self.assertTrue(store.store(PATH, IDENTITY, "domestic", usage))
        usage["by_day"]["2026-09-19"]["m"] = 999.0        # Mutating the caller's object...
        usage["total_credits"] = 999.0
        self.assertEqual(store.accounts()[PATH]["total_credits"], 1.0)   # ...must not leak in.
        self.assertEqual(store.accounts()[PATH]["by_day"]["2026-09-19"]["m"], 1.0)

    def test_an_overlong_path_is_rejected_on_write_and_load(self):
        store = UsageSnapshots(self.path)
        self.assertFalse(store.store("/" + "x" * 5000, IDENTITY, "domestic", USAGE))
        self.write({"/" + "x" * 5000: self.row()})
        self.assertEqual(UsageSnapshots(self.path).accounts(), {})

    def test_missing_path_stays_in_memory_only(self):
        store = UsageSnapshots()
        self.assertTrue(store.store(PATH, IDENTITY, "domestic", USAGE))
        self.assertIn(PATH, store.accounts())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_symlinked_file_is_not_adopted(self):
        target = self.root / "outside.json"
        target.write_text(json.dumps({"version": VERSION, "accounts": {PATH: self.row()}}), encoding="utf-8")
        link = self.root / "link.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        self.assertEqual(UsageSnapshots(link).accounts(), {})
        self.assertTrue(target.exists())

    def test_snapshot_is_owner_only_where_the_platform_supports_it(self):
        UsageSnapshots(self.path).store(PATH, IDENTITY, "domestic", USAGE)
        if sys.platform != "win32":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class UsageSnapshotIntegrationTests(unittest.TestCase):
    """Verify the cache feeds the published aggregate without becoming authoritative."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": str(self.root)}))
        self.enterContext(patch.dict(converter.CONFIG, {"log_path": None, "cred_pool": None, "cred": None,
                                                        "ledger": None, "model_catalogs": {},
                                                        "account_catalogs": None, "model_cache": None,
                                                        "usage_daily": None, "usage_daily_accounts": None,
                                                        "usage_snapshots": None}))
        self.enterContext(patch.object(converter, "_log"))
        self.path = self.root / "usage-snapshots.json"

    def credential(self, name="account.info", uid="synthetic-uid"):
        now = time.time()
        path = self.root / name
        path.write_text(json.dumps({"account": {"uid": uid}, "auth": {
            "accessToken": "synthetic-token", "refreshToken": "synthetic-refresh",
            "domain": "www.codebuddy.cn", "expiresAt": (now + 86400) * 1000,
            "lastRefreshTime": now * 1000}}), encoding="utf-8")
        return path

    def test_the_published_aggregate_is_repopulated_from_the_cache(self):
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        entry = pool._entries[0]
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        snapshots.store(entry["id"], entry["account_key"], "domestic",
                        {"by_day": {"2026-09-19": {"claude-sonnet-4": 4.0}}, "total_credits": 4.0,
                         "requests": 3, "partial": False})
        # A restart starts with an empty in-memory aggregate.
        converter.CONFIG["usage_daily_accounts"] = None
        converter._publish_usage_daily(pool)
        published = converter.CONFIG["usage_daily"]
        self.assertEqual(published["total_credits"], 4.0)
        self.assertEqual(published["requests"], 3)
        self.assertEqual(published["by_day"]["2026-09-19"]["claude-sonnet-4"], 4.0)

    def test_a_reused_path_does_not_show_the_previous_accounts_usage(self):
        path = self.credential(name="shared.info", uid="first-uid")
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        snapshots.store(pool._entries[0]["id"], pool._entries[0]["account_key"], "domestic",
                        {"by_day": {"2026-09-19": {"m": 9.0}}, "total_credits": 9.0,
                         "requests": 9, "partial": False})
        # Rewrite the same path for another account.
        self.credential(name="shared.info", uid="second-uid")
        replacement = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        converter.CONFIG["usage_daily_accounts"] = None
        converter._publish_usage_daily(replacement)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 0.0)

    def test_a_live_snapshot_is_not_overwritten_by_the_cache(self):
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        entry = pool._entries[0]
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        snapshots.store(entry["id"], entry["account_key"], "domestic",
                        {"by_day": {}, "total_credits": 1.0, "requests": 1, "partial": False})
        converter.CONFIG["usage_daily_accounts"] = {entry["id"]: {
            "identity": entry["account_key"], "site": "domestic", "by_day": {},
            "total_credits": 99.0, "requests": 99,
            "partial": False, "fetched_at": time.time()}}
        converter._publish_usage_daily(pool)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 99.0)

    def test_deleting_a_credential_forgets_its_cached_usage(self):
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        entry = pool._entries[0]
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        snapshots.store(entry["id"], entry["account_key"], "domestic",
                        {"by_day": {}, "total_credits": 1.0, "requests": 1, "partial": False})
        converter.CONFIG["usage_daily_accounts"] = {entry["id"]: {
            "identity": entry["account_key"], "site": "domestic", "by_day": {},
            "total_credits": 1.0, "requests": 1, "partial": False, "fetched_at": time.time()}}
        self.assertTrue(pool.remove_file("account.info"))
        self.assertEqual(UsageSnapshots(self.path).accounts(), {})
        # The live aggregate row must go too, or a re-add would resurrect the old figure.
        self.assertNotIn(entry["id"], converter.CONFIG["usage_daily_accounts"])

    def test_re_adding_a_deleted_credential_starts_with_no_usage(self):
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        entry = pool._entries[0]
        converter.CONFIG["usage_snapshots"] = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"].store(entry["id"], entry["account_key"], "domestic",
                                                  {"by_day": {}, "total_credits": 5.0, "requests": 5,
                                                   "partial": False})
        converter.CONFIG["usage_daily_accounts"] = {entry["id"]: {
            "identity": entry["account_key"], "site": "domestic", "by_day": {},
            "total_credits": 5.0, "requests": 5, "partial": False, "fetched_at": time.time()}}
        pool.remove_file("account.info")
        # Re-add the same path and re-publish from a cold aggregate.
        self.credential()
        revived = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        converter._publish_usage_daily(revived)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 0.0)

    def test_deleting_a_credential_also_drops_its_persisted_cooldowns(self):
        """The two cleanups are independent; deletion must run both."""
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json",
                                        cooldowns_path=self.root / "credential-cooldowns.json")
        entry = pool._entries[0]
        converter.CONFIG["usage_snapshots"] = UsageSnapshots(self.path)
        pool.cooldown(entry["cm"], reason="backend HTTP 401")
        converter.CONFIG["usage_snapshots"].store(entry["id"], entry["account_key"], "domestic",
                                                  {"by_day": {}, "total_credits": 1.0, "requests": 1,
                                                   "partial": False})
        self.assertTrue(pool.remove_file("account.info"))
        self.assertEqual(pool.cooldown_detail(), [])
        self.assertEqual(UsageSnapshots(self.path).accounts(), {})

    def test_a_write_failure_during_a_real_sync_still_publishes_usage(self):
        """Drive the real maintenance path, so the patched writer is genuinely exercised."""
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        converter.CONFIG["usage_daily_accounts"] = None
        usage = {"by_day": {"2026-09-19": {"m": 3.0}}, "total_credits": 3.0, "requests": 2,
                 "partial": False}
        with patch.object(converter.credits_mod, "fetch_request_usage", return_value=usage),                 patch("app.usage_snapshots.os.replace", side_effect=OSError("read-only")) as writer:
            stale = converter._sync_usage(pool)
        self.assertTrue(writer.called, "the cache writer was never reached")
        self.assertEqual(stale, set())
        # The failure is recorded, but the dashboard still shows the freshly fetched figures.
        self.assertIsNotNone(snapshots.last_error)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 3.0)
        self.assertNotIn("stale_accounts", converter.CONFIG["usage_daily"])

    def test_a_concurrent_deletion_during_publication_does_not_raise(self):
        """Publication must aggregate a consistent snapshot, not index the live map.

        forget_usage() pops from the same dictionary under the pool lock. A reader that copies the
        keys and then indexes the live dict therefore raises KeyError when a credential is deleted
        mid-pass, which aborts the usage pass. The live rows are seeded directly so the hook below
        fires only in the aggregation loop, and the lock is asserted rather than timed.
        """
        first = self.credential(name="first.info", uid="first-uid")
        second = self.credential(name="second.info", uid="second-uid")
        pool = converter.CredentialPool([first, second], blocks_path=self.root / "blocks.json")
        rows = {}
        for entry in pool._entries:
            rows[entry["id"]] = {"identity": entry["account_key"], "site": "domestic",
                                 "by_day": {"2026-09-19": {"m": 3.0}}, "total_credits": 3.0,
                                 "requests": 3, "partial": False, "fetched_at": time.time()}
        converter.CONFIG["usage_snapshots"] = UsageSnapshots(self.path)   # Empty store.
        converter.CONFIG["usage_daily_accounts"] = dict(rows)
        victim = pool._entries[1]["id"]

        original = converter._usage_row_expired
        observed = []

        def expiring(snap):
            if not observed:
                observed.append(True)
                # Runs inside the reader's aggregation loop: assert mutual exclusion, then delete
                # the row the reader has not reached yet.
                observed.append(pool._lock._is_owned())
                converter.CONFIG["usage_daily_accounts"].pop(victim, None)
            return original(snap)

        with patch.object(converter, "_usage_row_expired", side_effect=expiring):
            converter._publish_usage_daily(pool)          # Must not raise KeyError.
        self.assertEqual(observed[:2], [True, True], "interleaving not reached, or read unlocked")
        self.assertIsNotNone(converter.CONFIG["usage_daily"])
        self.assertNotIn(victim, converter.CONFIG["usage_daily_accounts"])
        # The pass aggregates the snapshot it took at entry, so a row deleted mid-pass is still
        # counted this once and simply disappears on the next pass. Consistency is the contract.
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 6.0)
        converter._publish_usage_daily(pool)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 3.0)

    def test_adoption_holds_the_pool_lock_while_reading_the_map(self):
        """_adopt_cached_usage() reads and prunes the live map, so it needs the same lock."""
        path = self.credential(name="shared.info", uid="first-uid")
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        entry = pool._entries[0]
        snapshots = UsageSnapshots(self.path)
        snapshots.store(entry["id"], entry["account_key"], "domestic",
                        {"by_day": {"2026-09-19": {"m": 7.0}}, "total_credits": 7.0,
                         "requests": 7, "partial": False})
        converter.CONFIG["usage_snapshots"] = snapshots
        # A live row whose recorded owner no longer matches forces the prune path.
        converter.CONFIG["usage_daily_accounts"] = {entry["id"]: {
            "identity": "0" * 64, "site": "domestic", "by_day": {},
            "total_credits": 1.0, "requests": 1, "fetched_at": time.time()}}

        original = UsageSnapshots.accounts
        observed = []

        def accounts_probe(self):
            rows = original(self)
            if not observed:
                observed.append(pool._lock._is_owned())
            return rows

        with patch.object(UsageSnapshots, "accounts", accounts_probe):
            converter._publish_usage_daily(pool)
        self.assertEqual(observed, [True], "the live map was read without the pool lock")
        # The mismatched row was pruned, and the cache refilled it for the current account.
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 7.0)

    def test_a_malformed_partial_flag_is_rejected_not_coerced(self):
        """A non-boolean partial must never be masked into a legitimate-looking flag.

        bool("false") is True, so coercing before the store sees the value would persist a
        wrong completeness flag for durable state. The raw value is passed through instead.
        """
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        converter.CONFIG["usage_daily_accounts"] = None
        for malformed in ("false", "true", 1, 0, None, [], {}):
            with self.subTest(partial=malformed):
                snapshots._data.clear()
                self.path.unlink(missing_ok=True)
                usage = {"by_day": {"2026-09-19": {"m": 2.0}}, "total_credits": 2.0,
                         "requests": 1, "partial": malformed}
                with patch.object(converter.credits_mod, "fetch_request_usage", return_value=usage):
                    converter._sync_usage(pool)
                # Nothing durable was written for this account...
                self.assertEqual(snapshots.accounts(), {}, malformed)
                # ...while the live dashboard row still reports the fetched figures.
                self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 2.0)

    def test_a_real_sync_persists_and_survives_a_restart(self):
        """The same path with a healthy writer must leave a restorable cache behind."""
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        converter.CONFIG["usage_snapshots"] = UsageSnapshots(self.path)
        converter.CONFIG["usage_daily_accounts"] = None
        usage = {"by_day": {"2026-09-19": {"m": 4.0}}, "total_credits": 4.0, "requests": 1,
                 "partial": False}
        with patch.object(converter.credits_mod, "fetch_request_usage", return_value=usage):
            converter._sync_usage(pool)
        entry = pool._entries[0]
        cached = UsageSnapshots(self.path).accounts()[entry["id"]]
        self.assertEqual(cached["total_credits"], 4.0)
        self.assertEqual(cached["identity"], entry["account_key"])
        # A cold aggregate seeded from disk reports the same figure, marked stale.
        converter.CONFIG["usage_daily_accounts"] = None
        converter._publish_usage_daily(pool)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 4.0)
        self.assertEqual(converter.CONFIG["usage_daily"]["stale_accounts"], ["account.info"])

    def test_startup_publication_hydrates_from_the_cache(self):
        """The startup path must populate usage without waiting for a maintenance pass."""
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        entry = pool._entries[0]
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        snapshots.store(entry["id"], entry["account_key"], "domestic",
                        {"by_day": {"2026-09-19": {"m": 2.0}}, "total_credits": 2.0,
                         "requests": 2, "partial": False})
        # Exactly what startup does, with a cold in-memory aggregate.
        converter.CONFIG["usage_daily_accounts"] = None
        converter._publish_usage_daily(pool)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 2.0)
        # A restored snapshot is stale until a refresh confirms it.
        self.assertTrue(converter.CONFIG["usage_daily_accounts"][entry["id"]]["stale"])
        self.assertEqual(converter.CONFIG["usage_daily"]["stale_accounts"], ["account.info"])
        # A restored snapshot is an incomplete view, so the aggregate marks itself partial.
        self.assertTrue(converter.CONFIG["usage_daily"]["partial"])

    def test_a_refresh_clears_only_that_accounts_staleness(self):
        first = self.credential(name="a.info", uid="uid-a")
        second = self.credential(name="b.info", uid="uid-b")
        pool = converter.CredentialPool([first, second], blocks_path=self.root / "blocks.json")
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        for entry in pool.entries():
            snapshots.store(entry["id"], entry["account_key"], "domestic",
                            {"by_day": {}, "total_credits": 1.0, "requests": 1, "partial": False})
        converter.CONFIG["usage_daily_accounts"] = None
        converter._publish_usage_daily(pool)
        self.assertEqual(len(converter.CONFIG["usage_daily"]["stale_accounts"]), 2)
        # One account refreshes successfully; the other does not.
        refreshed, other = pool.entries()
        converter.CONFIG["usage_daily_accounts"] = {
            refreshed["id"]: {"identity": refreshed["account_key"], "site": "domestic", "by_day": {},
                               "total_credits": 3.0, "requests": 2, "partial": False,
                               "fetched_at": time.time()}}
        converter._publish_usage_daily(pool, {other["id"]})
        self.assertEqual(converter.CONFIG["usage_daily"]["stale_accounts"], ["b.info"])
        # The failed account keeps its last good figure (1.0) alongside the refreshed 3.0.
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 4.0)

    def test_a_stale_restored_row_stops_being_shown_once_it_ages_out(self):
        path = self.credential()
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        entry = pool._entries[0]
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        snapshots.store(entry["id"], entry["account_key"], "domestic",
                        {"by_day": {}, "total_credits": 5.0, "requests": 1, "partial": False})
        with snapshots._lock:
            snapshots._data[entry["id"]]["fetched_at"] = time.time() - converter.USAGE_CACHE_MAX_AGE_S - 60
        converter.CONFIG["usage_daily_accounts"] = None
        converter._publish_usage_daily(pool)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 0.0)
        self.assertEqual(snapshots.accounts(), {})          # The aged row is dropped, not kept.

    def test_an_account_without_a_uid_never_owns_durable_usage(self):
        """Two accounts with no UID hash alike, so neither may key a cached snapshot."""
        path = self.root / "anonymous.info"
        now = time.time()
        path.write_text(json.dumps({"account": {}, "auth": {
            "accessToken": "t", "refreshToken": "r", "domain": "www.codebuddy.cn",
            "expiresAt": (now + 86400) * 1000, "lastRefreshTime": now * 1000}}), encoding="utf-8")
        pool = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        entry = pool._entries[0]
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        # Even a row written directly for that hash is not adopted, because the account cannot
        # prove it owns it.
        snapshots.store(entry["id"], entry["account_key"], "domestic",
                        {"by_day": {}, "total_credits": 8.0, "requests": 1, "partial": False})
        converter.CONFIG["usage_daily_accounts"] = None
        converter._publish_usage_daily(pool)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 0.0)

    def test_hydrated_rows_are_rechecked_against_current_ownership(self):
        """A row hydrated on an earlier pass must not survive a later path reuse."""
        path = self.credential(name="shared.info", uid="first-uid")
        first = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        snapshots = UsageSnapshots(self.path)
        converter.CONFIG["usage_snapshots"] = snapshots
        snapshots.store(first._entries[0]["id"], first._entries[0]["account_key"], "domestic",
                        {"by_day": {}, "total_credits": 7.0, "requests": 1, "partial": False})
        # Hydrate as a live row, as an earlier publication would have.
        converter.CONFIG["usage_daily_accounts"] = None
        converter._publish_usage_daily(first)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 7.0)
        # The path is then reused by another account.
        self.credential(name="shared.info", uid="second-uid")
        replacement = converter.CredentialPool([path], blocks_path=self.root / "blocks.json")
        converter._publish_usage_daily(replacement)
        self.assertEqual(converter.CONFIG["usage_daily"]["total_credits"], 0.0)


if __name__ == "__main__":
    unittest.main()
