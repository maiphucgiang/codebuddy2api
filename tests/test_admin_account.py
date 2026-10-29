"""Management-account startup, password, session and recovery contracts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hashlib
import io
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import admin_account
from app.admin_auth import AdminAuth
from app.control_store import ControlStore
from app.startup import resolve_startup_auth
from converter import admin_reset_password


class AdminAccountTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.control = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(self.control.close)
        self.config = {
            "control_store": self.control,
            "state_store": self.control.state,
            "settings_sources": {},
            "api_key": "",
            "host": "127.0.0.1",
            "session_path": self.root / "sessions.json",
        }

    def resolve(self, key="", source="default"):
        self.config["api_key"] = key
        self.config["settings_sources"]["api_key"] = source
        args = SimpleNamespace(api_key=key)
        output = io.StringIO()
        with patch("sys.stdout", output), patch("app.startup.terminal_stream", side_effect=OSError("no tty")):
            resolve_startup_auth(self.config, args)
        return args, output.getvalue()

    def auth(self):
        return AdminAuth(self.config)

    def request(self):
        return SimpleNamespace(client=SimpleNamespace(host="offline-test"), cookies={})

    def test_generated_account_is_disclosed_once_without_inference_key(self):
        args, first = self.resolve()
        account = admin_account.load(self.control)
        self.assertEqual(account.password_source, "generated")
        self.assertEqual(self.config["api_key"], "")
        self.assertEqual(args.api_key, "")
        self.assertEqual(self.control._db.execute("SELECT count(*) FROM api_keys").fetchone()[0], 0)
        self.assertEqual(first.count("初始管理密码："), 1)
        _, second = self.resolve()
        self.assertEqual(second, "")

    def test_external_change_blank_fallback_and_matching_password_transition(self):
        self.config["settings_sources"]["api_key"] = "cli"
        self.resolve("external-key", "cli")
        account = admin_account.load(self.control)
        self.assertEqual(account.password_source, "external")
        epoch = account.session_epoch
        self.resolve("external-key", "cli")
        self.assertEqual(admin_account.load(self.control).session_epoch, epoch)
        self.resolve("new-external-key", "environment")
        changed = admin_account.load(self.control)
        self.assertNotEqual(changed.session_epoch, epoch)
        self.assertEqual(changed.password_source, "external")
        _, output = self.resolve("   ", "environment")
        fallback = admin_account.load(self.control)
        self.assertEqual(fallback.password_source, "generated")
        self.assertEqual(self.config["api_key"], "")
        self.assertEqual(self.config["settings_sources"]["api_key"], "default")
        self.assertEqual(output.count("初始管理密码："), 1)

    def test_matching_generated_password_only_changes_source_not_epoch(self):
        _, output = self.resolve()
        password = re.search(r"初始管理密码：([^\n]+)", output).group(1)
        before = admin_account.load(self.control)
        self.resolve(password, "cli")
        after = admin_account.load(self.control)
        self.assertEqual(after.password_source, "external")
        self.assertEqual(after.session_epoch, before.session_epoch)

    def test_legacy_key_migrates_hash_and_never_returns_to_config(self):
        secret = "cb-" + "a" * 32
        self.control._db.execute(
            "INSERT INTO gateway_secrets(name,value,announced) VALUES('default_api_key',?,1)", (secret,))
        args, output = self.resolve()
        account = admin_account.load(self.control)
        self.assertEqual(account.password_source, "migrated")
        self.assertTrue(admin_account.verify(account, "admin", secret))
        self.assertEqual(output, "")
        self.assertEqual(args.api_key, "")
        self.assertEqual(self.config["api_key"], "")
        self.assertIsNone(self.control._db.execute(
            "SELECT value FROM gateway_secrets WHERE name='default_api_key'").fetchone())
        row = self.control._db.execute("SELECT type,secret,secret_hash FROM api_keys").fetchone()
        self.assertEqual(row[0], "admin")
        self.assertIsNone(row[1])
        self.assertEqual(row[2], hashlib.sha256(secret.encode()).hexdigest())

    def test_unannounced_legacy_output_failure_preserves_then_recovers(self):
        secret = "cb-" + "b" * 32
        self.control._db.execute(
            "INSERT INTO gateway_secrets(name,value,announced) VALUES('default_api_key',?,0)", (secret,))
        with patch("app.startup._announce_admin_password", side_effect=ValueError("无法输出初始管理密码")):
            with self.assertRaisesRegex(ValueError, "无法输出初始管理密码"):
                self.resolve()
        self.assertEqual(self.control._db.execute(
            "SELECT value FROM gateway_secrets WHERE name='default_api_key'").fetchone()[0], secret)
        self.assertIsNone(admin_account.load(self.control))
        self.assertEqual(self.control._db.execute("SELECT count(*) FROM api_keys").fetchone()[0], 0)
        _, output = self.resolve()
        self.assertIn(secret, output)
        self.assertEqual(admin_account.load(self.control).password_source, "migrated")
        self.assertEqual(self.control._db.execute(
            "SELECT imported FROM state_imports WHERE name='admin_auth_v1'").fetchone()[0], 1)

    def test_every_verify_runs_scrypt_exactly_once(self):
        account = admin_account.new("admin", "external-key", "external")
        with patch("app.admin_account._derive", return_value=b"x" * 32) as derive:
            self.assertFalse(admin_account.verify(account, "admin", "wrong-password"))
            self.assertFalse(admin_account.verify(account, "other", "external-key"))
            self.assertFalse(admin_account.verify(account, "admin", "external-key"))
        self.assertEqual(derive.call_count, 3)

    def test_login_epoch_race_never_issues_a_session(self):
        auth = self.auth()
        admin_account.save(self.control, admin_account.new("admin", "synthetic-key", "user"))
        original = admin_account.verify

        def rotate_during_verify(account, username, password):
            admin_account.save(self.control, admin_account.with_password(account, "new-password"))
            return original(account, username, password)

        with patch("app.admin_auth.admin_account.verify", side_effect=rotate_during_verify):
            result, status = auth.login(self.request(), "admin", "synthetic-key")
        self.assertEqual((result, status), (None, 503))
        self.assertEqual(len(auth.sessions), 0)

    def test_login_capacity_remember_and_revoke_all(self):
        admin_account.save(self.control, admin_account.new("admin", "synthetic-key", "user"))
        auth = self.auth()
        with patch("app.admin_account._VERIFY_SLOTS.acquire", return_value=False):
            result, status = auth.login(self.request(), "admin", "synthetic-key")
        self.assertEqual((result, status), (None, 429))
        (sid, session), status = auth.login(self.request(), "admin", "synthetic-key", True)
        self.assertEqual(status, 200)
        self.assertTrue(session["remember"])
        self.assertGreater(session["expires"], 399 * 86400)
        self.assertTrue(auth.revoke_all())
        self.assertEqual(auth.session(SimpleNamespace(cookies={"codebuddy_admin_session": sid})), (None, None))

    def test_cli_reset_updates_account_and_revokes_existing_session(self):
        admin_account.save(self.control, admin_account.new("admin", "synthetic-key", "user"))
        auth = self.auth()
        (sid, _), status = auth.login(self.request(), "admin", "synthetic-key")
        self.assertEqual(status, 200)
        args = SimpleNamespace(data_dir=str(self.root), username="operator")
        with patch("sys.stdin.isatty", return_value=True), \
                patch("converter.getpass.getpass", side_effect=["new-password", "new-password"]):
            self.assertEqual(admin_reset_password(args), 0)
        account = admin_account.load(self.control)
        self.assertEqual((account.username, account.password_source), ("operator", "user"))
        self.assertTrue(admin_account.verify(account, "operator", "new-password"))
        self.assertEqual(auth.session(SimpleNamespace(cookies={"codebuddy_admin_session": sid})), (None, None))


if __name__ == "__main__":
    unittest.main()
