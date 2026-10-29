#!/usr/bin/env python3
"""Deterministic realtime streaming and terminal-validation tests."""
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import converter
from app.audit_store import AuditStore
from app.control_store import ControlStore
from app.observability import AuditMiddleware
from app.settings import apply_persisted_settings, resolve_settings
from app.startup import load_startup_env, resolve_startup_auth
from app.adapters.anthropic_adapter import AnthropicStreamConverter
from app.adapters.responses_adapter import ResponsesStreamConverter
from app.upstream_io import ChatSSEAccumulator, StreamOutputBudget, UpstreamResponseError
from app.inference_resources import AccountCapacity, request_resources


def _line(delta, finish=None, usage=None):
    chunk = {"id": "synthetic", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        chunk["usage"] = usage
    return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"


def _events(raw):
    result = []
    for block in raw.strip().split("\n\n"):
        if not block:
            continue
        data = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), None)
        if data is not None:
            result.append(json.loads(data))
    return result


def _serial_blocks(test, raw, *, complete=True):
    active = None
    blocks = []
    for event in _events(raw):
        kind = event["type"]
        if kind == "content_block_start":
            test.assertIsNone(active, event)
            active = event["index"]
            test.assertEqual(active, len(blocks))
            blocks.append({**event["content_block"], "_pieces": [], "_closed": False})
        elif kind == "content_block_delta":
            test.assertEqual(event["index"], active)
            delta = event["delta"]
            field = {"text_delta": "text", "thinking_delta": "thinking",
                     "input_json_delta": "partial_json"}[delta["type"]]
            blocks[active]["_pieces"].append(delta[field])
        elif kind == "content_block_stop":
            test.assertEqual(event["index"], active)
            block = blocks[active]
            value = "".join(block["_pieces"])
            block["input" if block["type"] == "tool_use" else block["type"]] = (
                json.loads(value) if block["type"] == "tool_use" else value)
            block["_closed"] = True
            active = None
        elif kind in ("message_delta", "message_stop"):
            test.assertIsNone(active)
    if complete:
        test.assertIsNone(active)
        test.assertTrue(all(block["_closed"] for block in blocks))
    return blocks


class _PausedLines:
    def __init__(self, first, tail, at_boundary):
        self.first = first
        self.tail = tail
        self.at_boundary = at_boundary
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield self.first
        # The next __anext__ means the gateway has consumed the first line.  In realtime
        # mode any corresponding client event has already passed through ASGI send.
        self.at_boundary.set()
        await self.release.wait()
        for line in self.tail:
            yield line


class _PausedResponse:
    def __init__(self, lines):
        self.status_code = 200
        self.headers = {"Content-Type": "text/event-stream"}
        self.lines = lines

    async def aiter_lines(self):
        async for line in self.lines:
            yield line


class _FixedResponse:
    def __init__(self, lines=(), *, status=200, headers=None, error=None, body=b""):
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/event-stream"}
        self.lines = lines
        self.error = error
        self.body = body

    async def aiter_lines(self):
        for line in self.lines:
            yield line
        if self.error is not None:
            raise self.error

    async def aiter_bytes(self):
        yield self.body


class RealtimeTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {
            "api_key": "", "cred": None, "cred_pool": None, "model_guard": False,
            "max_images": 16, "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
            "log_body_limit": 0, "log_path": None, "desensitize": False, "no_compact": False,
            "max_collect_bytes": 0, "max_concurrent": 0, "stream_mode": "compatible",
            "request_context_mode": "legacy", "model_capability_guard": False,
            "upstream_keepalive": False, "retry_write_timeout": False, "failover_max": 0,
        }
        self.enterContext(patch.dict(converter.CONFIG, self.config, clear=False))
        self.enterContext(patch.object(converter, "_route_chat",
                                       side_effect=lambda payload, body, rid: (body, None, {}, "https://synthetic.invalid")))
        self.enterContext(patch.object(converter, "_log"))
        self.enterContext(patch.object(converter, "_note_cred_model_ok"))

    async def asgi_post(self, path, payload, backend, *, route=None, config=None):
        """Drive the real ASGI app while retaining each send message for assertions."""
        sent = []
        request = asyncio.Queue()
        await request.put({"type": "http.request", "body": json.dumps(payload).encode(),
                           "more_body": False})

        async def receive():
            return await request.get()

        async def send(message):
            sent.append(message)

        if route is None:
            def route(payload_, body, rid):
                return body, None, {}, "https://synthetic.invalid"
        scope = {"type": "http", "method": "POST", "path": path,
                 "raw_path": path.encode(), "query_string": b"", "headers": [],
                 "scheme": "http", "http_version": "1.1", "server": ("test", 80),
                 "client": ("test", 1), "asgi": {"version": "3.0", "spec_version": "2.3"}}
        with patch.dict(converter.CONFIG, config or {}, clear=False), \
             patch.object(converter, "_route_chat", side_effect=route), \
             patch.object(converter, "_backend_stream", backend), \
             patch.object(converter, "_log"), patch.object(converter, "_note_cred_model_ok"):
            await asyncio.wait_for(converter.app(scope, receive, send), 2)
        return sent

    def payload(self, protocol, tools):
        if protocol == "chat":
            body = {"model": "auto", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]}
            if tools:
                body["tools"] = [{"type": "function", "function": {
                    "name": "synthetic_tool", "parameters": {"type": "object"}}}]
        elif protocol == "responses":
            body = {"model": "auto", "stream": True, "input": "hi"}
            if tools:
                body["tools"] = [{"type": "function", "name": "synthetic_tool",
                                  "parameters": {"type": "object"}}]
        else:
            body = {"model": "auto", "stream": True, "max_tokens": 32,
                    "messages": [{"role": "user", "content": "hi"}]}
            if tools:
                body["tools"] = [{"name": "synthetic_tool", "input_schema": {"type": "object"}}]
        return body

    async def drive(self, protocol, mode, tools, *, change_to=None, first_delta=None):
        if first_delta is None:
            first_delta = ({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                             "function": {"name": "synthetic_tool", "arguments": '{"x":'}}]}
                          if tools else {"content": "early"})
        finish = "tool_calls" if tools else "stop"
        tail = [_line({}, finish, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}),
                "data: [DONE]\n\n"]
        if tools:
            tail.insert(0, _line({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}))
        boundary = asyncio.Event()
        response = _PausedResponse(_PausedLines(_line(first_delta), tail, boundary))
        sent = []
        request = asyncio.Queue()
        await request.put({"type": "http.request", "body": json.dumps(self.payload(protocol, tools)).encode(),
                           "more_body": False})

        async def receive():
            return await request.get()

        async def send(message):
            sent.append(message)

        @asynccontextmanager
        async def backend(*args, **kwargs):
            yield response

        converter.CONFIG["stream_mode"] = mode
        path = {"chat": "/v1/chat/completions", "responses": "/v1/responses",
                "messages": "/v1/messages"}[protocol]
        scope = {"type": "http", "method": "POST", "path": path,
                 "raw_path": path.encode(), "query_string": b"", "headers": [],
                 "scheme": "http", "http_version": "1.1", "server": ("test", 80),
                 "client": ("test", 1), "asgi": {"version": "3.0", "spec_version": "2.3"}}
        with patch.object(converter, "_backend_stream", backend):
            task = asyncio.create_task(converter.app(scope, receive, send))
            try:
                await asyncio.wait_for(boundary.wait(), 2)
                before_release = b"".join(message.get("body", b"") for message in sent
                                         if message["type"] == "http.response.body")
                if change_to is not None:
                    converter.CONFIG["stream_mode"] = change_to
                response.lines.release.set()
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        wire = b"".join(message.get("body", b"") for message in sent
                         if message["type"] == "http.response.body")
        return before_release, wire

    @staticmethod
    def meaningful(protocol, wire):
        if protocol == "chat":
            return b"early" in wire or b'\\"x\\":' in wire
        if protocol == "responses":
            return any(marker in wire for marker in (
                b"response.output_text.delta", b"response.reasoning_summary_text.delta",
                b"response.function_call_arguments.delta"))
        return b"content_block_delta" in wire and (b"early" in wire or b"partial_json" in wire)

    async def collect(self, protocol, response, *, max_collect_bytes=0, tool_choice="required"):
        body = {"model": "auto", "stream": True, "parallel_tool_calls": True,
                "messages": [{"role": "assistant", "content": ""}, {"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {
                    "name": "synthetic_tool", "parameters": {"type": "object"}}}],
                "tool_choice": tool_choice}
        attempts = 0

        @asynccontextmanager
        async def backend(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            yield response

        converter.CONFIG.update(stream_mode="realtime", max_collect_bytes=max_collect_bytes)
        factory = {"chat": lambda: converter._stream_upstream("https://synthetic.invalid", {}, body, "auto"),
                   "responses": lambda: converter._stream_responses("https://synthetic.invalid", {}, body, "auto"),
                   "messages": lambda: converter._stream_anthropic("https://synthetic.invalid", {}, body, "auto")}[protocol]
        chunks, failure = [], None
        with patch.object(converter, "_backend_stream", backend):
            try:
                async for chunk in factory():
                    chunks.append(chunk)
            except (httpx.HTTPError, UpstreamResponseError) as error:
                failure = error
        return b"".join(chunks), attempts, failure

    async def test_invalid_terminal_state_never_succeeds_or_replays(self):
        cases = {
            "malformed arguments": [
                _line({"tool_calls": [{"index": 0, "id": "call", "type": "function",
                                      "function": {"name": "synthetic_tool", "arguments": "{"}}]},
                      usage={"total_tokens": 7}),
                _line({"tool_calls": [{"index": 0, "function": {"arguments": "invalid"}}]}),
                _line({}, "tool_calls")],
            "missing completion": [_line({"content": "partial"})],
            "partial disconnect": [_line({"content": "partial"}),],
        }
        for protocol in ("chat", "responses", "messages"):
            for name, lines in cases.items():
                with self.subTest(protocol=protocol, case=name):
                    error = httpx.ReadError("synthetic reset") if name == "partial disconnect" else None
                    wire, attempts, failure = await self.collect(
                        protocol, _FixedResponse(lines, error=error))
                    self.assertEqual(attempts, 1)
                    self.assertIsNone(failure)
                    self.assertTrue(wire)
                    self.assertNotIn(b"data: [DONE]", wire)
                    self.assertNotIn(b"response.completed", wire)
                    self.assertNotIn(b"message_stop", wire)
                    self.assertIn(b"error", wire)

    async def test_compatible_budget_snapshot_applies_to_repair_attempts(self):
        policy = converter._StreamRequestPolicy("compatible", True, 3)
        first = [
            _line({"tool_calls": [{"index": 0, "id": "call_bad", "type": "function",
                                   "function": {"name": "wrong", "arguments": "{}"}}]},
                  "tool_calls"),
            "data: [DONE]\n\n",
        ]
        second = [_line({"content": "long output"}), _line({}, "stop"), "data: [DONE]\n\n"]
        responses = iter((_FixedResponse(first), _FixedResponse(second)))
        state = {"attempts": 0, "closed": 0}

        @asynccontextmanager
        async def backend(*args, **kwargs):
            state["attempts"] += 1
            try:
                yield next(responses)
            finally:
                state["closed"] += 1

        body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "declared"}}],
                "tool_choice": "required"}
        with patch.dict(converter.CONFIG, {"max_collect_bytes": 999, "tool_call_max_retry": 1}), \
             patch.object(converter, "_backend_stream", backend), \
             patch.object(converter, "_log"), patch.object(converter, "_note_cred_model_ok"):
            with self.assertRaises(UpstreamResponseError):
                _ = [chunk async for chunk in converter._chat_sse_lines(
                    "https://synthetic.invalid", {}, body, "auto", 0, "diag", policy=policy)]
        self.assertEqual(state, {"attempts": 2, "closed": 2})

    async def test_realtime_native_http_error_keeps_real_preflight_status_at_asgi(self):
        @asynccontextmanager
        async def backend(*args, **kwargs):
            yield _FixedResponse(status=429, headers={"Retry-After": "7"},
                                body=b'{"error":{"message":"limited"}}')

        sent = await self.asgi_post(
            "/v1/responses", {"model": "auto", "stream": True, "input": "hi"}, backend,
            config={"stream_mode": "realtime", "max_collect_bytes": 0})
        start = next(message for message in sent if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in sent
                         if message["type"] == "http.response.body")
        self.assertEqual(start["status"], 429)
        self.assertIn(b"limited", body)
        self.assertNotIn(b"text/event-stream", b"".join(
            value for key, value in start.get("headers", []) if key.lower() == b"content-type"))

    async def test_realtime_malformed_terminal_and_utf8_budget_have_no_success(self):
        @asynccontextmanager
        async def malformed(*args, **kwargs):
            yield _FixedResponse([
                _line({"content": "first"}),
                _line({}, "stop"),
                _line({"content": "late"}),
                "data: [DONE]\n\n",
            ])

        sent = await self.asgi_post(
            "/v1/responses", {"model": "auto", "stream": True, "input": "hi"}, malformed,
            config={"stream_mode": "realtime", "max_collect_bytes": 0})
        wire = b"".join(message.get("body", b"") for message in sent
                         if message["type"] == "http.response.body")
        self.assertIn(b"error", wire)
        self.assertNotIn(b"response.completed", wire)

        @asynccontextmanager
        async def oversized(*args, **kwargs):
            yield _FixedResponse([
                _line({"content": "你"}, usage={"total_tokens": 3}),
                _line({}, "stop"),
                "data: [DONE]\n\n",
            ])

        usage = []
        with patch.object(converter, "observe_usage", side_effect=usage.append):
            sent = await self.asgi_post(
                "/v1/responses", {"model": "auto", "stream": True, "input": "hi"}, oversized,
                config={"stream_mode": "realtime", "max_collect_bytes": 1})
        wire = b"".join(message.get("body", b"") for message in sent
                         if message["type"] == "http.response.body")
        self.assertIn(b"response_too_large", wire)
        self.assertNotIn(b"response.completed", wire)
        self.assertEqual(usage, [{"total_tokens": 3}])

    async def test_realtime_budget_error_closes_upstream_and_releases_capacity(self):
        capacity = AccountCapacity()
        state = {"closed": False}
        usage = []
        response = _FixedResponse([
            _line({"content": "a"}, usage={"total_tokens": 2}),
            _line({"content": "b"}),
            _line({}, "stop"),
            "data: [DONE]\n\n",
        ])

        @asynccontextmanager
        async def backend(*args, **kwargs):
            try:
                yield response
            finally:
                state["closed"] = True

        def route(payload, body, rid):
            resources = request_resources.get()
            lease = capacity.acquire("synthetic-account", 1, object(), 0)
            resources.add(lease)
            return body, lease, {}, "https://synthetic.invalid"

        sent = []
        request = asyncio.Queue()
        await request.put({"type": "http.request", "body": json.dumps({
            "model": "auto", "stream": True, "input": "hi"}).encode(), "more_body": False})

        async def receive():
            return await request.get()

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/responses",
                 "raw_path": b"/v1/responses", "query_string": b"", "headers": [],
                 "scheme": "http", "http_version": "1.1", "server": ("test", 80),
                 "client": ("test", 1), "asgi": {"version": "3.0", "spec_version": "2.3"}}
        with patch.dict(converter.CONFIG, {"stream_mode": "realtime", "max_collect_bytes": 1}), \
             patch.object(converter, "_route_chat", side_effect=route), \
             patch.object(converter, "_backend_stream", backend), \
             patch.object(converter, "observe_usage", side_effect=usage.append), \
             patch.object(converter, "_log"), patch.object(converter, "_note_cred_model_ok"):
            await asyncio.wait_for(converter.app(scope, receive, send), 2)
        wire = b"".join(message.get("body", b"") for message in sent
                         if message["type"] == "http.response.body")
        self.assertIn(b"response_too_large", wire)
        self.assertTrue(state["closed"])
        self.assertEqual(usage, [{"total_tokens": 2}])
        self.assertEqual(capacity._counts, {})

    async def test_realtime_cancellation_releases_capacity_and_closes_upstream(self):
        capacity = AccountCapacity()
        state = {"closed": False}
        paused = _PausedResponse(_PausedLines(
            _line({"content": "first"}, usage={"total_tokens": 2}),
            [_line({}, "stop", {"total_tokens": 2}), "data: [DONE]\n\n"],
            asyncio.Event()))

        @asynccontextmanager
        async def backend(*args, **kwargs):
            try:
                yield paused
            finally:
                state["closed"] = True

        def route(payload, body, rid):
            resources = request_resources.get()
            lease = capacity.acquire("synthetic-account", 1, object(), 0)
            resources.add(lease)
            return body, lease, {}, "https://synthetic.invalid"

        sent = []
        request = asyncio.Queue()
        await request.put({"type": "http.request", "body": json.dumps({
            "model": "auto", "stream": True, "input": "hi"}).encode(), "more_body": False})

        async def receive():
            return await request.get()

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/responses",
                 "raw_path": b"/v1/responses", "query_string": b"", "headers": [],
                 "scheme": "http", "http_version": "1.1", "server": ("test", 80),
                 "client": ("test", 1), "asgi": {"version": "3.0", "spec_version": "2.3"}}
        with patch.dict(converter.CONFIG, {"stream_mode": "realtime", "max_collect_bytes": 0}), \
             patch.object(converter, "_route_chat", side_effect=route), \
             patch.object(converter, "_backend_stream", backend), \
             patch.object(converter, "observe_usage"), patch.object(converter, "_log"), \
             patch.object(converter, "_note_cred_model_ok"):
            task = asyncio.create_task(converter.app(scope, receive, send))
            await asyncio.wait_for(paused.lines.at_boundary.wait(), 2)
            self.assertEqual(sum(capacity._counts.values()), 1)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertTrue(state["closed"])
        self.assertEqual(capacity._counts, {})

    async def test_realtime_prefirst_byte_failover_is_bounded(self):
        capacity = AccountCapacity()
        first_lease = capacity.acquire("first-account", 1, object(), 0)
        second_lease = capacity.acquire("second-account", 1, object(), 0)
        state = {"attempts": 0, "closed": 0}
        responses = iter((_FixedResponse(status=429, headers={"Retry-After": "3"},
                                        body=b'{"error":{"message":"limited"}}'),
                          _FixedResponse([_line({"content": "ok"}), _line({}, "stop"),
                                          "data: [DONE]\n\n"])))

        @asynccontextmanager
        async def backend(*args, **kwargs):
            state["attempts"] += 1
            try:
                yield next(responses)
            finally:
                state["closed"] += 1

        body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
        policy = converter._StreamRequestPolicy("realtime", False, 0)

        def make(routed, cred, headers, url):
            return converter._stream_responses(
                url, headers, converter._body_with_stream_policy(routed, policy), "auto", cred=cred)

        def route(payload, canonical, rid, *, tried=()):
            return canonical, second_lease, {}, "https://synthetic.invalid"

        with patch.dict(converter.CONFIG, {"stream_mode": "realtime", "failover_max": 1,
                                             "max_collect_bytes": 0}), \
             patch.object(converter, "_backend_stream", backend), \
             patch.object(converter, "_route_chat", side_effect=route), \
             patch.object(converter, "_log"), patch.object(converter, "_note_cred_model_ok"):
            stream, first = await converter._stream_plan(
                {"model": "auto"}, body, "auto", "diag", 0, make,
                body, first_lease, {}, "https://synthetic.invalid")
            self.assertTrue(first)
            await converter._close_stream(stream)
        converter.release_credential(second_lease)
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(state["closed"], 2)
        self.assertEqual(capacity._counts, {})

    async def test_realtime_native_length_and_filter_distinctions_do_not_replay(self):
        for protocol in ("chat", "responses", "messages"):
            for finish_reason in ("length", "content_filter"):
                with self.subTest(protocol=protocol, finish_reason=finish_reason):
                    lines = [_line({"content": "partial"}), _line({}, finish_reason),
                             "data: [DONE]\n\n"]
                    wire, attempts, failure = await self.collect(
                        protocol, _FixedResponse(lines), tool_choice="auto")
                    self.assertEqual(attempts, 1)
                    self.assertIsNone(failure)
                    self.assertNotIn(b"response.completed", wire)
                    if protocol == "responses":
                        self.assertIn(b"response.incomplete", wire)
                    elif protocol == "messages":
                        self.assertIn(b"message_stop", wire)
                        self.assertIn(b"max_tokens" if finish_reason == "length" else b"end_turn", wire)
                    else:
                        self.assertIn(b"data: [DONE]", wire)

    async def test_realtime_filter_only_terminals_keep_protocol_status_and_usage(self):
        for protocol in ("chat", "responses", "messages"):
            for reason in ("content_filter", "content-filter", "refusal"):
                for start_frame in (False, True):
                    with self.subTest(protocol=protocol, reason=reason, start_frame=start_frame):
                        rows = ([_line({"role": "assistant"}, "")] if start_frame else [])
                        rows += [_line({}, reason, {"total_tokens": 2}), "data: [DONE]\n\n"]
                        wire, attempts, failure = await self.collect(protocol, _FixedResponse(rows))
                        self.assertEqual(attempts, 1)
                        self.assertIsNone(failure)
                        self.assertNotIn(b'"error"', wire)
                        self.assertNotIn(b"response.completed", wire)
                        if protocol == "responses":
                            response = next(event["response"] for event in _events(wire.decode())
                                            if event["type"] == "response.incomplete")
                            self.assertEqual(response["output"], [])
                            self.assertEqual(response["incomplete_details"]["reason"], "content_filter")
                            self.assertEqual(response["usage"]["total_tokens"], 2)
                        elif protocol == "chat":
                            self.assertIn(b"data: [DONE]", wire)
                            self.assertIn(reason.encode(), wire)
                        else:
                            self.assertIn(b"message_stop", wire)
                            self.assertIn(b"end_turn", wire)


    async def test_messages_parallel_block_queue_keeps_first_tool_live_before_eof(self):
        before, wire = await self.drive("messages", "realtime", True, first_delta={"tool_calls": [
            {"index": 0, "id": "first", "function": {"name": "synthetic_tool", "arguments": '{"x":'}},
            {"index": 1, "id": "second", "function": {"name": "synthetic_tool", "arguments": '{}'}}]})
        blocks = _serial_blocks(self, before.decode(), complete=False)
        self.assertEqual([block["id"] for block in blocks], ["first"])
        self.assertEqual(blocks[0]["_pieces"], ['{"x":'])
        self.assertNotIn(b"message_stop", before)
        blocks = _serial_blocks(self, wire.decode())
        self.assertEqual([block["input"] for block in blocks], [{"x": 1}, {}])
        self.assertIn(b"message_stop", wire)

    async def test_messages_deferred_budget_failure_closes_upstream_without_replay(self):
        closed = False
        attempts = []
        @asynccontextmanager
        async def backend(*args, **kwargs):
            nonlocal closed
            attempts.append(1)
            try:
                yield _FixedResponse([
                    _line({"tool_calls": [{"index": 0, "id": "first", "function": {
                        "name": "synthetic_tool", "arguments": "{}"}}]}, usage={"total_tokens": 4}),
                    _line({"tool_calls": [{"index": 1, "id": "second", "function": {
                        "name": "synthetic_tool", "arguments": "{}"}}]}),
                    _line({}, "tool_calls")])
            finally:
                closed = True
        with patch.object(converter, "observe_usage") as usage:
            sent = await self.asgi_post("/v1/messages", self.payload("messages", True), backend,
                                        config={"stream_mode": "realtime", "max_collect_bytes": 120})
        self.assertTrue(closed)
        self.assertEqual(len(attempts), 1)
        usage.assert_any_call({"total_tokens": 4})
        wire = b"".join(event.get("body", b"") for event in sent)
        self.assertIn(b"response_too_large", wire)
        self.assertNotIn(b"message_stop", wire)
        self.assertEqual(len(_serial_blocks(self, wire.decode(), complete=False)), 1)


    async def test_realtime_budget_error_after_output_has_no_success_terminal(self):
        for protocol in ("chat", "responses", "messages"):
            with self.subTest(protocol=protocol):
                lines = [_line({"content": "ok"}),
                         _line({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                                                "function": {"name": "t", "arguments": "{}"}}]})]
                wire, attempts, failure = await self.collect(
                    protocol, _FixedResponse(lines), max_collect_bytes=2)
                self.assertEqual(attempts, 1)
                self.assertIsNone(failure)
                self.assertIn(b"ok", wire)
                self.assertIn(b"response_too_large", wire)
                self.assertNotIn(b"data: [DONE]", wire)
                self.assertNotIn(b"response.completed", wire)
                self.assertNotIn(b"message_stop", wire)

    async def test_realtime_http_failure_preserves_status_and_retry_after(self):
        for protocol in ("chat", "responses", "messages"):
            with self.subTest(protocol=protocol):
                response = _FixedResponse(status=429, headers={"Retry-After": "7"},
                                          body=b'{"error":{"message":"limited"}}')
                wire, attempts, failure = await self.collect(protocol, response)
                self.assertEqual(wire, b"")
                self.assertEqual(attempts, 1)
                self.assertIsInstance(failure, UpstreamResponseError)
                self.assertEqual((failure.status, failure.headers.get("Retry-After")), (429, "7"))

    async def test_realtime_streams_reasoning_while_compatible_coalesces(self):
        rows = [
            _line({"reasoning_content": "W"}),
            _line({"reasoning_content": "line"}),
            ": ping\n\n",
            _line({"reasoning_content": "478", "content": "85"}),
            _line({"content": "done"}),
            _line({}, "stop", {"prompt_tokens": 1, "completion_tokens": 4, "total_tokens": 5}),
            "data: [DONE]\n\n",
        ]

        async def collected(protocol, mode):
            response = _FixedResponse(rows)

            @asynccontextmanager
            async def backend(*args, **kwargs):
                yield response

            path = {"chat": "/v1/chat/completions", "responses": "/v1/responses",
                    "messages": "/v1/messages"}[protocol]
            sent = await self.asgi_post(
                path, self.payload(protocol, False), backend,
                config={"stream_mode": mode})
            return b"".join(message.get("body", b"") for message in sent
                            if message["type"] == "http.response.body")

        # Chat: realtime forwards each reasoning delta; compatible keeps one replay block.
        wire = await collected("chat", "realtime")
        events = [json.loads(line[6:]) for line in wire.decode().splitlines()
                  if line.startswith("data: ") and line[6:] != "[DONE]"]
        deltas = [event["choices"][0]["delta"] for event in events if event.get("choices")]
        self.assertEqual([delta.get("reasoning_content") for delta in deltas if delta.get("reasoning_content")],
                         ["W", "line", "478"])
        self.assertEqual([delta.get("content") for delta in deltas if delta.get("content")],
                         ["85", "done"])
        first_reasoning = next(i for i, delta in enumerate(deltas) if delta.get("reasoning_content"))
        first_content = next(i for i, delta in enumerate(deltas) if delta.get("content"))
        self.assertLess(first_reasoning, first_content)

        wire = await collected("chat", "compatible")
        events = [json.loads(line[6:]) for line in wire.decode().splitlines()
                  if line.startswith("data: ") and line[6:] != "[DONE]"]
        deltas = [event["choices"][0]["delta"] for event in events if event.get("choices")]
        self.assertEqual([delta.get("reasoning_content") for delta in deltas if delta.get("reasoning_content")],
                         ["Wline478"])

        # Responses: one logical reasoning item, delivered as incremental deltas.
        wire = await collected("responses", "realtime")
        events = _events(wire.decode())
        reasoning = [event["delta"] for event in events
                     if event["type"] == "response.reasoning_summary_text.delta"]
        self.assertEqual(reasoning, ["W", "line", "478"])
        response_items = [event["item"]["type"] for event in events
                          if event["type"] == "response.output_item.done"]
        self.assertEqual(response_items.count("reasoning"), 1)

        # Messages: one thinking block, delivered as incremental deltas.
        wire = await collected("messages", "realtime")
        events = _events(wire.decode())
        thinking = [event["delta"]["thinking"] for event in events
                    if event["type"] == "content_block_delta"
                    and event["delta"]["type"] == "thinking_delta"]
        self.assertEqual(thinking, ["W", "line", "478"])
        blocks = _serial_blocks(self, wire.decode())
        self.assertEqual([block["type"] for block in blocks], ["thinking", "text"])
        self.assertEqual(blocks[0]["thinking"], "Wline478")
        self.assertEqual(blocks[1]["text"], "85done")

    async def test_hot_mode_change_affects_only_the_next_request(self):
        before, _ = await self.drive("responses", "realtime", True, change_to="compatible")
        self.assertTrue(self.meaningful("responses", before), before)
        before, _ = await self.drive("responses", "compatible", True)
        self.assertFalse(self.meaningful("responses", before), before)

    async def test_upstream_empty_finish_reason_is_not_a_terminal_marker(self):
        for protocol in ("chat", "responses", "messages"):
            for mode in ("compatible", "realtime"):
                for tools in (False, True):
                    with self.subTest(protocol=protocol, mode=mode, tools=tools):
                        delta = ({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                  "function": {"name": "synthetic_tool", "arguments": "{}"}}]}
                                 if tools else {"content": "early"})
                        rows = [_line({"role": "assistant", "content": "", "reasoning_content": "",
                                       "tool_calls": []}, ""),
                                _line(delta, ""), _line({}, "tool_calls" if tools else "stop"),
                                _line({}, "", {"total_tokens": 2}), "data: [DONE]\n\n"]
                        @asynccontextmanager
                        async def backend(*args, **kwargs):
                            yield _FixedResponse(rows)
                        path = {"chat": "/v1/chat/completions", "responses": "/v1/responses",
                                "messages": "/v1/messages"}[protocol]
                        sent = await self.asgi_post(path, self.payload(protocol, tools), backend,
                                                    config={"stream_mode": mode})
                        self.assertEqual(sent[0]["status"], 200)
                        wire = b"".join(m.get("body", b"") for m in sent)
                        self.assertNotIn(b'"error"', wire)
                        terminal = {"chat": b"data: [DONE]", "responses": b"response.completed",
                                    "messages": b"message_stop"}[protocol]
                        self.assertIn(terminal, wire)
        for protocol in ("chat", "responses", "messages"):
            with self.subTest(protocol=protocol, no_terminal=True):
                wire, attempts, failure = await self.collect(protocol, _FixedResponse([
                    _line({"content": "early"}, ""), "data: [DONE]\n\n"]), tool_choice="none")
                self.assertEqual(attempts, 1)
                self.assertNotIn(b"response.completed", wire)
                self.assertNotIn(b"message_stop", wire)
                self.assertNotIn(b"data: [DONE]", wire)
                self.assertTrue(failure or b'"error"' in wire)


    async def test_nonstream_failover_retains_entry_budget_across_hot_changes(self):
        for protocol in ("chat", "responses", "messages"):
            for mode in ("compatible", "realtime"):
                for explicit in (False, True):
                    with self.subTest(protocol=protocol, mode=mode, explicit_stream_false=explicit):
                        attempts = []
                        credentials = [object(), object()]
                        def route(payload, body, rid, *, tried=()):
                            # Routing runs after entry policy capture, before the first collection.
                            converter.CONFIG["max_collect_bytes"] = 64
                            return body, credentials[len(tried)], {}, "https://synthetic.invalid"
                        @asynccontextmanager
                        async def backend(url, headers, body, **kwargs):
                            json.dumps(body)  # Policy snapshots must not leak onto the wire.
                            attempts.append(body)
                            if len(attempts) == 1:
                                converter.CONFIG["max_collect_bytes"] = 128
                                yield _FixedResponse(status=503, body=b'{"error":{"message":"busy"}}')
                            else:
                                yield _FixedResponse([_line({"content": "oversize"}), _line({}, "stop")])
                        body = self.payload(protocol, tools=False)
                        if explicit:
                            body["stream"] = False
                        else:
                            body.pop("stream")
                        path = {"chat": "/v1/chat/completions", "responses": "/v1/responses",
                                "messages": "/v1/messages"}[protocol]
                        sent = await self.asgi_post(path, body, backend, route=route, config={
                            "stream_mode": mode, "max_collect_bytes": 4, "failover_max": 1})
                        self.assertEqual(len(attempts), 2)
                        self.assertEqual(sent[0]["status"], 502)
                        self.assertIn(b"response_too_large", b"".join(m.get("body", b"") for m in sent))
                        # A later request sees the new budget, while the prior request did not.
                        sent = await self.asgi_post(path, body, backend, route=route, config={
                            "stream_mode": mode, "max_collect_bytes": 128, "failover_max": 1})
                        self.assertEqual(sent[0]["status"], 200)


    async def test_protocol_mode_and_tool_matrix_has_no_aggregate_in_realtime(self):
        for protocol in ("chat", "responses", "messages"):
            for tools in (False, True):
                for mode in ("compatible", "realtime"):
                    with self.subTest(protocol=protocol, tools=tools, mode=mode):
                        before, wire = await self.drive(protocol, mode, tools)
                        # Responses and tool-bearing Chat/Messages are compatible-mode aggregates.
                        aggregated = mode == "compatible" and (protocol == "responses" or tools)
                        self.assertNotEqual(self.meaningful(protocol, before), aggregated, before)
                        terminal = {b"data: [DONE]", b"response.completed", b"message_stop"}
                        self.assertTrue(any(marker in wire for marker in terminal), wire)


class RealtimeAdapterTests(unittest.TestCase):
    def realtime_converters(self):
        budget = StreamOutputBudget(0)
        tracker = ChatSSEAccumulator(collect=False, retain_tools=True, budget=budget)
        responses = ResponsesStreamConverter(model="m", realtime=True, budget=budget,
                                             tool_states=tracker.tools)
        anthropic = AnthropicStreamConverter(model="m", realtime=True, budget=budget,
                                              tool_states=tracker.tools)
        return budget, tracker, responses, anthropic

    @staticmethod
    def feed_pair(tracker, converter, delta, finish=None, usage=None):
        raw = _line(delta, finish, usage)
        tracker.feed_line(raw)
        return converter.feed_line(raw)

    def test_messages_serializes_parallel_tools_and_following_text_thinking(self):
        for shared in (False, True):
            with self.subTest(shared_tool_state=shared):
                _, tracker, _, adapter = self.realtime_converters()
                if not shared:
                    adapter = AnthropicStreamConverter(realtime=True)
                def feed(delta, finish=None):
                    return self.feed_pair(tracker, adapter, delta, finish)
                raw = feed({"tool_calls": [{"index": 0, "id": "first",
                    "function": {"name": "synthetic_tool", "arguments": '{"a":'}}]})
                blocked = feed({"tool_calls": [{"index": 1, "id": "second",
                    "function": {"name": "synthetic_tool", "arguments": '{"b":'}}]})
                blocked += feed({"reasoning_content": "later-thought"})
                blocked += feed({"content": "later-text"})
                self.assertEqual(blocked, "")
                active_delta = feed({"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]})
                self.assertIn('1}', active_delta)
                raw += active_delta
                self.assertEqual(len(_serial_blocks(self, raw, complete=False)), 1)
                self.assertEqual(feed({"tool_calls": [{"index": 1, "function": {"arguments": '2}'}}]}), "")
                raw += feed({}, "tool_calls")
                adapter.set_validated_tools(tracker.result()["tool_calls"])
                raw += adapter.finish()
                blocks = _serial_blocks(self, raw)
                self.assertEqual([b["type"] for b in blocks], ["tool_use", "tool_use", "thinking", "text"])
                self.assertEqual([b["input"] for b in blocks[:2]], [{"a": 1}, {"b": 2}])
                self.assertEqual(blocks[2]["thinking"], "later-thought")
                self.assertEqual(blocks[3]["text"], "later-text")

    def test_messages_delayed_identity_does_not_hold_unrelated_text(self):
        _, tracker, _, adapter = self.realtime_converters()
        raw = self.feed_pair(tracker, adapter, {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"v":1}'}}]})
        text = self.feed_pair(tracker, adapter, {"content": "visible-now"})
        self.assertIn("visible-now", text)
        raw += text
        raw += self.feed_pair(tracker, adapter, {"tool_calls": [
            {"index": 0, "id": "late", "function": {"name": "synthetic_tool"}}]})
        raw += self.feed_pair(tracker, adapter, {}, "tool_calls")
        adapter.set_validated_tools(tracker.result()["tool_calls"])
        raw += adapter.finish()
        blocks = _serial_blocks(self, raw)
        self.assertEqual([b["type"] for b in blocks], ["text", "tool_use"])
        self.assertEqual(blocks[1]["input"], {"v": 1})

    def test_messages_deferred_event_bytes_share_the_request_budget(self):
        budget = StreamOutputBudget(120)
        tracker = ChatSSEAccumulator(collect=False, retain_tools=True, budget=budget)
        adapter = AnthropicStreamConverter(realtime=True, budget=budget, tool_states=tracker.tools)
        self.feed_pair(tracker, adapter, {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "t", "arguments": '{}'}}]})
        next_tool = _line({"tool_calls": [
            {"index": 1, "id": "b", "function": {"name": "t", "arguments": '{}'}}]})
        tracker.feed_line(next_tool)
        with self.assertRaises(UpstreamResponseError) as raised:
            adapter.feed_line(next_tool)
        self.assertIn(b"response_too_large", raised.exception.raw)


    def test_responses_uses_first_seen_order_and_never_reuses_an_index(self):
        _, tracker, converter, _ = self.realtime_converters()
        raw = ""
        raw += self.feed_pair(tracker, converter, {"content": "text-first"})
        raw += self.feed_pair(tracker, converter, {"reasoning_content": "think"})
        raw += self.feed_pair(tracker, converter, {"tool_calls": [{
            "index": 0, "id": "call_1", "function": {"name": "synthetic_tool", "arguments": "{}"}}]})
        raw += self.feed_pair(tracker, converter, {"reasoning_content": "-again"})
        raw += self.feed_pair(tracker, converter, {"content": "-again"})
        raw += self.feed_pair(tracker, converter, {}, "tool_calls")
        raw += self.feed_pair(tracker, converter, {}, None,
                              {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        converter.set_validated_tools(tracker.result()["tool_calls"])
        raw += converter.finish()
        events = _events(raw)
        added = [event for event in events if event["type"] == "response.output_item.added"]
        indices = [event["output_index"] for event in added]
        self.assertEqual(indices, [0, 1, 2])
        self.assertEqual(len(set(indices)), 3)
        completed = next(event for event in events if event["type"] == "response.completed")
        self.assertEqual([item["type"] for item in completed["response"]["output"]],
                         ["message", "reasoning", "function_call"])
        sequence = [event["sequence_number"] for event in events]
        self.assertEqual(sequence, list(range(1, len(sequence) + 1)))
        final_indices = {item["id"]: index for index, item in enumerate(completed["response"]["output"])}
        for event in added:
            self.assertEqual(final_indices[event["item"]["id"]], event["output_index"])

    def test_responses_tool_then_text_keeps_tool_index_zero(self):
        _, tracker, converter, _ = self.realtime_converters()
        raw = self.feed_pair(tracker, converter, {"tool_calls": [{
            "index": 0, "id": "call", "function": {"name": "synthetic_tool", "arguments": "{}"}}]})
        raw += self.feed_pair(tracker, converter, {"content": "after"})
        raw += self.feed_pair(tracker, converter, {}, "tool_calls")
        converter.set_validated_tools(tracker.result()["tool_calls"])
        raw += converter.finish()
        events = _events(raw)
        added = [event for event in events if event["type"] == "response.output_item.added"]
        self.assertEqual([(event["output_index"], event["item"]["type"]) for event in added],
                         [(0, "function_call"), (1, "message")])
        completed = next(event for event in events if event["type"] == "response.completed")
        self.assertEqual([item["type"] for item in completed["response"]["output"]],
                         ["function_call", "message"])

    def test_responses_delays_tool_start_and_flushes_fragmented_identity_and_arguments_in_order(self):
        _, tracker, converter, _ = self.realtime_converters()
        raw = self.feed_pair(tracker, converter, {"tool_calls": [{
            "index": 0, "function": {"name": "synthetic_", "arguments": '{"x":'}}]})
        self.assertNotIn("response.output_item.added", raw)
        raw += self.feed_pair(tracker, converter, {"tool_calls": [{
            "index": 0, "id": "call_", "function": {"name": "tool", "arguments": "1}"}}]})
        raw += self.feed_pair(tracker, converter, {}, "tool_calls")
        converter.set_validated_tools(tracker.result()["tool_calls"])
        raw += converter.finish()
        events = _events(raw)
        start = next(event for event in events if event["type"] == "response.output_item.added")
        self.assertEqual(start["item"]["call_id"], "call_")
        self.assertEqual(start["item"]["name"], "synthetic_tool")
        deltas = [event["delta"] for event in events
                  if event["type"] == "response.function_call_arguments.delta"]
        self.assertEqual("".join(deltas), '{"x":1}')

    def test_fragmented_id_and_conflicting_emitted_metadata(self):
        _, tracker, converter, _ = self.realtime_converters()
        self.feed_pair(tracker, converter, {"tool_calls": [{
            "index": 0, "id": "call_", "function": {"arguments": "{"}}]})
        raw = self.feed_pair(tracker, converter, {"tool_calls": [{
            "index": 0, "id": "1", "function": {"name": "synthetic_tool", "arguments": "}"}}]})
        start = next(event for event in _events(raw) if event["type"] == "response.output_item.added")
        self.assertEqual(start["item"]["call_id"], "call_1")
        with self.assertRaises(httpx.RemoteProtocolError):
            tracker.feed_line(_line({"tool_calls": [{"index": 0, "id": "other",
                                                    "function": {"name": "synthetic_tool"}}]}))

    def test_anthropic_interleaved_parallel_tools_keep_stable_block_identities(self):
        _, tracker, _, converter = self.realtime_converters()
        raw = ""
        raw += self.feed_pair(tracker, converter, {"reasoning_content": "plan"})
        raw += self.feed_pair(tracker, converter, {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "one", "arguments": '{"x":'}},
            {"index": 1, "id": "b", "function": {"name": "two", "arguments": '{"y":'}}]})
        raw += self.feed_pair(tracker, converter, {"tool_calls": [
            {"index": 1, "function": {"arguments": "2}"}},
            {"index": 0, "function": {"arguments": "1}"}}]})
        raw += self.feed_pair(tracker, converter, {"content": "done"})
        raw += self.feed_pair(tracker, converter, {"reasoning_content": "checked"})
        raw += self.feed_pair(tracker, converter, {}, "tool_calls")
        converter.set_validated_tools(tracker.result()["tool_calls"])
        raw += converter.finish()
        events = _events(raw)
        starts = [event for event in events if event["type"] == "content_block_start"]
        tools = {event["index"]: event["content_block"] for event in starts
                 if event["content_block"]["type"] == "tool_use"}
        self.assertEqual({key: value["id"] for key, value in tools.items()}, {1: "a", 2: "b"})
        arguments = {index: [] for index in tools}
        for event in events:
            if event["type"] == "content_block_delta" and event["delta"]["type"] == "input_json_delta":
                arguments[event["index"]].append(event["delta"]["partial_json"])
        self.assertEqual(arguments[1], ['{"x":', "1}"])
        self.assertEqual(arguments[2], ['{"y":', "2}"])
        stops = [event["index"] for event in events if event["type"] == "content_block_stop"]
        self.assertEqual(stops, [0, 1, 2, 3, 4])
        _serial_blocks(self, raw)

    def test_responses_tool_start_and_done_reconstruct_arguments_once(self):
        complete = [
            _line({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                   "function": {"name": "synthetic_tool", "arguments": '{"x":1}'}}]}),
            _line({}, "tool_calls", {"total_tokens": 2}),
            "data: [DONE]\n\n",
        ]
        delayed = [
            _line({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                   "function": {"name": "synthetic_", "arguments": ""}}]}),
            _line({"tool_calls": [{"index": 0, "function": {"name": "tool",
                                                            "arguments": '{"x":'}}]}),
            _line({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]},
                  "tool_calls", {"total_tokens": 2}),
            "data: [DONE]\n\n",
        ]
        for rows in (complete, delayed):
            with self.subTest(delayed=rows is delayed):
                budget = StreamOutputBudget(0)
                tracker = ChatSSEAccumulator(collect=False, retain_tools=True, budget=budget,
                                             declared_names={"synthetic_tool"})
                converter = ResponsesStreamConverter(
                    model="m", realtime=True, budget=budget, tool_states=tracker.tools,
                    declared_names={"synthetic_tool"})
                raw = ""
                for row in rows:
                    tracker.feed_line(row)
                    raw += converter.feed_line(row)
                merged = tracker.result()
                converter.set_validated_tools(merged["tool_calls"])
                raw += converter.finish()
                events = _events(raw)
                added = [event for event in events
                         if event["type"] == "response.output_item.added"
                         and event["item"]["type"] == "function_call"]
                self.assertEqual(len(added), 1)
                deltas = [event["delta"] for event in events
                          if event["type"] == "response.function_call_arguments.delta"]
                done = next(event for event in events
                            if event["type"] == "response.function_call_arguments.done")
                self.assertEqual(added[0]["item"]["arguments"], "")
                self.assertEqual(json.loads(added[0]["item"]["arguments"] + "".join(deltas)),
                                 {"x": 1})
                self.assertEqual(done["arguments"], added[0]["item"]["arguments"] + "".join(deltas))
                final = next(event for event in events if event["type"] == "response.completed")
                final_call = next(item for item in final["response"]["output"]
                                  if item["type"] == "function_call")
                self.assertEqual(final_call["arguments"], done["arguments"])

    def test_fragmented_metadata_waits_for_a_safe_boundary(self):
        cases = {
            "split-name": [
                {"id": "call_a", "name": "synthetic_", "arguments": ""},
                {"name": "tool", "arguments": "{}"},
            ],
            "split-id": [
                {"id": "call_", "name": "synthetic_tool", "arguments": ""},
                {"id": "abc", "arguments": "{}"},
            ],
            "both-fragmented": [
                {"id": "call_", "name": "synthetic_", "arguments": ""},
                {"id": "abc", "name": "tool", "arguments": "{}"},
            ],
            "arguments-before-metadata": [
                {"arguments": '{"x":'},
                {"id": "call_a", "name": "synthetic_tool"},
            ],
            "empty-fragment": [
                {"id": "", "name": "", "arguments": ""},
                {"id": "call_a", "name": "synthetic_tool", "arguments": "{}"},
            ],
        }
        for name, metadata in cases.items():
            with self.subTest(case=name):
                budget = StreamOutputBudget(0)
                tracker = ChatSSEAccumulator(collect=False, retain_tools=True, budget=budget,
                                             declared_names={"synthetic_tool"})
                converter = ResponsesStreamConverter(
                    model="m", realtime=True, budget=budget, tool_states=tracker.tools,
                    declared_names={"synthetic_tool"})
                raw = ""
                starts = []
                for index, item in enumerate(metadata):
                    tool = {"index": 0, "type": "function",
                            "function": {"name": item.get("name", ""),
                                         "arguments": item.get("arguments", "")}}
                    if item.get("id") is not None:
                        tool["id"] = item["id"]
                    row = _line({"tool_calls": [tool]})
                    tracker.feed_line(row)
                    raw += converter.feed_line(row)
                    starts.extend(event for event in _events(raw)
                                  if event["type"] == "response.output_item.added")
                    if index == 0 and item.get("id") == "":
                        self.assertFalse(starts)
                terminal = _line({}, "tool_calls", {"total_tokens": 1})
                tracker.feed_line(terminal)
                raw += converter.feed_line(terminal)
                merged = tracker.result()
                converter.set_validated_tools(merged["tool_calls"])
                raw += converter.finish()
                events = _events(raw)
                added = [event for event in events
                         if event["type"] == "response.output_item.added"
                         and event["item"]["type"] == "function_call"]
                self.assertEqual(len(added), 1)
                self.assertEqual(added[0]["item"]["call_id"], "call_a" if name != "split-id"
                                 and name != "both-fragmented" else "call_abc")
                self.assertEqual(added[0]["item"]["name"], "synthetic_tool")
                arguments = "".join(event["delta"] for event in events
                                    if event["type"] == "response.function_call_arguments.delta")
                self.assertEqual(arguments, '{"x":' if name == "arguments-before-metadata" else "{}")
                self.assertEqual(merged["tool_calls"][0]["id"], added[0]["item"]["call_id"])

                if name != "arguments-before-metadata":
                    with self.assertRaises(httpx.RemoteProtocolError):
                        tracker.feed_line(_line({"tool_calls": [{
                            "index": 0, "id": "other", "function": {"name": "synthetic_tool"}}]}))

    def test_tool_identity_fragments_preserve_repeated_prefixes(self):
        for cls in (ResponsesStreamConverter, AnthropicStreamConverter):
            for field, values in (("name", ("test", "aaaa", "tool_tool", "lookup")),
                                  ("id", ("call_abc", "call_call", "aaaa"))):
                for value in values:
                    for split in range(1, len(value)):
                        with self.subTest(adapter=cls.__name__, field=field, value=value, split=split):
                            name = value if field == "name" else "lookup"
                            identifier = value if field == "id" else "call_x"
                            tracker = ChatSSEAccumulator(collect=False, retain_tools=True,
                                                         declared_names={name})
                            adapter = cls(realtime=True, tool_states=tracker.tools, declared_names={name})
                            first = {"index": 0, "id": identifier, "function": {"name": name}}
                            last = {"index": 0, "function": {"arguments": "{}"}}
                            if field == "name":
                                first["function"][field] = value[:split]
                                last["function"][field] = value[split:]
                            else:
                                first[field], last[field] = value[:split], value[split:]
                            raw = ""
                            for tool in (first, last):
                                row = _line({"tool_calls": [tool]})
                                tracker.feed_line(row)
                                raw += adapter.feed_line(row)
                            row = _line({}, "tool_calls")
                            tracker.feed_line(row)
                            raw += adapter.feed_line(row)
                            calls = tracker.result()["tool_calls"]
                            self.assertEqual(calls[0]["id"], identifier)
                            self.assertEqual(calls[0]["function"]["name"], name)
                            converter._validate_realtime_tools(calls, {"tools": [{
                                "type": "function", "function": {"name": name}}]}, "tool_calls")
                            adapter.set_validated_tools(calls)
                            raw += adapter.finish()
                            if cls is ResponsesStreamConverter:
                                start = next(event["item"] for event in _events(raw)
                                             if event["type"] == "response.output_item.added")
                                self.assertEqual((start["call_id"], start["name"]), (identifier, name))
                            else:
                                start = next(event["content_block"] for event in _events(raw)
                                             if event["type"] == "content_block_start")
                                self.assertEqual((start["id"], start["name"]), (identifier, name))


    def test_complete_metadata_streams_without_guessing_id_or_name_prefixes(self):
        cases = [("read", "call_ok", {"read", "read_file"}),
                 ("lookup", "call_", {"lookup"}), ("tool_", "call_ok", {"tool_"})]
        for cls in (ResponsesStreamConverter, AnthropicStreamConverter):
            for name, identifier, declared in cases:
                with self.subTest(adapter=cls.__name__, name=name, identifier=identifier):
                    tracker = ChatSSEAccumulator(collect=False, retain_tools=True,
                                                 declared_names=declared)
                    adapter = cls(realtime=True, tool_states=tracker.tools, declared_names=declared)
                    row = _line({"tool_calls": [{"index": 0, "id": identifier,
                                                "function": {"name": name, "arguments": '{"x":'}}]})
                    tracker.feed_line(row)
                    events = _events(adapter.feed_line(row))
                    kind = ("response.function_call_arguments.delta" if cls is ResponsesStreamConverter
                            else "content_block_delta")
                    delta = next((event for event in events if event["type"] == kind), None)
                    self.assertIsNotNone(delta, "Complete metadata must not defer arguments until EOF")
                    self.assertEqual(delta["delta"] if cls is ResponsesStreamConverter
                                     else delta["delta"]["partial_json"], '{"x":')


    def test_standalone_realtime_adapters_merge_normal_metadata(self):
        for cls in (ResponsesStreamConverter, AnthropicStreamConverter):
            with self.subTest(adapter=cls.__name__):
                converter = cls(model="m", realtime=True)
                raw = converter.feed_line(_line({"tool_calls": [{
                    "index": 0, "id": "call_a", "type": "function",
                    "function": {"name": "lookup", "arguments": ""}}]}))
                self.assertNotIn("content_block_start" if cls is AnthropicStreamConverter
                                 else "response.output_item.added", raw)
                raw += converter.feed_line(_line({"tool_calls": [{
                    "index": 0, "function": {"arguments": "{}"}}]}))
                self.assertIn("content_block_start" if cls is AnthropicStreamConverter
                              else "response.output_item.added", raw)

    def test_realtime_terminal_marker_and_choice_boundaries(self):
        body = {
            "tools": [{"type": "function", "function": {"name": "declared"}}],
            "tool_choice": "required",
            "parallel_tool_calls": True,
        }
        valid = {"id": "call_a", "type": "function",
                 "function": {"name": "declared", "arguments": "{}"}}
        with self.assertRaises(UpstreamResponseError):
            converter._validate_realtime_tools([valid], body, "stop")
        converter._validate_realtime_tools([valid], body, "tool_calls")
        for finish in ("length", "content_filter", "refusal"):
            converter._validate_realtime_tools([], body, finish)
        named = body | {"tool_choice": {"type": "function", "function": {"name": "declared"}}}
        converter._validate_realtime_tools([], named, "content_filter")
        with self.assertRaises(UpstreamResponseError):
            converter._validate_realtime_tools([], named, "stop")
        malformed_filtered = {**valid, "function": {"name": "declared", "arguments": "{"}}
        with self.assertRaises(UpstreamResponseError):
            converter._validate_realtime_tools([malformed_filtered], body, "content_filter")

        tracker = ChatSSEAccumulator(collect=False, retain_tools=True)
        tracker.feed_line(_line({"content": "first"}))
        tracker.feed_line(_line({}, "stop"))
        with self.assertRaises(httpx.RemoteProtocolError):
            tracker.feed_line(_line({"content": "late"}))
        with self.assertRaises(httpx.RemoteProtocolError):
            tracker.feed_line(_line({}, "length"))
        for cls in (ResponsesStreamConverter, AnthropicStreamConverter):
            with self.subTest(adapter=cls.__name__):
                adapter = cls(model="m", realtime=True)
                adapter.feed_line(_line({"content": "first"}))
                adapter.feed_line(_line({}, "stop"))
                with self.assertRaises(ValueError):
                    adapter.feed_line(_line({"content": "late"}))
                marker_adapter = cls(model="m", realtime=True)
                marker_adapter.feed_line(_line({"tool_calls": [{
                    "index": 0, "id": "call_a", "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"}}]}))
                marker_adapter.feed_line(_line({}, "stop"))
                with self.assertRaises(ValueError):
                    marker_adapter.finish()

    def test_delayed_tool_then_text_uses_emission_order_and_stable_indices(self):
        budget = StreamOutputBudget(0)
        tracker = ChatSSEAccumulator(collect=False, retain_tools=True, budget=budget,
                                     declared_names={"synthetic_tool"})
        converter = ResponsesStreamConverter(model="m", realtime=True, budget=budget,
                                             tool_states=tracker.tools,
                                             declared_names={"synthetic_tool"})
        rows = [
            _line({"tool_calls": [{"index": 0, "id": "call_a", "function": {
                "name": "synthetic_", "arguments": ""}}]}),
            _line({"content": "available text"}),
            _line({"tool_calls": [{"index": 0, "function": {
                "name": "tool", "arguments": "{}"}}]}, "tool_calls"),
            "data: [DONE]\n\n",
        ]
        raw = ""
        after_text = ""
        for row in rows:
            tracker.feed_line(row)
            raw += converter.feed_line(row)
            if row is rows[1]:
                after_text = raw
        before_text_events = _events(after_text)
        self.assertTrue(any(event["type"] == "response.output_text.delta"
                            for event in before_text_events))
        self.assertFalse(any(event["type"] == "response.output_item.added"
                             and event["item"]["type"] == "function_call"
                             for event in before_text_events))
        merged = tracker.result()
        converter.set_validated_tools(merged["tool_calls"])
        raw += converter.finish()
        events = _events(raw)
        added = [event for event in events if event["type"] == "response.output_item.added"]
        self.assertEqual([(event["output_index"], event["item"]["type"]) for event in added],
                         [(0, "message"), (1, "function_call")])
        final = next(event for event in events if event["type"] == "response.completed")
        self.assertEqual([item["type"] for item in final["response"]["output"]],
                         ["message", "function_call"])
        self.assertEqual(final["response"]["output"][1]["arguments"], "{}")

    def test_utf8_budget_counts_each_logical_fragment_once(self):
        budget = StreamOutputBudget(3)
        budget.charge_text("你")
        with self.assertRaises(UpstreamResponseError):
            budget.charge_text("!")

    def test_terminal_tool_validation_rejects_bad_identity_json_names_and_choices(self):
        valid = {"id": "call", "type": "function",
                 "function": {"name": "declared", "arguments": "{}"}}
        body = {"tools": [{"type": "function", "function": {"name": "declared"}}],
                "tool_choice": "required", "parallel_tool_calls": True}
        self.assertTrue(converter._tool_calls_healthy([valid], body))
        self.assertTrue(converter._tool_choice_satisfied([valid], body))
        malformed = [
            None, {}, {**valid, "id": ""}, {**valid, "function": {**valid["function"], "name": "other"}},
            {**valid, "function": {**valid["function"], "arguments": "[]"}},
            {**valid, "function": {**valid["function"], "arguments": "{"}},
            valid, valid,
        ]
        for calls in malformed:
            with self.subTest(calls=calls), self.assertRaises(UpstreamResponseError):
                converter._validate_realtime_tools(calls, body)
        for choice in ("none", "auto", "required"):
            selected = body | {"tool_choice": choice}
            expected = choice != "none"
            self.assertEqual(converter._tool_choice_satisfied([valid] if expected else [], selected), True)
        with self.assertRaises(UpstreamResponseError):
            converter._validate_realtime_tools([], {"tools": [], "tool_choice": "required"})
        with self.assertRaises(UpstreamResponseError):
            converter._validate_realtime_tools([valid], body | {"tool_choice": "none"})
        second = {**valid, "id": "call_2"}
        with self.assertRaises(UpstreamResponseError):
            converter._validate_realtime_tools([valid, second], body | {"parallel_tool_calls": False})


class RealtimeAuditTests(unittest.TestCase):
    def test_all_generation_audits_record_the_entry_mode(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = AuditStore(root / "audit.sqlite3")
        self.addCleanup(store.close)
        application = FastAPI()
        application.router.routes = list(converter.app.router.routes)
        application.add_middleware(AuditMiddleware, store)
        @asynccontextmanager
        async def backend(*args, **kwargs):
            yield _FixedResponse([_line({"content": "ok"}), _line({}, "stop", {"total_tokens": 2})])
        config = {"api_key": "", "model_guard": False, "max_images": 16, "image_policy": "truncate",
                  "max_request_bytes": 32 * 1024 * 1024, "max_collect_bytes": 128, "log_path": None}
        with patch.dict(converter.CONFIG, config), patch.object(converter, "_backend_stream", backend), \
             patch.object(converter, "_log"), patch.object(converter, "_note_cred_model_ok"):
            client = self.enterContext(TestClient(application))
            seen = set()
            for protocol in ("chat", "responses", "messages"):
                for mode in ("compatible", "realtime"):
                    for stream in (None, False, True):
                        with self.subTest(protocol=protocol, mode=mode, stream=stream):
                            converter.CONFIG["stream_mode"] = mode
                            def route(payload, body, rid):
                                converter.CONFIG["stream_mode"] = "realtime" if mode == "compatible" else "compatible"
                                return body, None, {}, "https://synthetic.invalid"
                            body = {"model": "auto", "max_tokens": 32}
                            body.update({"input": "hi"} if protocol == "responses" else {
                                "messages": [{"role": "user", "content": "hi"}]})
                            if stream is not None:
                                body["stream"] = stream
                            path = {"chat": "/v1/chat/completions", "responses": "/v1/responses",
                                    "messages": "/v1/messages"}[protocol]
                            with patch.object(converter, "_route_chat", side_effect=route):
                                response = client.post(path, json=body)
                            self.assertEqual(response.status_code, 200, response.text)
                            records = [item for item in store.list_records()["items"] if item["id"] not in seen]
                            self.assertEqual(len(records), 1)
                            record = records[0]
                            seen.add(record["id"])
                            self.assertEqual(record["stream_mode"], mode)
                            self.assertEqual(record["outcome"], "success")


    def test_empty_realtime_filter_audits_as_rejected_without_replay(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = AuditStore(root / "audit.sqlite3")
        self.addCleanup(store.close)
        application = FastAPI()
        application.router.routes = list(converter.app.router.routes)
        application.add_middleware(AuditMiddleware, store)
        attempts = []
        @asynccontextmanager
        async def backend(*args, **kwargs):
            attempts.append(1)
            yield _FixedResponse([_line({}, "content_filter", {"total_tokens": 2})])
        config = {"api_key": "", "model_guard": False, "max_images": 16, "image_policy": "truncate",
                  "max_request_bytes": 32 * 1024 * 1024, "max_collect_bytes": 128,
                  "log_path": None, "stream_mode": "realtime", "failover_max": 2}
        with patch.dict(converter.CONFIG, config), patch.object(converter, "_backend_stream", backend), \
             patch.object(converter, "_route_chat", side_effect=lambda payload, body, rid: (body, None, {}, "https://synthetic.invalid")), \
             patch.object(converter, "_log"), patch.object(converter, "_note_cred_model_ok"):
            client = self.enterContext(TestClient(application))
            response = client.post("/v1/responses", json={"model": "auto", "input": "hi", "stream": True})
        self.assertEqual(response.status_code, 200)
        self.assertIn("response.incomplete", response.text)
        self.assertNotIn("response.completed", response.text)
        self.assertEqual(len(attempts), 1)
        record = store.list_records()["items"][0]
        self.assertEqual((record["outcome"], record["stream_mode"], record["total_tokens"]),
                         ("error", "realtime", 2))
        self.assertEqual(record["error_code"], "response_incomplete")
        self.assertTrue(any(attempt["stage"] == "content_filter" for attempt in record["attempts"]))


    def test_failed_realtime_terminal_is_error_with_mode_and_actual_usage(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = AuditStore(root / "audit.sqlite3")
        self.addCleanup(store.close)
        application = FastAPI()
        application.router.routes = list(converter.app.router.routes)
        application.add_middleware(AuditMiddleware, store)
        response_lines = [
            _line({"tool_calls": [{"index": 0, "id": "call", "type": "function",
                                  "function": {"name": "synthetic_tool", "arguments": "{"}}]},
                   usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}),
            _line({"tool_calls": [{"index": 0, "function": {"arguments": "invalid"}}]}),
            _line({}, "tool_calls")]

        @asynccontextmanager
        async def backend(*args, **kwargs):
            yield _FixedResponse(response_lines)

        config = {"api_key": "", "model_guard": False, "max_images": 16,
                  "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
                  "max_collect_bytes": 0, "log_path": None, "stream_mode": "realtime"}
        with patch.dict(converter.CONFIG, config, clear=False), \
             patch.object(converter, "_route_chat", side_effect=lambda payload, body, rid: (body, None, {}, "https://synthetic.invalid")), \
             patch.object(converter, "_backend_stream", backend), patch.object(converter, "_log"), \
             patch.object(converter, "_note_cred_model_ok"):
            client = self.enterContext(TestClient(application))
            response = client.post("/v1/responses", json={
                "model": "auto", "stream": True, "input": "hi",
                "tools": [{"type": "function", "name": "synthetic_tool",
                           "parameters": {"type": "object"}}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("response.completed", response.text)
        record = store.list_records()["items"][0]
        self.assertEqual((record["outcome"], record["stream_mode"], record["total_tokens"]),
                         ("error", "realtime", 5))
        self.assertTrue(record["error_code"])


class StreamPolicyTests(unittest.TestCase):
    def test_dotenv_overrides_sqlite_and_process_environment_overrides_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".env"
            path.write_text("CODEBUDDY2API_STREAM_MODE=realtime\n", encoding="utf-8")
            store = ControlStore(root / "control.sqlite3")
            self.addCleanup(store.close)
            store.update_settings({"stream_mode": "compatible"}, store.snapshot()["revision"])
            with patch.dict(os.environ, {"CODEBUDDY2API_KEY": "synthetic-test-key"}, clear=True):
                dotenv_keys = load_startup_env(path)
                config = {"control_store": store, "settings_sources": {"api_key": "environment"}}
                apply_persisted_settings(config, environ=os.environ)
                config["state_store"] = store.state
                resolve_startup_auth(config, SimpleNamespace(api_key="synthetic-test-key"), dotenv_keys)
            item = next(value for value in resolve_settings(config) if value["key"] == "stream_mode")
            self.assertEqual((config["stream_mode"], item["source"], item["locked"]),
                             ("realtime", "dotenv", True))

            with patch.dict(os.environ, {
                    "CODEBUDDY2API_KEY": "synthetic-test-key",
                    "CODEBUDDY2API_STREAM_MODE": "compatible"}, clear=True):
                dotenv_keys = load_startup_env(path)
                config = {"control_store": store, "settings_sources": {"api_key": "environment"}}
                apply_persisted_settings(config, environ=os.environ)
                config["state_store"] = store.state
                resolve_startup_auth(config, SimpleNamespace(api_key="synthetic-test-key"), dotenv_keys)
            item = next(value for value in resolve_settings(config) if value["key"] == "stream_mode")
            self.assertEqual((config["stream_mode"], item["source"]), ("compatible", "environment"))

    def test_snapshot_freezes_mode_budget_and_aggregate_selection(self):
        with patch.dict(converter.CONFIG, {"stream_mode": "compatible", "max_collect_bytes": 17}):
            chat = converter._snapshot_stream_policy("chat", {"tools": [{}]})
            responses = converter._snapshot_stream_policy("responses", {})
            messages = converter._snapshot_stream_policy("messages", {})
            self.assertTrue(chat.aggregate and responses.aggregate)
            self.assertFalse(messages.aggregate)
            self.assertEqual(chat.max_collect_bytes, 17)
            converter.CONFIG.update(stream_mode="realtime", max_collect_bytes=99)
            self.assertEqual(chat.mode, "compatible")
            self.assertEqual(chat.max_collect_bytes, 17)
            self.assertFalse(converter._snapshot_stream_policy("responses", {}).aggregate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
