"""Exercise SQLite migration, persisted defaults and authentication using synthetic state."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import concurrent.futures
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.admin_auth import AdminAuth, SessionStoreError
from app.catalog_probe import AggregateCatalog
from app.control_store import ControlStore
from app.credential_cooldowns import CredentialCooldowns
from app.credits import CreditLedger, ModelCatalogCache
from app.model_blocks import ModelBlocks
from app.startup import load_startup_env, resolve_startup_auth
from app.state_store import LEGACY_FILES
from app.trial_rewards import TrialLedger, attempt_trial
from app.usage_snapshots import UsageSnapshots

IDENTITY = "a" * 64
FIXED_KEY = "synthetic-fixed-key"


class Terminal(io.StringIO):
    def isatty(self):
        return True


class SQLiteStateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.control = self.open_control()
        self.state = self.control.state
        self.now = time.time()
        self.console_open = self.enterContext(patch("app.startup.terminal_stream", side_effect=OSError("no fixture terminal")))

    def console(self):
        terminal = Terminal()
        self.console_open.side_effect = lambda: contextlib.nullcontext(terminal)
        return terminal

    def open_control(self):
        control = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(control.close)
        return control

    def create_legacy(self):
        ledger = CreditLedger(self.root / LEGACY_FILES["credits"])
        ledger.bind_identity("fixture.info", IDENTITY)
        ledger.mark_checkin("fixture.info", "2026-09-22", True, 0, "ok")
        ledger.update_credits("fixture.info", {"credits": 12, "segments": [], "partial": True})
        cache = ModelCatalogCache(self.root / LEGACY_FILES["catalog"])
        cache.put("account:version", [{"id": "model"}])
        blocks = ModelBlocks(self.root / LEGACY_FILES["model_blocks"])
        blocks.note("https://example.invalid/chat", "model", now=self.now)
        cooldowns = CredentialCooldowns(self.root / LEGACY_FILES["cooldowns"])
        cooldowns.note_credential(IDENTITY, "cn-work", self.now + 300)
        cooldowns.note_model(IDENTITY, "cn-work", "model", self.now + 600)
        usage = UsageSnapshots(self.root / LEGACY_FILES["usage"])
        usage.store("fixture.info", IDENTITY, "domestic", {"total_credits": 12, "requests": 2,
                    "by_day": {"2026-09-22": {"model": 12}}}, partial=True)
        probe = self.root / LEGACY_FILES["catalog_probe"]
        probe.write_text(json.dumps({"version": 1, "profiles": {"cn-cli": {
            "source": IDENTITY, "source_generation": 1, "fetched_at": self.now,
            "retry_at": self.now + 300, "failures": 0, "status": "ok", "models": ["model"]}}}))
        trial = TrialLedger(self.root / LEGACY_FILES["trial"])
        self.assertTrue(trial.begin(IDENTITY))
        trial.finish(IDENTITY, {"ok": True, "already": False, "code": 0, "status": 200})
        auth = AdminAuth({"api_key": FIXED_KEY, "session_path": self.root / LEGACY_FILES["sessions"]})
        result, status = auth.login(SimpleNamespace(client=SimpleNamespace(host="fixture"), cookies={}), FIXED_KEY)
        self.assertEqual(status, 200)
        return result[0]

    def test_all_legacy_sources_migrate_once_and_runtime_never_updates_json(self):
        sid = self.create_legacy()
        before = {name: (self.root / file).read_bytes() for name, file in LEGACY_FILES.items()}
        self.state.migrate(self.root)
        ledger = CreditLedger(store=self.state)
        self.assertTrue(ledger.checkin_done("fixture.info", "2026-09-22"))
        self.assertTrue(ledger.entry("fixture.info")["credits"]["partial"])
        self.assertTrue(ModelCatalogCache(store=self.state).fresh("account:version"))
        self.assertTrue(ModelBlocks(store=self.state).blocked("https://example.invalid/chat", "model"))
        cooldowns = CredentialCooldowns(store=self.state)
        self.assertGreater(cooldowns.credential_until(IDENTITY, "cn-work"), self.now)
        self.assertGreater(cooldowns.model_until(IDENTITY, "cn-work", "model"), self.now)
        self.assertTrue(UsageSnapshots(store=self.state).accounts()["fixture.info"]["partial"])
        self.assertEqual(AggregateCatalog(store=self.state).row("cn-cli")["source"], IDENTITY)
        trial = TrialLedger(store=self.state)
        self.assertFalse(trial.begin(IDENTITY, self.now + 3 * 86400))
        auth = AdminAuth({"api_key": FIXED_KEY, "state_store": self.state})
        auth.reconcile()
        self.assertIn(sid, auth.sessions)
        ledger.note_error("fixture.info", "fixture error")
        cooldowns.clear_credential(IDENTITY, "cn-work")
        UsageSnapshots(store=self.state).forget("fixture.info")
        auth.config["api_key"] = "rotated-fixture"
        auth.reconcile()
        self.state.migrate(self.root)
        self.assertFalse(CredentialCooldowns(store=self.state).credential_until(IDENTITY, "cn-work"))
        self.assertFalse(UsageSnapshots(store=self.state).accounts())
        auth.config["api_key"] = FIXED_KEY
        auth.reconcile()
        self.assertNotIn(sid, auth.sessions)
        self.assertEqual(before, {name: (self.root / file).read_bytes() for name, file in LEGACY_FILES.items()})
        self.assertEqual(self.control._db.execute("SELECT count(*) FROM state_imports").fetchone()[0], 8)

    def test_absent_legacy_file_cannot_resurrect_later(self):
        self.state.migrate(self.root)
        (self.root / LEGACY_FILES["credits"]).write_text('{"version":1,"creds":{"old":{}}}')
        self.state.migrate(self.root)
        self.assertIsNone(self.state.get("credits"))

    def test_invalid_migration_rolls_back_every_document_and_marker(self):
        self.create_legacy()
        (self.root / LEGACY_FILES["trial"]).write_text('{"version":1,"accounts":{"bad":{}}}')
        with self.assertRaisesRegex(ValueError, "trial-ledger"):
            self.state.migrate(self.root)
        for table in ("state_imports", "runtime_state"):
            self.assertEqual(self.control._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_duplicate_fields_and_upstream_tokens_are_rejected(self):
        file = self.root / LEGACY_FILES["credits"]
        for payload in ('{"version":1,"version":1,"creds":{}}',
                        '{"version":1,"creds":{"f":{"accessToken":"fixture"}}}'):
            file.write_text(payload)
            with self.assertRaises(ValueError):
                self.state.migrate(self.root)

    def test_symlink_legacy_source_is_rejected(self):
        target = self.root / "target"
        target.write_text('{"version":1,"creds":{}}')
        (self.root / LEGACY_FILES["credits"]).symlink_to(target)
        with self.assertRaises(ValueError):
            self.state.migrate(self.root)

    def test_upgrade_v1_preserves_settings_and_rejects_old_readers(self):
        self.control.update_settings({"max_images": 3}, 0)
        self.control._db.execute("PRAGMA user_version=1")
        other = self.open_control()
        self.assertEqual(other.snapshot()["settings"]["max_images"], 3)
        self.assertEqual(other._db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_trial_reservation_is_atomic_across_connections_and_failures_are_closed(self):
        other = self.open_control()
        ledgers = [TrialLedger(store=self.state), TrialLedger(store=other.state)]
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda ledger: ledger.begin(IDENTITY), ledgers))
        self.assertEqual(sorted(results), [False, True])
        self.assertFalse(ledgers[0].begin(IDENTITY))
        with patch.object(self.state, "_put", side_effect=sqlite3.OperationalError("fixture")), \
             patch("app.trial_rewards.claim_trial") as claim:
            with self.assertRaises(sqlite3.OperationalError):
                attempt_trial(ledgers[0], "b" * 64, {"X-Domain": "www.workbuddy.ai"})
            claim.assert_not_called()

    def test_cooldown_write_failure_is_visible_and_retried(self):
        cooldowns = CredentialCooldowns(store=self.state)
        with patch.object(self.state, "put", side_effect=sqlite3.OperationalError("fixture")):
            self.assertFalse(cooldowns.note_credential(IDENTITY, "cn-work", self.now + 300))
        self.assertEqual(cooldowns.last_error, "OperationalError")
        self.assertTrue(cooldowns.note_credential(IDENTITY, "cn-work", self.now + 300))
        self.assertGreater(CredentialCooldowns(store=self.state).credential_until(IDENTITY, "cn-work"), self.now)

    def test_rebuildable_cache_write_failure_keeps_in_memory_routing(self):
        blocks = ModelBlocks(store=self.state)
        catalogs = ModelCatalogCache(store=self.state)
        with patch.object(self.state, "put", side_effect=sqlite3.OperationalError("fixture")):
            with self.assertWarns(RuntimeWarning):
                blocks.note("https://example.invalid/chat", "model", now=self.now)
            with self.assertWarns(RuntimeWarning):
                catalogs.put("account:version", [{"id": "model"}])
        self.assertTrue(blocks.blocked("https://example.invalid/chat", "model"))
        self.assertTrue(catalogs.fresh("account:version"))
        self.assertEqual(catalogs.models("account:version"), [{"id": "model"}])


    def test_sqlite_session_read_failure_preserves_record_and_retries_epoch(self):
        self.create_legacy()
        self.state.migrate(self.root)
        before = self.state.get("sessions")
        for error in (sqlite3.OperationalError("locked"), sqlite3.DatabaseError("malformed"),
                      OSError("unreadable"), ValueError("invalid state")):
            auth = AdminAuth({"api_key": FIXED_KEY, "state_store": self.state})
            with self.subTest(error=type(error).__name__), \
                 patch.object(self.state, "get", side_effect=error), \
                 patch.object(self.state, "delete") as delete:
                for attempt in (auth.reconcile, auth.enabled):
                    with self.assertRaises(SessionStoreError):
                        attempt()
                    self.assertIsNone(auth._configured_key)
                    self.assertFalse(auth.sessions)
                delete.assert_not_called()
                self.assertEqual(auth.storage()["last_error"], type(error).__name__)
            self.assertEqual(self.state.get("sessions"), before)
            auth.reconcile()
            self.assertEqual(auth._configured_key, FIXED_KEY)
            self.assertEqual(set(auth.sessions), set(before["sessions"]))
            self.assertFalse(auth.storage()["degraded"])

    def test_invalid_sqlite_sessions_stop_startup_without_deleting_evidence(self):
        self.control._db.execute("INSERT INTO runtime_state VALUES('sessions', 'invalid-json')")
        auth = AdminAuth({"api_key": FIXED_KEY, "state_store": self.state})
        with self.assertRaises(SessionStoreError):
            auth.reconcile()
        self.assertIsNone(auth._configured_key)
        self.assertEqual(self.control._db.execute("SELECT payload FROM runtime_state WHERE name='sessions'").fetchone()[0],
                         "invalid-json")

    def test_all_sqlite_loaders_propagate_read_failures_without_empty_fallbacks(self):
        loaders = (lambda: CreditLedger(store=self.state), lambda: ModelCatalogCache(store=self.state),
                   lambda: ModelBlocks(store=self.state), lambda: CredentialCooldowns(store=self.state),
                   lambda: UsageSnapshots(store=self.state), lambda: TrialLedger(store=self.state).snapshot())
        for error in (sqlite3.OperationalError("locked"), ValueError("invalid state"), OSError("unreadable")):
            for loader in loaders:
                with self.subTest(error=type(error).__name__, loader=loader), \
                     patch.object(self.state, "get", side_effect=error):
                    with self.assertRaises(type(error)):
                        loader()


    def test_session_revocation_failure_does_not_activate_new_key(self):
        auth = AdminAuth({"api_key": FIXED_KEY, "state_store": self.state})
        auth.reconcile()
        auth.config["api_key"] = "rotated-fixture"
        with patch.object(self.state, "put", side_effect=sqlite3.OperationalError("fixture")), \
             patch.object(self.state, "delete", side_effect=sqlite3.OperationalError("fixture")):
            with self.assertRaises(SessionStoreError):
                auth.reconcile()
        self.assertEqual(auth._configured_key, FIXED_KEY)

    def config(self, source="default", key=""):
        return {"state_store": self.state, "control_store": self.control, "api_key": key,
                "host": "127.0.0.1", "settings_sources": {"api_key": source}}


    def test_dotenv_is_optional_and_process_environment_wins(self):
        path = self.root / ".env"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_startup_env(path), set())
            path.write_text("CODEBUDDY2API_PORT=9090\nCODEBUDDY2API_KEY=file-fixture\n")
            os.environ["CODEBUDDY2API_PORT"] = "9091"
            changed = load_startup_env(path)
            self.assertEqual(os.environ["CODEBUDDY2API_PORT"], "9091")
            self.assertEqual(os.environ["CODEBUDDY2API_KEY"], "file-fixture")
            self.assertEqual(changed, {"CODEBUDDY2API_KEY"})
            config = self.config("environment", "file-fixture")
            resolve_startup_auth(config, SimpleNamespace(api_key="file-fixture"), changed)
            self.assertEqual(config["settings_sources"]["api_key"], "dotenv")

    def test_invalid_dotenv_fails_without_loading_partial_values(self):
        path = self.root / ".env"
        path.write_text('CODEBUDDY2API_PORT=9090\nCODEBUDDY2API_KEY="unterminated\n')
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "第 2 行") as caught:
                load_startup_env(path)
            self.assertNotIn("unterminated", str(caught.exception))
            self.assertNotIn("CODEBUDDY2API_PORT", os.environ)


    def test_full_startup_precedence_and_external_removal(self):
        import converter
        from app import admin_account, runtime_management
        from fastapi import FastAPI

        def boot(env=None, flags=()):
            observed = {}
            terminal = self.console()
            with contextlib.chdir(self.root), contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"HOME": str(self.root), "CODEBUDDY_AUTH_DIR": str(self.root), **(env or {})}, clear=True))
                stack.enter_context(patch.dict(converter.CONFIG))
                stack.enter_context(patch.object(converter, "app", FastAPI()))
                stack.enter_context(patch.object(sys, "argv", ["converter.py", "--skip-check", *flags]))
                stack.enter_context(patch.object(converter, "seed_credentials"))
                stack.enter_context(patch.object(converter, "CredentialPool"))
                stack.enter_context(patch.object(converter, "_publish_model_cache"))
                stack.enter_context(patch.object(converter.threading, "Thread"))
                stack.enter_context(patch.object(runtime_management, "install"))
                output = stack.enter_context(patch("sys.stderr", Terminal()))
                def serve(app, config, **kwargs):
                    observed.update(kwargs, key=config["api_key"], sources=dict(config["settings_sources"]),
                                    account=admin_account.load(config["control_store"]))
                stack.enter_context(patch.object(converter, "run_server", side_effect=serve))
                converter.main()
                observed["output"] = output.getvalue()
                observed["terminal"] = terminal.getvalue()
            return observed

        first = boot()
        self.assertEqual(first["key"], "")
        self.assertEqual(first["sources"]["api_key"], "default")
        self.assertEqual(first["account"].password_source, "generated")
        self.assertEqual(first["terminal"].count("初始管理密码："), 1)
        self.assertNotIn("初始管理密码：", first["output"])
        envfile = self.root / ".env"
        envfile.write_text("CODEBUDDY2API_PORT=9092\nCODEBUDDY2API_KEY=file-fixture\n")
        file_run = boot()
        self.assertEqual((file_run["port"], file_run["key"], file_run["sources"]["api_key"]), (9092, "file-fixture", "dotenv"))
        self.assertEqual(file_run["account"].password_source, "external")
        environment = boot({"CODEBUDDY2API_PORT": "9093", "CODEBUDDY2API_KEY": "env-fixture"})
        self.assertEqual((environment["port"], environment["key"], environment["sources"]["api_key"]),
                         (9093, "env-fixture", "environment"))
        cli = boot({"CODEBUDDY2API_PORT": "9093", "CODEBUDDY2API_KEY": "env-fixture"},
                   ("--port", "9094", "--api-key", "cli-fixture"))
        self.assertEqual((cli["port"], cli["key"], cli["sources"]["api_key"]), (9094, "cli-fixture", "cli"))
        envfile.unlink()
        resumed = boot()
        self.assertEqual(resumed["key"], "")
        self.assertEqual(resumed["sources"]["api_key"], "default")
        self.assertEqual(resumed["account"].password_source, "generated")
        self.assertEqual(resumed["terminal"].count("初始管理密码："), 1)
        self.assertFalse(list(self.root.glob("*.json")))

    def test_listener_failure_keeps_initialized_account(self):
        import asyncio
        import uvicorn
        from fastapi import FastAPI
        from unittest.mock import AsyncMock
        from app import admin_account
        from app.startup import run_server
        config = self.config()
        terminal = self.console()
        with patch("sys.stderr", io.StringIO()):
            resolve_startup_auth(config, SimpleNamespace(api_key=""))
            with patch.object(uvicorn.Server, "startup", new=AsyncMock(side_effect=SystemExit(3))), \
                 patch.object(uvicorn.Server, "run", lambda server: asyncio.run(server.startup())):
                with self.assertRaises(SystemExit):
                    run_server(FastAPI(), config, host="127.0.0.1", port=8787)
            self.assertEqual(terminal.getvalue().count("初始管理密码："), 1)
            self.assertEqual(admin_account.load(self.control).password_source, "generated")
            self.assertEqual(config["api_key"], "")



if __name__ == "__main__":
    unittest.main()
