"""Offline configuration migration: target identity, lease refusal and atomic writes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hashlib
import sqlite3
import tempfile
import unittest

from app import config_migration
from app.control_store import ControlStore


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.db = self.auth / "control.sqlite3"
        store = ControlStore(self.db)
        store.close()
        self.env = self.root / ".env"

    def write_env(self, text):
        self.env.write_text(text, encoding="utf-8")
        return self.env

    def snapshot_settings(self):
        store = ControlStore(self.db)
        try:
            return store.snapshot()
        finally:
            store.close()

    def test_resolves_the_env_file_and_the_data_directory_it_declares(self):
        env = self.write_env("CODEBUDDY_AUTH_DIR=./auth\nCODEBUDDY2API_MAX_IMAGES=8\n")
        targets = config_migration.resolve_targets(cwd=self.root, env_file=env, environ={})
        self.assertEqual(targets["db_path"], (self.auth / "control.sqlite3").resolve())
        self.assertEqual(targets["env_values"]["CODEBUDDY2API_MAX_IMAGES"], "8")
        explicit = config_migration.resolve_targets(cwd=self.root, env_file=env, environ={},
                                                    data_dir=self.root / "other")
        self.assertEqual(explicit["db_path"], (self.root / "other" / "control.sqlite3").resolve())

    def test_rejects_conflicting_or_unsafe_paths(self):
        with self.assertRaises(config_migration.MigrationError):
            config_migration.resolve_targets(no_env=True, env_file=self.env, cwd=self.root)
        with self.assertRaises(config_migration.MigrationError):
            config_migration.resolve_targets(db="a", data_dir="b", cwd=self.root)
        link = self.root / "linked.env"
        link.symlink_to(self.write_env("A=1\n"))
        with self.assertRaisesRegex(config_migration.MigrationError, "符号链接"):
            config_migration.read_env_file(link)
        broken = self.write_env('A="unterminated\n')
        with self.assertRaises(config_migration.MigrationError):
            config_migration.read_env_file(broken)

    def test_identity_check_rejects_foreign_or_stale_databases(self):
        plain = self.root / "foreign.sqlite3"
        conn = sqlite3.connect(plain)
        conn.execute("CREATE TABLE other (name TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(config_migration.MigrationError, "user_version=0"):
            config_migration.inspect_database(plain)
        empty = self.root / "empty.sqlite3"
        empty.touch()
        with self.assertRaisesRegex(config_migration.MigrationError, "文件为空"):
            config_migration.inspect_database(empty)
        text_file = self.root / "notes.txt"
        text_file.write_text("hello", encoding="utf-8")
        with self.assertRaisesRegex(config_migration.MigrationError, "不是 SQLite"):
            config_migration.inspect_database(text_file)
        logs = self.root / "logs.sqlite3"
        ControlStore(logs).close()
        with self.assertRaisesRegex(config_migration.MigrationError, "logs.sqlite3"):
            config_migration.inspect_database(logs)
        older = self.root / "older.sqlite3"
        conn = sqlite3.connect(older)
        conn.execute("CREATE TABLE control (id INTEGER PRIMARY KEY, revision INTEGER, payload TEXT)")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(config_migration.MigrationError, "schema=1"):
            config_migration.inspect_database(older)

    def test_identity_check_accepts_a_control_store(self):
        info = config_migration.inspect_database(self.db)
        self.assertEqual((info["version"], info["revision"], info["accounts"]), (3, 0, 0))
        self.assertIsNone(info["lease"])


    def test_v2_requires_upgrade_and_api_keys_table_is_mandatory(self):
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA user_version=2")
        with self.assertRaisesRegex(config_migration.MigrationError, "schema=2.*先用当前版本服务启动"):
            config_migration.inspect_database(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA user_version=3")
            db.execute("DROP TABLE api_keys")
        with self.assertRaisesRegex(config_migration.MigrationError, "api_keys"):
            config_migration.inspect_database(self.db)
    def test_dry_run_reports_changes_without_writing(self):
        self.write_env("CODEBUDDY2API_MAX_IMAGES=8\nCODEBUDDY2API_STREAM_MODE=realtime\n")
        report = config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env, environ={}, dry_run=True)
        self.assertFalse(report["written"])
        self.assertEqual(report["changes"], {"max_images": 8, "stream_mode": "realtime"})
        self.assertEqual(self.snapshot_settings()["revision"], 0)

    def test_migrate_writes_runtime_values_and_mutes_the_notice(self):
        self.write_env("CODEBUDDY2API_MAX_IMAGES=8\nCODEBUDDY2API_BIND=0.0.0.0\n")
        report = config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env, environ={})
        self.assertTrue(report["written"])
        snapshot = self.snapshot_settings()
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["settings"]["max_images"], 8)
        self.assertTrue(snapshot["settings"]["config_notice_muted"])
        self.assertNotIn("host", snapshot["settings"])  # Deployment keys stay in .env.
        again = config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env, environ={})
        self.assertFalse(again["written"])
        self.assertEqual(self.snapshot_settings()["revision"], 1)

    def test_invalid_value_leaves_the_database_untouched(self):
        self.write_env("CODEBUDDY2API_MAX_IMAGES=not-a-number\n")
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaises(config_migration.MigrationError):
            config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env, environ={})
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_running_lease_blocks_migration_unless_forced(self):
        store = ControlStore(self.db)
        try:
            store.claim_service_lease()
        finally:
            store.close()
        self.write_env("CODEBUDDY2API_MAX_IMAGES=8\n")
        with self.assertRaisesRegex(config_migration.MigrationError, "运行中的实例"):
            config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env, environ={})
        forced = config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env, environ={}, force=True)
        self.assertTrue(forced["written"])

    def test_key_filter_limits_the_batch(self):
        self.write_env("CODEBUDDY2API_MAX_IMAGES=8\nCODEBUDDY2API_STREAM_MODE=realtime\n")
        report = config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env,
                                          environ={}, keys=["max_images"])
        self.assertEqual(report["changes"], {"max_images": 8})
        with self.assertRaisesRegex(config_migration.MigrationError, "未知配置项"):
            config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env, environ={}, keys=["nope"])

    def test_language_is_saved_with_the_same_transaction(self):
        self.write_env("CODEBUDDY2API_MAX_IMAGES=8\n")
        report = config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env,
                                          environ={}, language="en")
        self.assertEqual(report["language"], "en")
        snapshot = self.snapshot_settings()
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["settings"]["language"], "en")
        again = config_migration.migrate(cwd=self.root, db=self.db, env_file=self.env,
                                         environ={}, language="en")
        self.assertFalse(again["written"])
        self.assertIsNone(again["language"])

    def test_language_only_change_does_not_require_env_values(self):
        report = config_migration.migrate(cwd=self.root, db=self.db, no_env=True,
                                          environ={}, language="zh")
        self.assertEqual(report["changes"], {})
        self.assertEqual(report["language"], "zh")
        self.assertTrue(report["written"])
        self.assertEqual(self.snapshot_settings()["settings"]["language"], "zh")

    def test_missing_database_is_never_created(self):
        missing = self.root / "nope" / "control.sqlite3"
        with self.assertRaisesRegex(config_migration.MigrationError, "控制库不存在"):
            config_migration.migrate(cwd=self.root, db=missing, no_env=True, environ={})
        self.assertFalse(missing.parent.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
