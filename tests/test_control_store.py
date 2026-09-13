"""Control metadata tests: isolated temporary databases only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import unittest

from app.control_store import ConflictError, ControlStore
from app.settings import apply_persisted_settings, resolve_settings, validate_settings


class ControlStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "control.sqlite3"
        self.store = ControlStore(self.path)
        self.addCleanup(self.store.close)

    def test_reopen_and_schema(self):
        state = self.store.update_settings({"max_images": 3}, 0)
        self.assertEqual(state["revision"], 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        other = ControlStore(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.snapshot()["settings"], {"max_images": 3})

    def test_snapshot_is_detached(self):
        self.store.update_model("real", {"public_id": "public", "credential_ids": ["fingerprint"]}, 0)
        snapshot = self.store.snapshot()
        snapshot["models"]["real"]["credential_ids"].append("other")
        self.assertEqual(self.store.snapshot()["models"]["real"]["credential_ids"], ["fingerprint"])

    def test_atomic_revision_conflict(self):
        def update(value):
            try:
                self.store.update_settings({"max_images": value}, 0)
                return True
            except ConflictError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(update, range(8))), 1)
        self.assertEqual(self.store.snapshot()["revision"], 1)

    def test_cross_connection_conflict(self):
        other = ControlStore(self.path)
        self.addCleanup(other.close)
        self.store.update_settings({"max_images": 2}, 0)
        with self.assertRaises(ConflictError):
            other.update_settings({"max_images": 5}, 0)

    def test_invalid_rules_never_commit(self):
        for rule in ({"region": "cn", "profile": "intl-work"}, {"enabled": 1}, {"credential_ids": "id"},
                     {"public_id": "other"}, {"accessToken": "not-allowed"}):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                self.store.update_model("real", rule, 0, known_models=["real", "other"])
        self.assertEqual(self.store.snapshot()["revision"], 0)

    def test_alias_collision_and_chains(self):
        self.store.update_model("real", {"public_id": "alias", "enabled": False}, 0)
        for source, rule in (("other", {"public_id": "alias"}), ("alias", {"public_id": "third"})):
            with self.assertRaises(ValueError):
                self.store.update_model(source, rule, 1)
        self.assertEqual(self.store.snapshot()["revision"], 1)

    def test_credential_metadata_and_no_secret_settings(self):
        self.store.set_credential("account-fingerprint", False)
        self.assertEqual(self.store.snapshot()["credentials"], {"account-fingerprint": {"enabled": False}})
        for values in ({"api_key": "secret"}, {"auth_dir": "/private"}, {"accessToken": "secret"}, {"max_images": True}):
            with self.assertRaises(ValueError):
                self.store.update_settings(values, 1)
        with sqlite3.connect(self.path) as db:
            raw = db.execute("SELECT payload FROM control").fetchone()[0]
        self.assertNotIn("secret", raw)
        self.assertEqual(self.store.snapshot()["revision"], 1)

    def test_corrupt_and_unknown_database_are_not_reinitialized(self):
        bad = self.root / "bad.sqlite3"
        bad.write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            ControlStore(bad)
        empty = self.root / "empty.sqlite3"
        empty.touch()
        with self.assertRaises(ValueError):
            ControlStore(empty)
        unknown = self.root / "unknown.sqlite3"
        with sqlite3.connect(unknown) as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError):
            ControlStore(unknown)
        with sqlite3.connect(unknown) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_setting_precedence_and_source_redaction(self):
        self.store.update_settings({"max_images": 3, "image_policy": "error", "port": 9000}, 0)
        config = {"control_store": self.store, "max_images": 7, "api_key": "synthetic-secret", "auth_dir": "/private"}
        apply_persisted_settings(config, explicit=["max_images"], environ={"CODEBUDDY2API_IMAGE_POLICY": "truncate"})
        self.assertEqual((config["max_images"], config["image_policy"], config["port"]), (7, "truncate", 9000))
        items = {item["key"]: item for item in resolve_settings(config)}
        self.assertEqual(items["max_images"]["source"], "cli")
        self.assertTrue(items["max_images"]["locked"])
        self.assertEqual(items["image_policy"]["source"], "environment")
        self.assertIsNone(items["api_key"]["value"])
        self.assertIsNone(items["auth_dir"]["value"])
        for invalid in ({"usd_rate": float("nan")}, {"port": 65536}, {"audit_diagnostic_bytes": 8193}):
            with self.assertRaises(ValueError):
                validate_settings(invalid)


if __name__ == "__main__":
    unittest.main()
