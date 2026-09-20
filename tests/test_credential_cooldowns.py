#!/usr/bin/env python3
"""Test that credential cooldowns survive a restart without ever over-extending them."""

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
from app.credential_cooldowns import (AUTH_CEILING_S, MAX_ACCOUNTS, MAX_BYTES, MAX_MODELS,
                                      MODEL_CEILING_S, VERSION, CredentialCooldowns)

IDENTITY = "a" * 64
OTHER_IDENTITY = "b" * 64
PROFILE = "cn-cli"
OTHER_PROFILE = "intl-cli"
MODEL = "claude-sonnet-4"


class CooldownStoreTests(unittest.TestCase):
    """Exercise the store directly, with no credential files and no network access."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "credential-cooldowns.json"

    def row(self, **overrides):
        row = {"profile": PROFILE, "fail_until": time.time() + 300, "reason": "backend HTTP 401",
               "failed_at": time.time(), "models": {}}
        row.update(overrides)
        return row

    def write(self, accounts):
        self.path.write_text(json.dumps({"version": VERSION, "accounts": accounts}), encoding="utf-8")

    def test_credential_cooldown_survives_a_restart(self):
        first = CredentialCooldowns(self.path)
        self.assertTrue(first.note_credential(IDENTITY, PROFILE, time.time() + 300, reason="backend HTTP 401"))
        restored = CredentialCooldowns(self.path)
        self.assertGreater(restored.credential_until(IDENTITY, PROFILE), time.time())
        self.assertEqual(restored.restore(IDENTITY, PROFILE)["reason"], "backend HTTP 401")

    def test_model_cooldown_survives_a_restart_and_stays_per_model(self):
        first = CredentialCooldowns(self.path)
        first.note_model(IDENTITY, PROFILE, MODEL, time.time() + 600)
        restored = CredentialCooldowns(self.path)
        self.assertGreater(restored.model_until(IDENTITY, PROFILE, MODEL), time.time())
        self.assertEqual(restored.model_until(IDENTITY, PROFILE, "another-model"), 0.0)

    def test_repeated_restarts_never_extend_a_deadline(self):
        first = CredentialCooldowns(self.path)
        first.note_credential(IDENTITY, PROFILE, time.time() + 300)
        original = json.loads(self.path.read_text())["accounts"][IDENTITY]["fail_until"]
        for _ in range(5):
            store = CredentialCooldowns(self.path)
            store.note_credential(IDENTITY, PROFILE, store.credential_until(IDENTITY, PROFILE))
        self.assertLessEqual(json.loads(self.path.read_text())["accounts"][IDENTITY]["fail_until"],
                             original + 0.01)

    def test_the_writer_never_produces_a_file_the_reader_rejects(self):
        """A file the reader discards would lose every cooldown it held, not just one row."""
        now = time.time()
        store = CredentialCooldowns(self.path)
        # Fill the table to capacity in memory, then write once: the byte bound must hold
        # even at the worst case of MAX_ACCOUNTS x MAX_MODELS x longest strings.
        with store._lock:
            for index in range(MAX_ACCOUNTS):
                identity = f"{index:064x}"
                store._data[identity] = {"profile": PROFILE, "fail_until": now + 300,
                                         "reason": "x" * 256, "failed_at": now,
                                         "models": {f"model-name-{model:03d}": now + 600
                                                    for model in range(MAX_MODELS)}}
            content, shed = store._serialize_locked()
            self.assertLessEqual(len(content), MAX_BYTES)
            self.assertTrue(shed)                   # Capacity really did force shedding.
            self.path.write_bytes(content)
        restored = CredentialCooldowns(self.path)
        self.assertGreater(len(restored.detail()), 0)          # The reader still accepts it.
        self.assertLessEqual(self.path.stat().st_size, MAX_BYTES)

    def test_auth_and_model_ceilings_are_enforced_separately(self):
        store = CredentialCooldowns(self.path)
        now = time.time()
        store.note_credential(IDENTITY, PROFILE, now + MODEL_CEILING_S)   # A quota-length deadline.
        store.note_model(IDENTITY, PROFILE, MODEL, now + 100 * MODEL_CEILING_S)
        self.assertLessEqual(store.credential_until(IDENTITY, PROFILE) - now, AUTH_CEILING_S + 1)
        self.assertLessEqual(store.model_until(IDENTITY, PROFILE, MODEL) - now, MODEL_CEILING_S + 1)

    def test_a_deadline_beyond_the_ceiling_is_never_adopted(self):
        now = time.time()
        cases = {"far future": 1e12, "past the auth ceiling": now + AUTH_CEILING_S + 3600,
                 "in the past": now - 60, "bool": True, "string": "nan", "infinity": float("inf")}
        for label, until in cases.items():
            with self.subTest(fail_until=label):
                self.write({IDENTITY: self.row(fail_until=until)})
                self.assertEqual(CredentialCooldowns(self.path).credential_until(IDENTITY, PROFILE), 0.0)
        for label, until in {"far future": 1e12, "past the model ceiling": now + MODEL_CEILING_S + 3600,
                             "huge integer": 10 ** 400}.items():
            with self.subTest(models=label):
                self.write({IDENTITY: self.row(models={MODEL: until})})
                self.assertEqual(CredentialCooldowns(self.path).model_until(IDENTITY, PROFILE, MODEL), 0.0)

    def test_expired_rows_are_dropped_rather_than_restored(self):
        self.write({IDENTITY: self.row(fail_until=time.time() - 1, models={MODEL: time.time() - 1})})
        store = CredentialCooldowns(self.path)
        self.assertEqual(store.detail(), [])
        self.assertEqual(store.restore(IDENTITY, PROFILE), {})

    def test_prune_removes_dead_rows_and_keeps_live_ones(self):
        store = CredentialCooldowns(self.path)
        store.note_model(IDENTITY, PROFILE, "live-model", time.time() + 600)
        with store._lock:                      # Write an already-expired breaker directly.
            store._data[IDENTITY]["fail_until"] = time.time() - 1
        self.assertTrue(store.prune()["changed"])
        self.assertEqual(set(store.restore(IDENTITY, PROFILE)["models"]), {"live-model"})
        self.assertEqual(store.credential_until(IDENTITY, PROFILE), 0.0)

    def test_a_full_table_reports_shedding_instead_of_claiming_success(self):
        """A caller must not be told its cooldown is durable when it was shed for capacity."""
        now = time.time()
        store = CredentialCooldowns(self.path)
        with store._lock:
            for index in range(MAX_ACCOUNTS):
                store._data[f"{index:064x}"] = {"profile": PROFILE, "fail_until": now + 300,
                                                "reason": "x" * 256, "failed_at": now,
                                                "models": {f"model-name-{model:03d}": now + 600
                                                           for model in range(MAX_MODELS)}}
        self.assertFalse(store.note_model("f" * 64, PROFILE, "new-model", now + 600))
        self.assertEqual(store.last_error, "capacity")

    def test_invalid_deadlines_are_rejected_without_raising(self):
        store = CredentialCooldowns(self.path)
        for label, until in (("huge integer", 10 ** 400), ("nan", float("nan")), ("none", None),
                             ("string", "abc"), ("infinity", float("inf"))):
            with self.subTest(until=label):
                self.assertFalse(store.note_credential(IDENTITY, PROFILE, until))
                self.assertFalse(store.note_model(IDENTITY, PROFILE, MODEL, until))
        self.assertEqual(store.detail(), [])

    def test_untrusted_snapshots_are_rejected_wholesale(self):
        payloads = {
            "not json": b"nope",
            "empty object": b"{}",
            "unsupported version": json.dumps({"version": 99, "accounts": {}}).encode(),
            "boolean version": json.dumps({"version": True, "accounts": {}}).encode(),
            "extra top-level field": json.dumps({"version": VERSION, "accounts": {}, "x": 1}).encode(),
            "non-hex identity": json.dumps({"version": VERSION, "accounts": {"zzz": self.row()}}).encode(),
            "extra row field": json.dumps({"version": VERSION,
                                           "accounts": {IDENTITY: {**self.row(), "extra": 1}}}).encode(),
            "duplicate keys": b'{"version":1,"version":1,"accounts":{}}',
            "infinity literal": ('{"version":1,"accounts":{"' + IDENTITY + '":{"profile":"cn-cli",'
                                 '"fail_until":Infinity,"reason":"","failed_at":0,"models":{}}}}').encode(),
            "bad profile token": json.dumps({"version": VERSION, "accounts": {
                IDENTITY: {**self.row(), "profile": "bad profile!"}}}).encode(),
            "oversized file": b"x" * (MAX_BYTES + 1),
        }
        for label, content in payloads.items():
            with self.subTest(payload=label):
                self.path.write_bytes(content)
                self.assertEqual(CredentialCooldowns(self.path).detail(), [])

    def test_oversized_tables_are_rejected_on_load(self):
        accounts = {f"{index:064x}": self.row() for index in range(MAX_ACCOUNTS + 1)}
        self.write(accounts)
        self.assertEqual(CredentialCooldowns(self.path).detail(), [])
        self.write({IDENTITY: self.row(models={f"m{index}": time.time() + 300
                                               for index in range(MAX_MODELS + 1)})})
        self.assertEqual(CredentialCooldowns(self.path).detail(), [])

    def test_writes_reject_values_the_reader_would_refuse(self):
        store = CredentialCooldowns(self.path)
        now = time.time()
        self.assertFalse(store.note_credential("not-a-hash", PROFILE, now + 300))
        self.assertFalse(store.note_credential(IDENTITY, "bad profile!", now + 300))
        self.assertFalse(store.note_model(IDENTITY, PROFILE, "bad\nmodel", now + 300))
        self.assertEqual(store.detail(), [])

    def test_slash_qualified_model_names_round_trip(self):
        """Vendor-qualified identifiers may contain slashes; the reader must accept them."""
        store = CredentialCooldowns(self.path)
        store.note_model(IDENTITY, PROFILE, "vendor/model-v2", time.time() + 600)
        self.assertGreater(CredentialCooldowns(self.path).model_until(IDENTITY, PROFILE, "vendor/model-v2"),
                           time.time())

    def test_reused_path_does_not_inherit_another_products_cooldowns(self):
        store = CredentialCooldowns(self.path)
        store.note_credential(IDENTITY, PROFILE, time.time() + 300)
        store.note_credential(IDENTITY, OTHER_PROFILE, time.time() + 300)
        self.assertEqual(store.credential_until(IDENTITY, PROFILE), 0.0)
        self.assertGreater(store.credential_until(IDENTITY, OTHER_PROFILE), time.time())

    def test_clear_and_forget_remove_state(self):
        store = CredentialCooldowns(self.path)
        store.note_credential(IDENTITY, PROFILE, time.time() + 300)
        store.note_model(IDENTITY, PROFILE, MODEL, time.time() + 600)
        self.assertTrue(store.clear_model(IDENTITY, PROFILE, MODEL)["changed"])
        self.assertEqual(store.restore(IDENTITY, PROFILE)["models"], {})
        self.assertTrue(store.clear_credential(IDENTITY, PROFILE)["changed"])
        self.assertEqual(store.detail(), [])
        store.note_credential(IDENTITY, PROFILE, time.time() + 300)
        self.assertTrue(store.forget(IDENTITY)["changed"])
        self.assertEqual(json.loads(self.path.read_text())["accounts"], {})

    def test_a_write_failure_is_reported_and_keeps_state_in_memory(self):
        store = CredentialCooldowns(self.path)
        store.note_credential(IDENTITY, PROFILE, time.time() + 300)
        with patch("app.credential_cooldowns.os.replace", side_effect=OSError("read-only")):
            self.assertFalse(store.note_model(IDENTITY, PROFILE, MODEL, time.time() + 600))
        self.assertIsNotNone(store.last_error)
        # The in-memory view still reflects the change even though it was not durable.
        self.assertGreater(store.model_until(IDENTITY, PROFILE, MODEL), time.time())
        self.assertEqual(json.loads(self.path.read_text())["accounts"][IDENTITY]["models"], {})

    def test_a_failed_clear_is_not_reported_as_durable(self):
        store = CredentialCooldowns(self.path)
        store.note_credential(IDENTITY, PROFILE, time.time() + 300)
        with patch("app.credential_cooldowns.os.replace", side_effect=OSError("read-only")):
            outcome = store.clear_credential(IDENTITY, PROFILE)
        self.assertTrue(outcome["changed"])          # The in-memory row was dropped.
        self.assertFalse(outcome["durable"])         # But the disk row survived.
        self.assertIsNotNone(store.last_error)
        self.assertEqual(store.credential_until(IDENTITY, PROFILE), 0.0)     # Cleared in memory.
        self.assertGreater(json.loads(self.path.read_text())["accounts"][IDENTITY]["fail_until"], 0)

    def test_a_clear_retries_a_previously_failed_write(self):
        """A second clear must retry the write, not report "nothing to do" while disk is stale."""
        store = CredentialCooldowns(self.path)
        store.note_credential(IDENTITY, PROFILE, time.time() + 300)
        with patch("app.credential_cooldowns.os.replace", side_effect=OSError("read-only")):
            self.assertFalse(store.clear_credential(IDENTITY, PROFILE)["durable"])
        self.assertGreater(json.loads(self.path.read_text())["accounts"][IDENTITY]["fail_until"], 0)
        # The retry runs with the fault removed.
        outcome = store.clear_credential(IDENTITY, PROFILE)
        self.assertFalse(outcome["changed"])         # Nothing left to change in memory.
        self.assertTrue(outcome["durable"])          # But the stale disk row was cleared.
        self.assertNotIn(IDENTITY, json.loads(self.path.read_text())["accounts"])

    def test_clear_outcomes_distinguish_noop_from_failure(self):
        store = CredentialCooldowns(self.path)
        # Nothing recorded at all: a genuine no-op, which is durable by definition.
        self.assertEqual(store.clear_credential(IDENTITY, PROFILE), {"changed": False, "durable": True})
        self.assertEqual(store.clear_model(IDENTITY, PROFILE, MODEL), {"changed": False, "durable": True})
        self.assertEqual(store.forget(IDENTITY), {"changed": False, "durable": True})

    def test_missing_path_stays_in_memory_only(self):
        store = CredentialCooldowns()
        self.assertTrue(store.note_credential(IDENTITY, PROFILE, time.time() + 300))
        self.assertGreater(store.credential_until(IDENTITY, PROFILE), time.time())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_symlinked_file_is_not_adopted_and_the_target_is_untouched(self):
        target = self.root / "outside.json"
        target.write_text(json.dumps({"version": VERSION, "accounts": {IDENTITY: self.row()}}), encoding="utf-8")
        link = self.root / "link.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        self.assertEqual(CredentialCooldowns(link).detail(), [])
        self.assertTrue(target.exists())

    def test_a_fifo_is_not_opened(self):
        if sys.platform == "win32":
            self.skipTest("FIFOs are a POSIX feature")
        fifo = self.root / "pipe.json"
        os.mkfifo(fifo)
        self.assertEqual(CredentialCooldowns(fifo).detail(), [])   # Must not block.

    def test_snapshot_is_owner_only_where_the_platform_supports_it(self):
        store = CredentialCooldowns(self.path)
        store.note_credential(IDENTITY, PROFILE, time.time() + 300)
        if sys.platform != "win32":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class PoolCooldownPersistenceTests(unittest.TestCase):
    """Verify the pool adopts and records cooldowns through its real public methods."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": str(self.root)}))
        self.enterContext(patch.dict(converter.CONFIG, {"log_path": None, "cred_pool": None, "cred": None,
                                                        "ledger": None, "model_catalogs": {},
                                                        "account_catalogs": None, "model_cache": None}))
        self.enterContext(patch.object(converter, "_log"))
        self.path = self.root / "credential-cooldowns.json"

    def credential(self, name="account.info", uid="synthetic-uid", domain="www.codebuddy.cn",
                   access_token="synthetic-token"):
        now = time.time()
        path = self.root / name
        path.write_text(json.dumps({"account": {"uid": uid}, "auth": {
            "accessToken": access_token, "refreshToken": "synthetic-refresh", "domain": domain,
            "expiresAt": (now + 86400) * 1000, "lastRefreshTime": now * 1000}}), encoding="utf-8")
        return path

    def pool(self, *paths):
        return converter.CredentialPool(list(paths), blocks_path=self.root / "blocks.json",
                                        cooldowns_path=self.path)

    def test_auth_circuit_breaker_survives_a_restart(self):
        first = self.pool(self.credential())
        first.cooldown(first._entries[0]["cm"], reason="backend HTTP 401")
        self.assertFalse(first._healthy(first._entries[0]))
        revived = self.pool(self.credential())
        self.assertGreater(revived._entries[0]["fail_until"], time.time())
        self.assertEqual(revived._entries[0]["last_error"], "backend HTTP 401")
        self.assertFalse(revived._healthy(revived._entries[0]))

    def test_model_cooldown_survives_a_restart_and_stays_per_model(self):
        first = self.pool(self.credential())
        first.note_status(first._entries[0]["cm"], 429, model=MODEL, raw=b"")
        revived = self.pool(self.credential())
        self.assertFalse(revived._model_healthy(revived._entries[0], MODEL))
        self.assertTrue(revived._model_healthy(revived._entries[0], "another-model"))

    def test_repeated_restarts_do_not_extend_the_breaker(self):
        first = self.pool(self.credential())
        first.cooldown(first._entries[0]["cm"], reason="backend HTTP 401")
        original = first._entries[0]["fail_until"]
        for _ in range(4):
            self.pool(self.credential())
        self.assertLessEqual(self.pool(self.credential())._entries[0]["fail_until"], original + 0.01)

    def test_an_explicit_reset_lifts_both_kinds_of_cooldown(self):
        first = self.pool(self.credential())
        entry = first._entries[0]
        first.cooldown(entry["cm"], reason="backend HTTP 401")
        first.note_status(entry["cm"], 429, model=MODEL, raw=b"")
        self.assertTrue(first.clear_cooldowns(entry["cm"], MODEL)["durable"])
        self.assertTrue(first.clear_cooldowns(entry["cm"])["durable"])
        revived = self.pool(self.credential())._entries[0]
        self.assertTrue(self.pool(self.credential())._healthy(revived))
        self.assertTrue(self.pool(self.credential())._model_healthy(revived, MODEL))
        self.assertIsNone(revived.get("last_error"))

    def test_a_same_path_replacement_does_not_inherit_the_previous_accounts_cooldowns(self):
        """A credential file rewritten for another account must not keep the old cooldowns."""
        path = self.credential(name="shared.info", uid="first-uid")
        first = self.pool(path)
        first.cooldown(first._entries[0]["cm"], reason="backend HTTP 401")
        first.note_status(first._entries[0]["cm"], 429, model=MODEL, raw=b"")
        self.assertGreater(self.pool(path)._entries[0]["fail_until"], time.time())

        # Rewrite the same path for a different account, which already has its own cooldowns.
        self.credential(name="shared.info", uid="second-uid")
        second = self.pool(path)
        replacement = second._entries[0]
        self.assertNotEqual(replacement["account_key"], first._entries[0]["account_key"])
        self.assertEqual(replacement["fail_until"], 0.0)
        self.assertEqual(second._model_fail, {})
        self.assertTrue(second._model_healthy(replacement, MODEL))

    def test_in_process_replacement_keeps_the_incoming_accounts_own_breaker(self):
        """The same replacement, but through reload() on one live pool.

        This is the path the gateway actually takes when a credential file changes on disk,
        and it is where an explicit reset must not wipe the *incoming* account's breaker.
        """
        shared = self.credential(name="shared.info", uid="first-uid")
        incoming = self.credential(name="incoming.info", uid="second-uid")
        # The incoming account's breaker is already on disk, as it would be after a restart.
        seed = self.pool(incoming)
        seed.cooldown(seed._entries[0]["cm"], reason="backend HTTP 403")
        identity = seed._entries[0]["account_key"]

        pool = self.pool(shared, incoming)
        self.assertNotEqual(pool._entries[0]["account_key"], identity)
        # Point the shared path at the incoming account and reload in place.
        self.credential(name="shared.info", uid="second-uid")
        pool.reload([shared], reset=True)
        entry = next(e for e in pool._entries if Path(e["id"]).name == "shared.info")
        self.assertEqual(entry["account_key"], identity)
        self.assertGreater(entry["fail_until"], time.time())       # B's breaker survived.
        self.assertEqual(entry["last_error"], "backend HTTP 403")   # Not A's reason.
        self.assertFalse(pool._healthy(entry))

    def test_an_account_without_a_uid_never_keys_durable_state(self):
        """An empty UID still hashes, so every such account would share one identity."""
        path = self.root / "anonymous.info"
        now = time.time()
        path.write_text(json.dumps({"account": {}, "auth": {
            "accessToken": "synthetic-token", "refreshToken": "synthetic-refresh",
            "domain": "www.codebuddy.cn", "expiresAt": (now + 86400) * 1000,
            "lastRefreshTime": now * 1000}}), encoding="utf-8")
        pool = self.pool(path)
        entry = pool._entries[0]
        self.assertFalse(pool._durable_identity(entry))
        pool.cooldown(entry["cm"], reason="backend HTTP 401")
        self.assertFalse(pool._healthy(entry))                     # Still enforced in memory.
        self.assertEqual(pool.cooldown_detail(), [])               # But never written to disk.

    def test_clear_cooldowns_reports_memory_and_durable_separately(self):
        pool = self.pool(self.credential())
        entry = pool._entries[0]
        pool.cooldown(entry["cm"], reason="backend HTTP 401")
        with patch("app.credential_cooldowns.os.replace", side_effect=OSError("read-only")):
            outcome = pool.clear_cooldowns(entry["cm"])
        self.assertTrue(outcome["changed_in_memory"])              # The runtime view is reset.
        self.assertFalse(outcome["durable"])                       # But it is not durable.
        self.assertTrue(pool._healthy(entry))

    def test_a_failed_clear_is_never_reported_as_durable(self):
        """Memory already lacking a row says nothing about whether disk still has it."""
        pool = self.pool(self.credential())
        entry = pool._entries[0]
        pool.note_status(entry["cm"], 429, model=MODEL, raw=b"")
        pool._model_fail.pop((entry["id"], MODEL), None)    # Memory is already clear...
        with patch("app.credential_cooldowns.os.replace", side_effect=OSError("read-only")):
            outcome = pool.clear_cooldowns(entry["cm"], MODEL)
        self.assertFalse(outcome["changed_in_memory"])
        self.assertFalse(outcome["durable"])                 # ...but the disk row survived.
        self.assertFalse(self.pool(entry["cm"].path)._model_healthy(
            self.pool(entry["cm"].path)._entries[0], MODEL))

    def test_a_write_failure_is_reported_operationally_once_per_interval(self):
        pool = self.pool(self.credential())
        entry = pool._entries[0]
        with patch("app.credential_cooldowns.os.replace", side_effect=OSError("read-only")):
            for _ in range(3):
                pool.cooldown(entry["cm"], reason="backend HTTP 401")
        warnings = [call for call in converter._log.call_args_list
                    if "持久化失败" in str(call.args[0] if call.args else "")]
        self.assertEqual(len(warnings), 1)                          # Rate limited, not per failure.

    def test_a_replacement_inherits_only_its_own_persisted_cooldowns(self):
        old = self.credential(name="shared.info", uid="first-uid")
        first = self.pool(old)
        first.cooldown(first._entries[0]["cm"], reason="backend HTTP 401")
        # Give the incoming account its own persisted breaker before the swap.
        incoming = self.credential(name="incoming.info", uid="second-uid")
        probe = self.pool(incoming)
        probe.cooldown(probe._entries[0]["cm"], reason="backend HTTP 403")
        identity = probe._entries[0]["account_key"]
        self.credential(name="shared.info", uid="second-uid")
        revived = self.pool(old)
        self.assertEqual(revived._entries[0]["account_key"], identity)
        self.assertGreater(revived._entries[0]["fail_until"], time.time())
        self.assertEqual(revived._entries[0]["last_error"], "backend HTTP 403")

    def test_an_explicit_reload_lifts_the_breaker_and_keeps_model_cooldowns(self):
        path = self.credential()
        first = self.pool(path)
        entry = first._entries[0]
        first.cooldown(entry["cm"], reason="backend HTTP 401")
        first.note_status(entry["cm"], 429, model=MODEL, raw=b"")
        first.reload([path], reset=True)          # The upstream explicit-reload path.
        self.assertTrue(first._healthy(entry))
        self.assertFalse(first._model_healthy(entry, MODEL))
        revived = self.pool(path)                 # A restart must not resurrect the breaker.
        self.assertTrue(revived._healthy(revived._entries[0]))
        self.assertFalse(revived._model_healthy(revived._entries[0], MODEL))

    def test_a_same_account_token_refresh_keeps_cooldowns(self):
        """A refresh writes a new token for the same account; the 429 cooldown must persist."""
        path = self.credential()
        first = self.pool(path)
        entry = first._entries[0]
        first.note_status(entry["cm"], 429, model=MODEL, raw=b"")
        generation = entry["generation"]
        # A real refresh: the token file is rewritten and the manager is invalidated, which is
        # what CredentialManager itself does after writing new credentials.
        self.credential(access_token="rotated-access-token")
        entry["cm"].invalidate()
        first.reload([path], reset=False)
        self.assertNotEqual(entry["generation"], generation)   # The reload really re-read it.
        self.assertEqual(entry["account_key"], first._entries[0]["account_key"])
        self.assertFalse(first._model_healthy(entry, MODEL))
        # And it survives a restart too.
        self.assertFalse(self.pool(path)._model_healthy(self.pool(path)._entries[0], MODEL))

    def test_deleting_and_re_adding_a_credential_starts_clean(self):
        path = self.credential()
        first = self.pool(path)
        first.cooldown(first._entries[0]["cm"], reason="backend HTTP 401")
        self.assertTrue(first.remove_file("account.info"))
        self.assertEqual(first.cooldown_detail(), [])
        revived = self.pool(self.credential())
        self.assertEqual(revived._entries[0]["fail_until"], 0.0)

    def test_international_auto_is_isolated_per_routed_model(self):
        """`auto` maps to `default-model` internationally, so the two must not alias."""
        path = self.credential(domain="www.codebuddy.ai")
        pool = self.pool(path)
        entry = pool._entries[0]
        self.assertEqual(entry["profile"], "intl-cli")
        pool.note_status(entry["cm"], 429, model="auto", raw=b"")
        revived = self.pool(path)
        self.assertFalse(revived._model_healthy(revived._entries[0], "auto"))
        self.assertIn(("default-model"), [model for _, model in revived._model_fail])

    def test_a_pool_without_a_path_keeps_cooldowns_in_memory(self):
        pool = converter.CredentialPool([self.credential()], blocks_path=self.root / "blocks.json")
        pool.cooldown(pool._entries[0]["cm"], reason="backend HTTP 401")
        self.assertFalse(pool._healthy(pool._entries[0]))
        self.assertEqual(list(self.root.glob("credential-cooldowns.json")), [])

    def test_a_write_failure_is_visible_and_still_degrades_gracefully(self):
        pool = self.pool(self.credential())
        entry = pool._entries[0]
        with patch("app.credential_cooldowns.os.replace", side_effect=OSError("read-only")):
            pool.cooldown(entry["cm"], reason="backend HTTP 401")
        self.assertFalse(pool._healthy(entry))            # Runtime behaviour is unchanged.
        self.assertTrue(pool.cooldown_storage()["degraded"])
        self.assertIsNotNone(pool.cooldown_storage()["last_error"])

    def test_cooldowns_are_visible_to_the_admin_snapshot(self):
        pool = self.pool(self.credential())
        entry = pool._entries[0]
        pool.cooldown(entry["cm"], reason="backend HTTP 401")
        pool.note_status(entry["cm"], 429, model=MODEL, raw=b"")
        row = self.pool(self.credential()).snapshot()[0]    # A restarted pool must still report both.
        self.assertFalse(row["healthy"])
        self.assertIn(MODEL, row["model_cooldowns"])


if __name__ == "__main__":
    unittest.main()
