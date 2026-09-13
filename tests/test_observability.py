"""Mock ASGI regression tests; only disposable SQLite databases are used."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.audit_store import AuditStore
from app.observability import (AuditMiddleware, _Observation, _Parser, normalize_usage,
                               observe_attempt, observe_failure, observe_route, observe_usage)


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.temp.name) / "logs.sqlite3")

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def invoke(self, app, path="/v1/chat/completions", method="POST", receiver=None, sender=None):
        sent = []
        async def receive():
            self.fail("request body must not be inspected by middleware")
        async def send(message):
            sent.append(message)
        middleware = AuditMiddleware(app, {"audit_store": self.store})
        await middleware({"type": "http", "method": method, "path": path}, receiver or receive, sender or send)
        return sent

    def only_record(self):
        records = self.store.list_records()["items"]
        self.assertEqual(len(records), 1)
        return records[0]

    async def test_nonstream_wire_unchanged_hooks_usage_and_safety(self):
        body = json.dumps({"model": "public-model", "choices": [{"message": {"content": "private response"}}],
                           "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
                                     "prompt_tokens_details": {"cached_tokens": 8},
                                     "completion_tokens_details": {"reasoning_tokens": 2}, "credit": 0}}).encode()
        messages = [{"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]},
                    {"type": "http.response.body", "body": body}]
        async def app(scope, receive, send):
            observe_route("public-model", "upstream-model", "profile-a", "fingerprint")
            observe_attempt("send", status_code=200, body="private request", token="sk-synthetic")
            observe_usage({"input_tokens": 12, "credit": 0})
            for message in messages:
                await send(message)
        sent = await self.invoke(app)
        self.assertEqual(sent, messages)
        self.assertTrue(all(a is b for a, b in zip(sent, messages)))
        record = self.only_record()
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["input_tokens"], 12)
        self.assertEqual(record["total_tokens"], 16)
        self.assertEqual(record["credit"], 0)
        self.assertEqual(record["cache_read_tokens"], 8)
        self.assertEqual(record["reasoning_tokens"], 2)
        self.assertEqual(record["usage_source"], "mixed")
        self.assertEqual(record["usage_sources"]["input_tokens"], "upstream_hook")
        self.assertIsNone(record["first_token_ms"])
        self.assertNotIn("private", json.dumps(record))
        self.assertNotIn("sk-synthetic", json.dumps(record))

    async def stream(self, chunks, path="/v1/chat/completions", complete=True):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            for chunk in chunks:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if complete:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        return await self.invoke(app, path=path)

    async def test_first_effective_output_not_role_or_usage(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n', "more_body": True})
            await asyncio.sleep(0.02)
            await send({"type": "http.response.body", "body": b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n', "more_body": True})
            await send({"type": "http.response.body", "body": b'data: {"usage":{"completion_tokens":0,"credit":0}}\n\ndata: [DONE]\n\n'})
        await self.invoke(app)
        record = self.only_record()
        self.assertEqual(record["outcome"], "success")
        self.assertGreaterEqual(record["first_token_ms"], 20)
        self.assertEqual(record["output_tokens"], 0)
        self.assertIsNone(record["input_tokens"])
        self.assertEqual(record["credit"], 0)

    async def test_http200_sse_error_is_error(self):
        await self.stream([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                           b'event: error\ndata: {"error":{"code":"upstream_timeout","message":"sk-secret"}}\n\n',
                           b'data: [DONE]\n\n'])
        record = self.only_record()
        self.assertEqual(record["status_code"], 200)
        self.assertEqual(record["outcome"], "error")
        self.assertEqual(record["error_code"], "upstream_timeout")
        self.assertNotIn("sk-secret", json.dumps(record))

    async def test_http200_sse_without_terminal_is_error(self):
        await self.stream([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'])
        self.assertEqual(self.only_record()["outcome"], "error")
        self.assertEqual(self.only_record()["error_code"], "incomplete_stream")

    async def test_http200_missing_final_asgi_body_is_error(self):
        await self.stream([b'data: [DONE]\n\n'], complete=False)
        self.assertEqual(self.only_record()["outcome"], "error")
        self.assertEqual(self.only_record()["error_code"], "incomplete_response")

    async def test_cancelled_request_records_once_and_propagates(self):
        async def app(scope, receive, send):
            observe_route("cancelled-model", None, None, None)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke(app)
        record = self.only_record()
        self.assertEqual(record["outcome"], "cancelled")
        self.assertEqual(self.store.dashboard()["summary"]["cancelled"], 1)

    async def test_disconnect_and_failed_send_are_cancelled(self):
        async def receive():
            return {"type": "http.disconnect"}
        async def app(scope, receive, send):
            await receive()
        await self.invoke(app, receiver=receive)
        self.assertEqual(self.only_record()["outcome"], "cancelled")
        self.store.clear("all")
        async def disconnected_send(message):
            raise BrokenPipeError()
        async def sending_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
        with self.assertRaises(BrokenPipeError):
            await self.invoke(sending_app, sender=disconnected_send)
        self.assertEqual(self.only_record()["outcome"], "cancelled")

    async def test_application_exception_is_not_swallowed_or_replayed(self):
        calls = 0
        async def app(scope, receive, send):
            nonlocal calls
            calls += 1
            raise RuntimeError("sensitive-body")
        with self.assertRaises(RuntimeError):
            await self.invoke(app)
        self.assertEqual(calls, 1)
        record = self.only_record()
        self.assertEqual(record["outcome"], "error")
        self.assertNotIn("sensitive-body", json.dumps(record))

    async def test_responses_and_anthropic_usage(self):
        for path, chunks, expected in (
            ("/v1/responses", [b'data: {"type":"response.completed","response":{"usage":{"input_tokens":0,"output_tokens":2,"total_tokens":2,"credit":0}}}\n\n'], 0),
            ("/v1/messages", [b'data: {"type":"message_start","message":{"usage":{"input_tokens":3,"cache_read_input_tokens":8}}}\n\n',
                               b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n',
                               b'data: {"type":"message_stop"}\n\n'], 3),
        ):
            with self.subTest(path=path):
                self.store.clear("all")
                await self.stream(chunks, path)
                record = self.only_record()
                self.assertEqual(record["outcome"], "success")
                self.assertEqual(record["input_tokens"], expected)
                self.assertEqual(record["output_tokens"], 2)
                self.assertIsNone(record["first_token_ms"])
                if path.endswith("messages"):
                    self.assertEqual(record["cache_read_tokens"], 8)
                    self.assertIsNone(record["total_tokens"])

    async def test_failed_responses_event_even_with_200(self):
        await self.stream([b'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"rate_limited"}}}\n\n',
                           b'data: [DONE]\n\n'], "/v1/responses")
        self.assertEqual(self.only_record()["outcome"], "error")

    async def test_stream_chunk_boundaries_and_large_payload_are_bounded(self):
        frame = b'data: {"choices":[{"delta":{"content":"ok"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
        await self.stream([frame[i:i + 3] for i in range(0, len(frame), 3)])
        self.assertEqual(self.only_record()["outcome"], "success")
        self.store.clear("all")
        await self.stream([b'data: {"body":"' + b"x" * 200000 + b'"}\n\n', b'data: [DONE]\n\n'])
        record = self.only_record()
        self.assertEqual(record["outcome"], "success")
        self.assertIn("bounded_parse_skipped", json.dumps(record))
        self.assertLess(len(json.dumps(record)), 3000)
        observation = _Observation({}, 0, streaming=True)
        parser = _Parser(observation)
        parser.feed(b"data: " + b"x" * 1000000)
        self.assertLessEqual(len(parser.buffer), 16384)
        self.assertLessEqual(len(parser.event_data), 16384)

    async def test_non_inference_routes_and_get_are_untouched(self):
        async def app(scope, receive, send):
            observe_failure("must_not_record")
            await send({"type": "http.response.body", "body": b"private management response"})
        for path in ("/admin/session", "/admin/credentials/upload", "/admin/credentials/export", "/v1/models"):
            await self.invoke(app, path=path)
        await self.invoke(app, method="GET")
        self.assertEqual(self.store.list_records()["items"], [])

    async def test_context_isolation_and_hook_precedence(self):
        barrier = asyncio.Event()
        arrived = 0
        async def app(scope, receive, send):
            nonlocal arrived
            model = "model-a" if scope["path"].endswith("responses") else "model-b"
            observe_route(model, model, None, None)
            observe_usage({"input_tokens": 9, "credit": 0})
            arrived += 1
            if arrived == 2:
                barrier.set()
            await barrier.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"usage":{"input_tokens":0,"credit":5}}'})
        await asyncio.gather(self.invoke(app), self.invoke(app, path="/v1/responses"))
        records = self.store.list_records()["items"]
        self.assertEqual({r["public_model"] for r in records}, {"model-a", "model-b"})
        self.assertTrue(all(r["input_tokens"] == 9 and r["credit"] == 0 for r in records))
        observe_failure("outside_context")
        self.assertTrue(all(r["outcome"] == "success" for r in records))

    async def test_clear_during_inflight_respects_generation_and_epoch(self):
        for scope in ("details", "all"):
            with self.subTest(scope=scope):
                self.store.clear("all")
                async def app(asgi_scope, receive, send):
                    await asyncio.to_thread(self.store.clear, scope)
                    await send({"type": "http.response.start", "status": 200, "headers": []})
                    await send({"type": "http.response.body", "body": b"{}"})
                await self.invoke(app)
                self.assertEqual(self.store.list_records()["items"], [])
                self.assertEqual(self.store.dashboard()["summary"]["requests"], 1 if scope == "details" else 0)

    async def test_storage_failure_does_not_change_response_or_replay(self):
        self.store.close()
        calls = 0
        async def app(scope, receive, send):
            nonlocal calls
            calls += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        sent = await self.invoke(app)
        self.assertEqual(calls, 1)
        self.assertEqual(sent[-1]["body"], b"{}")
        self.assertTrue(self.store.storage()["degraded"])
        self.assertGreaterEqual(self.store.storage()["dropped_records"], 1)

    async def test_cancellation_during_ticket_is_still_recorded(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.store.ticket
        def delayed_ticket():
            entered.set()
            release.wait(timeout=2)
            return original()
        async def app(scope, receive, send):
            self.fail("cancelled request must not invoke upstream app")
        with patch.object(self.store, "ticket", side_effect=delayed_ticket):
            task = asyncio.create_task(self.invoke(app))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.only_record()["outcome"], "cancelled")

    async def test_failure_hook_preserves_specific_code_on_truncated_stream(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            observe_failure("upstream_timeout")
            await send({"type": "http.response.body", "body": b""})
        await self.invoke(app)
        self.assertEqual(self.only_record()["error_code"], "upstream_timeout")

    async def test_cancelled_submission_is_drained_before_lifespan_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store.record_request
        def delayed_record(record):
            entered.set()
            release.wait(timeout=2)
            return original(record)
        shutdown_records = []
        async def app(scope, receive, send):
            if scope["type"] == "lifespan":
                await receive()
                shutdown_records.extend(self.store.list_records()["items"])
                await send({"type": "lifespan.shutdown.complete"})
                return
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        async def receive():
            return {"type": "lifespan.shutdown"}
        async def send(message):
            pass
        middleware = AuditMiddleware(app, self.store)
        with patch.object(self.store, "record_request", side_effect=delayed_record):
            task = asyncio.create_task(middleware({"type": "http", "method": "POST", "path": "/v1/responses"}, receive, send))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            await middleware({"type": "lifespan"}, receive, send)
        self.assertEqual(len(shutdown_records), 1)
        self.assertEqual(len(middleware._pending), 0)

    def test_tool_identifiers_alone_do_not_fabricate_first_output(self):
        observation = _Observation({}, 0, streaming=True)
        observation.payload({"choices": [{"delta": {"tool_calls": [{"id": "call-id", "index": 0}]}}]}, "wire_sse")
        self.assertIsNone(observation.first_token_ms)
        observation.payload({"choices": [{"delta": {"tool_calls": [{"function": {"name": "tool"}}]}}]}, "wire_sse")
        self.assertIsNotNone(observation.first_token_ms)

    def test_usage_invalid_values_are_unknown(self):
        result = normalize_usage({"input_tokens": False, "output_tokens": -1, "total_tokens": float("nan"),
                                  "credit": 0, "cache_read_input_tokens": 0, "reasoning_tokens": "3"})
        self.assertEqual(result, {"credit": 0, "cache_read_tokens": 0})


if __name__ == "__main__":
    unittest.main()
