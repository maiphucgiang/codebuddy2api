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

from app.admin_auth import AdminAuth, COOKIE_NAME
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
