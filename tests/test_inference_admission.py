"""Admission regressions using the production middleware installation order."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request

from app import runtime_management
from app.inbound_limits import ConcurrencyLimitMiddleware


ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages")


class InferenceAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {"api_key": "synthetic-key", "max_concurrent": 1,
                       "max_inbound_bytes": 1024, "management": Mock()}
        app = FastAPI()

        async def endpoint(request: Request):
            return {"size": len(await request.body())}

        @app.get("/v1/models")
        async def models(request: Request):
            from app.inference_auth import require_api_key
            require_api_key(self.config["api_key"], request.headers.get("authorization"))
            return {"object": "list", "data": []}

        for path in (*ROUTES, "/v1/messages/count_tokens", "/admin/test"):
            app.add_api_route(path, endpoint, methods=["POST"])
        gateway = SimpleNamespace(app=app, CONFIG=self.config, __file__=str(Path(__file__).parent.parent / "converter.py"))
        with patch.object(runtime_management, "install_admin"), patch.object(runtime_management, "install_pages"):
            runtime_management.install(gateway)
        self.app = app.build_middleware_stack()
        layer = self.app
        while not isinstance(layer, ConcurrencyLimitMiddleware):
            layer = layer.app
        self.gate = layer

    async def request(self, path, headers=(), *, forbid_receive=False, method="POST"):
        sent = []
        replayed = False

        async def receive():
            nonlocal replayed
            if forbid_receive:
                self.fail("rejected headers must not read or wait for a request body")
            if replayed:
                await asyncio.Event().wait()
            replayed = True
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                 "query_string": b"", "headers": list(headers), "scheme": "http", "http_version": "1.1",
                 "server": ("test", 80), "client": ("127.0.0.1", 1234),
                 "asgi": {"version": "3.0", "spec_version": "2.3"}}
        await asyncio.wait_for(self.app(scope, receive, send), 2)
        start = next(message for message in sent if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in sent)
        content_type = dict(start.get("headers", [])).get(b"content-type", b"")
        payload = json.loads(body) if b"application/json" in content_type else body.decode()
        return start["status"], payload

    def assert_unauthorized(self, path, status, body):
        self.assertEqual(status, 401, body)
        self.assertEqual(body["error"]["message"], "invalid api key")
        if path.startswith("/v1/messages"):
            self.assertEqual(body["type"], "error")
            self.assertEqual(body["error"]["type"], "authentication_error")
        else:
            self.assertNotIn("type", body)
            self.assertEqual(body["error"]["type"], "auth_error")
        self.assertNotIn("synthetic-key", json.dumps(body))

    async def test_unauthorized_bodies_never_reserve_or_consume_inference_capacity(self):
        invalid = ((), ((b"authorization", b"Bearer wrong"),), ((b"x-api-key", b"wrong"),),
                   ((b"cookie", b"codebuddy_admin=synthetic-cookie"),))
        for path in (*ROUTES, "/v1/messages/count_tokens"):
            for headers in invalid:
                with self.subTest(path=path, headers=headers):
                    status, body = await self.request(path, headers, forbid_receive=True)
                    self.assert_unauthorized(path, status, body)
                    self.assertIsNone(self.gate._semaphore)
        status, body = await self.request(ROUTES[0], ((b"authorization", b"Bearer synthetic-key"),))
        self.assertEqual(status, 200, body)

    async def test_full_gate_still_authenticates_before_returning_capacity_errors(self):
        gate = self.gate._gate()
        await gate.acquire()
        try:
            for path in ROUTES:
                with self.subTest(path=path):
                    status, body = await self.request(path, forbid_receive=True)
                    self.assert_unauthorized(path, status, body)
                    status, body = await self.request(path, ((b"x-api-key", b"synthetic-key"),), forbid_receive=True)
                    self.assertEqual(status, 503, body)
                    self.assertEqual(body["error"]["code"], "concurrency_limit")
            status, body = await self.request("/v1/messages/count_tokens", ((b"x-api-key", b"synthetic-key"),))
            self.assertEqual(status, 200, body)
        finally:
            gate.release()

    async def test_current_key_and_header_precedence_are_preserved(self):
        valid = (((b"authorization", b"Bearer synthetic-key"),), ((b"x-api-key", b"synthetic-key"),),
                 ((b"authorization", b"Bearer "), (b"x-api-key", b"synthetic-key")))
        for path in ROUTES:
            for headers in valid:
                with self.subTest(path=path, headers=headers):
                    status, body = await self.request(path, headers)
                    self.assertEqual(status, 200, body)
            status, body = await self.request(path, ((b"authorization", b"Bearer wrong"),
                                                    (b"x-api-key", b"synthetic-key")), forbid_receive=True)
            self.assert_unauthorized(path, status, body)
        self.config["api_key"] = "replacement-key"
        status, body = await self.request(ROUTES[0], ((b"x-api-key", b"synthetic-key"),), forbid_receive=True)
        self.assert_unauthorized(ROUTES[0], status, body)
        status, body = await self.request(ROUTES[0], ((b"x-api-key", b"replacement-key"),))
        self.assertEqual(status, 200, body)

    async def test_auth_middleware_allows_conditional_inference_requests(self):
        for path in ROUTES:
            with self.subTest(path=path):
                status, body = await self.request(path, ((b"if-none-match", b'"etag"'),
                                                         (b"authorization", b"Bearer synthetic-key")))
                self.assertEqual(status, 200, body)

    async def test_unknown_routes_and_methods_preserve_routing_errors(self):
        for path, method, expected in (("/v1/missing", "GET", 404),
                                       ("/v1/missing", "POST", 404),
                                       ("/v1/messages/unknown", "POST", 404),
                                       ("/v1/messages", "GET", 405)):
            with self.subTest(path=path, method=method):
                status, body = await self.request(path, method=method, forbid_receive=True)
                self.assertEqual(status, expected, body)


    async def test_bodyless_models_reaches_route_auth_without_buffering(self):
        for headers, expected in (((), 401), (((b"authorization", b"Bearer synthetic-key"),), 200)):
            with self.subTest(status=expected):
                status, body = await self.request("/v1/models", headers, method="GET", forbid_receive=True)
                self.assertEqual(status, expected, body)
                self.assertIsNone(self.gate._semaphore)


    async def test_disabled_gate_still_authenticates_and_empty_key_remains_optional(self):
        self.config["max_concurrent"] = 0
        status, body = await self.request(ROUTES[0], forbid_receive=True)
        self.assert_unauthorized(ROUTES[0], status, body)
        self.config["api_key"] = ""
        status, body = await self.request(ROUTES[0])
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.gate._semaphore)
        self.config["api_key"] = "synthetic-key"
        status, body = await self.request("/admin/test")
        self.assertEqual(status, 200, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
