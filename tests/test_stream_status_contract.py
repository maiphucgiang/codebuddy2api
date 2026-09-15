#!/usr/bin/env python3
"""流式状态码契约回归：上游在落第一个字节之前就把请求判死时，不许回 HTTP 200。

下游（Codex CLI、Claude Code、任意 OpenAI 兼容 SDK）是按 chunk / 事件解析的：一个既没有
choices、也等不到 response.completed 的 200 流会被读成「模型答了个空」，会话静默结束 ——
既不重试也不报错，审计里还记成一次成功。StreamingResponse 一旦被迭代就把响应头发出去，
而打上游发生在生成器里面，所以过去只有 stream=false 才吃得到真实状态码。

这里钉住修好的口径：**同一个失败，流式与非流式必须给同一个状态码**；而真的中途断流
（字节已经发出去了）仍然只能在流内报错，收不回状态码。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import json
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import converter
from app import upstream_io

ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
TOOLS = [{"type": "function", "function": {"name": "synthetic_tool", "parameters": {"type": "object"}}}]
# 写超时/建连失败时上游还没收下请求体，open_backend_stream 会换新连接重放一次；
# 中途 reset 与协议错误属于「歧义请求」，绝不重放 POST。
REPLAYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.WriteTimeout)
AMBIGUOUS = (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.WriteError)
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

    # --- 上游用 HTTP 状态码说的不：流式必须原样转出去 ---
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

    # --- 传输层失败（写超时/中途 reset）：非流式一直是 502，流式过去是 200 ---
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

    # --- 一个字节都没发出去时，不得留下「看起来成功」的流 ---
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

    # --- 已经吐过字节的中途断流：状态码收不回来，只能在流内报错（不许过度修正）---
    def test_break_after_first_byte_stays_in_band(self):
        class Partial(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                raise httpx.ReadError("synthetic mid-stream reset")

        for route in ROUTES:
            if route == "/v1/responses":
                continue   # Responses 端点总是先聚合再落字节，失败时一个字节都没发出去 → 502
            with self.subTest(route=route):
                self.respond = lambda request: httpx.Response(200, stream=Partial(),
                                                              headers={"content-type": "text/event-stream"})
                response = self.post(route, stream=True)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("ReadError", response.text)
                self.assertIn("partial", response.text)
                self.assertNotIn("data: [DONE]", response.text)
                self.assertNotIn('"message_stop"', response.text)

    # --- 成功路径不能被预取吞掉第一段 ---
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
