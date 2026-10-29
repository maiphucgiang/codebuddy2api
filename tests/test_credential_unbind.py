"""Deleting credentials that are still bound by model rules must auto-unbind on confirmation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import threading


from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import converter
from app import admin_account
from app.admin_api import install_admin
from app.audit_store import AuditStore
from tests.admin_session import login as session_login
from app.control_store import ControlStore
from app.gateway_management import Management


class UnbindDeleteTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": str(root)}))
        self.enterContext(patch.dict(converter.CONFIG, {"log_path": None, "cred_pool": None, "cred": None}))
        self.path = root / "account.info"
        self.path.write_text(json.dumps({"account": {"uid": "synthetic"}, "auth": {
            "accessToken": "old-synthetic-token", "refreshToken": "synthetic-refresh",
            "domain": "www.codebuddy.cn", "expiresAt": (time.time() + 86400) * 1000,
            "lastRefreshTime": time.time() * 1000}}), encoding="utf-8")
        self.pool = converter.CredentialPool([self.path])
        self.store = ControlStore(root / "control.sqlite3")
        self.addCleanup(self.store.close)
        self.config = {"cred_pool": self.pool, "control_store": self.store,
                       "ledger": None, "trial_ledger": None}
        self.management = Management(SimpleNamespace(CONFIG=self.config))
        self.identity = self.pool.entries()[0]["account_key"]

    def bind(self):
        self.store.update_model("glm-4-flash", {"credential_ids": [self.identity]}, 0)


    def route_client(self, config):
        audit = AuditStore(self.store.path.parent / "audit.sqlite3")
        self.addCleanup(audit.close)
        admin_account.save(self.store, admin_account.new("admin", "secret-key", "user"))
        config.update(audit_store=audit, state_store=self.store.state,
                      session_path=self.store.path.parent / "sessions.json")
        app = FastAPI()
        app.router.routes = list(converter.app.router.routes)
        install_admin(app, config, self.management)
        client = self.enterContext(TestClient(app, base_url="https://testserver"))
        headers = session_login(client, origin="https://testserver", password="secret-key")
        return client, headers
    def test_bound_delete_is_blocked_without_the_confirmation_flag(self):
        self.bind()
        with self.assertRaises(HTTPException) as ctx:
            self.management.admin_delete_guard("account.info")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["models"], ["glm-4-flash"])

    def test_confirmed_unbind_rolls_back_when_removal_fails(self):
        self.store.update_model("glm-4-flash", {"public_id": "glm4", "credential_ids": [self.identity, "other"]}, 0)
        identity = self.management.admin_delete_guard("account.info", unbind=True)
        self.assertEqual(identity, self.identity)
        rollback = self.management.admin_unbind_credential(identity)
        self.assertEqual(rollback, {"identity": self.identity, "rules": {"glm-4-flash": [self.identity, "other"]}})
        self.assertEqual(self.store.snapshot()["models"]["glm-4-flash"]["credential_ids"], ["other"])
        self.management.admin_restore_bindings(rollback)  # A failed removal undoes the unbind.
        rule = self.store.snapshot()["models"]["glm-4-flash"]
        self.assertEqual(rule["credential_ids"], [self.identity, "other"])
        self.assertEqual(rule["public_id"], "glm4")
        self.assertTrue(self.path.exists())

    def test_rollback_preserves_concurrent_binding_edits(self):
        self.store.update_model("glm-4-flash", {"credential_ids": [self.identity, "other"]}, 0)
        identity = self.management.admin_delete_guard("account.info", unbind=True)
        rollback = self.management.admin_unbind_credential(identity)
        # Another administrator rebinds the rule between the unbind and the rollback.
        revision = self.store.snapshot()["revision"]
        self.store.update_model("glm-4-flash", {"credential_ids": ["fresh"]}, revision)
        self.management.admin_restore_bindings(rollback)
        self.assertEqual(self.store.snapshot()["models"]["glm-4-flash"]["credential_ids"], ["fresh"])

    def test_route_keeps_bindings_when_removal_fails(self):
        self.bind()
        config = dict(self.config, api_key="secret", management=self.management)
        client, headers = self.route_client(config)
        self.enterContext(patch.dict(converter.CONFIG, config))
        with patch.object(self.pool, "remove_file", return_value=False):
            response = client.delete("/admin/credentials/account.info?unbind=1", headers=headers)
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.store.snapshot()["models"]["glm-4-flash"]["credential_ids"],
                         [self.identity])  # A failed removal never widens routing to automatic.
        response = client.delete("/admin/credentials/account.info?unbind=1", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.snapshot()["models"]["glm-4-flash"]["credential_ids"], [])
        self.assertFalse(self.path.exists())

    def test_route_sweeps_a_rebind_that_raced_the_removal(self):
        self.bind()
        config = dict(self.config, api_key="secret", management=self.management,
                      admin_mutation_lock=threading.RLock())
        client, headers = self.route_client(config)
        self.enterContext(patch.dict(converter.CONFIG, config))
        real_remove = self.pool.remove_file
        def rebind_during_removal(file_name):
            # A writer outside the shared lock rebinds while the credential is pooled.
            revision = self.store.snapshot()["revision"]
            self.store.update_model("glm-4-flash", {"credential_ids": [self.identity]}, revision)
            return real_remove(file_name)
        with patch.object(self.pool, "remove_file", side_effect=rebind_during_removal):
            response = client.delete("/admin/credentials/account.info?unbind=1", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.snapshot()["models"]["glm-4-flash"]["credential_ids"], [])
        self.assertFalse(self.path.exists())

    def test_unbound_and_unknown_credentials_stay_on_the_fast_path(self):
        self.assertIsNone(self.management.admin_delete_guard("account.info"))  # No bindings.
        self.assertIsNone(self.management.admin_delete_guard("missing.info", unbind=True))  # Pool reports 404.
        self.assertEqual(self.store.snapshot()["revision"], 0)



if __name__ == "__main__":
    unittest.main(verbosity=2)
