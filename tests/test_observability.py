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
from app.observability import (AuditMiddleware, _Observation, _Parser, client_meta, normalize_usage,
                               observe_attempt, observe_failure, observe_failure_seq, observe_nested,
                               observe_recovery, observe_responses_projection, observe_route,
                               observe_usage)

class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.temp.name) / "logs.sqlite3")

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def invoke(self, app, path="/v1/chat/completions", method="POST", receiver=None, sender=None,
                     attrs=None, trusted_proxies=None):
        sent = []
        async def receive():
            self.fail("request body must not be inspected by middleware")
        async def send(message):
            sent.append(message)
        config = {"audit_store": self.store}
        if trusted_proxies is not None:
            config["trusted_proxies"] = trusted_proxies
        middleware = AuditMiddleware(app, config)
        scope = {"type": "http", "method": method, "path": path, **(attrs or {})}
        await middleware(scope, receiver or receive, sender or send)
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
            observe_responses_projection({"mode": "balanced", "max_item_bytes": 40000, "truncated_items": 2})
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
        self.assertEqual(record["responses_projection_mode"], "balanced")
        self.assertEqual(record["responses_projection_max_bytes"], 40000)
        self.assertEqual(record["responses_truncated_items"], 2)
        self.assertNotIn("private", json.dumps(record))
        self.assertNotIn("sk-synthetic", json.dumps(record))

    async def test_embeddings_protocol_and_usage(self):
        body = json.dumps({"object": "list", "model": "baai/bge-m3", "data": [],
                           "usage": {"prompt_tokens": 9, "total_tokens": 9, "credit": 0.01}}).encode()
        messages = [{"type": "http.response.start", "status": 200,
                     "headers": [(b"content-type", b"application/json")]},
                    {"type": "http.response.body", "body": body}]

        async def app(scope, receive, send):
            for message in messages:
                await send(message)

        await self.invoke(app, path="/v1/embeddings")
        record = self.only_record()
        self.assertEqual(record["protocol"], "embeddings")
        self.assertEqual(record["public_model"], "baai/bge-m3")
        self.assertEqual(record["input_tokens"], 9)
        self.assertEqual(record["total_tokens"], 9)
        self.assertEqual(record["credit"], 0.01)

    async def test_rerank_protocol(self):
        body = json.dumps({"model": "reranker", "results": [], "usage": {"total_tokens": 0}}).encode()
        messages = [{"type": "http.response.start", "status": 200,
                     "headers": [(b"content-type", b"application/json")]},
                    {"type": "http.response.body", "body": body}]

        async def app(scope, receive, send):
            for message in messages:
                await send(message)

        await self.invoke(app, path="/v1/rerank")
        record = self.only_record()
        self.assertEqual(record["protocol"], "rerank")
        self.assertEqual(record["public_model"], "reranker")
        self.assertEqual(record["total_tokens"], 0)

    async def test_non_responses_audit_omits_projection_fields(self):
        body = json.dumps({"model": "public-model", "choices": [{"message": {"content": "ok"}}]}).encode()
        messages = [{"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]},
                   {"type": "http.response.body", "body": body}]
        async def app(scope, receive, send):
            for message in messages:
                await send(message)
        await self.invoke(app, path="/v1/chat/completions")
        record = self.only_record()
        for key in ("responses_projection_mode", "responses_projection_max_bytes",
                    "responses_truncated_items"):
            self.assertNotIn(key, record)

    async def stream(self, chunks, path="/v1/chat/completions", complete=True):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            for chunk in chunks:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if complete:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        return await self.invoke(app, path=path)

    async def test_trailing_disconnect_preserves_completed_stream_outcome(self):
        cases = (
            ("/v1/chat/completions", b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n', "success"),
            ("/v1/responses", b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n', "success"),
            ("/v1/messages", b'data: {"type":"message_stop"}\n\n', "success"),
            ("/v1/chat/completions", b'event: error\ndata: {"error":{"code":"upstream_timeout"}}\n\ndata: [DONE]\n\n', "error"),
        )
        for path, body, outcome in cases:
            with self.subTest(path=path, outcome=outcome):
                self.store.clear("all")
                received = []
                async def receive():
                    message = {"type": "http.disconnect"}
                    received.append(message)
                    return message
                async def app(scope, receive, send):
                    await send({"type": "http.response.start", "status": 200,
                                "headers": [(b"content-type", b"text/event-stream")]})
                    await send({"type": "http.response.body", "body": body, "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    await receive()
                sent = await self.invoke(app, path=path, receiver=receive)
                self.assertEqual(received, [{"type": "http.disconnect"}])
                self.assertEqual(sent[1]["body"], body)
                self.assertFalse(sent[-1]["more_body"])
                self.assertEqual(self.only_record()["outcome"], outcome)

    async def test_disconnect_during_final_send_is_still_cancelled(self):
        final_started, disconnected = asyncio.Event(), asyncio.Event()
        async def receive():
            await final_started.wait()
            disconnected.set()
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                final_started.set()
                await disconnected.wait()
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": True})
            listener = asyncio.create_task(receive())
            try:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                await listener
            finally:
                if not listener.done():
                    listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
        await asyncio.wait_for(self.invoke(app, receiver=receive, sender=send), timeout=2)
        self.assertEqual(self.only_record()["outcome"], "cancelled")

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

    async def test_client_meta_recorded_and_proxy_unwrapped(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        attrs = {"client": ("127.0.0.1", 4000),
                 "headers": [(b"x-forwarded-for", b"198.51.100.9"), (b"user-agent", b"ua-test/2.0")]}
        await self.invoke(app, attrs=attrs, trusted_proxies="127.0.0.1")
        record = self.only_record()
        self.assertEqual(record["client_ip"], "198.51.100.9")
        self.assertEqual(record["user_agent"], "ua-test/2.0")
        self.store.clear("all")
        # Without a trusted proxy the forwarded header must not be trusted.
        await self.invoke(app, attrs=attrs)
        record = self.only_record()
        self.assertEqual(record["client_ip"], "127.0.0.1")
        self.assertEqual(record["user_agent"], "ua-test/2.0")

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

    async def invoke_ok(self, work):
        """Serve one 200 response after running ``work`` inside the observed request."""
        async def app(scope, receive, send):
            await work()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        await self.invoke(app)
        return self.only_record()

    def test_nested_observation_is_a_noop_without_an_enclosing_one(self):
        with observe_nested("web-search"):
            observe_route("web-search", None, "profile-b", "fp-b")
            observe_attempt("upstream_attempt")
            observe_failure("upstream_500")

    async def test_nested_observation_leaves_request_fields_and_adds_one_attempt(self):
        async def work():
            observe_route("public-model", "upstream-model", "profile-a", "fp-a")
            observe_attempt("upstream_attempt", profile="profile-a")
            with observe_nested("web-search"):
                observe_route("web-search", None, "profile-b", "fp-b")
                observe_attempt("upstream_attempt", upstream_request_id="rid-1")
                observe_attempt("upstream_http", status_code=200)
                observe_usage({"input_tokens": 99})
            observe_usage({"input_tokens": 5})
        record = await self.invoke_ok(work)
        self.assertEqual((record["public_model"], record["profile"], record["credential"]),
                         ("public-model", "profile-a", "fp-a"))
        self.assertEqual(record["outcome"], "success")
        self.assertIsNone(record["error_code"])
        self.assertEqual(record["input_tokens"], 5)
        self.assertEqual([a["stage"] for a in record["attempts"]], ["upstream_attempt", "server_tool"])
        entry = record["attempts"][1]
        self.assertEqual(set(entry), {"stage", "model", "outcome", "profile", "credential", "attempt",
                                      "duration_ms", "status_code", "upstream_request_id"})
        self.assertEqual({key: value for key, value in entry.items() if key != "duration_ms"},
                         {"stage": "server_tool", "model": "web-search", "outcome": "success",
                          "profile": "profile-b", "credential": "fp-b", "attempt": 1,
                          "status_code": 200, "upstream_request_id": "rid-1"})
        self.assertGreaterEqual(entry["duration_ms"], 0)

    async def test_nested_failure_stays_off_the_request_outcome(self):
        async def work():
            observe_route("public-model", None, "profile-a", "fp-a")
            with observe_nested("web-fetch"):
                observe_attempt("upstream_attempt", upstream_request_id="rid-1")
                observe_attempt("upstream_error", status_code=500)
                observe_failure("upstream_500")
            with self.assertRaises(ValueError):
                with observe_nested("web-search"):
                    raise ValueError("boom")
        record = await self.invoke_ok(work)
        self.assertEqual(record["outcome"], "success")
        self.assertIsNone(record["error_code"])
        failed, raised = record["attempts"]
        self.assertEqual((failed["model"], failed["outcome"], failed["error_code"], failed["status_code"]),
                         ("web-fetch", "error", "upstream_500", 500))
        self.assertEqual(failed["upstream_request_id"], "rid-1")
        # An exception without a recorded failure falls back to the exception class name.
        self.assertEqual((raised["model"], raised["outcome"], raised["error_code"], raised["attempt"]),
                         ("web-search", "error", "ValueError", 0))
        self.assertNotIn("status_code", raised)

    async def test_nested_recovered_failover_counts_as_success(self):
        async def work():
            with observe_nested("web-search"):
                observe_attempt("upstream_attempt", upstream_request_id="rid-1")
                observe_attempt("upstream_error", status_code=500)
                seq = observe_failure("upstream_500")
                self.assertEqual(observe_failure_seq(), seq)
                observe_attempt("upstream_attempt", upstream_request_id="rid-2")
                observe_attempt("upstream_http", status_code=200)
                observe_recovery(seq)
        record = await self.invoke_ok(work)
        entry = record["attempts"][0]
        self.assertEqual((entry["outcome"], entry["attempt"], entry["status_code"],
                          entry["upstream_request_id"]), ("success", 2, 200, "rid-2"))
        self.assertNotIn("error_code", entry)

    async def test_nested_entry_shares_the_attempt_cap(self):
        async def work():
            for _ in range(32):
                observe_attempt("upstream_attempt")
            for _ in range(2):
                with observe_nested("web-search"):
                    pass
        record = await self.invoke_ok(work)
        self.assertEqual(len(record["attempts"]), 32)
        self.assertEqual(record["attempts"][-1], {"stage": "attempts_truncated", "dropped": 3})

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

    def test_usage_zero_alias_does_not_shadow_real_cache_counters(self):
        # CodeBuddy ships a permanent top-level cached_tokens: 0 while the real
        # hit count lives in prompt_tokens_details / prompt_cache_hit_tokens.
        result = normalize_usage({"prompt_tokens": 552, "cached_tokens": 0,
                                  "cache_read_input_tokens": 0,
                                  "prompt_cache_hit_tokens": 448,
                                  "prompt_cache_write_tokens": 104,
                                  "completion_thinking_tokens": 5,
                                  "prompt_tokens_details": {"cached_tokens": 448},
                                  "completion_tokens_details": {"reasoning_tokens": 5}})
        self.assertEqual(result["cache_read_tokens"], 448)
        self.assertEqual(result["cache_creation_tokens"], 104)
        self.assertEqual(result["reasoning_tokens"], 5)

    def test_usage_anthropic_style_cache_fields_still_win(self):
        result = normalize_usage({"cache_read_input_tokens": 1200,
                                  "cache_creation_input_tokens": 300})
        self.assertEqual(result["cache_read_tokens"], 1200)
        self.assertEqual(result["cache_creation_tokens"], 300)


class ClientMetaTests(unittest.TestCase):
    def scope(self, client=("203.0.113.7", 443), headers=()):
        return {"type": "http", "client": client,
                "headers": [(key.encode(), value.encode()) for key, value in headers]}

    def test_untrusted_direct_peer_ignores_forwarded_headers(self):
        scope = self.scope(headers=[("X-Forwarded-For", "9.9.9.9"), ("User-Agent", "synthetic/1.0")])
        self.assertEqual(client_meta(scope, "127.0.0.1"), ("203.0.113.7", "synthetic/1.0"))
        self.assertEqual(client_meta(scope, ""), ("203.0.113.7", "synthetic/1.0"))

    def test_repeated_forwarded_fields_are_combined_in_order(self):
        # Client spoofs one XFF field; the trusted proxy appends the real
        # chain as a second field. The combined chain must prefer the
        # proxy-appended hop, not the client-supplied first field.
        scope = self.scope(
            client=("127.0.0.1", 8080),
            headers=[
                ("X-Forwarded-For", "6.6.6.6"),
                ("X-Forwarded-For", "198.51.100.9, 10.0.0.9"),
            ],
        )
        self.assertEqual(client_meta(scope, "127.0.0.1, 10.0.0.0/8")[0], "198.51.100.9")

    def test_malformed_fallback_values_are_rejected(self):
        real = self.scope(
            client=("127.0.0.1", 8080),
            headers=[("X-Forwarded-For", "198.51.100.9, not-an-ip"), ("X-Real-IP", "garbage")],
        )
        # The malformed hop stops the walk; the X-Real-IP fallback is not an
        # address either, so the trusted proxy address itself is recorded.
        self.assertEqual(client_meta(real, "127.0.0.1")[0], "127.0.0.1")
        # Even with a trusted proxy ahead of the garbage, a broken chain never
        # promotes the client-controlled leftmost entry.
        first = self.scope(
            client=("127.0.0.1", 8080),
            headers=[("X-Forwarded-For", "198.51.100.9, not-an-ip")],
        )
        self.assertEqual(client_meta(first, "127.0.0.1, 10.0.0.0/8")[0], "127.0.0.1")

    def test_trusted_proxy_unwraps_rightmost_untrusted_hop(self):
        scope = self.scope(client=("127.0.0.1", 8080),
                           headers=[("X-Forwarded-For", "198.51.100.9, 10.0.0.9")])
        self.assertEqual(client_meta(scope, "127.0.0.1,10.0.0.0/8")[0], "198.51.100.9")

    def test_forged_entries_beyond_the_trusted_chain_are_ignored(self):
        scope = self.scope(client=("127.0.0.1", 8080),
                           headers=[("X-Forwarded-For", "6.6.6.6, 198.51.100.9, 10.0.0.9")])
        self.assertEqual(client_meta(scope, "127.0.0.1, 10.0.0.0/8")[0], "198.51.100.9")

    def test_all_trusted_chain_falls_back_to_leftmost_and_real_ip(self):
        chain = self.scope(client=("127.0.0.1", 1),
                           headers=[("X-Forwarded-For", "10.0.0.9, 10.0.1.9")])
        self.assertEqual(client_meta(chain, "127.0.0.1, 10.0.0.0/8")[0], "10.0.0.9")
        real = self.scope(client=("127.0.0.1", 1), headers=[("X-Real-IP", "198.51.100.9")])
        self.assertEqual(client_meta(real, "127.0.0.1")[0], "198.51.100.9")

    def test_user_agent_is_sanitized(self):
        dirty = self.scope(headers=[("User-Agent", "a\nb Bearer x  y")])
        self.assertIsNone(client_meta(dirty, "")[1])
        long = self.scope(headers=[("User-Agent", "x" * 500)])
        self.assertEqual(client_meta(long, "")[1], "x" * 200)

    def test_missing_client_scope_still_records_agent(self):
        scope = {"type": "http", "headers": [(b"user-agent", b"ua/9")]}
        self.assertEqual(client_meta(scope, ""), (None, "ua/9"))


if __name__ == "__main__":
    unittest.main()
