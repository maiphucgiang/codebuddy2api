"""Management identity epochs and public static-resource boundaries."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.admin_auth import AdminAuth, COOKIE_NAME, MAX_PERSISTED_SESSIONS, SESSION_FILE_VERSION, SESSION_TTL
from app.gateway_management import install_pages


def request(key=None, cookie=None):
    headers = []
    if key is not None:
        headers.append((b"authorization", ("Bearer " + key).encode()))
    if cookie is not None:
        headers.append((b"cookie", (COOKIE_NAME + "=" + cookie).encode()))
    return Request({"type": "http", "method": "GET", "path": "/admin/session",
                    "headers": headers, "client": ("127.0.0.1", 1)})


class IdentityEpochTests(unittest.TestCase):
    def setUp(self):
        self.config = {"api_key": "synthetic-management-key"}
        self.auth = AdminAuth(self.config)

    def test_header_identity_is_stable_but_not_derived_from_the_key(self):
        owner = self.auth.header_identity(request(self.config["api_key"]))
        self.assertTrue(owner.startswith("key:"))
        self.assertEqual(owner, self.auth.header_identity(request(self.config["api_key"])))
        other = AdminAuth(dict(self.config))
        self.assertNotEqual(owner, other.header_identity(request(self.config["api_key"])))
        self.assertFalse(self.auth.check_key(owner))

    def test_reusing_an_old_key_never_revives_an_old_identity_or_cookie(self):
        key = self.config["api_key"]
        owner = self.auth.header_identity(request(key))
        (sid, _), status = self.auth.login(request(), key)
        self.assertEqual(status, 200)
        self.config["api_key"] = "rotated-synthetic-key"
        self.assertTrue(self.auth.enabled())
        self.assertIsNone(self.auth.header_identity(request(key)))
        self.assertEqual(self.auth.session(request(cookie=sid)), (None, None))
        self.config["api_key"] = key
        self.assertNotEqual(owner, self.auth.header_identity(request(key)))
        self.assertEqual(self.auth.session(request(cookie=sid)), (None, None))

    def test_equal_config_values_preserve_current_sessions(self):
        key = self.config["api_key"]
        owner = self.auth.header_identity(request(key))
        (sid, _), _ = self.auth.login(request(), key)
        self.config["api_key"] = "".join(list(key))
        self.assertEqual(owner, self.auth.header_identity(request(key)))
        self.assertEqual(self.auth.session(request(cookie=sid))[0], sid)

    def test_disabled_or_invalid_key_invalidates_sessions(self):
        for disabled in ("", None, 1, []):
            with self.subTest(disabled=disabled):
                config = {"api_key": "synthetic-key"}
                auth = AdminAuth(config)
                (sid, _), _ = auth.login(request(), config["api_key"])
                config["api_key"] = disabled
                self.assertFalse(auth.enabled())
                self.assertEqual(auth.session(request(cookie=sid)), (None, None))

    def test_key_comparison_remains_constant_time(self):
        import hmac
        compare = hmac.compare_digest
        with patch("app.admin_auth.hmac.compare_digest", wraps=compare) as checked:
            self.assertTrue(self.auth.check_key(self.config["api_key"]))
            self.assertFalse(self.auth.check_key("wrong-key"))
            checked.assert_any_call(b"wrong-key", self.config["api_key"].encode())


class PersistedSessionTests(unittest.TestCase):
    """A restart must not force another login, while revocation must stay durable."""

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "admin-sessions.json"
        self.key = "synthetic-management-key"
        self.config = {"api_key": self.key, "session_path": self.path}

    def login(self, auth, key=None):
        (sid, session), status = auth.login(request(), self.key if key is None else key)
        self.assertEqual(status, 200)
        return sid, session

    def revive(self, sid, config=None):
        """Model a full process restart against the same session file."""
        return AdminAuth(dict(config or self.config)).session(request(cookie=sid))

    def assert_revoked(self, sid):
        """Assert that a restart cannot revive the session."""
        self.assertIsNone(self.revive(sid)[0])

    def write_document(self, **document):
        import json
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def write_document_at(self, path, **document):
        import json
        path.write_text(json.dumps(document), encoding="utf-8")

    def read_document(self):
        import json
        return json.loads(self.path.read_text(encoding="utf-8"))

    def fingerprint(self, key=None):
        return AdminAuth._fingerprint(self.key if key is None else key)

    def test_session_survives_a_restart(self):
        sid, session = self.login(AdminAuth(self.config))
        self.assertTrue(self.path.exists())
        self.assertEqual(self.revive(sid), (sid, session))

    def test_restored_session_keeps_its_csrf_token_and_deadline(self):
        first = AdminAuth(self.config)
        sid, session = self.login(first)
        restored = self.revive(sid)[1]
        self.assertEqual(restored["csrf_token"], session["csrf_token"])
        self.assertEqual(restored["expires"], session["expires"])

    def test_expiry_uses_the_injected_wall_clock_across_instances(self):
        now = [1_000_000.0]
        config = dict(self.config)
        sid, _ = self.login(AdminAuth(config, wall_clock=lambda: now[0]))
        alive = AdminAuth(dict(config), wall_clock=lambda: now[0] + SESSION_TTL - 1)
        self.assertEqual(alive.session(request(cookie=sid))[0], sid)
        # A restart past the deadline drops the session, on disk as well as in memory, so
        # only the injected wall clock — not a monotonic one — decides expiry.
        expired = AdminAuth(dict(config), wall_clock=lambda: now[0] + SESSION_TTL + 1)
        self.assertIsNone(expired.session(request(cookie=sid))[0])
        self.assertNotIn(sid, self.read_document()["sessions"])

    def test_rotated_key_discards_persisted_sessions(self):
        sid, _ = self.login(AdminAuth(self.config))
        rotated = {"api_key": "rotated-synthetic-key", "session_path": self.path}
        self.assertIsNone(AdminAuth(rotated).session(request(cookie=sid))[0])
        # Switching back to the original key must never resurrect the old epoch.
        self.assert_revoked(sid)

    def test_starting_with_a_disabled_key_revokes_persisted_sessions(self):
        sid, _ = self.login(AdminAuth(self.config))
        AdminAuth({"api_key": "", "session_path": self.path}).enabled()
        self.assert_revoked(sid)

    def test_startup_reconciles_without_any_admin_request(self):
        """An inference-only process must still retire the superseded key's snapshot.

        `_key()` is lazy and inference traffic bypasses `AdminMiddleware`, so without an
        explicit startup reconcile the intermediate process never touches the file and
        restarting back to the original key adopts it.
        """
        for intermediate in ("rotated-synthetic-key", ""):
            with self.subTest(intermediate=intermediate):
                sid, _ = self.login(AdminAuth(self.config))
                # A restart that only serves inference: construct and reconcile, no request.
                AdminAuth({"api_key": intermediate, "session_path": self.path}).reconcile()
                self.assert_revoked(sid)

    def test_startup_reconcile_happens_for_the_installed_admin(self):
        """`install_admin` must reconcile, not wait for the first `/admin` request."""
        from unittest.mock import Mock
        from app.admin_api import install_admin
        from fastapi import FastAPI
        config = dict(self.config, control_store=Mock(), audit_store=Mock())
        with patch.object(AdminAuth, "reconcile", autospec=True) as reconciled:
            install_admin(FastAPI(), config, Mock())
        reconciled.assert_called_once()

    def test_logout_revokes_the_persisted_session(self):
        first = AdminAuth(self.config)
        sid, _ = self.login(first)
        self.assertTrue(first.logout(request(cookie=sid)))
        self.assert_revoked(sid)

    def test_logout_reports_a_revocation_that_could_not_be_persisted(self):
        """Both the rewrite and the unlink fallback can fail; that must not be acknowledged."""
        first = AdminAuth(self.config)
        sid, _ = self.login(first)
        with patch("app.admin_auth.tempfile.mkstemp", side_effect=OSError("read-only")), \
                patch("app.admin_auth.os.unlink", side_effect=OSError("read-only")):
            self.assertFalse(first.logout(request(cookie=sid)))
        self.assertTrue(first.storage()["degraded"])
        # The session is still live, so a retry can still revoke it once storage recovers.
        self.assertEqual(first.session(request(cookie=sid))[0], sid)
        self.assertTrue(first.logout(request(cookie=sid)))
        self.assertFalse(first.storage()["degraded"])
        self.assert_revoked(sid)

    def test_in_process_revocation_clears_the_snapshot(self):
        for disabled in ("", None, 1, []):
            with self.subTest(disabled=disabled):
                config = dict(self.config)
                auth = AdminAuth(config)
                sid, _ = self.login(auth)
                config["api_key"] = disabled
                self.assertFalse(auth.enabled())
                self.assertEqual(auth.session(request(cookie=sid)), (None, None))
                self.assertFalse(self.path.exists())        # Revoked on disk, not only in memory.
                self.assert_revoked(sid)

    def test_expired_sessions_are_not_restored(self):
        sid, _ = self.login(AdminAuth(self.config))
        self.write_document(version=SESSION_FILE_VERSION, fingerprint=self.fingerprint(),
                            sessions={sid: {"csrf_token": "a" * 32, "expires": 1}})
        self.assert_revoked(sid)

    def test_symlinked_snapshot_is_never_followed(self):
        import json
        # Both cases matter: a link to a plain file, and — the one that can actually tell
        # "rejected" from "parsed and discarded" — a link to a valid session snapshot.
        sentinel = self.directory / "sentinel.json"
        sentinel.write_text("outside-sentinel", encoding="utf-8")
        target = self.directory / "real.json"
        self.write_document_at(target, version=SESSION_FILE_VERSION, fingerprint=self.fingerprint(),
                               sessions={"a" * 32: {"csrf_token": "b" * 32, "expires": 9_999_999_999}})
        for name, victim in (("plain file", sentinel), ("valid snapshot", target)):
            with self.subTest(target=name):
                link = self.directory / f"link-{name.replace(' ', '-')}.json"
                try:
                    link.symlink_to(victim)
                except (OSError, NotImplementedError):
                    self.skipTest("symlinks are unavailable on this platform")
                auth = AdminAuth({"api_key": self.key, "session_path": link})
                self.assertTrue(auth.enabled())                # Rejected, not a startup crash.
                self.assertEqual(dict(auth.sessions), {})       # The target was never adopted.
                self.assertEqual(auth.session(request(cookie="a" * 32)), (None, None))
                self.assertTrue(victim.exists())                # And was left untouched.
        self.assertIn("a" * 32, json.loads(target.read_text(encoding="utf-8"))["sessions"])

    def test_expired_records_are_dropped_from_the_snapshot(self):
        sid, _ = self.login(AdminAuth(self.config))
        self.write_document(version=SESSION_FILE_VERSION, fingerprint=self.fingerprint(),
                            sessions={sid: {"csrf_token": "a" * 32, "expires": 1_000.0}})
        later = AdminAuth(dict(self.config), wall_clock=lambda: 5_000.0)
        self.assertTrue(later.enabled())
        self.assertEqual(dict(later.sessions), {})
        # The record is gone from disk, so a wall clock that moves backward cannot revive it.
        self.assertNotIn(sid, self.read_document()["sessions"])
        rolled_back = AdminAuth(dict(self.config), wall_clock=lambda: 500.0)
        self.assertTrue(rolled_back.enabled())
        self.assertIsNone(rolled_back.session(request(cookie=sid))[0])

    def test_untrusted_snapshots_are_revoked_rather_than_adopted(self):
        import json
        sid, _ = self.login(AdminAuth(self.config))
        live = {"csrf_token": "a" * 32, "expires": 9_999_999_999}
        huge = "9" * 400
        documents = {
            "not-json": b"not json",
            "empty object": b"{}",
            "unsupported version": json.dumps({"version": 99, "fingerprint": self.fingerprint(), "sessions": {}}).encode(),
            "boolean version": json.dumps({"version": True, "fingerprint": self.fingerprint(), "sessions": {}}).encode(),
            "foreign fingerprint": json.dumps({"version": SESSION_FILE_VERSION, "fingerprint": "0" * 64,
                                               "sessions": {sid: live}}).encode(),
            "non-ascii fingerprint": json.dumps({"version": SESSION_FILE_VERSION, "fingerprint": "\u4e2d\u6587",
                                                 "sessions": {}}).encode(),
            "unbounded expiry": json.dumps({"version": SESSION_FILE_VERSION, "fingerprint": self.fingerprint(),
                                            "sessions": {sid: {"csrf_token": "a" * 32, "expires": 1e12}}}).encode(),
            "non-finite expiry": b'{"version": 1, "fingerprint": "' + self.fingerprint().encode()
                                 + b'", "sessions": {"' + sid.encode() + b'": {"csrf_token": "'
                                 + b'a' * 32 + b'", "expires": Infinity}}}',
            "oversized sid": json.dumps({"version": SESSION_FILE_VERSION, "fingerprint": self.fingerprint(),
                                          "sessions": {"a" * 500: live}}).encode(),
            "oversized csrf": json.dumps({"version": SESSION_FILE_VERSION, "fingerprint": self.fingerprint(),
                                           "sessions": {sid: {"csrf_token": "a" * 500, "expires": 9e9}}}).encode(),
            "duplicate field": ('{"version": 1, "fingerprint": "' + self.fingerprint()
                                + '", "fingerprint": "' + self.fingerprint() + '", "sessions": {}}').encode(),
            "extra field": json.dumps({"version": SESSION_FILE_VERSION, "fingerprint": self.fingerprint(),
                                        "sessions": {}, "extra": 1}).encode(),
            "oversized file": b"x" * (300 * 1024),
            # An integer too large for float() must not raise OverflowError out of enabled().
            "expiry beyond float": ('{"version": 1, "fingerprint": "' + self.fingerprint()
                                    + '", "sessions": {"' + sid + '": {"csrf_token": "' + "a" * 32
                                    + '", "expires": ' + huge + '}}}').encode(),
            "negative expiry beyond float": ('{"version": 1, "fingerprint": "' + self.fingerprint()
                                             + '", "sessions": {"' + sid + '": {"csrf_token": "' + "a" * 32
                                             + '", "expires": -' + huge + '}}}').encode(),
            # Deeply nested JSON inside the size bound must not raise RecursionError either.
            "deeply nested": b"[" * 50_000 + b"]" * 50_000,
        }
        for name, content in documents.items():
            with self.subTest(name=name):
                self.path.write_bytes(content)
                auth = AdminAuth(dict(self.config))
                self.assertTrue(auth.enabled())             # Malformed input never breaks startup.
                self.assertEqual(auth.session(request(cookie=sid)), (None, None))
                self.assertFalse(self.path.exists())        # It is revoked, not left for a later start.
                self.assert_revoked(sid)

    def test_write_failure_revokes_instead_of_leaving_a_stale_snapshot(self):
        first = AdminAuth(self.config)
        sid, _ = self.login(first)
        with patch("app.admin_auth.tempfile.mkstemp", side_effect=OSError("disk full")):
            first.logout(request(cookie=sid))
        self.assert_revoked(sid)

    def test_key_rotation_survives_a_write_failure(self):
        sid, _ = self.login(AdminAuth(self.config))
        with patch("app.admin_auth.tempfile.mkstemp", side_effect=OSError("disk full")):
            AdminAuth({"api_key": "rotated-synthetic-key", "session_path": self.path}).enabled()
        self.assert_revoked(sid)

    def test_missing_path_keeps_sessions_in_memory_only(self):
        auth = AdminAuth({"api_key": self.key})
        sid, _ = self.login(auth)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertIsNone(AdminAuth({"api_key": self.key}).session(request(cookie=sid))[0])

    def test_snapshot_bounds_the_session_table(self):
        auth = AdminAuth(self.config)
        for _ in range(MAX_PERSISTED_SESSIONS + 20):
            self.login(auth)
        self.assertLessEqual(len(auth.sessions), MAX_PERSISTED_SESSIONS)
        self.assertLessEqual(len(self.revive(next(iter(auth.sessions)))[1]), 2)

    def test_snapshot_is_owner_only_where_the_platform_supports_it(self):
        import stat as stat_module
        self.login(AdminAuth(self.config))
        if sys.platform != "win32":
            self.assertEqual(stat_module.S_IMODE(self.path.stat().st_mode), 0o600)


class StaticBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.directory / "dist"
        self.assets = self.root / "assets"
        self.assets.mkdir(parents=True)
        (self.root / "index.html").write_text("<html>synthetic-shell</html>")
        (self.root / "private.txt").write_text("not-an-asset")
        self.outside = self.directory / "outside.txt"
        self.outside.write_text("outside-sentinel")
        (self.assets / "app.js").write_text("console.log('synthetic');")
        self.application = FastAPI()
        install_pages(self.application, self.root)
        self.client = self.enterContext(TestClient(self.application))

    def test_assets_support_get_head_and_conditional_requests(self):
        response = self.client.get("/dashboard/assets/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response.headers["cache-control"])
        head = self.client.head("/dashboard/assets/app.js")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        cached = self.client.get("/dashboard/assets/app.js", headers={"If-None-Match": response.headers["etag"]})
        self.assertEqual(cached.status_code, 304)
        self.assertIn("immutable", cached.headers["cache-control"])

    def test_asset_requests_cannot_escape_to_the_shell_or_private_files(self):
        for path in ("%2e%2e/index.html", "%2e%2e/private.txt", "%2e%2e/%2e%2e/outside.txt",
                     "%2fetc%2fpasswd", "%5c..%5cprivate.txt", "%00.js"):
            with self.subTest(path=path):
                response = self.client.get("/dashboard/assets/" + path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("immutable", response.headers.get("cache-control", ""))
                self.assertNotIn("synthetic-shell", response.text)
                self.assertNotIn("outside-sentinel", response.text)

    def test_symlinks_cannot_escape_the_asset_directory(self):
        for name, target in (("outside.txt", self.outside), ("shell.html", self.root / "index.html")):
            (self.assets / name).symlink_to(target)
            with self.subTest(name=name):
                self.assertEqual(self.client.get("/dashboard/assets/" + name).status_code, 404)
        (self.assets / "external").symlink_to(self.directory, target_is_directory=True)
        self.assertEqual(self.client.get("/dashboard/assets/external/outside.txt").status_code, 404)

    def test_nested_assets_work_without_directory_indexes(self):
        nested = self.assets / "chunks"
        nested.mkdir()
        (nested / "vendor.js").write_text("export {};")
        self.assertEqual(self.client.get("/dashboard/assets/chunks/vendor.js").status_code, 200)
        for path in ("/dashboard/assets", "/dashboard/assets/", "/dashboard/assets/chunks/"):
            self.assertEqual(self.client.get(path, follow_redirects=False).status_code, 404)

    def test_shell_never_uses_the_immutable_asset_cache(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        for path in ("/v1/unknown", "/admin/unknown", "/dashboard/assets/missing.js"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("<html>", response.text)

    def test_assets_can_be_built_after_server_initialization(self):
        root = self.directory / "late-dist"
        application = FastAPI()
        install_pages(application, root)
        with TestClient(application) as client:
            self.assertEqual(client.get("/dashboard").status_code, 503)
            self.assertEqual(client.get("/dashboard/assets/new.js").status_code, 404)
            (root / "assets").mkdir(parents=True)
            (root / "index.html").write_text("<html>new-shell</html>")
            (root / "assets/new.js").write_text("export {};")
            self.assertEqual(client.get("/dashboard").status_code, 200)
            self.assertEqual(client.get("/dashboard/assets/new.js").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
