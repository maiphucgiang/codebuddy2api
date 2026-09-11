"""Synthetic, standalone safe-logging regression tests (no application imports)."""

import copy
import json
import unittest
from unittest.mock import patch

import safe_logging
from safe_logging import format_log_body, sanitize_log_text


class SafeLoggingTests(unittest.TestCase):
    def assert_bounded(self, result, limit):
        self.assertIsInstance(result, str)
        self.assertLessEqual(len(result.encode("utf-8")), limit)

    def test_normal_error_summary_is_complete(self):
        body = {
            "error": {"type": "AuthenticationError", "message": "Invalid API key"},
            "status": 401,
            "request_id": "req_0123456789abcdef0123456789abcdef",
        }
        self.assertEqual(json.loads(format_log_body(body)), body)
        text = "AuthenticationError: Invalid API key; HTTP 401; request_id=req-123"
        self.assertEqual(sanitize_log_text(text), text)

    def test_huge_image_is_redacted_before_serialization(self):
        encoded = "QWxwaGFTeW50aGV0aWNPbmx5" * 200_000
        body = {"model": "synthetic-model", "messages": [{"content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
        ]}], "request_id": "req-image"}
        dumps = json.dumps

        def bounded_dumps(value, *args, **kwargs):
            self.assertNotIsInstance(value, (list, dict))
            if isinstance(value, str):
                self.assertLess(len(value), 8192)
            return dumps(value, *args, **kwargs)

        with patch.object(safe_logging.json, "dumps", side_effect=bounded_dumps):
            result = format_log_body(body)
        self.assertNotIn(encoded[:24], result)
        for text in ("image/png", "base64 redacted", "chars total", "req-image", "synthetic-model"):
            self.assertIn(text, result)
        self.assert_bounded(result, 65536)
        self.assertLess(len(result), 1000)

    def test_image_variants_and_embedded_text(self):
        for url in (
            "data:image/jpeg;base64,U1lOVEhFVElDU0VDUkVU==",
            "DATA:IMAGE/PNG;BASE64,U1lOVEhFVElDU0VDUkVU==",
            "data:image/webp;charset=utf-8;base64,U1lOVEhFVElDU0VDUkVU==",
            r"data:image\/png;base64,U1lOVEhFVElDU0VDUkVU==",
        ):
            with self.subTest(url=url):
                text = 'error preview="' + url + '" HTTP 502 request_id=req-image'
                for render in (format_log_body, sanitize_log_text):
                    result = render(text)
                    self.assertNotIn("U1lOVEhFVElDU0VDUkVU", result)
                    self.assertIn("base64 redacted", result)
                    self.assertIn("HTTP 502", result)
                    self.assertIn("req-image", result)

    def test_nested_credentials_are_not_visited(self):
        class ForbiddenList(list):
            def __iter__(self):
                raise AssertionError("secret values must not be traversed")

        keys = (
            "Authorization", "proxy-authorization", "API keys", "apiKey", "X-API-Key",
            "accessToken", "refresh_token", "idToken", "password", "passwd", "pwd",
            "Cookie", "cookies", "Set-Cookie", "client_secret", "secretKey", "private_key",
        )
        for key in keys:
            with self.subTest(key=key):
                body = {"outer": [{key: ForbiddenList(["synthetic-secret"])}], "status": 403}
                result = format_log_body(body)
                self.assertNotIn("synthetic-secret", result)
                self.assertIn("[REDACTED]", result)
                self.assertIn('"status": 403', result)

    def test_no_mutation(self):
        value = {"a": [{"accessToken": "synthetic-token", "n": None}],
                 "image": "data:image/png;base64,U1lOVEhFVElD", "ok": [True, 1.2, "你好"]}
        before = copy.deepcopy(value)
        for limit in (0, 1, 20, 65536):
            format_log_body(value, limit)
        self.assertEqual(value, before)

    def test_utf8_budgets(self):
        text = "中文🙂é\n" * 200
        for limit in (0, 1, 2, 3, 4, 8, 13, 14, 15, 16, 31, 63, 127, 1024):
            for render, value in ((sanitize_log_text, text), (format_log_body, {"message": text})):
                with self.subTest(limit=limit, render=render.__name__):
                    result = render(value, limit)
                    self.assert_bounded(result, limit)
                    if limit:
                        self.assertTrue(result.endswith("...[truncated]"[:limit]))
                    else:
                        self.assertEqual(result, "")

    def test_exact_fit_and_small_values(self):
        self.assertEqual(sanitize_log_text("中文", 6), "中文")
        self.assertEqual(sanitize_log_text("abc", 3), "abc")
        for value in (None, True, False, 42, -1, 1.25, [], {}, "", "🙂"):
            expected = json.dumps(value, ensure_ascii=False)
            self.assertEqual(format_log_body(value, len(expected.encode("utf-8"))), expected)

    def test_zero_does_not_inspect_body(self):
        class ForbiddenDict(dict):
            def items(self):
                raise AssertionError("disabled logging must not inspect values")

        self.assertEqual(format_log_body(ForbiddenDict(secret="synthetic"), 0), "")
        self.assertEqual(sanitize_log_text("Authorization: Bearer synthetic", 0), "")

    def test_invalid_limits(self):
        for render in (format_log_body, sanitize_log_text):
            with self.assertRaises(ValueError):
                render("body", -1)
            for limit in (None, "12", 1.5):
                with self.assertRaises(TypeError):
                    render("body", limit)

    def test_long_array_has_shape_without_full_iteration(self):
        class GuardedList(list):
            def __iter__(self):
                for index, item in enumerate(super().__iter__()):
                    if index >= 32:
                        raise AssertionError("too many array entries visited")
                    yield item

        value = GuardedList([{"role": "user"}] * 100_000)
        result = format_log_body(value)
        self.assertIn('"role": "user"', result)
        self.assertIn("99968 more items", result)
        self.assert_bounded(result, 65536)

    def test_wide_mapping_has_shape_without_full_iteration(self):
        class GuardedDict(dict):
            def items(self):
                for index, pair in enumerate(super().items()):
                    if index >= 32:
                        raise AssertionError("too many object entries visited")
                    yield pair

        value = GuardedDict((f"field_{i}", i) for i in range(10_000))
        result = format_log_body(value)
        self.assertIn("9968 more fields", result)
        self.assertIn('"field_0": 0', result)

    def test_deep_nesting_and_cycles(self):
        root = {"leaf": "safe"}
        for _ in range(5000):
            root = {"child": root}
        result = format_log_body(root)
        self.assertIn("depth limit", result)
        self.assertLess(len(result), 300)
        current = root
        for _ in range(5000):
            current = current["child"]
        self.assertEqual(current, {"leaf": "safe"})
        circular = []
        circular.append(circular)
        self.assertIn("circular reference", format_log_body(circular))
        self.assertIs(circular[0], circular)

    def test_global_node_budget(self):
        visits = 0

        class CountedList(list):
            def __iter__(self):
                nonlocal visits
                for item in super().__iter__():
                    visits += 1
                    if visits > 600:
                        raise AssertionError("global traversal budget exceeded")
                    yield item

        # Shared structure represents 32**7 leaves without allocating them.
        value = CountedList([None] * 32)
        for _ in range(6):
            value = CountedList([value] * 32)
        result = format_log_body(value, 1_000_000)
        self.assertIn("truncated", result)
        self.assertLess(visits, 600)
        self.assertLess(len(result), 10_000)

    def test_huge_integer_and_unknown_type_are_safe(self):
        self.assertIn("integer: 100001 bits", format_log_body(1 << 100_000))

        class NoRepr:
            def __repr__(self):
                raise AssertionError("must not call application repr")

        self.assertEqual(format_log_body(NoRepr()), "<unsupported value>")

    def test_log_text_assignments_and_auth_headers(self):
        text = (
            "Authorization: Bearer synthetic-bearer\n"
            "proxy-authorization: Basic c3ludGhldGljOnBhc3M=\n"
            "X-API-Key: synthetic-api\n"
            "Cookie: session=synthetic-session; other=synthetic-cookie\n"
            "Set-Cookie: sid=synthetic-set; Path=/; HttpOnly\n"
            "accessToken=synthetic-access&refresh_token=synthetic-refresh\n"
            "password: 'synthetic-password' client_secret=synthetic-client\n"
            "HTTP 401 request_id=req-text error=AuthenticationError"
        )
        result = sanitize_log_text(text)
        for secret in ("synthetic-", "c3ludGhldGljOnBhc3M="):
            self.assertNotIn(secret, result)
        for diagnostic in ("HTTP 401", "request_id=req-text", "AuthenticationError"):
            self.assertIn(diagnostic, result)

    def test_embedded_json_credential_values(self):
        text = json.dumps({
            "nested": {"password": 'synthetic-secret"with quote',
                       "API keys": ["synthetic-first", {"a": "synthetic-second"}],
                       "cookie": {"session": "synthetic-cookie"}},
            "type": "HTTPError", "status": 429, "request_id": "req-json",
        })
        result = sanitize_log_text(text)
        self.assertNotIn("synthetic-", result)
        parsed = json.loads(result.replace(': [REDACTED]', ': "[REDACTED]"'))
        self.assertEqual(parsed["status"], 429)
        self.assertEqual(parsed["request_id"], "req-json")
        self.assertIn("HTTPError", result)

    def test_free_text_token_shapes(self):
        tokens = (
            "Bearer synthetic-bearer", "Basic c3ludGhldGljOnBhc3M=",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c3ludGhldGlj",
            "sk-proj-SyntheticOnly123456", "sk_live_SyntheticOnly123456",
            "rk_test_SyntheticOnly123456", "AIzaSyntheticOnly123456",
            "AKIASYNTHETICONLY1234", "ghp_SyntheticOnly123456",
            "github_pat_SyntheticOnly123456", "xoxb-SyntheticOnly123456",
        )
        for token in tokens:
            with self.subTest(token=token):
                text = "upstream token was " + token + " HTTP 403 request_id=req-shape"
                for render in (format_log_body, sanitize_log_text):
                    result = render(text)
                    self.assertNotIn(token, result)
                    self.assertNotIn("SyntheticOnly", result)
                    self.assertIn("[REDACTED]", result)
                    self.assertIn("HTTP 403", result)
                    self.assertIn("req-shape", result)

    def test_redaction_before_truncation_at_every_boundary(self):
        cases = (
            ("Bearer ", "Zynthetic-authentication-value"),
            ('{"accessToken": "', "Zynthetic-authentication-value"),
            ("data:image/png;base64,", "U1lOVEhFVElDU0VDUkVU"),
            ("sk-proj-", "SyntheticOnly123456"),
            ("eyJ", "hbGciOiJIUzI1NiJ9.e30.c2ln"),
        )
        for prefix, secret in cases:
            for limit in range(len(prefix) + 1, len(prefix + secret) + 1):
                with self.subTest(prefix=prefix, limit=limit):
                    result = sanitize_log_text(prefix + secret + " trailing context", limit)
                    self.assert_bounded(result, limit)
                    # First secret character is absent from labels/markers too.
                    self.assertNotIn(secret[0], result)

    def test_only_bounded_text_prefix_is_inspected(self):
        class GuardedText(str):
            def __getitem__(self, index):
                if not isinstance(index, slice) or index.stop > 64:
                    raise AssertionError("unbounded string inspection")
                return super().__getitem__(index)

        result = sanitize_log_text(GuardedText("a" * 2_000_000), 64)
        self.assert_bounded(result, 64)
        self.assertTrue(result.endswith("...[truncated]"))

    def test_lone_surrogates_and_idempotent_normal_redaction(self):
        for render in (format_log_body, sanitize_log_text):
            result = render("malformed \ud800 text", 64)
            self.assert_bounded(result, 64)
        text = "Authorization: Bearer synthetic-secret\nHTTP 401 request_id=req-stable"
        sanitized = sanitize_log_text(text)
        self.assertEqual(sanitize_log_text(sanitized), sanitized)
        self.assertEqual(format_log_body({"a": 1}), format_log_body({"a": 1}))


if __name__ == "__main__":
    unittest.main()
