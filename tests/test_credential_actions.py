"""Offline single-account maintenance, authorization, and stale-result contracts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import time
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import converter
from app.admin_api import install_admin
from app.audit_store import AuditStore
from app.control_store import ControlStore
from app.gateway_management import Management
from tests import test_region_routing as fixtures


class CredentialActionTests(unittest.TestCase):
    add_account = fixtures.RegionRoutingTests.add_account
    configure = fixtures.RegionRoutingTests.configure
    handle_upstream = fixtures.RegionRoutingTests.handle_upstream

    def setUp(self):
        fixtures.RegionRoutingTests.setUp(self)
        self.control = ControlStore(self.root / "control.sqlite3")
        self.audit = AuditStore(self.root / "audit.sqlite3")
        self.addCleanup(self.control.close)
        self.addCleanup(self.audit.close)
        converter.CONFIG.update(control_store=self.control, audit_store=self.audit,
            api_key="synthetic-management-key", usage_daily_accounts={}, usage_daily={})
        app = FastAPI()
        app.router.routes = list(converter.app.router.routes)
        install_admin(app, converter.CONFIG, Management(converter))
        self.client = self.enterContext(TestClient(app, base_url="https://testserver",
            headers={"Authorization": "Bearer synthetic-management-key"}))
        self.status_query = self.enterContext(patch.object(converter.credits_mod, "fetch_checkin_status",
            return_value={"ok": False, "state": "available", "code": 0}))
        self.travel_mock = self.enterContext(patch("app.travel.perform",
            return_value={"ok": True, "skipped": True, "state": "traveling", "message": "Buddy 旅行中"}))
        self.entry = self.entries["cn-cli"]
        self.url = "/admin/credentials/" + self.entry["account_key"]

    def post(self, action):
        response = self.client.post(self.url + "/" + action)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"][0]

    def test_single_checkin_is_idempotent_and_does_not_sync(self):
        with patch.object(converter.credits_mod, "daily_checkin", return_value={"ok": True}) as checkin, \
             patch.object(converter, "_sync_credits") as sync:
            self.assertTrue(self.post("checkin")["ok"])
            self.assertTrue(self.post("checkin")["already"])
            self.assertEqual(checkin.call_count, 1)
            self.assertEqual(checkin.call_args.kwargs["uid"], "cn-cli")
            sync.assert_not_called()
        self.assertFalse(self.ledger.checkin_done(self.entries["intl-work"]["id"], time.strftime("%Y-%m-%d")))

    def test_single_sync_never_signs_in_or_claims_trial_and_keeps_other_staleness(self):
        other = self.entries["intl-work"]["id"]
        converter.CONFIG["usage_daily"] = {"stale_accounts": [Path(other).name]}
        with patch.object(converter.credits_mod, "daily_checkin") as checkin, \
             patch.object(converter.trial_rewards, "claim_trial") as trial, \
             patch.object(converter.credits_mod, "fetch_credits", return_value={"credits": 80, "intl": False}) as balance, \
             patch.object(converter.credits_mod, "fetch_request_usage", return_value={"by_day": {}, "total_credits": 0, "requests": 0}) as usage:
            self.assertTrue(self.post("sync")["ok"])
            checkin.assert_not_called()
            trial.assert_not_called()
            self.assertEqual(balance.call_count, 1)
            self.assertEqual(usage.call_count, 1)
            self.assertEqual(usage.call_args.kwargs["uid"], "cn-cli")
        self.assertEqual(set(converter.CONFIG["usage_daily_accounts"]), {self.entry["id"]})
        self.assertEqual(converter.CONFIG["usage_daily"]["stale_accounts"], [Path(other).name])
        self.assertNotIn(self.entry["id"], self.pool._sync_pending)
        self.assertIn(other, self.pool._sync_pending)

    def test_failed_sync_preserves_balance_and_hides_exception(self):
        before = self.ledger.entry(self.entry["id"])["credits"]
        with patch.object(converter.credits_mod, "fetch_credits", side_effect=ValueError("synthetic-secret")):
            result = self.post("sync")
        self.assertFalse(result["ok"])
        self.assertNotIn("synthetic-secret", str(result))
        self.assertEqual(self.ledger.entry(self.entry["id"])["credits"], before)

    def test_partial_sync_reports_incomplete(self):
        with patch.object(converter.credits_mod, "fetch_credits", return_value={"credits": 5, "intl": False, "partial": True}), \
             patch.object(converter.credits_mod, "fetch_request_usage", return_value={"by_day": {}, "total_credits": 0, "requests": 0}):
            self.assertFalse(self.post("sync")["ok"])

    def test_explicit_refresh_only_calls_selected_account_and_releases_lock(self):
        self.allowed_profiles = {"cn-cli"}
        before = self.entry["cm"]._generation
        with patch.object(converter, "_sync_credits") as sync:
            self.assertTrue(self.post("refresh")["ok"])
            sync.assert_not_called()
        self.assertGreater(self.entry["cm"]._generation, before)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(str(self.requests[0].url).endswith("/v2/plugin/auth/token/refresh"))
        self.assertTrue(converter._HOUSEKEEP_LOCK.acquire(blocking=False))
        converter._HOUSEKEEP_LOCK.release()

    def test_stale_checkin_result_not_published(self):
        def changed(*args, **kwargs):
            self.entry["cm"].invalidate()
            return {"ok": True}
        with patch.object(converter.credits_mod, "daily_checkin", side_effect=changed):
            self.assertFalse(self.post("checkin")["ok"])
        self.assertFalse(self.ledger.checkin_done(self.entry["id"], time.strftime("%Y-%m-%d")))

    def test_catalog_sync_queries_only_the_selected_account(self):
        converter.CONFIG["model_cache"] = converter.credits_mod.ModelCatalogCache(self.root / "models.json")
        with patch.object(converter.credits_mod, "fetch_credits", return_value={"credits": 8, "intl": False}), \
             patch.object(converter.credits_mod, "fetch_model_scopes", return_value={"picker": [], "account": []}) as catalog, \
             patch.object(converter.credits_mod, "fetch_request_usage", return_value={"by_day": {}, "total_credits": 0, "requests": 0}):
            self.assertTrue(self.post("sync")["ok"])
            self.assertEqual(catalog.call_count, 1)
            self.assertEqual(catalog.call_args.kwargs["uid"], "cn-cli")

    def test_changed_identity_before_file_lock_does_not_refresh_other_account(self):
        import contextlib
        import json
        @contextlib.contextmanager
        def changed(*args):
            data = json.loads(self.entry["cm"].path.read_text())
            data["account"]["uid"] = "different-owner"
            self.entry["cm"].path.write_text(json.dumps(data))
            yield
        with patch("app.credential_actions.credential_file_lock", changed):
            self.assertFalse(self.post("refresh")["ok"])
        self.assertEqual(self.requests, [])

    def test_batch_sync_has_no_signin_or_trial_side_effects(self):
        def balance(token, **kwargs):
            return {"credits": 8, "intl": kwargs["uid"].startswith("intl-")}
        with patch.object(converter.credits_mod, "fetch_credits", side_effect=balance), \
             patch.object(converter.credits_mod, "fetch_request_usage", return_value={"by_day": {}, "total_credits": 0, "requests": 0}), \
             patch.object(converter.credits_mod, "daily_checkin") as checkin, \
             patch.object(converter.trial_rewards, "claim_trial") as trial:
            response = self.client.post("/admin/sync")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"], response.text)
            self.assertEqual(len(response.json()["results"]), 4)
            checkin.assert_not_called()
            trial.assert_not_called()


    def test_busy_and_missing_actions_fail_without_upstream_calls(self):
        with converter._HOUSEKEEP_LOCK:
            self.assertEqual(self.client.post(self.url + "/sync").status_code, 409)
        self.assertEqual(self.client.post("/admin/credentials/missing/sync").status_code, 404)
        self.assertEqual(self.client.post(self.url + "/invalid").status_code, 404)
        self.assertEqual(self.requests, [])

    def test_disabled_account_skipped_including_refresh(self):
        Management(converter).admin_set_credential_enabled(self.entry["account_key"], False)
        for action in ("checkin", "sync", "refresh"):
            self.assertTrue(self.post(action)["skipped"])
        self.assertEqual(self.requests, [])

    def test_batch_checkin_does_not_run_maintenance(self):
        with patch.object(converter.credits_mod, "daily_checkin", return_value={"ok": True}) as checkin, \
             patch.object(converter, "_housekeep_once") as full:
            response = self.client.post("/admin/checkin")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["results"]), 4)
            self.assertEqual(checkin.call_count, 4)
            full.assert_not_called()

    def test_api_and_cookie_auth_require_csrf_for_new_actions(self):
        self.client.headers.pop("authorization")
        self.assertEqual(self.client.post(self.url + "/refresh").status_code, 401)
        session = self.client.post("/admin/session", json={"api_key": "synthetic-management-key"},
                                   headers={"Origin": "https://testserver"})
        self.assertEqual(session.status_code, 200, session.text)
        self.assertEqual(self.client.post(self.url + "/checkin").status_code, 403)
        self.client.headers["X-CSRF-Token"] = session.json()["csrf_token"]
        self.client.headers["Origin"] = "https://testserver"
        with patch.object(converter.credits_mod, "daily_checkin", return_value={"ok": True}):
            self.assertTrue(self.post("checkin")["ok"])


class ResetCooldownTests(unittest.TestCase):
    """The admin reset is the supported replacement for restarting the gateway to clear one."""

    add_account = fixtures.RegionRoutingTests.add_account
    configure = fixtures.RegionRoutingTests.configure
    handle_upstream = fixtures.RegionRoutingTests.handle_upstream

    def setUp(self):
        fixtures.RegionRoutingTests.setUp(self)
        converter.CONFIG.update(api_key="synthetic-management-key", usage_daily_accounts={},
                                usage_daily={}, cooldowns_path=self.root / "cooldowns.json")
        self.control = ControlStore(self.root / "control.sqlite3")
        self.audit = AuditStore(self.root / "audit.sqlite3")
        self.addCleanup(self.control.close)
        self.addCleanup(self.audit.close)
        converter.CONFIG.update(control_store=self.control, audit_store=self.audit)
        app = FastAPI()
        app.router.routes = list(converter.app.router.routes)
        install_admin(app, converter.CONFIG, Management(converter))
        self.client = self.enterContext(TestClient(app, base_url="https://testserver",
            headers={"Authorization": "Bearer synthetic-management-key"}))

    def build_pool(self, *, cooldowns_path=None):
        """Build a fresh pool bound to a real cooldown file, as a restart would."""
        path = cooldowns_path if cooldowns_path is not None else self.root / "cooldowns.json"
        pool = converter.CredentialPool(
            [self.root / (profile + ".info") for profile in ("cn-cli", "intl-work")],
            cooldowns_path=path)
        converter.CONFIG["cred_pool"] = pool
        return pool

    def arm(self, pool):
        """Record both kinds of cooldown on the first account."""
        entry = pool._entries[0]
        pool.cooldown(entry["cm"], reason="backend HTTP 401")
        pool.note_status(entry["cm"], 429, model="glm-5.3-flash", raw=b"")
        return entry

    def url(self, entry):
        return "/admin/credentials/" + entry["account_key"] + "/reset-cooldown"

    def test_reset_lifts_both_cooldown_kinds_and_survives_a_restart(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        self.assertFalse(pool._healthy(entry))
        self.assertFalse(pool._model_healthy(entry, "glm-5.3-flash"))

        response = self.client.post(self.url(entry))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["results"][0]
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["changed_in_memory"])
        self.assertTrue(result["durable"])
        # The same live pool must be usable immediately; checking only a rebuilt pool would miss
        # an in-memory model cooldown that was never lifted.
        self.assertTrue(pool._healthy(entry))
        self.assertTrue(pool._model_healthy(entry, "glm-5.3-flash"))

        # A brand new pool reads the file back, which is what a restart does.
        revived = self.build_pool()._entries[0]
        self.assertTrue(self.build_pool()._healthy(revived))
        self.assertTrue(self.build_pool()._model_healthy(revived, "glm-5.3-flash"))
        self.assertIsNone(revived.get("last_error"))

    def test_reset_never_contacts_upstream_or_refreshes_tokens(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        before = entry["cm"]._generation
        with patch.object(converter.credits_mod, "fetch_credits") as credits_call, \
                patch.object(converter, "_sync_credits") as sync:
            self.client.post(self.url(entry))
        self.assertEqual(self.requests, [])                 # No upstream call at all.
        credits_call.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(entry["cm"]._generation, before)  # No token refresh.

    def test_reset_is_allowed_while_maintenance_holds_the_lock(self):
        """A local state edit must not queue behind the hourly sweep."""
        pool = self.build_pool()
        entry = self.arm(pool)
        with converter._HOUSEKEEP_LOCK:
            response = self.client.post(self.url(entry))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["results"][0]["ok"])

    def test_reset_works_without_a_ledger(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        converter.CONFIG["ledger"] = None
        response = self.client.post(self.url(entry))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["results"][0]["ok"])

    def test_reset_works_for_a_manually_disabled_account(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        Management(converter).admin_set_credential_enabled(entry["account_key"], False)
        response = self.client.post(self.url(entry))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["results"][0]
        self.assertTrue(result["ok"], result)
        self.assertNotIn("skipped", result)
        # _healthy() also requires manual enablement, so check the cooldown itself: the reset
        # must clear it even though the account stays disabled.
        self.assertEqual(self.build_pool()._entries[0]["fail_until"], 0.0)

    def test_reset_leaves_other_accounts_untouched(self):
        pool = self.build_pool()
        first = self.arm(pool)
        other = pool._entries[1]
        pool.cooldown(other["cm"], reason="backend HTTP 403")
        self.client.post(self.url(first))
        revived = self.build_pool()
        others = {e["account_key"]: e for e in revived._entries}
        self.assertGreater(others[other["account_key"]]["fail_until"], time.time())

    def test_reset_of_an_unknown_identity_is_rejected(self):
        self.build_pool()
        response = self.client.post("/admin/credentials/" + "0" * 64 + "/reset-cooldown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.requests, [])

    def test_reset_rejects_a_replaced_identity_instead_of_clearing_the_new_account(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        stale = entry["account_key"]
        # The file now holds a different account, so the old identity no longer resolves.
        self.add_account("replacement-uid", "cn-cli")
        (self.root / "cn-cli.info").write_text(json.dumps(self.credentials["replacement-uid"]),
                                               encoding="utf-8")
        response = self.client.post("/admin/credentials/" + stale + "/reset-cooldown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.requests, [])

    def test_a_failed_write_reports_failure_and_stays_retryable(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        with patch("app.credential_cooldowns.os.replace", side_effect=OSError("read-only")):
            response = self.client.post(self.url(entry))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["results"][0]
        self.assertFalse(result["ok"])                      # Never report a failed write as done.
        self.assertTrue(result["changed_in_memory"])
        self.assertFalse(result["durable"])
        self.assertIn("写入失败", result["message"])
        self.assertTrue(pool._healthy(entry))               # In-memory effect is real.
        # The disk row is still there, so a restart restores it...
        self.assertFalse(self.build_pool()._healthy(self.build_pool()._entries[0]))
        # ...and the retry (with the fault removed) clears it for good.
        retry = self.client.post(self.url(entry)).json()["results"][0]
        self.assertTrue(retry["ok"], retry)
        self.assertTrue(self.build_pool()._healthy(self.build_pool()._entries[0]))

    def test_reset_requires_authentication(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        self.client.headers.pop("authorization")
        self.assertEqual(self.client.post(self.url(entry)).status_code, 401)
        self.assertFalse(pool._healthy(entry))              # Rejected without mutating.

    def test_reset_requires_csrf_and_same_origin_for_cookie_sessions(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        self.client.headers.pop("authorization")
        session = self.client.post("/admin/session", json={"api_key": "synthetic-management-key"},
                                   headers={"Origin": "https://testserver"})
        self.assertEqual(session.status_code, 200, session.text)
        self.assertEqual(self.client.post(self.url(entry)).status_code, 403)          # No CSRF yet.
        self.assertFalse(pool._healthy(entry))
        self.client.headers["X-CSRF-Token"] = session.json()["csrf_token"]
        self.client.headers["Origin"] = "https://evil.example"
        self.assertEqual(self.client.post(self.url(entry)).status_code, 403)          # Cross-site.
        self.assertFalse(pool._healthy(entry))
        self.client.headers["Origin"] = "https://testserver"
        self.assertEqual(self.client.post(self.url(entry)).status_code, 200)
        self.assertTrue(pool._healthy(entry))

    def test_reset_rejects_a_request_body(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        response = self.client.post(self.url(entry), json={"model": "glm-5.3-flash"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(pool._healthy(entry))              # Rejected without mutating.

    def test_unknown_action_is_still_rejected(self):
        self.build_pool()
        self.assertEqual(self.client.post("/admin/credentials/missing/reset-cooldowns").status_code, 404)

    def test_reset_with_nothing_recorded_is_a_successful_no_op(self):
        pool = self.build_pool()
        entry = pool._entries[0]
        response = self.client.post(self.url(entry))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["results"][0]
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["changed_in_memory"])
        self.assertTrue(result["durable"])

    def test_reset_is_recorded_in_the_audit_trail(self):
        pool = self.build_pool()
        entry = self.arm(pool)
        self.client.post(self.url(entry))
        events = self.audit.list_records("admin", limit=50)["items"]
        matching = [e for e in events if e.get("action") == "credential.reset-cooldown"]
        self.assertTrue(matching, events)
        self.assertEqual(matching[0]["details"]["stage"], "durable")
        self.assertEqual(matching[0]["details"]["outcome"], "success")


if __name__ == "__main__":
    unittest.main()
