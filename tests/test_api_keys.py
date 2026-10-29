"""API-key admission, restrictions, management API and accounting contracts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import admin_account, model_policy
from app.admin_api import install_admin
from app.api_keys import ApiKeyError, KeyStore, Principal, api_principal
from app.audit_store import AuditStore
from app.control_store import ControlStore
from app.runtime_management import UnavailableAudit
from app.settings import normalize_trusted_proxies, parse_networks
from tests.admin_session import login as session_login


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.control = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(self.control.close)
        self.keys = KeyStore(self.control)
        self.config = {"api_key": "", "allow_open_noauth": False, "trusted_proxies": ""}

    def scope(self, secret=None, *, path="/v1/chat/completions", method="POST", ip="127.0.0.1"):
        headers = [] if secret is None else [(b"authorization", f"Bearer {secret}".encode())]
        return {"type": "http", "method": method, "path": path, "headers": headers, "client": (ip, 1)}

    def code(self, error):
        return error.detail["error"]["code"]

    def test_empty_and_disabled_sets_control_noauth(self):
        with self.assertRaises(ApiKeyError) as missing:
            self.keys.authenticate(self.scope(), self.config)
        self.assertEqual(self.code(missing.exception), "api_key_required")
        self.config["allow_open_noauth"] = True
        principal = self.keys.authenticate(self.scope("placeholder"), self.config)
        self.assertEqual((principal.id, principal.type), ("anonymous", "admin"))
        disabled, _ = self.keys.create(name="disabled", kind="standard")
        self.keys.update(disabled["id"], 1, {"enabled": False})
        with self.assertRaises(ApiKeyError) as unknown:
            self.keys.authenticate(self.scope("unknown"), self.config)
        self.assertEqual(self.code(unknown.exception), "invalid_api_key")

    def test_external_and_overridden_admin_keys(self):
        self.config["api_key"] = "external-secret"
        _, admin_secret = self.keys.create(name="database admin", kind="admin")
        _, standard_secret = self.keys.create(name="standard", kind="standard")
        principal = self.keys.authenticate(self.scope("external-secret"), self.config)
        self.assertEqual((principal.id, principal.type), ("admin-external", "admin"))
        with self.assertRaises(ApiKeyError) as overridden:
            self.keys.authenticate(self.scope(admin_secret), self.config)
        self.assertEqual(self.code(overridden.exception), "invalid_api_key")
        self.assertEqual(self.keys.authenticate(self.scope(standard_secret), self.config).type, "standard")

    def test_disabled_expired_ip_and_rpm_results(self):
        disabled, _ = self.keys.create(name="disabled", kind="standard")
        self.keys.update(disabled["id"], 1, {"enabled": False})
        with self.assertRaises(ApiKeyError) as stopped:
            self.keys.authenticate(self.scope(disabled["secret"]), self.config)
        self.assertEqual(self.code(stopped.exception), "api_key_disabled")
        _, expired = self.keys.create(name="expired", kind="standard", expires_at=time.time() + 1)
        with patch("app.api_keys.time.time", return_value=time.time() + 2):
            with self.assertRaises(ApiKeyError) as old:
                self.keys.authenticate(self.scope(expired), self.config)
        self.assertEqual(self.code(old.exception), "api_key_expired")
        limited, _ = self.keys.create(name="limited", kind="standard", ip_allow=["198.51.100.0/24"], rpm=1)
        with self.assertRaises(ApiKeyError) as denied:
            self.keys.authenticate(self.scope(limited["secret"], ip="203.0.113.9"), self.config)
        self.assertEqual(self.code(denied.exception), "ip_not_allowed")
        self.keys.authenticate(self.scope(limited["secret"], ip="198.51.100.9"), self.config)
        with self.assertRaises(ApiKeyError) as rpm:
            self.keys.authenticate(self.scope(limited["secret"], ip="198.51.100.9"), self.config)
        self.assertEqual(self.code(rpm.exception), "rpm_exceeded")
        self.assertIn("Retry-After", rpm.exception.headers)
        self.assertEqual(self.keys.authenticate(
            self.scope(limited["secret"], path="/v1/messages/count_tokens", ip="198.51.100.9"), self.config).id, limited["id"])

    def test_other_connection_change_is_visible_within_one_second(self):
        _, secret = self.keys.create(name="cross-process", kind="standard")
        other = sqlite3.connect(self.control.path)
        other.execute("UPDATE api_keys SET enabled=0")
        other.commit()
        other.close()
        with patch("app.api_keys.time.monotonic", return_value=time.monotonic() + 2):
            with self.assertRaises(ApiKeyError) as stopped:
                self.keys.authenticate(self.scope(secret), self.config)
        self.assertEqual(self.code(stopped.exception), "api_key_disabled")

    def test_model_aliases_source_and_permission(self):
        self.control.update_model("upstream", {"public_id": "public", "upstream_id": "upstream",
                                               "keep_original": True}, 0)
        self.config["control_store"] = self.control
        self.assertEqual(model_policy.source_for(self.config, "default-model"), "auto")
        self.assertEqual(model_policy.source_for(self.config, "public"), "upstream")
        principal = Principal("k_test", "limited", "standard", ("upstream",))
        token = api_principal.set(principal)
        policy_token = model_policy._request_policy.set({})
        try:
            self.assertEqual(model_policy.resolve(self.config, "public"), "upstream")
            self.assertEqual(model_policy.resolve(self.config, "upstream"), "upstream")
            with self.assertRaises(Exception) as denied:
                api_principal.set(Principal("k_test", "limited", "standard", ("other",)))
                model_policy.resolve(self.config, "public")
            self.assertEqual(denied.exception.detail["error"]["code"], "model_not_allowed")
        finally:
            model_policy._request_policy.reset(policy_token)
            api_principal.reset(token)


    def test_default_model_uses_auto_permission(self):
        self.config["control_store"] = self.control
        token = api_principal.set(Principal("k_test", "automatic", "standard", ("auto",)))
        policy_token = model_policy._request_policy.set({})
        try:
            self.assertEqual(model_policy.resolve(self.config, "default-model"), "default-model")
        finally:
            model_policy._request_policy.reset(policy_token)
            api_principal.reset(token)

    def test_models_route_filters_by_principal_sources(self):
        import converter
        self.control.update_model("upstream", {"public_id": "public", "upstream_id": "upstream"}, 0)
        self.config.update(control_store=self.control, key_store=self.keys)
        _, secret = self.keys.create(name="limited", kind="standard", models=["upstream"])
        items = [{"id": name, "credits": 0, "credits_by_profile": {}} for name in ("public", "other")]
        with patch.dict(converter.CONFIG, self.config), patch.object(model_policy, "public_details", return_value=items):
            client = self.enterContext(TestClient(converter.app))
            result = client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([item["id"] for item in result.json()["data"]], ["public"])

    def test_billing_requires_admin_or_anonymous_principal(self):
        import converter
        item, secret = self.keys.create(name="standard", kind="standard")
        self.config.update(control_store=self.control, key_store=self.keys, usage_daily={})
        totals = {"quota_usd": 0, "soonest_expiry": None, "price_cny": 0, "price_usd": 0,
                  "remaining": 0, "used": 0, "remaining_usd": 0, "remaining_cny": 0,
                  "used_usd": 0, "groups": {}, "partial": False, "rate": 1}
        with patch.dict(converter.CONFIG, self.config), patch.object(converter, "_billing_totals", return_value=totals):
            client = self.enterContext(TestClient(converter.app))
            for path in ("/v1/dashboard/billing/subscription", "/v1/dashboard/billing/usage"):
                denied = client.get(path, headers={"Authorization": f"Bearer {secret}"})
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(denied.json()["error"]["code"], "admin_key_required")
                self.assertEqual(denied.json()["error"]["type"], "permission_error")
                converter.CONFIG["api_key"] = "external-secret"
                allowed = client.get(path, headers={"Authorization": "Bearer external-secret"})
                self.assertEqual(allowed.status_code, 200, allowed.text)
            converter.CONFIG["api_key"] = ""
            self.keys.delete(item["id"], 1)
            converter.CONFIG["allow_open_noauth"] = True
            anonymous = client.get("/v1/dashboard/billing/subscription", headers={"Authorization": "Bearer placeholder"})
            self.assertEqual(anonymous.status_code, 200, anonymous.text)

    def test_anthropic_permission_and_rpm_envelopes(self):
        from app.inference_auth import InferenceAuthMiddleware
        self.config["key_store"] = self.keys
        app = FastAPI()

        @app.post("/v1/messages")
        async def messages():
            return {"ok": True}

        app.add_middleware(InferenceAuthMiddleware, config=self.config)
        client = self.enterContext(TestClient(app))
        _, ip_secret = self.keys.create(name="ip", kind="standard", ip_allow=["198.51.100.0/24"])
        denied = client.post("/v1/messages", headers={"Authorization": f"Bearer {ip_secret}"})
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["type"], "error")
        self.assertEqual(denied.json()["error"]["type"], "permission_error")
        _, rpm_secret = self.keys.create(name="rpm", kind="standard", rpm=1)
        headers = {"Authorization": f"Bearer {rpm_secret}"}
        self.assertEqual(client.post("/v1/messages", headers=headers).status_code, 200)
        limited = client.post("/v1/messages", headers=headers)
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertEqual(limited.json()["error"]["type"], "rate_limit_error")
        self.assertIn("Retry-After", limited.headers)
    def test_network_limits_stay_independent(self):
        self.assertEqual(len(parse_networks([f"10.0.0.{i}" for i in range(64)], limit=64, label="IP 白名单")), 64)
        with self.assertRaisesRegex(ValueError, "IP 白名单"):
            parse_networks([f"10.0.0.{i}" for i in range(65)], limit=64, label="IP 白名单")
        self.assertEqual(len(parse_networks([f"10.0.0.{i}" for i in range(32)],
                                            limit=32, label="trusted_proxies")), 32)
        with self.assertRaisesRegex(ValueError, "trusted_proxies"):
            normalize_trusted_proxies([f"10.0.0.{i}" for i in range(33)])

    def test_api_key_stats_window_filter_and_clear(self):
        audit = AuditStore(self.root / "logs.sqlite3")
        self.addCleanup(audit.close)
        now = int(time.time() // 3600) * 3600
        key_id = "k_abcdefghijkl"
        for index, age in enumerate((0, 23 * 3600, 24 * 3600)):
            audit.record_request({"id": f"request-{index}", "started_at": now - age,
                                  "api_key": key_id, "outcome": "success", "total_tokens": 10,
                                  "credit": 2, "model": "model"})
        audit.record_request({"id": "invalid-key", "started_at": now, "api_key": "cb-secret",
                              "outcome": "success", "total_tokens": 100, "model": "model"})
        stats = audit.api_key_stats(now)["items"][key_id]
        self.assertEqual((stats["requests"], stats["success"], stats["total_tokens"], stats["credit"]),
                         (2, 2, 20, 4))
        self.assertEqual(len(audit.list_records(api_key=key_id)["items"]), 3)
        self.assertNotIn("cb-secret", str(audit.list_records()["items"]))
        audit.clear("all")
        self.assertEqual(audit.api_key_stats(now)["items"].get(key_id, {}).get("requests", 0), 0)
        self.assertEqual(UnavailableAudit("fixture").api_key_stats(), {"degraded": True, "items": {}})

    def test_management_api_status_and_version_contract(self):
        admin_account.save(self.control, admin_account.new("admin", "synthetic-key", "user"))
        audit = Mock()
        audit.storage.return_value = {}
        audit.api_key_stats.return_value = {"items": {}}
        self.config.update(control_store=self.control, audit_store=audit,
                           session_path=self.root / "sessions.json")
        gateway = SimpleNamespace(admin_model_inventory=lambda: [{"id": "upstream", "public_id": "public"}])
        app = FastAPI()
        install_admin(app, self.config, gateway)
        client = self.enterContext(TestClient(app))
        headers = session_login(client)
        for field in ("expires_at", "ip_allow", "rpm", "models"):
            restricted = client.post("/admin/keys", headers=headers,
                                     json={"name": "invalid admin", "type": "admin", field: None})
            self.assertEqual(restricted.status_code, 400, restricted.text)
        created = client.post("/admin/keys", headers=headers,
                              json={"name": "admin", "type": "admin"})
        self.assertEqual(created.status_code, 200, created.text)
        duplicate = client.post("/admin/keys", headers=headers, json={"name": "admin2", "type": "admin"})
        self.assertEqual(duplicate.status_code, 422)
        standard = client.post("/admin/keys", headers=headers,
                               json={"name": "standard", "type": "standard", "models": ["public"]})
        self.assertEqual(standard.status_code, 200, standard.text)
        self.assertEqual(standard.json()["models"], ["upstream"])
        renamed = client.patch(f"/admin/keys/{standard.json()['id']}", headers=headers,
                               json={"version": 1, "models": ["public"]})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["models"], ["upstream"])
        unknown_update = client.patch(f"/admin/keys/{standard.json()['id']}", headers=headers,
                                      json={"version": 2, "models": ["missing"]})
        self.assertEqual(unknown_update.status_code, 400)
        automatic = client.post("/admin/keys", headers=headers,
                                json={"name": "automatic", "type": "standard", "models": ["default-model"]})
        self.assertEqual(automatic.status_code, 200, automatic.text)
        self.assertEqual(automatic.json()["models"], ["auto"])
        unknown = client.post("/admin/keys", headers=headers,
                              json={"name": "unknown", "type": "standard", "models": ["missing"]})
        self.assertEqual(unknown.status_code, 400)
        for secret in (created.json()["secret"], standard.json()["secret"]):
            anonymous = TestClient(app)
            self.assertEqual(anonymous.get("/admin/keys", headers={"Authorization": f"Bearer {secret}"}).status_code, 401)
            anonymous.close()
        missing = client.post("/admin/keys/k_abcdefghijkl/rotate", headers=headers, json={"version": 1})
        self.assertEqual(missing.status_code, 404)
        stale = client.patch(f"/admin/keys/{standard.json()['id']}", headers=headers,
                             json={"version": 99, "name": "stale"})
        self.assertEqual(stale.status_code, 409)
        stale_rotate = client.post(f"/admin/keys/{standard.json()['id']}/rotate", headers=headers,
                                   json={"version": 99})
        self.assertEqual(stale_rotate.status_code, 409)
        self.config["api_key"] = "external-secret"
        external = client.post(f"/admin/keys/{created.json()['id']}/rotate", headers=headers,
                               json={"version": 1})
        self.assertEqual(external.status_code, 403)
        for origin in ("cli", "environment", "dotenv"):
            self.config["settings_sources"] = {"api_key": origin}
            listed = client.get("/admin/keys", headers=headers).json()["external"]
            self.assertEqual((listed["origin"], listed["occupied"]), (origin, True))
            self.assertNotIn("prefix", listed)
    def test_missing_account_locks_http_even_with_external_key(self):
        audit = Mock()
        audit.storage.return_value = {}
        audit.api_key_stats.return_value = {"items": {}}
        self.config.update(control_store=self.control, audit_store=audit,
                           session_path=self.root / "sessions.json", api_key="external-secret")
        app = FastAPI()
        install_admin(app, self.config, SimpleNamespace())
        with self.control.state.transaction():
            self.control._db.execute("DELETE FROM gateway_secrets WHERE name='admin_account'")
        client = self.enterContext(TestClient(app))
        for response in (
            client.get("/admin/session"),
            client.post("/admin/session", json={"username": "external-secret", "password": "external-secret"}),
            client.get("/admin/keys", headers={"Authorization": "Bearer external-secret"}),
        ):
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["error"]["message"], "管理账户未初始化")



if __name__ == "__main__":
    unittest.main()
