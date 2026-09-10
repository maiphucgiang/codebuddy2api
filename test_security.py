"""凭据导入、健康接口和会话标识的安全回归测试。"""

import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import converter
import credential_io
from credential_io import (
    CredentialFileError, MAX_CREDENTIAL_BYTES, atomic_write_credential, read_import_file,
)


def credential(uid="test-user", token="test-only-token"):
    return {"account": {"uid": uid, "nickname": "test-name"},
            "auth": {"accessToken": token, "domain": "www.codebuddy.cn",
                     "expiresAt": int((time.time() + 3600) * 1000)}}


def encoded(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.imports = self.auth / "imports"
        self.imports.mkdir(parents=True)
        self.pool = converter.CredentialPool([], scan=False)
        env = patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": str(self.auth),
                                      "CODEBUDDY_IMPORT_DIR": str(self.imports)})
        env.start()
        self.addCleanup(env.stop)
        config = patch.dict(converter.CONFIG, {"api_key": "", "cred_pool": self.pool,
                                               "cred": None, "log_path": None})
        config.start()
        self.addCleanup(config.stop)

    def source(self, name="account.info", data=None):
        path = self.imports / name
        path.write_bytes(encoded(credential() if data is None else data))
        return path

    def post(self, path, authorization=None):
        return asyncio.run(converter.admin_add_credential(Request({"path": path}), authorization, None))

    def assert_http(self, code, call):
        with self.assertRaises(converter.HTTPException) as caught:
            call()
        self.assertEqual(caught.exception.status_code, code)
        text = json.dumps(caught.exception.detail, ensure_ascii=False)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("synthetic-secret", text)
        return caught.exception

    def test_import_name_and_absolute_path(self):
        path = self.source()
        for value in [path.name, str(path)]:
            result = self.post(value)
            self.assertEqual(Path(result["imported"]), self.auth / path.name)
            self.assertEqual((self.auth / path.name).read_bytes(), path.read_bytes())
            self.assertEqual(len(self.pool.entries()), 1)
            self.assertEqual(self.pool.first().summary()["uid"], "test-user")
            if os.name != "nt":
                self.assertEqual((self.auth / path.name).stat().st_mode & 0o777, 0o600)

    def test_default_import_directory(self):
        path = self.source()
        with patch.dict(os.environ):
            os.environ.pop("CODEBUDDY_IMPORT_DIR", None)
            self.post(path.name)
        self.assertTrue((self.auth / path.name).is_file())

    def test_arbitrary_paths_never_opened(self):
        self.source()
        outside = self.root / "outside.info"
        outside.write_text("synthetic-secret", encoding="utf-8")
        fake_prefix = self.root / "auth" / "imports-other"
        fake_prefix.mkdir()
        (fake_prefix / "account.info").write_bytes(encoded(credential()))
        inputs = [str(outside), "../../outside.info", "../imports/account.info",
                  str(fake_prefix / "account.info"), "subdir/account.info", "account.info\x00"]
        with patch.object(credential_io.os, "open", side_effect=AssertionError("must not open outside")):
            for value in inputs:
                with self.subTest(path=value):
                    self.assert_http(400, lambda: self.post(value))

    def test_invalid_request_types(self):
        for value in [None, True, 123, [], {}, "", "a" * 4097]:
            with self.subTest(value=type(value).__name__):
                self.assert_http(400, lambda: self.post(value))
        for body in [None, [], "path", 123]:
            self.assert_http(400, lambda: asyncio.run(
                converter.admin_add_credential(Request(body), None, None)))
        request = Mock()
        async def invalid_json():
            raise ValueError("synthetic-secret")
        request.json = invalid_json
        self.assert_http(400, lambda: asyncio.run(
            converter.admin_add_credential(request, None, None)))

    def test_non_info_and_directory_rejected(self):
        self.source("account.json")
        self.assert_http(400, lambda: self.post("account.json"))
        (self.imports / "directory.info").mkdir()
        self.assert_http(400, lambda: self.post("directory.info"))
        self.assertFalse((self.auth / "account.json").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_fifo_rejected_without_open(self):
        os.mkfifo(self.imports / "pipe.info")
        with patch.object(credential_io.os, "open", side_effect=AssertionError("must not open FIFO")):
            self.assert_http(400, lambda: self.post("pipe.info"))

    def test_source_symlink_rejected(self):
        outside = self.root / "outside.info"
        outside.write_bytes(encoded(credential()))
        link = self.imports / "link.info"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with patch.object(credential_io.os, "open", side_effect=AssertionError("must not open link")):
            self.assert_http(400, lambda: self.post(link.name))

    def test_destination_symlink_rejected(self):
        source = self.source()
        outside = self.root / "outside"
        outside.write_bytes(b"synthetic-secret")
        try:
            (self.auth / source.name).symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assert_http(400, lambda: self.post(source.name))
        self.assertEqual(outside.read_bytes(), b"synthetic-secret")

    def test_source_change_before_open_rejected(self):
        source = self.source()
        real_open = credential_io.os.open
        def replace_before_open(path, flags, *args, **kwargs):
            replacement = self.imports / "replacement"
            replacement.write_bytes(encoded(credential("changed")))
            os.replace(replacement, source)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(credential_io.os, "open", side_effect=replace_before_open):
            self.assert_http(400, lambda: self.post(source.name))
        self.assertFalse((self.auth / source.name).exists())

    def test_source_change_after_validation_cannot_change_saved_bytes(self):
        source = self.source()
        original = source.read_bytes()
        real_validate = converter.auth_oauth.validate_cred_data
        def change_after_validation(data):
            result = real_validate(data)
            source.write_bytes(b"synthetic-secret-not-json")
            return result
        with patch.object(converter.auth_oauth, "validate_cred_data", side_effect=change_after_validation):
            self.post(source.name)
        self.assertEqual((self.auth / source.name).read_bytes(), original)

    def test_oversize_file_rejected(self):
        source = self.imports / "big.info"
        source.write_bytes(b" " * (MAX_CREDENTIAL_BYTES + 1))
        self.assert_http(400, lambda: self.post(source.name))
        self.assertFalse((self.auth / source.name).exists())

    def test_oversize_growth_during_read_rejected(self):
        source = self.source()
        actual = os.fstat
        def grow_after_stat(fd):
            result = actual(fd)
            source.write_bytes(b" " * (MAX_CREDENTIAL_BYTES + 1))
            return result
        with patch.object(credential_io.os, "fstat", side_effect=grow_after_stat):
            self.assert_http(400, lambda: self.post(source.name))

    def test_invalid_credentials_do_not_write_or_leak(self):
        source = self.imports / "bad.info"
        values = [b"synthetic-secret", b"\xffsynthetic-secret", b"[]", b"null",
                  encoded({"account": {"uid": "u"}}),
                  encoded({"account": {"uid": "u"}, "auth": {
                      "accessToken": "synthetic-secret", "domain": "synthetic-secret.invalid"}}),
                  encoded({**credential(), "auth": {**credential()["auth"], "expiresAt": "synthetic-secret"}})]
        for data in values:
            with self.subTest(data=data[:15]):
                source.write_bytes(data)
                self.assert_http(400, lambda: self.post(source.name))
                self.assertFalse((self.auth / source.name).exists())

    def test_same_name_updates_pool(self):
        source = self.source()
        self.post(source.name)
        changed = credential(token="updated-test-token")
        source.write_bytes(encoded(changed))
        self.post(source.name)
        self.assertEqual(self.pool.first()._session()["auth"]["accessToken"], "updated-test-token")
        self.assertEqual(len(self.pool.entries()), 1)

    def test_duplicate_uid_different_name_conflict(self):
        source = self.source()
        self.post(source.name)
        second = self.source("second.info")
        self.assert_http(409, lambda: self.post(second.name))
        self.assertFalse((self.auth / second.name).exists())

    def test_atomic_failure_preserves_old_file(self):
        source = self.source()
        self.post(source.name)
        target = self.auth / source.name
        old = target.read_bytes()
        source.write_bytes(encoded(credential(token="updated-test-token")))
        with patch.object(credential_io.os, "replace", side_effect=OSError("synthetic-secret")):
            self.assert_http(500, lambda: self.post(source.name))
        self.assertEqual(target.read_bytes(), old)
        self.assertEqual(list(self.auth.glob(".credential-*")), [])
        self.assertEqual(self.pool.first()._session()["auth"]["accessToken"], "test-only-token")

    def test_link_swapped_after_check_is_replaced_not_followed(self):
        target = self.auth / "account.info"
        outside = self.root / "outside"
        outside.write_bytes(b"synthetic-secret")
        actual = credential_io.os.replace
        def insert_link(source, destination):
            Path(destination).symlink_to(outside)
            return actual(source, destination)
        with patch.object(credential_io.os, "replace", side_effect=insert_link):
            atomic_write_credential(self.auth, target.name, b"{}")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(), b"{}")
        self.assertEqual(outside.read_bytes(), b"synthetic-secret")

    def test_atomic_writer_rejects_bad_names(self):
        for name in ["../outside.info", "/absolute.info", "dir\\file.info", "file:stream.info",
                     "plain.json", "", ".info", "bad\x00.info", None]:
            with self.subTest(name=name), self.assertRaises(CredentialFileError):
                atomic_write_credential(self.auth, name, b"{}")

    def test_auth_before_body_or_filesystem_access(self):
        converter.CONFIG["api_key"] = "test-only-api-key"
        request = Mock()
        with patch.object(converter, "read_import_file", side_effect=AssertionError("must not read")):
            self.assert_http(401, lambda: asyncio.run(
                converter.admin_add_credential(request, None, None)))
            request.json.assert_not_called()
        source = self.source()
        self.post(source.name, "Bearer test-only-api-key")

    def test_public_health_never_touches_credentials(self):
        converter.CONFIG["api_key"] = "test-only-api-key"
        pool = Mock()
        pool.snapshot.side_effect = AssertionError("synthetic-secret")
        cred = Mock()
        cred.summary.side_effect = AssertionError("synthetic-secret")
        for configured_pool in [pool, None]:
            with patch.dict(converter.CONFIG, {"cred_pool": configured_pool, "cred": cred}):
                self.assertEqual(converter.health(), {"status": "ok"})
        pool.snapshot.assert_not_called()
        cred.summary.assert_not_called()

    def test_snapshot_masks_exception(self):
        cm = Mock()
        cm.summary.side_effect = RuntimeError("synthetic-secret")
        self.pool._entries = [{"id": str(self.auth / "account.info"), "cm": cm,
                               "fail_until": 0.0, "uid": None}]
        result = self.pool.snapshot()
        self.assertTrue(result[0]["error"])
        self.assertNotIn("synthetic-secret", json.dumps(result))

    def test_sha256_session_and_conversation_ids(self):
        payload = {"messages": [{"role": "system", "content": "system"},
                                {"role": "user", "content": "first"}]}
        key = converter.session_key(payload)
        self.assertEqual(key, hashlib.sha256(b"system\x00first").hexdigest()[:32])
        continued = {"messages": payload["messages"] + [{"role": "assistant", "content": "answer"},
                                                       {"role": "user", "content": "next"}]}
        self.assertEqual(converter.session_key(continued), key)
        self.assertNotEqual(converter.session_key({"input": "different"}), key)
        self.assertIsNone(converter.session_key({}))
        first = converter._dynamic_request_headers(key)
        second = converter._dynamic_request_headers(key)
        expected = str(uuid.UUID(hex=hashlib.sha256(key.encode()).hexdigest()[:32]))
        self.assertEqual(first["X-Conversation-ID"], expected)
        self.assertEqual(second["X-Conversation-ID"], expected)
        self.assertNotEqual(first["X-Request-ID"], second["X-Request-ID"])
        uuid.UUID(converter._dynamic_request_headers(None)["X-Conversation-ID"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
