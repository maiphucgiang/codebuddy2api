#!/usr/bin/env python3
"""Test that upstream error codes and messages reach the audit trail, sanitized."""

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import converter
from app import upstream_io
from app.audit_store import AuditStore

ROUTE = "/v1/chat/completions"


class UpstreamDiagnosticTests(unittest.TestCase):
    """Upstream error bodies are the only way to tell a WAF block from a business rejection."""

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.audit = self.enterContext(contextlib.closing(
            AuditStore(Path(self.directory) / "logs.sqlite3")))
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "model_guard": False,
            "max_images": 16, "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
            "log_body_limit": 65536, "log_path": None, "desensitize": False, "no_compact": False,
            "audit_store": self.audit}))
        self.enterContext(patch.object(converter, "_cred_for", return_value=(None, {})))
        self.enterContext(patch.object(converter, "_log"))
        self.requests = []
        self.respond = lambda request: httpx.Response(200, content=b'data: [DONE]\n\n')
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.handle)
        self.enterContext(patch.object(upstream_io.httpx, "AsyncClient",
                                       side_effect=lambda **kw: real_client(transport=transport, **kw)))
        self.client = self.enterContext(TestClient(self._build_app()))

    def handle(self, request):
        self.requests.append(request)
        return self.respond(request)

    def _build_app(self):
        """Rebuild the inference app with the audit middleware the raw app does not carry."""
        from app.observability import AuditMiddleware
        from app.request_context import RequestContextMiddleware
        app = FastAPI()
        for route in converter.app.routes:
            app.routes.append(route)
        app.add_middleware(AuditMiddleware, config=converter.CONFIG)
        app.add_middleware(RequestContextMiddleware, config=converter.CONFIG)
        return app

    def _attempts(self):
        for record in self.audit.list_records("request", limit=20)["items"]:
            for attempt in record.get("attempts") or []:
                yield attempt

    def test_business_error_code_and_message_reach_the_audit_trail(self):
        self.respond = lambda request: httpx.Response(
            403, json={"code": "11-128", "msg": "conversation id missing"})
        response = self.client.post(ROUTE, json={
            "model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 403)
        entry = next(a for a in self._attempts() if a.get("stage") == "upstream_error")
        self.assertEqual(entry["status_code"], 403)
        self.assertEqual(entry["upstream_code"], "11-128")
        self.assertEqual(entry["upstream_msg"], "conversation id missing")

    def test_nested_envelope_is_unwrapped(self):
        self.respond = lambda request: httpx.Response(
            403, json={"error": {"code": "11140", "message": "request illegal"}})
        self.client.post(ROUTE, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        entry = next(a for a in self._attempts() if a.get("stage") == "upstream_error")
        self.assertEqual(entry["upstream_code"], "11140")
        self.assertEqual(entry["upstream_msg"], "request illegal")

    def test_nested_terminal_code_masks_generic_outer_code(self):
        """A generic outer code must not hide a terminal nested restriction code."""
        self.respond = lambda request: httpx.Response(
            403, json={"code": "error", "msg": "forbidden",
                       "error": {"code": "11140", "message": "request illegal"}})
        self.client.post(ROUTE, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        entry = next(a for a in self._attempts() if a.get("stage") == "upstream_error")
        self.assertEqual(entry["upstream_code"], "11140")
        self.assertEqual(entry["upstream_msg"], "request illegal")

    def test_non_json_body_records_no_diagnostic_but_keeps_the_status(self):
        self.respond = lambda request: httpx.Response(403, text="<html>blocked</html>")
        self.client.post(ROUTE, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        entry = next(a for a in self._attempts() if a.get("stage") == "upstream_error")
        self.assertEqual(entry["status_code"], 403)
        self.assertNotIn("upstream_code", entry)
        self.assertNotIn("upstream_msg", entry)
        self.assertNotIn("upstream_body", entry)

    def test_restriction_body_is_recorded_in_full(self):
        """The 11140 restriction envelope is the evidence that decides account handling."""
        self.respond = lambda request: httpx.Response(403, json={
            "code": 11140, "msg": "request illegal", "requestId": "01a162757c61710a82d9c17d3e9f060e",
            "displayMsg": {"en": "Your account has been restricted due to unusual activity",
                           "zh": "账号存在异常行为，已被限制使用"},
            "displayTips": {"zh": "系统检测到该账号存在异常使用行为，如需申诉请提交反馈"},
            "actions": ["EDIT_INPUT", "SUBMIT_FEEDBACK"]})
        self.client.post(ROUTE, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        entry = next(a for a in self._attempts() if a.get("stage") == "upstream_error")
        self.assertEqual(entry["upstream_code"], "11140")
        for fragment in ("已被限制使用", "SUBMIT_FEEDBACK", "01a162757c61710a82d9c17d3e9f060e"):
            self.assertIn(fragment, entry["upstream_body"])

    def test_body_is_capped_at_256k(self):
        payload = {f"key{index}": "x" * 4096 for index in range(80)}
        self.respond = lambda request: httpx.Response(403, json={"code": "1", "msg": "wide", **payload})
        self.client.post(ROUTE, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        entry = next(a for a in self._attempts() if a.get("stage") == "upstream_error")
        self.assertLessEqual(len(entry["upstream_body"]), 256 * 1024 + 64)
        self.assertIn("truncated", entry["upstream_body"])

    def test_body_tokens_are_redacted_before_storage(self):
        self.respond = lambda request: httpx.Response(403, json={
            "code": "11-128", "msg": "denied", "accessToken": "synthetic-private-token"})
        self.client.post(ROUTE, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        entry = next(a for a in self._attempts() if a.get("stage") == "upstream_error")
        self.assertNotIn("synthetic-private-token", entry["upstream_body"])
        raw = b"".join(f.read_bytes() for f in Path(self.directory).glob("logs.sqlite3*"))
        # Storage itself must never persist the secret, not only the record read path.
        self.assertNotIn(b"synthetic-private-token", raw)

    def test_secrets_in_upstream_messages_are_dropped(self):
        self.respond = lambda request: httpx.Response(
            403, json={"code": "11-128", "msg": "denied for cb-" + "a" * 32})
        self.client.post(ROUTE, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        entry = next(a for a in self._attempts() if a.get("stage") == "upstream_error")
        self.assertEqual(entry["upstream_code"], "11-128")
        self.assertNotIn("upstream_msg", entry)
        raw = b"".join(f.read_bytes() for f in Path(self.directory).glob("logs.sqlite3*"))
        self.assertNotIn(b"cb-" + b"a" * 32, raw)




if __name__ == "__main__":
    unittest.main(verbosity=2)
