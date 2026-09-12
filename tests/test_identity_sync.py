"""账号/租户目录、路径复用和启动屏障回归；仅临时合成凭据与 mock，无联网。

运行：.venv/bin/python -B -m unittest -v tests/test_identity_sync.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app import client_profiles
import converter as c
from app import credits


DOMAINS = {"cn-cli": "www.codebuddy.cn", "cn-work": "www.workbuddy.cn",
           "intl-cli": "www.codebuddy.ai", "intl-work": "www.workbuddy.ai"}


def model(name, credits=None):
    value = {"id": name, "supportsToolCall": True}
    if credits is not None:
        value["credits"] = credits
    return value


def credential(uid="A", profile="cn-cli", tenant="tenant", token=None):
    return {"account": {"uid": uid, "enterpriseId": tenant}, "auth": {
        "domain": DOMAINS[profile], "accessToken": token or "synthetic-" + uid,
        "refreshToken": "synthetic-refresh", "expiresAt": (time.time() + 86400) * 1000,
        "lastRefreshTime": time.time() * 1000}}


def balance(token, **kwargs):
    return {"credits": 100, "intl": kwargs.get("domain", "").endswith(".ai"),
            "segments": [], "soonest_expiry": None}


class IdentitySyncTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        self.enterContext(patch.object(c, "app", FastAPI()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": str(self.root),
            "CODEBUDDY2API_LOG": "", "CODEBUDDY2API_KEY": ""}))
        self.enterContext(patch.dict(c.CONFIG, {"cred_pool": None, "cred": None, "ledger": None,
            "model_cache": None, "model_catalogs": {}, "account_catalogs": None,
            "models_remote": None, "models_intl": None, "model_guard": True, "log_path": None}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(c, "_log"))
        self.credit_fetch = self.enterContext(patch.object(credits, "fetch_credits", side_effect=balance))
        self.catalog_fetch = self.enterContext(patch.object(credits, "fetch_model_catalog",
            side_effect=lambda token, **kw: [model("a-only"), model("shared")]
            if kw["uid"] == "A" else [model("shared")]))
        self.addCleanup(c.invalidate_model_table)

    def write_credential(self, name="slot.info", **kwargs):
        # 与真实导入一致使用原子替换，避免同大小原地写入命中文件系统时间戳粒度。
        return c.atomic_write_credential(self.root, name, json.dumps(credential(**kwargs)).encode("utf-8"))

    def configure(self, *paths):
        self.pool = c.CredentialPool(list(paths))
        self.ledger = credits.CreditLedger(self.root / "credits-ledger.json")
        self.pool.set_ledger(self.ledger)
        self.cache = credits.ModelCatalogCache(self.root / "model-catalog.json")
        c.CONFIG.update(cred_pool=self.pool, ledger=self.ledger, model_cache=self.cache)
        c._publish_model_cache()

    def sync(self):
        c._housekeep_once(self.pool, self.ledger, pending_only=True)

    def key(self, entry=None):
        entry = entry or self.pool.entries()[0]
        return client_profiles.catalog_cache_key(entry["profile"], entry["account_key"])

    def picked_uid(self, name, region="cn"):
        picked = self.pool.headers_for(None, name, region=region)
        return picked[1]["X-User-Id"] if picked else None

    def test_account_fingerprint_is_stable_and_unambiguous(self):
        key = client_profiles.account_key
        self.assertEqual(key("cn-cli", "A", "tenant"), key("cn-cli", "A", "tenant"))
        self.assertNotEqual(key("cn-cli", "a:b", "c"), key("cn-cli", "a", "b:c"))
        self.assertNotEqual(key("cn-cli", "A", "tenant"), key("cn-work", "A", "tenant"))
        self.assertNotEqual(key("cn-cli", "A", "tenant"), key("cn-cli", "A", "other"))
        self.assertEqual(len(key("cn-cli", "A")), 64)
        a = c.CredentialManager(self.write_credential("first.info"))
        b = c.CredentialManager(self.write_credential("second.info", token="another-synthetic-token"))
        self.assertEqual(a.summary()["account_key"], b.summary()["account_key"])
        old = client_profiles.catalog_cache_key("cn-cli")
        new = client_profiles.catalog_cache_key("cn-cli", a.summary()["account_key"])
        self.assertTrue(new.startswith(old + ":account:"))
        with patch.object(client_profiles, "CLI_VERSION", "future-test-version"):
            self.assertNotEqual(new, client_profiles.catalog_cache_key("cn-cli", a.summary()["account_key"]))

    def test_each_account_fetches_and_only_declared_shared_models_rotate(self):
        self.configure(self.write_credential("a.info"), self.write_credential("b.info", uid="B"))
        self.sync()
        self.assertEqual([call.kwargs["uid"] for call in self.catalog_fetch.call_args_list], ["A", "B"])
        self.assertEqual({self.picked_uid("a-only") for _ in range(6)}, {"A"})
        self.assertEqual({self.picked_uid("shared") for _ in range(6)}, {"A", "B"})
        self.assertEqual(len(c.CONFIG["account_catalogs"]), 2)
        self.assertNotEqual(*[self.key(entry) for entry in self.pool.entries()])

    def test_new_import_forces_own_catalog_despite_fresh_profile_or_account_cache(self):
        self.configure(self.write_credential("a.info"))
        self.sync()
        b = self.write_credential("b.info", uid="B")
        b_identity = c.CredentialManager(b).summary()["account_key"]
        self.cache.put(client_profiles.catalog_cache_key("cn-cli", b_identity), [model("obsolete")])
        self.pool.reload([b])
        self.catalog_fetch.reset_mock()
        self.sync()
        self.assertEqual([call.kwargs["uid"] for call in self.catalog_fetch.call_args_list], ["B"])
        self.assertFalse(self.pool._eligible(self.pool.entries()[1], "a-only"))
        self.assertFalse(self.pool._eligible(self.pool.entries()[1], "obsolete"))

    def test_same_account_token_relogin_forces_refresh_and_preserves_quota_cooldown(self):
        path = self.write_credential()
        self.configure(path)
        self.sync()
        original_key = self.key()
        self.pool.note_status(self.pool.first(), 429, model="shared")
        data = credential(token="synthetic-new-login")
        c._store_credential(self.root, path.name, json.dumps(data).encode(), "A")
        self.catalog_fetch.reset_mock()
        self.sync()
        self.catalog_fetch.assert_called_once()
        self.assertEqual(self.catalog_fetch.call_args.args[0], "synthetic-new-login")
        self.assertEqual(self.key(), original_key)
        self.assertEqual(self.picked_uid("shared"), None)
        self.assertEqual(self.picked_uid("a-only"), "A")
        self.assertEqual(self.ledger.entry(str(path))["credits"]["credits"], 100)

    def test_new_identity_does_not_borrow_cache_or_ledger_before_or_after_failure(self):
        path = self.write_credential(profile="intl-cli")
        self.configure(path)
        self.sync()
        self.pool._sticky["intl:old"] = (str(path), time.time())
        self.pool.note_status(self.pool.first(), 429, model="a-only")
        old_key = self.key()
        self.write_credential(uid="B", profile="intl-cli")
        self.pool.reload([path])
        self.assertNotEqual(self.key(), old_key)
        self.assertFalse(self.pool._sticky)
        self.assertFalse(self.pool._model_fail)
        self.assertEqual(self.ledger.entry(str(path))["credits"], {})
        self.assertIsNone(self.picked_uid("a-only", "intl"))
        self.credit_fetch.side_effect = RuntimeError("synthetic unavailable")
        self.sync()
        self.assertIsNone(self.picked_uid("a-only", "intl"))
        self.assertEqual(c.current_models("intl"), [])

    def test_relogin_new_identity_fetches_despite_previous_fresh_cache(self):
        path = self.write_credential()
        self.configure(path)
        self.sync()
        self.write_credential(uid="B")
        self.pool.reload([path])
        self.catalog_fetch.reset_mock()
        self.sync()
        self.assertEqual(self.catalog_fetch.call_args.kwargs["uid"], "B")
        self.assertIsNone(self.picked_uid("a-only"))
        self.assertEqual(self.picked_uid("shared"), "B")

    def test_same_account_cached_catalog_survives_fetch_failure(self):
        path = self.write_credential()
        self.configure(path)
        self.sync()
        self.write_credential(token="synthetic-new-login")
        self.pool.reload([path])
        self.catalog_fetch.side_effect = RuntimeError("synthetic unavailable")
        self.sync()
        self.assertEqual(self.picked_uid("a-only"), "A")
        self.assertTrue(self.pool.sync_pending("cn"))

    def test_unchanged_account_honors_catalog_ttl_on_periodic_sync(self):
        self.configure(self.write_credential())
        self.sync()
        self.pool._queue_sync(self.pool.entries()[0]["id"])
        self.catalog_fetch.reset_mock()
        self.sync()
        self.catalog_fetch.assert_not_called()

    def test_empty_and_unknown_account_catalogs_never_borrow_same_profile(self):
        self.configure(self.write_credential("a.info"), self.write_credential("b.info", uid="B"))
        entries = self.pool.entries()
        self.cache.put(self.key(entries[0]), [model("shared")])
        for models in (None, []):
            if models is not None:
                self.cache.put(self.key(entries[1]), models)
            c._publish_model_cache()
            self.assertEqual({self.picked_uid("shared") for _ in range(4)}, {"A"})
            self.assertFalse(self.pool._eligible(entries[1], "auto"))

    def test_account_catalogs_keep_product_region_and_default_strategies_separate(self):
        self.configure(*(self.write_credential(profile + ".info", profile=profile) for profile in DOMAINS))
        tables = {profile: [model("shared"), model(profile + "-only")] for profile in DOMAINS}
        tables["cn-work"].append(model("auto"))
        tables["intl-cli"].append(model("default-model"))
        self.catalog_fetch.side_effect = lambda token, **kw: tables[
            next(profile for profile, domain in DOMAINS.items() if domain == kw["domain"])]
        self.sync()
        self.assertEqual(len(c.CONFIG["account_catalogs"]), 4)
        for region in ("cn", "intl"):
            seen = {c.profile_for_headers(self.pool.headers_for(None, "shared", region=region)[1])
                    for _ in range(6)}
            self.assertEqual(seen, {region + "-cli", region + "-work"})
            for product in ("cli", "work"):
                profile = region + "-" + product
                selected = self.pool.headers_for(None, profile + "-only", region=region)
                self.assertEqual(c.profile_for_headers(selected[1]), profile)
            self.assertIsNone(self.pool.headers_for(None, "cn-cli-only" if region == "intl" else "intl-cli-only",
                                                   region=region))
        for region, profile in (("cn", "cn-work"), ("intl", "intl-cli")):
            selected = self.pool.headers_for(None, "auto", region=region)
            self.assertEqual(c.profile_for_headers(selected[1]), profile)
        c.CONFIG["model_guard"] = False
        self.assertIsNone(self.pool.headers_for(None, "unlisted", region="cn"))
        self.assertIsNone(self.pool.headers_for(None, "unlisted", region="intl"))

    def test_guard_disabled_does_not_lend_known_owner_capability_or_empty_auto(self):
        self.configure(self.write_credential("a.info"), self.write_credential("b.info", uid="B"))
        self.sync()
        c.CONFIG["model_guard"] = False
        self.assertEqual({self.picked_uid("a-only") for _ in range(6)}, {"A"})
        self.assertEqual({self.picked_uid("explicit-unlisted") for _ in range(6)}, {"A", "B"})
        for entry in self.pool.entries():
            self.cache.put(self.key(entry), [])
        c._publish_model_cache()
        self.assertIsNone(self.picked_uid("auto"))
        self.assertEqual({self.picked_uid("explicit-unlisted") for _ in range(6)}, {"A", "B"})
        self.assertIsNone(self.picked_uid("explicit-unlisted", "intl"))

    def test_zero_balance_account_still_refreshes_own_catalog_but_cannot_route(self):
        self.configure(self.write_credential())
        self.credit_fetch.side_effect = lambda *a, **kw: dict(balance(*a, **kw), credits=0)
        self.sync()
        self.catalog_fetch.assert_called_once()
        self.assertEqual([item["id"] for item in self.cache.models(self.key())], ["a-only", "shared"])
        self.assertIsNone(self.picked_uid("a-only"))

    def test_zero_balance_account_leaves_paid_models_but_keeps_free_ones(self):
        self.catalog_fetch.side_effect = lambda token, **kw: (
            [model("free-only", credits="x0.00"), model("paid-only", credits="x0.03")]
            if kw["uid"] == "A" else [model("paid-only", credits="x0.03")])
        self.configure(self.write_credential("a.info"), self.write_credential("b.info", uid="B"))
        self.sync()
        entries = {entry["cm"].summary()["uid"]: entry for entry in self.pool.entries()}
        self.ledger.update_credits(entries["A"]["id"], {
            "credits": 0, "intl": False, "segments": [], "soonest_expiry": None})
        self.assertFalse(self.pool._eligible(entries["A"], "paid-only"))
        self.assertTrue(self.pool._eligible(entries["A"], "free-only"))
        self.assertEqual({self.picked_uid("paid-only") for _ in range(6)}, {"B"})
        self.assertEqual(self.picked_uid("free-only"), "A")
        # 余额恢复后重新进入付费模型轮询。
        self.ledger.update_credits(entries["A"]["id"], {
            "credits": 100, "intl": False, "segments": [], "soonest_expiry": None})
        self.assertEqual({self.picked_uid("paid-only") for _ in range(6)}, {"A", "B"})

    def test_old_v1_root_and_unbound_profile_caches_never_publish(self):
        self.configure(self.write_credential())
        for group in ("domestic", client_profiles.catalog_cache_key("cn-cli")):
            self.cache.path.write_text(json.dumps({"version": 1, "groups": {
                group: {"models": [model("work-only")], "fetched_at": time.time()}}}))
            c.CONFIG["model_cache"] = credits.ModelCatalogCache(self.cache.path)
            c._publish_model_cache()
            self.assertEqual(c.current_models(), [])
            self.assertIsNone(self.picked_uid("work-only"))
            with self.assertRaises(c.HTTPException) as raised:
                c.guard_model("hy3")
            self.assertEqual(raised.exception.status_code, 503)
            self.assertIn("Retry-After", raised.exception.headers)

    def test_no_trusted_catalog_remains_503_after_retry_queue_cleared(self):
        self.configure(self.write_credential())
        ids = self.pool.begin_sync(all_entries=True)
        self.pool.end_sync(ids)
        self.assertFalse(self.pool.sync_pending())
        with self.assertRaises(c.HTTPException) as raised:
            c.guard_model("hy3")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("Retry-After", raised.exception.headers)

    def test_delete_and_prune_clear_persisted_ledger_before_path_reuse(self):
        for operation in ("remove_file", "prune"):
            with self.subTest(operation=operation):
                path = self.write_credential(profile="intl-cli")
                self.configure(path)
                self.sync()
                if operation == "remove_file":
                    self.assertTrue(self.pool.remove_file(path.name))
                else:
                    path.unlink()
                    self.pool.prune()
                self.assertEqual(credits.CreditLedger(self.ledger.path).entry(str(path)), {})
                self.write_credential(uid="B", profile="intl-cli")
                self.configure(path)
                self.assertEqual(self.ledger.entry(str(path))["credits"], {})
                self.assertIsNone(self.picked_uid("shared", "intl"))

    def test_restart_rebinds_reused_path_and_discards_unmarked_legacy_balance(self):
        path = self.write_credential(profile="intl-cli")
        self.configure(path)
        self.sync()
        self.write_credential(uid="B", profile="intl-cli")
        self.configure(path)
        self.assertEqual(self.ledger.entry(str(path))["credits"], {})
        self.assertEqual(self.ledger.entry(str(path))["identity"], self.pool.first().summary()["account_key"])
        legacy = credits.CreditLedger(self.root / "legacy-ledger.json")
        legacy.update_credits(str(path), balance("", domain=DOMAINS["intl-cli"]))
        self.pool.set_ledger(legacy)
        self.assertEqual(legacy.entry(str(path))["credits"], {})

    def test_restart_same_bound_identity_preserves_ledger(self):
        path = self.write_credential(profile="intl-cli")
        self.configure(path)
        self.sync()
        self.configure(path)
        self.assertEqual(self.ledger.entry(str(path))["credits"]["credits"], 100)
        self.assertEqual(self.picked_uid("shared", "intl"), "A")

    def test_same_uid_different_profiles_and_tenants_coexist_and_oauth_never_overwrites(self):
        original = self.write_credential("A.info")
        before = original.read_bytes()
        self.configure(original)
        work = c._save_oauth_credential(credential(profile="cn-work"))
        tenant = c._save_oauth_credential(credential(tenant="other-tenant"))
        self.assertNotEqual(work, original)
        self.assertNotEqual(tenant, original)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(len(self.pool.entries()), 3)
        self.assertEqual(len({entry["account_key"] for entry in self.pool.entries()}), 3)
        self.assertEqual(c._save_oauth_credential(credential(profile="cn-work", token="new-work")), work)
        self.assertEqual(len(self.pool.entries()), 3)
        duplicate = self.write_credential("duplicate.info")
        self.pool.reload([duplicate])
        self.assertEqual(len(self.pool.entries()), 3)

    def test_explicit_same_filename_import_can_change_identity_and_binds_before_use(self):
        path = self.write_credential()
        self.configure(path)
        self.sync()
        data = credential(uid="B", profile="cn-work")
        c._store_credential(self.root, path.name, json.dumps(data).encode(), "B")
        entry = self.pool.entries()[0]
        self.assertEqual(entry["profile"], "cn-work")
        self.assertEqual(self.ledger.entry(str(path))["identity"], entry["account_key"])
        self.assertEqual(self.ledger.entry(str(path))["credits"], {})
        self.assertIsNone(self.picked_uid("a-only"))

    def test_late_catalog_and_credit_results_cannot_modify_replacement_identity(self):
        path = self.write_credential()
        self.configure(path)
        old_key = self.key()
        def replace_during_catalog(*args, **kwargs):
            self.write_credential(uid="B")
            self.pool.reload([path])
            return [model("a-only")]
        self.catalog_fetch.side_effect = replace_during_catalog
        self.sync()
        self.assertIsNone(self.cache.age(old_key))
        self.assertEqual(self.ledger.entry(str(path))["credits"], {})
        self.assertIsNone(self.picked_uid("a-only"))
        def replace_during_credits(*args, **kwargs):
            self.write_credential(uid="C")
            self.pool.reload([path])
            return balance(*args, **kwargs)
        self.credit_fetch.side_effect = replace_during_credits
        self.sync()
        self.assertEqual(self.pool.first().summary()["uid"], "C")
        self.assertEqual(self.ledger.entry(str(path))["credits"], {})

    def test_latest_header_profile_is_rechecked_without_blocking_normal_intl(self):
        path = self.write_credential(profile="intl-cli")
        self.configure(path)
        self.sync()
        self.assertEqual(self.picked_uid("shared", "intl"), "A")
        manager = self.pool.first()
        original = manager.get_headers
        def change_region():
            self.write_credential(profile="cn-cli")
            return original()
        with patch.object(manager, "get_headers", side_effect=change_region):
            self.assertIsNone(self.picked_uid("shared", "intl"))
        self.assertEqual(self.ledger.entry(str(path))["credits"], {})

    def test_main_publishes_bound_empty_or_nonempty_cache_before_any_thread_or_server(self):
        path = self.write_credential(profile="intl-cli")
        identity = c.CredentialManager(path).summary()["account_key"]
        key = client_profiles.catalog_cache_key("intl-cli", identity)
        ledger = credits.CreditLedger(self.root / "credits-ledger.json")
        ledger.bind_identity(str(path), identity)
        ledger.update_credits(str(path), balance("", domain=DOMAINS["intl-cli"]))
        for models in ([], [model("shared")]):
            cache = credits.ModelCatalogCache(self.root / "model-catalog.json")
            cache.put(key, models)
            cache.put("international", [model("legacy-forbidden")])
            observed = []
            def barrier(*args, **kwargs):
                self.assertEqual(c.CONFIG["ledger"].entry(str(path))["identity"], identity)
                self.assertEqual(c.CONFIG["account_catalogs"][identity]["models"], models)
                self.assertEqual(c.CONFIG["models_intl"], models)
                self.assertNotIn("legacy-forbidden", c.current_models("intl"))
                self.assertEqual(c.current_models("intl"), ["shared"] if models else [])
                self.assertIsNone(c.CONFIG["cred_pool"].headers_for(None, "hy3", region="intl"))
                observed.append(True)
            thread = Mock()
            thread.start.side_effect = barrier
            with patch("sys.argv", ["converter.py", "--auth-file", str(path), "--skip-check"]), \
                    patch.object(c.threading, "Thread", return_value=thread), \
                    patch.object(c.uvicorn, "run", side_effect=barrier), \
                    patch("sys.stderr", new=io.StringIO()):
                c.main()
            self.assertEqual(len(observed), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
