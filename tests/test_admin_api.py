"""Management HTTP tests: mock gateway/audit, temporary .info fixtures only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from app.admin_api import CLEAR_CONFIRMATION, install_admin
from app.admin_auth import COOKIE_NAME, same_origin
from app.control_store import ControlStore


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(self.store.close)
        self.config = {"api_key": "synthetic-key", "control_store": self.store, "max_images": 16}
        self.audit = Mock()
        self.audit.storage.return_value = {"db_bytes": 0}
        self.audit.list_records.return_value = {"items": [], "next_cursor": None, "has_more": False}
        self.audit.get_request.return_value = None
        self.audit.dashboard.return_value = {"summary": {}, "series": [], "models": [], "profiles": []}
        self.audit.clear.side_effect = lambda scope: {"scope": scope, "cleared": True}
        self.config["audit_store"] = self.audit
        self.credentials = [{"account_key": "fingerprint", "name": "first.info", "profile": "cn-cli", "enabled": True, "accessToken": "NEVER-RETURN"}]
        self.gateway = SimpleNamespace(
            admin_credential_inventory=Mock(side_effect=lambda: self.credentials),
            admin_model_inventory=Mock(return_value=[{"id": "upstream", "credits": 1}, {"id": "vendor/model"}]),
            admin_model_preview=Mock(return_value={"candidates": [], "excluded": []}),
            admin_set_credential_enabled=Mock(side_effect=lambda identity, enabled: self.store.set_credential(identity, enabled)),
            admin_apply_settings=Mock(), admin_delete_guard=Mock(),
            managed_auth_dir=Mock(return_value=self.root), _store_credential=Mock(),
            _credential_identity=Mock(return_value="fingerprint"),
            _save_oauth_credential=Mock(return_value=self.root / "first.info"), _OAUTH=Mock())
        self.gateway._OAUTH.start.return_value = {"login_id": "task", "verification_uri": "https://www.codebuddy.cn/login", "expires_in": 600}
        self.gateway._OAUTH.poll.return_value = {"done": True, "uid": "u", "cred": {"synthetic": True}}
        self.app = FastAPI()
        self.legacy_auth = []

        @self.app.post("/admin/legacy")
        def legacy(authorization: str = Header(default="")):
            self.legacy_auth.append(authorization)
            return {"ok": True}

        @self.app.get("/admin/credentials")
        def old_inventory():
            raise AssertionError("inventory must be adapted")

        @self.app.post("/admin/oauth/start")
        def old_oauth_start():
            raise AssertionError("OAuth must be adapted")

        @self.app.get("/admin/oauth/poll")
        def old_oauth_poll():
            raise AssertionError("OAuth must be adapted")

        @self.app.delete("/admin/credentials/{name}")
        def old_delete(name: str):
            return {"removed": name}

        @self.app.get("/v1/identity")
        def inference(authorization: str = Header(default="")):
            return {"authorization_present": bool(authorization)}

        self.auth = install_admin(self.app, self.config, self.gateway)
        self.client = self.enterContext(TestClient(self.app))
        self.headers = {"Authorization": "Bearer synthetic-key"}

    def login(self, client=None):
        client = client or self.client
        result = client.post("/admin/session", json={"api_key": "synthetic-key"}, headers={"Origin": "http://testserver"})
        self.assertEqual(result.status_code, 200, result.text)
        return {"Origin": "http://testserver", "X-CSRF-Token": result.json()["csrf_token"]}

    def test_header_cookie_isolation_csrf_and_no_cache(self):
        self.assertEqual(self.client.post("/admin/legacy").status_code, 401)
        csrf = self.login()
        cookie = self.client.cookies.get(COOKIE_NAME)
        self.assertTrue(cookie)
        self.assertEqual(self.client.post("/admin/legacy").status_code, 403)
        self.assertEqual(self.client.post("/admin/legacy", headers={**csrf, "Origin": "https://evil.invalid"}).status_code, 403)
        good = self.client.post("/admin/legacy", headers=csrf)
        self.assertEqual(good.status_code, 200)
        self.assertEqual(self.legacy_auth, ["Bearer synthetic-key"])
        self.assertEqual(good.headers["cache-control"], "no-store")
        # Force the cookie onto an inference request: middleware still never injects key.
        self.assertFalse(self.client.get("/v1/identity", headers={"Cookie": f"{COOKIE_NAME}={cookie}"}).json()["authorization_present"])
        self.assertEqual(self.client.post("/admin/legacy", headers=self.headers).status_code, 200)

    def test_same_origin_referer_fallback_is_strict_and_get_only(self):
        from starlette.requests import Request
        valid = "http://testserver/dashboard/credentials?tab=oauth"
        cases = [
            ("GET", {"Referer": valid}, True),
            ("HEAD", {"Referer": "http://TESTSERVER:80/dashboard"}, True),
            ("GET", {"Sec-Fetch-Site": "same-origin"}, True),
            ("GET", {}, False),
            ("POST", {"Referer": valid}, False),
            ("DELETE", {"Referer": valid}, False),
            ("GET", {"Sec-Fetch-Site": "cross-site", "Referer": valid}, False),
            ("GET", {"Sec-Fetch-Site": "same-site", "Referer": valid}, False),
            ("GET", {"Sec-Fetch-Site": "none", "Referer": valid}, False),
            ("GET", {"Sec-Fetch-Site": "", "Referer": valid}, False),
            ("GET", {"Origin": "null", "Referer": valid, "Sec-Fetch-Site": "same-origin"}, False),
            ("GET", {"Origin": "", "Referer": valid, "Sec-Fetch-Site": "same-origin"}, False),
            ("GET", {"Origin": "https://evil.invalid", "Referer": valid}, False),
            ("GET", {"Origin": "http://testserver/path", "Referer": valid}, False),
            ("GET", {"Origin": "http://testserver", "Referer": "https://evil.invalid"}, True),
        ]
        for invalid in ("https://testserver/dashboard", "http://testserver:8787/dashboard",
                        "http://testserver:0/dashboard", "http://testserver.evil.invalid/dashboard",
                        "http://user:password@testserver/dashboard", "http://@testserver/dashboard",
                        "http://testserver/dashboard#fragment", "//testserver/dashboard", "/dashboard",
                        "http://testserver:invalid/dashboard", "http://[invalid", "null"):
            cases.append(("GET", {"Referer": invalid}, False))
        for method, headers, expected in cases:
            with self.subTest(method=method, headers=headers):
                request = Request({"type": "http", "method": method, "scheme": "http",
                                   "server": ("testserver", 80), "path": "/admin/oauth/poll", "query_string": b"",
                                   "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()]})
                self.assertIs(same_origin(request), expected)

    def test_lan_oauth_poll_uses_referer_without_disabling_csrf(self):
        origin = "http://192.168.1.10:8787"
        client = self.enterContext(TestClient(self.app, base_url=origin))
        login = client.post("/admin/session", headers={"Origin": origin}, json={"api_key": "synthetic-key"})
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["csrf_token"]
        started = client.post("/admin/oauth/start", headers={"Origin": origin, "X-CSRF-Token": token})
        self.assertEqual(started.status_code, 200, started.text)
        params = {"login_id": started.json()["login_id"]}
        referer = origin + "/dashboard/credentials"
        rejected = [
            {"Referer": referer},
            {"Referer": referer, "X-CSRF-Token": "wrong"},
            {"Referer": "http://evil.invalid/dashboard", "X-CSRF-Token": token},
            {"Referer": referer, "X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"},
        ]
        for headers in rejected:
            with self.subTest(headers=headers):
                self.assertEqual(client.get("/admin/oauth/poll", params=params, headers=headers).status_code, 403)
        other = self.enterContext(TestClient(self.app, base_url=origin))
        other_login = other.post("/admin/session", headers={"Origin": origin}, json={"api_key": "synthetic-key"})
        self.assertEqual(other_login.status_code, 200)
        response = other.get("/admin/oauth/poll", params=params, headers={
            "Referer": referer, "X-CSRF-Token": other_login.json()["csrf_token"]})
        self.assertEqual(response.status_code, 404)
        self.gateway._OAUTH.poll.assert_not_called()
        self.gateway._save_oauth_credential.assert_not_called()
        response = client.get("/admin/oauth/poll", params=params, headers={"Referer": referer, "X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["imported"], "first.info")
        self.assertTrue(self.auth.csrf_enabled())
        self.gateway._OAUTH.poll.assert_called_once()
        self.gateway._save_oauth_credential.assert_called_once()

    def test_admin_csrf_defaults_to_enabled_and_requires_explicit_false(self):
        self.assertTrue(self.auth.csrf_enabled())
        for value in (True, None, 0, "false"):
            with self.subTest(value=value):
                self.config["admin_csrf"] = value
                self.assertTrue(self.auth.csrf_enabled())
                self.assertEqual(self.client.post("/admin/session", json={"api_key": "synthetic-key"}).status_code, 403)
        self.config["admin_csrf"] = False
        self.assertFalse(self.auth.csrf_enabled())

    def test_disabled_csrf_allows_cookie_writes_and_oauth_poll(self):
        self.config["admin_csrf"] = False
        response = self.client.post("/admin/session", json={"api_key": "synthetic-key"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["csrf_token"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertIn("Path=/admin", response.headers["set-cookie"])
        self.assertEqual(self.client.post("/admin/legacy").status_code, 200)
        self.assertEqual(self.client.post("/admin/legacy", headers={
            "Origin": "https://different.invalid", "X-CSRF-Token": "invalid"}).status_code, 200)
        started = self.client.post("/admin/oauth/start?site=cn")
        self.assertEqual(started.status_code, 200, started.text)
        for _ in range(2):
            polled = self.client.get("/admin/oauth/poll", params={"login_id": started.json()["login_id"]})
            self.assertEqual(polled.status_code, 200, polled.text)
            self.assertEqual(polled.json()["imported"], "first.info")
        self.gateway._OAUTH.start.assert_called_once_with(site="cn")
        self.gateway._OAUTH.poll.assert_called_once()
        self.gateway._save_oauth_credential.assert_called_once()
        self.assertEqual(self.client.delete("/admin/session").status_code, 200)
        self.assertEqual(self.client.post("/admin/legacy").status_code, 401)

    def test_disabled_csrf_keeps_authentication_and_sensitive_action_checks(self):
        self.config["admin_csrf"] = False
        self.assertEqual(self.client.post("/admin/legacy").status_code, 401)
        self.assertEqual(self.client.post("/admin/legacy", headers={"X-Api-Key": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/admin/session", json={"api_key": "wrong"}).status_code, 401)
        self.login()
        cookie = self.client.cookies.get(COOKIE_NAME)
        self.assertFalse(self.client.get("/v1/identity", headers={"Cookie": f"{COOKIE_NAME}={cookie}"}).json()["authorization_present"])
        response = self.client.post("/admin/logs/clear", json={"scope": "all", "confirmation": CLEAR_CONFIRMATION})
        self.assertEqual(response.status_code, 403)
        self.audit.clear.assert_not_called()
        self.gateway.admin_delete_guard.side_effect = ValueError("referenced")
        self.assertEqual(self.client.delete("/admin/credentials/first.info").status_code, 409)
        self.config["api_key"] = "rotated-key"
        self.assertEqual(self.client.post("/admin/legacy").status_code, 401)
        self.config["api_key"] = ""
        for path in ("/admin/session", "/admin/settings", "/admin/credentials"):
            self.assertEqual(self.client.get(path).status_code, 503)

    def test_disabled_csrf_keeps_oauth_ownership_and_official_site_checks(self):
        self.config["admin_csrf"] = False
        self.login()
        started = self.client.post("/admin/oauth/start")
        self.assertEqual(started.status_code, 200)
        other = self.enterContext(TestClient(self.app))
        self.login(other)
        response = other.get("/admin/oauth/poll", params={"login_id": started.json()["login_id"]})
        self.assertEqual(response.status_code, 404)
        self.gateway._OAUTH.poll.assert_not_called()
        self.gateway._save_oauth_credential.assert_not_called()
        self.gateway._OAUTH.start.return_value = {"login_id": "evil", "verification_uri": "https://www.codebuddy.cn.evil.invalid/login"}
        self.assertEqual(self.client.post("/admin/oauth/start").status_code, 502)

    def test_disabled_csrf_keeps_login_throttle(self):
        self.config["admin_csrf"] = False
        for _ in range(10):
            self.assertEqual(self.client.post("/admin/session", json={"api_key": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/admin/session", json={"api_key": "synthetic-key"}).status_code, 429)

    def test_admin_csrf_cannot_be_changed_through_management_settings(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.config["admin_csrf"] = enabled
                response = self.client.patch("/admin/settings", headers=self.headers, json={
                    "revision": 0, "values": {"admin_csrf": not enabled}})
                self.assertEqual(response.status_code, 400)
                self.assertIs(self.config["admin_csrf"], enabled)
                self.assertNotIn("admin_csrf", self.store.snapshot()["settings"])

    def test_session_cookie_flags_logout_and_rotation(self):
        response = self.client.post("/admin/session", json={"api_key": "synthetic-key"}, headers={"Origin": "http://testserver"})
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        csrf = {"Origin": "http://testserver", "X-CSRF-Token": response.json()["csrf_token"]}
        old = self.client.cookies.get(COOKIE_NAME)
        self.assertTrue(self.client.get("/admin/session").json()["authenticated"])
        self.assertEqual(self.client.delete("/admin/session", headers=csrf).status_code, 200)
        self.assertEqual(self.client.get("/admin/settings", headers={"Cookie": f"{COOKIE_NAME}={old}"}).status_code, 401)
        self.login()
        self.config["api_key"] = "rotated-key"
        self.assertEqual(self.client.get("/admin/settings").status_code, 401)
        self.assertEqual(self.client.get("/admin/settings", headers=self.headers).status_code, 401)
        self.assertEqual(self.client.get("/admin/settings", headers={"X-Api-Key": "rotated-key"}).status_code, 200)

    def test_https_cookie_and_bounded_sessions(self):
        from starlette.requests import Request
        secure = self.enterContext(TestClient(self.app, base_url="https://testserver"))
        response = secure.post("/admin/session", headers={"Origin": "https://testserver"}, json={"api_key": "synthetic-key"})
        self.assertIn("Secure", response.headers["set-cookie"])
        request = Request({"type": "http", "headers": [], "client": ("synthetic", 1)})
        for _ in range(300):
            self.auth.login(request, "synthetic-key")
        self.assertEqual(len(self.auth.sessions), 256)

    def test_empty_key_locks_every_management_route(self):
        self.config["api_key"] = ""
        for path in ("/admin/session", "/admin/settings", "/admin/credentials", "/admin/missing"):
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 503)

    def test_login_origin_throttle_and_bound(self):
        self.assertEqual(self.client.post("/admin/session", json={"api_key": "synthetic-key"}).status_code, 403)
        for _ in range(10):
            self.assertEqual(self.client.post("/admin/session", json={"api_key": "wrong"}, headers={"Origin": "http://testserver"}).status_code, 401)
        self.assertEqual(self.client.post("/admin/session", json={"api_key": "synthetic-key"}, headers={"Origin": "http://testserver"}).status_code, 429)
        self.assertLessEqual(len(self.auth.failures), 1024)

    def test_storage_read_failures_are_not_empty_successes(self):
        from app.runtime_management import UnavailableAudit
        unavailable = UnavailableAudit("SyntheticDatabaseError")
        self.audit.list_records.side_effect = unavailable.list_records
        self.audit.dashboard.side_effect = unavailable.dashboard
        self.audit.storage.side_effect = unavailable.storage
        for path in ("/admin/logs", "/admin/logs/missing", "/admin/dashboard"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=self.headers).status_code, 503)

    def test_failed_audit_configuration_does_not_publish_runtime_values(self):
        self.audit.configure.return_value = {"ok": False}
        response = self.client.patch("/admin/settings", headers=self.headers, json={
            "revision": 0, "values": {"max_images": 3, "audit_max_bytes": 1048576}})
        self.assertEqual(response.status_code, 503)
        self.gateway.admin_apply_settings.assert_not_called()
        self.assertEqual(self.config["max_images"], 16)
        self.assertEqual(self.store.snapshot()["settings"]["max_images"], 3)


    def test_key_rotation_does_not_revive_pending_header_oauth(self):
        started = self.client.post("/admin/oauth/start?site=cn", headers=self.headers)
        self.assertEqual(started.status_code, 200)
        task = started.json()["login_id"]
        self.config["api_key"] = "rotated-synthetic-key"
        response = self.client.get("/admin/session", headers={"Authorization": "Bearer rotated-synthetic-key"})
        self.assertEqual(response.status_code, 200)
        self.config["api_key"] = "synthetic-key"
        response = self.client.get("/admin/oauth/poll", params={"login_id": task}, headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.gateway._OAUTH.poll.assert_not_called()
        self.gateway._save_oauth_credential.assert_not_called()


    def test_tool_metadata_setting_is_hot_persisted_and_respects_locks(self):
        key = "keep_tool_metadata"
        current = self.client.get("/admin/settings", headers=self.headers).json()
        item = next(item for item in current["items"] if item["key"] == key)
        self.assertEqual((item["value"], item["locked"], item["mode"]), (False, False, "hot"))
        response = self.client.patch("/admin/settings", headers=self.headers, json={
            "revision": current["revision"], "values": {key: True}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(self.config[key], True)
        self.assertIs(self.store.snapshot()["settings"][key], True)
        self.gateway.admin_apply_settings.assert_called_once_with({key: True})
        item = next(item for item in response.json()["items"] if item["key"] == key)
        self.assertEqual((item["value"], item["source"], item["locked"]), (True, "management", False))
        self.config["settings_sources"][key] = "environment"
        response = self.client.patch("/admin/settings", headers=self.headers, json={
            "revision": response.json()["revision"], "values": {key: False}})
        self.assertEqual(response.status_code, 400)
        self.assertIs(self.config[key], True)
        self.assertIs(self.store.snapshot()["settings"][key], True)

    def test_failover_settings_are_hot_and_persisted(self):
        """换凭证重放与写超时开关都要能在系统设置里改完立即生效，不需要重启进程。"""
        current = self.client.get("/admin/settings", headers=self.headers).json()
        items = {item["key"]: item for item in current["items"]}
        for key, kind, default in (("failover_max", "integer", 0), ("retry_write_timeout", "boolean", False)):
            with self.subTest(key=key):
                spec = items[key]
                self.assertEqual((spec["mode"], spec["locked"], spec["type"]), ("hot", False, kind))
                self.assertEqual(spec["value"], default, "默认必须与上游行为一致：关闭")
        response = self.client.patch("/admin/settings", headers=self.headers, json={
            "revision": current["revision"], "values": {"failover_max": 2, "retry_write_timeout": True}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.config["failover_max"], self.config["retry_write_timeout"]), (2, True))
        self.assertEqual(self.store.snapshot()["settings"]["failover_max"], 2)
        self.assertIs(self.store.snapshot()["settings"]["retry_write_timeout"], True)
        self.gateway.admin_apply_settings.assert_called_once_with(
            {"failover_max": 2, "retry_write_timeout": True})
        applied = {item["key"]: item for item in response.json()["items"]}
        for key, value in (("failover_max", 2), ("retry_write_timeout", True)):
            self.assertEqual((applied[key]["value"], applied[key]["source"], applied[key]["locked"]),
                             (value, "management", False), applied[key])

    def test_settings_revision_locked_sources_and_secret_redaction(self):
        self.config["auth_dir"] = "/private-directory"
        self.config["settings_sources"] = {"max_images": "environment"}
        response = self.client.get("/admin/settings", headers=self.headers)
        self.assertNotIn("synthetic-key", response.text)
        self.assertNotIn("private-directory", response.text)
        patch = self.client.patch("/admin/settings", headers=self.headers, json={"revision": 0, "values": {"max_images": 5}})
        self.assertEqual(patch.status_code, 400)
        self.config["settings_sources"] = {}
        patch = self.client.patch("/admin/settings", headers=self.headers, json={"revision": 0, "values": {"max_images": 5, "port": 9000, "audit_retention_days": 7}})
        self.assertEqual(patch.status_code, 200, patch.text)
        self.assertEqual(self.config["max_images"], 5)
        self.assertNotIn("port", self.gateway.admin_apply_settings.call_args.args[0])
        self.audit.configure.assert_called_once_with(retention_days=7)
        stale = self.client.patch("/admin/settings", headers=self.headers, json={"revision": 0, "values": {"max_images": 6}})
        self.assertEqual(stale.status_code, 409)

    def test_model_inventory_uses_revision_after_identity_sync(self):
        def inventory():
            self.store.update_model("custom:synced", {"public_id": "synced-model", "upstream_id": "upstream", "custom": True}, 0)
            return [{"id": "custom:synced", "public_id": "synced-model", "upstream_id": "upstream", "custom": True}]
        self.gateway.admin_model_inventory.side_effect = inventory
        response = self.client.get("/admin/models", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], 1)
        self.assertEqual(response.json()["models"][0]["public_id"], "synced-model")


    def test_dashboard_granularity_is_validated_and_forwarded(self):
        response = self.client.get("/admin/dashboard?days=1&granularity=hour", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.audit.dashboard.assert_called_with(1, granularity="hour")
        for value in ("week", "minute", "unknown"):
            self.assertEqual(self.client.get("/admin/dashboard?days=1&granularity=" + value, headers=self.headers).status_code, 400)


    def test_model_rules_preview_and_credential_constraints(self):
        response = self.client.put("/admin/models/upstream", headers=self.headers,
                                    json={"revision": 0, "public_id": "public", "credential_ids": ["fingerprint"]})
        self.assertEqual(response.status_code, 200, response.text)
        models = self.client.get("/admin/models", headers=self.headers).json()
        self.assertEqual(models["models"][0]["public_id"], "public")
        self.assertEqual(self.client.post("/admin/models/vendor/model/preview", headers=self.headers, json={}).status_code, 200)
        self.gateway.admin_model_preview.assert_called_once()
        invalid = self.client.put("/admin/models/upstream", headers=self.headers,
                                  json={"revision": 1, "region": "intl", "credential_ids": ["fingerprint"]})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.store.snapshot()["revision"], 1)

    def test_inventory_toggle_and_delete_guard(self):
        response = self.client.get("/admin/credentials", headers=self.headers)
        self.assertNotIn("NEVER-RETURN", response.text)
        self.assertEqual(response.json()["credentials"][0]["id"], "fingerprint")
        response = self.client.patch("/admin/credentials/fingerprint", headers=self.headers, json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.store.snapshot()["credentials"]["fingerprint"]["enabled"])
        self.gateway.admin_set_credential_enabled.assert_called_once_with("fingerprint", False)
        self.gateway.admin_delete_guard.side_effect = ValueError("referenced")
        self.assertEqual(self.client.delete("/admin/credentials/first.info", headers=self.headers).status_code, 409)

    def test_upload_limits_paths_and_secret_errors(self):
        for files in ([{"name": "../first.info", "content": "{}"}], [{"name": "db.sqlite3", "content": "{}"}],
                      [{"name": "first.info", "content": "x" * (1024**2 + 1)}], [{"name": "first.info", "content": "{}"}] * 101):
            result = self.client.post("/admin/credentials/upload", headers=self.headers, json={"files": files})
            self.assertEqual(result.status_code, 400)
        result = self.client.post("/admin/credentials/upload", headers=self.headers, json={"files": [{"name": "first.info", "content": '{"accessToken":"NEVER-ECHO"}'}]})
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["results"][0]["ok"])
        self.assertNotIn("NEVER-ECHO", result.text)
        self.gateway._store_credential.assert_not_called()

    def credential(self):
        return {"account": {"uid": "u"}, "auth": {"accessToken": "synthetic-token", "domain": "https://www.codebuddy.cn", "expiresAt": 1}}

    def test_valid_upload_and_replace_confirmation(self):
        content = json.dumps(self.credential())
        payload = {"files": [{"name": "first.info", "content": content}]}
        response = self.client.post("/admin/credentials/upload", headers=self.headers, json=payload)
        self.assertTrue(response.json()["results"][0]["ok"], response.text)
        self.gateway._store_credential.assert_called_once()
        (self.root / "first.info").write_text(content)
        response = self.client.post("/admin/credentials/upload", headers=self.headers, json=payload)
        self.assertFalse(response.json()["results"][0]["ok"])

    def test_upload_normalizes_token_aliases_to_canonical_fields(self):
        """只含 access_token/token 别名的凭据：落盘内容必须折叠为 accessToken，别名键移除。"""
        data = self.credential()
        auth = data.pop("auth")
        data["auth"] = {**{k: v for k, v in auth.items() if k != "accessToken"},
                        "access_token": auth["accessToken"], "token_type": "Bearer",
                        "refresh_token": "synthetic-refresh"}
        payload = {"files": [{"name": "first.info", "content": json.dumps(data)}]}
        response = self.client.post("/admin/credentials/upload", headers=self.headers, json=payload)
        self.assertTrue(response.json()["results"][0]["ok"], response.text)
        saved = json.loads(self.gateway._store_credential.call_args.args[2].decode("utf-8"))
        self.assertEqual(saved["auth"]["accessToken"], "synthetic-token")
        self.assertEqual(saved["auth"]["refreshToken"], "synthetic-refresh")
        self.assertNotIn("access_token", saved["auth"])
        self.assertNotIn("refresh_token", saved["auth"])
        self.assertNotIn("token", saved["auth"])

    def test_export_only_selected_info_and_symlink_identity_protection(self):
        content = json.dumps(self.credential())
        (self.root / "first.info").write_text(content)
        response = self.client.post("/admin/credentials/export", headers=self.headers, json={"ids": ["fingerprint"], "confirm": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, content.encode())
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(self.client.post("/admin/credentials/export", headers=self.headers, json={"ids": ["fingerprint"]}).status_code, 400)
        self.gateway._credential_identity.return_value = "changed"
        self.assertEqual(self.client.post("/admin/credentials/export", headers=self.headers, json={"ids": ["fingerprint"], "confirm": True}).status_code, 400)
        (self.root / "first.info").unlink()
        (self.root / "first.info").symlink_to(self.root / "control.sqlite3")
        self.assertEqual(self.client.post("/admin/credentials/export", headers=self.headers, json={"ids": ["fingerprint"], "confirm": True}).status_code, 400)

    def test_export_zip_contains_no_database_or_lock_files(self):
        import io
        import zipfile
        self.credentials = []
        for index in range(2):
            data = self.credential()
            identity, name = f"account-{index}", f"{index}.info"
            data["account"]["uid"] = identity
            (self.root / name).write_text(json.dumps(data))
            self.credentials.append({"id": identity, "name": name})
        self.gateway._credential_identity.side_effect = lambda data: data["account"]["uid"]
        response = self.client.post("/admin/credentials/export", headers=self.headers,
                                    json={"ids": ["account-0", "account-1"], "confirm": True})
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["0.info", "1.info"])

    def test_oauth_international_products_are_selectable_without_arbitrary_hosts(self):
        sites = {"cn": "www.codebuddy.cn", "intl": "www.workbuddy.ai", "intl-codebuddy": "www.codebuddy.ai"}
        for site, host in sites.items():
            with self.subTest(site=site):
                uri = f"https://{host}/login"
                self.gateway._OAUTH.start.return_value = {"login_id": site, "verification_uri": uri}
                response = self.client.post("/admin/oauth/start", params={"site": site}, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["verification_uri"], uri)
                self.gateway._OAUTH.start.assert_called_with(site=site)
        self.gateway._OAUTH.start.reset_mock()
        for site in ("", "codebuddy", "https://www.codebuddy.ai", "https://evil.invalid"):
            with self.subTest(invalid=site):
                response = self.client.post("/admin/oauth/start", params={"site": site}, headers=self.headers)
                self.assertEqual(response.status_code, 400)
        self.gateway._OAUTH.start.assert_not_called()

    def test_oauth_binding_csrf_idempotence_and_whitelist(self):
        self.gateway._OAUTH.start.return_value = {
            "login_id": "task", "verification_uri": "https://www.codebuddy.ai/login"}
        csrf = self.login()
        response = self.client.post("/admin/oauth/start?site=intl-codebuddy", headers=csrf)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["verification_uri"], "https://www.codebuddy.ai/login")
        self.gateway._OAUTH.start.assert_called_once_with(site="intl-codebuddy")
        self.assertEqual(self.client.get("/admin/oauth/poll?login_id=task").status_code, 403)
        self.assertEqual(self.client.get("/admin/oauth/poll?login_id=task", headers={
            "X-CSRF-Token": csrf["X-CSRF-Token"]}).status_code, 403)
        other = self.enterContext(TestClient(self.app))
        other_csrf = self.login(other)
        self.assertEqual(other.get("/admin/oauth/poll?login_id=task", headers=other_csrf).status_code, 404)
        for _ in range(2):
            response = self.client.get("/admin/oauth/poll?login_id=task", headers=csrf)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["imported"], "first.info")
        self.gateway._save_oauth_credential.assert_called_once()
        self.gateway._OAUTH.poll.assert_called_once()
        self.gateway._OAUTH.start.return_value = {"login_id": "evil", "verification_uri": "https://www.codebuddy.cn.evil.invalid/login"}
        self.assertEqual(self.client.post("/admin/oauth/start", headers=csrf).status_code, 502)

    def test_logs_dashboard_clear_confirmations_and_404(self):
        self.assertEqual(self.client.get("/admin/logs?kind=admin&limit=10&model=m", headers=self.headers).status_code, 200)
        self.audit.list_records.assert_called_once_with("admin", 10, None, model="m")
        self.assertEqual(self.client.get("/admin/logs/missing", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/admin/dashboard?days=7", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/admin/dashboard?days=8", headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/admin/logs/clear", headers=self.headers, json={"scope": "all", "confirmation": CLEAR_CONFIRMATION}).status_code, 403)
        response = self.client.post("/admin/logs/clear", headers=self.headers, json={"scope": "all", "confirmation": CLEAR_CONFIRMATION, "api_key": "synthetic-key"})
        self.assertEqual(response.status_code, 200)
        self.audit.clear.assert_called_once_with("all")
        self.assertEqual(self.client.get("/admin/missing", headers=self.headers).status_code, 404)

    def test_actual_audit_configuration_contract(self):
        from app.audit_store import AuditStore
        real = AuditStore(self.root / "audit.sqlite3")
        self.addCleanup(real.close)
        self.audit.configure.side_effect = real.configure
        response = self.client.patch("/admin/settings", headers=self.headers,
                                     json={"revision": 0, "values": {"audit_max_bytes": 2 * 1024**2,
                                                                         "audit_diagnostic_bytes": 1024,
                                                                         "audit_retention_days": 7}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(real.storage()["preview_limit"], 1024)
        self.assertEqual(real.storage()["max_bytes"], 2 * 1024**2)

    def test_external_errors_are_redacted(self):
        self.gateway.admin_model_preview.side_effect = ValueError("synthetic-secret-token")
        response = self.client.post("/admin/models/upstream/preview", headers=self.headers, json={})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("synthetic-secret-token", response.text)


if __name__ == "__main__":
    unittest.main()
