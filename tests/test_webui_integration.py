"""Managed gateway integration using the existing four-profile MockTransport fixture."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import converter
from app.admin_api import install_admin
from app.audit_store import AuditStore
from app.control_store import ControlStore
from app.gateway_management import Management, install_pages
from app.model_policy import PolicyScopeMiddleware, default_rule
from app.observability import AuditMiddleware
from tests import test_region_routing as fixtures


class ManagedRoutingTests(fixtures.RegionRoutingTests):
    def setUp(self):
        super().setUp()
        self.control = ControlStore(self.root / "control.sqlite3")
        self.audit = AuditStore(self.root / "logs.sqlite3")
        self.addCleanup(self.control.close)
        self.addCleanup(self.audit.close)
        converter.CONFIG.update(control_store=self.control, audit_store=self.audit, api_key="synthetic-management-key")
        self.management = Management(converter)
        converter.CONFIG["management"] = self.management
        application = FastAPI()
        application.router.routes = list(converter.app.router.routes)
        install_admin(application, converter.CONFIG, self.management)
        application.add_middleware(AuditMiddleware, config=converter.CONFIG)
        application.add_middleware(PolicyScopeMiddleware)
        self.assets = self.root / "web-dist"
        self.assets.mkdir()
        (self.assets / "index.html").write_text("<html>isolated management shell</html>")
        install_pages(application, self.assets)
        self.client = self.enterContext(TestClient(application, headers={"Authorization": "Bearer synthetic-management-key"},
                                                  base_url="https://testserver"))

    def policy(self, source="shared-model", **kwargs):
        rule = {**default_rule(source), **kwargs}
        self.control.update_model(source, rule, self.control.snapshot()["revision"],
                                  known_models=[item["id"] for item in self.management.admin_model_inventory()])
        return rule

    def test_managed_alias_all_protocols_strict_binding_and_response_names(self):
        identity = self.entries["intl-work"]["account_key"]
        self.policy(public_id="garden-fast", region="intl", profile="intl-work", credential_ids=[identity])
        published = self.client.get("/v1/models").json()["data"]
        self.assertIn("garden-fast", [model["id"] for model in published])
        self.assertNotIn("shared-model", [model["id"] for model in published])
        for endpoint in fixtures.GENERATIONS:
            for stream in (False, True):
                with self.subTest(endpoint=endpoint, stream=stream):
                    request, body = self.post_ok(endpoint, self.payload(endpoint, "garden-fast", stream=stream), {"intl-work"})
                    self.assertEqual(body["model"], "shared-model")
                    response = self.client.post("/v1/" + endpoint, json=self.payload(endpoint, "garden-fast", stream=stream))
                    self.assertIn("garden-fast", response.text)
                    self.post_rejected(endpoint, self.payload(endpoint, "shared-model"), (404,))
        self.management.admin_set_credential_enabled(identity, False)
        self.post_rejected("chat/completions", self.payload(selected_model="garden-fast"), (503,))

    def test_managed_disabled_cannot_bypass_guard_or_alias(self):
        self.policy(public_id="garden-fast", enabled=False, keep_original=True)
        converter.CONFIG["model_guard"] = False
        for endpoint in fixtures.GENERATIONS:
            for name in ("shared-model", "garden-fast"):
                self.post_rejected(endpoint, self.payload(endpoint, name), (404,))

    def test_managed_disabled_maintenance_and_persistence(self):
        identity = self.entries["intl-work"]["account_key"]
        self.policy(credential_ids=[identity])
        self.management.admin_set_credential_enabled(identity, False)
        self.pool._queue_sync(self.entries["intl-work"]["id"])
        ids = self.pool.begin_sync(all_entries=True)
        self.assertNotIn(self.entries["intl-work"]["id"], ids)
        reopened = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.snapshot()["credentials"][identity]["enabled"])
        with self.assertRaises(Exception):
            self.management.admin_delete_guard("intl-work.info")
        self.management.admin_set_credential_enabled(identity, True)
        self.post_ok("chat/completions", self.payload(), {"intl-work"})

    def test_managed_log_clear_preserves_stats_and_full_clear_is_guarded(self):
        self.post_ok("chat/completions", self.payload(), fixtures.PROFILES)
        before = self.audit.dashboard(1)["summary"]["requests"]
        self.assertGreater(before, 0)
        result = self.client.post("/admin/logs/clear", json={"scope": "details"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.audit.dashboard(1)["summary"]["requests"], before)
        self.assertEqual(self.audit.list_records()["items"], [])
        denied = self.client.post("/admin/logs/clear", json={"scope": "all"})
        self.assertEqual(denied.status_code, 403)
        done = self.client.post("/admin/logs/clear", json={"scope": "all", "confirmation": "清空全部日志与统计",
                                                         "api_key": "synthetic-management-key"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(self.audit.dashboard(1)["summary"]["requests"], 0)
        self.assertEqual(len(list(self.root.glob("*.info"))), 4)

    def test_managed_pages_and_unknown_api_do_not_mix(self):
        for path in ("/", "/unknown-page", "/dashboard/not-a-page"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/dashboard")
        for path in ("/dashboard", "/dashboard/models", "/dashboard/login"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("isolated management shell", response.text)
        for path in ("/v1/unknown", "/admin/unknown", "/dashboard/assets/missing.js"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("<html>", response.text)

    def test_managed_export_excludes_databases_and_tokens_never_reach_audit(self):
        identity = self.entries["intl-work"]["account_key"]
        response = self.client.post("/admin/credentials/export", json={"ids": [identity], "confirm": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["account"]["uid"], "intl-work")
        self.assertIn("no-store", response.headers["cache-control"])
        listing = self.client.get("/admin/credentials").text
        self.assertNotIn("synthetic-access", listing)
        self.assertNotIn(str(self.root), listing)
        for path in self.root.glob("logs.sqlite3*"):
            self.assertNotIn(b"synthetic-access", path.read_bytes())
            self.assertNotIn(b"synthetic-management-key", path.read_bytes())

    def test_managed_zero_price_setting_is_not_replaced_by_default(self):
        response = self.client.patch("/admin/settings", json={"revision": self.control.snapshot()["revision"],
                                                             "values": {"credit_price_cny": 0, "credit_price_usd": 0}})
        self.assertEqual(response.status_code, 200, response.text)
        totals = converter._billing_totals()
        self.assertEqual(totals["price_cny"], 0)
        self.assertEqual(totals["price_usd"], 0)

    def test_managed_no_overwrite_is_checked_under_file_lock(self):
        path = self.root / "intl-work.info"
        original = path.read_bytes()
        with self.assertRaises(converter.CredentialConflictError):
            converter._store_credential(self.root, path.name, original, "intl-work", replace_existing=False)
        self.assertEqual(path.read_bytes(), original)


    def test_managed_cookie_oauth_poll_without_browser_origin_still_requires_csrf(self):
        from unittest.mock import Mock
        origin = {"Origin": "https://testserver"}
        login = self.client.post("/admin/session", json={"api_key": "synthetic-management-key"}, headers=origin)
        csrf = login.json()["csrf_token"]
        self.client.headers.pop("authorization")
        manager = Mock()
        manager.start.return_value = {"login_id": "isolated-oauth", "verification_uri": "https://www.codebuddy.cn/login?state=synthetic", "expires_in": 600}
        manager.poll.return_value = {"done": False}
        with patch.object(converter, "_OAUTH", manager):
            start = self.client.post("/admin/oauth/start", headers={**origin, "X-CSRF-Token": csrf})
            self.assertEqual(start.status_code, 200, start.text)
            url = "/admin/oauth/poll?login_id=isolated-oauth"
            good = self.client.get(url, headers={"Sec-Fetch-Site": "same-origin", "X-CSRF-Token": csrf})
            self.assertEqual(good.status_code, 200, good.text)
            denied = self.client.get(url, headers={"Sec-Fetch-Site": "same-origin"})
            self.assertEqual(denied.status_code, 403)
            cross = self.client.get(url, headers={"Origin": "https://untrusted.invalid", "X-CSRF-Token": csrf})
            self.assertEqual(cross.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
