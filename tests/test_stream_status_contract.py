#!/usr/bin/env python3
"""Preserve upstream HTTP errors before streaming starts and report midstream failures in-band."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import asyncio
import json
import time
import unittest
from unittest.mock import patch

import anyio
import httpx
from fastapi.testclient import TestClient

import converter
from app import upstream_io
from app.inbound_limits import ConcurrencyLimitMiddleware

ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
TOOLS = [{"type": "function", "function": {"name": "synthetic_tool", "parameters": {"type": "object"}}}]
# Retry pre-send connection failures once; ambiguous post-send failures do not replay by default.
REPLAYABLE = (httpx.ConnectError, httpx.ConnectTimeout)
AMBIGUOUS = (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.WriteError,
             httpx.WriteTimeout)
HTTP_STATUSES = (400, 403, 429, 503)
TERMINALS = ("data: [DONE]", '"response.completed"', '"message_stop"')


def sse_body(text="ok"):
    chunks = [{"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}], "model": "auto"},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "model": "auto",
               "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}]
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


class StreamStatusTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "model_guard": False,
            "max_images": 16, "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
            "log_body_limit": 65536, "log_path": None, "desensitize": False, "no_compact": False}))
        self.enterContext(patch.object(converter, "_cred_for", return_value=(None, {})))
        self.enterContext(patch.object(converter, "_log"))
        self.enterContext(patch.object(converter, "_note_cred_status"))
        self.requests = []
        self.respond = lambda request: httpx.Response(200, content=sse_body())
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.handle)
        self.enterContext(patch.object(upstream_io.httpx, "AsyncClient",
                                       side_effect=lambda **kw: real_client(transport=transport, **kw)))
        self.client = self.enterContext(TestClient(converter.app))

    def handle(self, request):
        self.requests.append(request)
        return self.respond(request)

    def post(self, route, *, stream, tools=False, model="auto"):
        if route == "/v1/responses":
            payload = {"model": model, "stream": stream, "input": [{"role": "user", "content": "hi"}]}
            if tools:
                payload["tools"] = TOOLS
        elif route == "/v1/messages":
            payload = {"model": model, "max_tokens": 16,
                       "messages": [{"role": "user", "content": "hi"}]}
            if stream is False:
                payload["stream"] = False
            else:
                payload["stream"] = True
            if tools:
                payload["tools"] = [{"name": "synthetic_tool", "input_schema": {"type": "object"}}]
        else:
            payload = {"model": model, "stream": stream, "messages": [{"role": "user", "content": "hi"}]}
            if tools:
                payload["tools"] = TOOLS
        return self.client.post(route, json=payload)

    # Preserve upstream rejection status before streaming starts.
    def test_upstream_http_status_is_not_swallowed_by_streaming(self):
        for route in ROUTES:
            for status in HTTP_STATUSES:
                for tools in (False, True):
                    with self.subTest(route=route, status=status, tools=tools):
                        self.respond = lambda request: httpx.Response(
                            status, json={"error": {"message": "synthetic rejection", "code": "rate_limit"}})
                        self.requests.clear()
                        streamed = self.post(route, stream=True, tools=tools)
                        self.requests.clear()
                        plain = self.post(route, stream=False, tools=tools)
                        self.assertEqual(streamed.status_code, status, streamed.text)
                        self.assertEqual(plain.status_code, status, plain.text)
                        self.assertEqual(len(self.requests), 1, "失败不得重放上游")

    # Surface transport failures consistently across response modes.
    def test_transport_failure_is_502_on_both_stream_modes(self):
        for route in ROUTES:
            for error_type in REPLAYABLE + AMBIGUOUS:
                expected = 2 if error_type in REPLAYABLE else 1
                with self.subTest(route=route, error=error_type.__name__):
                    def raise_transport(request):
                        raise error_type("synthetic transport failure")
                    self.respond = raise_transport
                    for stream in (True, False):
                        with self.subTest(stream=stream):
                            self.requests.clear()
                            response = self.post(route, stream=stream)
                            self.assertEqual(response.status_code, 502, response.text)
                            self.assertIn(error_type.__name__, response.text, response.text)
                            self.assertEqual(len(self.requests), expected, response.text)

    # Pre-response failures must not produce a successful stream.
    def test_failed_stream_never_carries_a_success_terminal(self):
        self.respond = lambda request: httpx.Response(429, json={"error": {"message": "slow down"}})
        for route in ROUTES:
            for tools in (False, True):
                with self.subTest(route=route, tools=tools):
                    response = self.post(route, stream=True, tools=tools)
                    self.assertEqual(response.status_code, 429, response.text)
                    self.assertNotEqual(response.headers.get("content-type", "").split(";")[0],
                                        "text/event-stream")
                    for marker in TERMINALS:
                        self.assertNotIn(marker, response.text)

    # Midstream failures remain in-band because headers are already committed.
    def test_break_after_first_byte_stays_in_band(self):
        class Partial(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                raise httpx.ReadError("synthetic mid-stream reset")

        for route in ROUTES:
            if route == "/v1/responses":
                continue   # Responses aggregation fails before sending bytes and returns HTTP 502.
            with self.subTest(route=route):
                self.respond = lambda request: httpx.Response(200, stream=Partial(),
                                                              headers={"content-type": "text/event-stream"})
                response = self.post(route, stream=True)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("ReadError", response.text)
                self.assertIn("partial", response.text)
                self.assertNotIn("data: [DONE]", response.text)
                self.assertNotIn('"message_stop"', response.text)

    # Preflight must retain the first successful segment.
    def test_happy_stream_still_delivers_every_event_once(self):
        for route in ROUTES:
            for tools in (False, True):
                with self.subTest(route=route, tools=tools):
                    self.requests.clear()
                    response = self.post(route, stream=True, tools=tools)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
                    self.assertEqual(len(self.requests), 1)
                    if route == "/v1/responses":
                        self.assertEqual(response.text.count('"response.created"'), 1)
                        self.assertEqual(response.text.count('"response.completed"'), 1)
                    elif route == "/v1/messages":
                        self.assertEqual(response.text.count("event: message_start"), 1)
                        self.assertEqual(response.text.count("event: message_stop"), 1)
                    else:
                        self.assertIn("ok", response.text)
                        self.assertIn("data: [DONE]", response.text)


class PreflightDisconnectTests(unittest.IsolatedAsyncioTestCase):
    """Cancel preflight reads and release concurrency slots on downstream disconnect."""

    PAYLOAD = {"model": "auto", "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    def setUp(self):
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "model_guard": False,
            "max_images": 16, "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
            "log_body_limit": 65536, "log_path": None, "desensitize": False, "no_compact": False,
            "max_concurrent": 1, "max_collect_bytes": 0}))
        self.enterContext(patch.object(converter, "_cred_for", return_value=(None, {})))
        self.enterContext(patch.object(converter, "_log"))
        self.enterContext(patch.object(converter, "_note_cred_status"))
        self.enterContext(patch.object(converter, "_note_cred_model_ok"))
        # Match production middleware order with a private concurrency gate for each test.
        self.app = ConcurrencyLimitMiddleware(converter.app.build_middleware_stack(),
                                              converter.CONFIG)
        self.stuck = asyncio.Event()
        self.closed = 0
        self.mode = "before-first-segment"

    async def upstream(self, url, headers, body, model_name="?", t0=0.0, rid="", cred=None):
        """Block synthetic upstream reads before or after the first segment with async cleanup."""
        try:
            if self.mode != "before-first-segment":
                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "ok"}}],
                                             "model": model_name}) + "\n\n"
            self.stuck.set()      # Signal that the upstream is pending.
            if self.mode == "done":
                yield "data: [DONE]\n\n"
                return
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            self.closed += 1

    async def drive(self, *, disconnect):
        """Return response status and whether disconnect cleanup completed within the deadline."""
        sent, queue = [], asyncio.Queue()

        async def receive():
            return await queue.get()

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
                 "raw_path": b"/v1/chat/completions", "query_string": b"", "headers": [],
                 "scheme": "http", "http_version": "1.1", "server": ("test", 80),
                 "client": ("127.0.0.1", 1234), "asgi": {"version": "3.0", "spec_version": "2.3"}}
        await queue.put({"type": "http.request", "body": json.dumps(self.PAYLOAD).encode(),
                         "more_body": False})
        with patch.object(converter, "_stream_upstream", new=self.upstream):
            task = asyncio.create_task(self.app(scope, receive, send))
            try:
                await asyncio.wait_for(self.stuck.wait(), 2)
                if self.mode == "after-first-segment":   # Wait until headers are sent.
                    for _ in range(400):
                        if any(m["type"] == "http.response.start" for m in sent):
                            break
                        await asyncio.sleep(0.005)
                if disconnect:
                    await queue.put({"type": "http.disconnect"})
                await asyncio.wait_for(task, 2)          # Cleanup must release capacity promptly.
            finally:
                task.cancel()
        start = next((m for m in sent if m["type"] == "http.response.start"), None)
        return start["status"] if start else None, sent

    async def test_disconnect_before_first_segment_aborts_without_a_response(self):
        """Close upstream work without emitting bytes when preflight is cancelled."""
        status, sent = await self.drive(disconnect=True)
        self.assertIsNone(status, f"客户端已经走了， yet 发出了响应头：{sent}")
        self.assertEqual(self.closed, 1)

    async def test_slot_is_returned_so_the_next_request_still_runs(self):
        """Release admission capacity so a request after disconnect can succeed."""
        await self.drive(disconnect=True)
        self.stuck = asyncio.Event()
        self.mode = "done"
        status, _ = await self.drive(disconnect=False)
        self.assertEqual(status, 200)

    async def test_disconnect_after_streaming_started_closes_the_upstream(self):
        """Cancel pending reads and close generators after streaming has begun."""
        self.mode = "after-first-segment"
        status, sent = await self.drive(disconnect=True)
        self.assertEqual(status, 200, sent)
        self.assertEqual(self.closed, 1)


class TeardownCloseTests(unittest.IsolatedAsyncioTestCase):
    """Complete isolated generator cleanup despite repeated cancellation of the calling task."""

    async def test_cleanup_await_completes_inside_a_cancelled_scope(self):
        done = []

        async def upstream():
            try:
                yield "data: x\n\n"          # Pause between segments before closure.
            finally:
                await asyncio.sleep(0.01)
                done.append("closed")

        agen = upstream()
        await agen.__anext__()

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                await converter._close_stream(agen)
                done.append("cleanup-survived")

        async with anyio.create_task_group() as group:
            group.start_soon(worker)
            await asyncio.sleep(0)
            group.cancel_scope.cancel()
        self.assertEqual(done, ["closed", "cleanup-survived"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
