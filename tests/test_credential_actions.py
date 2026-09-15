"""Offline single-account maintenance, authorization, and stale-result contracts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
             patch.object(converter, "_sync_trial") as trial, \
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
             patch.object(converter, "_sync_trial") as trial:
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


if __name__ == "__main__":
    unittest.main()
