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
# 建连失败/建连超时：上游手里没有正文，open_backend_stream 会换新连接重放一次；
# 中途 reset、协议错误、以及写超时（正文没写完 ≠ 上游没处理）都属于歧义请求，默认不重放。
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


class PreflightDisconnectTests(unittest.IsolatedAsyncioTestCase):
    """预取窗口里的下游断连：取消要打得断挂起的上游读，并发名额当场归还。

    用真实的中间件顺序手工驱动 ASGI。评审 P1 的场景：预取曾在端点里直接 `await`，而
    `StreamingResponse.__call__` 是把 `stream_response` 和 `listen_for_disconnect` 放进同一个
    任务组跑的 —— 端点返回之前没有谁在消费 `http.disconnect`。于是「上游首段卡住 + 客户端已经
    走了」会一路挂到读超时，`ConcurrencyLimitMiddleware` 的名额跟着陪葬，单并发部署整个网关
    变 503。这里钉住：断连必须当场收尾（首段之前、以及已经开始流式之后两种），并且下一次请求
    拿得到名额。
    """

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
        # 名额层套在真实 app 外面（与 runtime_management.install 的层次一致）。
        # 每次新建实例：信号量挂在中间件实例上，测试之间不能互相借位。
        self.app = ConcurrencyLimitMiddleware(converter.app.build_middleware_stack(),
                                              converter.CONFIG)
        self.stuck = asyncio.Event()
        self.closed = 0
        self.mode = "before-first-segment"

    async def upstream(self, url, headers, body, model_name="?", t0=0.0, rid="", cred=None):
        """假上游：按模式卡在首段之前或之后；收尾一定要 await，跟真实 httpx 一样。"""
        try:
            if self.mode != "before-first-segment":
                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "ok"}}],
                                             "model": model_name}) + "\n\n"
            self.stuck.set()      # 「已经挂在上游上」/「首段已经交出去」的信号
            if self.mode == "done":
                yield "data: [DONE]\n\n"
                return
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            self.closed += 1

    async def drive(self, *, disconnect):
        """跑一次请求；返回 (响应状态码或 None, 断连后是否在超时内收尾)。"""
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
                if self.mode == "after-first-segment":   # 等响应头真的发出去
                    for _ in range(400):
                        if any(m["type"] == "http.response.start" for m in sent):
                            break
                        await asyncio.sleep(0.005)
                if disconnect:
                    await queue.put({"type": "http.disconnect"})
                await asyncio.wait_for(task, 2)          # 收尾不干净就会在这里超时（= 名额被占）
            finally:
                task.cancel()
        start = next((m for m in sent if m["type"] == "http.response.start"), None)
        return start["status"] if start else None, sent

    async def test_disconnect_before_first_segment_aborts_without_a_response(self):
        """首段还没来就断连：一个字节都不该发出去，上游要当场关掉。"""
        status, sent = await self.drive(disconnect=True)
        self.assertIsNone(status, f"客户端已经走了， yet 发出了响应头：{sent}")
        self.assertEqual(self.closed, 1)

    async def test_slot_is_returned_so_the_next_request_still_runs(self):
        """名额归还：断连之后紧接着的请求必须是正常响应，而不是「并发已满」的 503。"""
        await self.drive(disconnect=True)
        self.stuck = asyncio.Event()
        self.mode = "done"
        status, _ = await self.drive(disconnect=False)
        self.assertEqual(status, 200)

    async def test_disconnect_after_streaming_started_closes_the_upstream(self):
        """已经开始流式之后断连：取消照常打到挂起的上游读，生成器被关干净。"""
        self.mode = "after-first-segment"
        status, sent = await self.drive(disconnect=True)
        self.assertEqual(status, 200, sent)
        self.assertEqual(self.closed, 1)


class TeardownCloseTests(unittest.IsolatedAsyncioTestCase):
    """`_close_stream` 要扛得住「当前任务正在被反复取消」这件事。

    直接 `await agen.aclose()` 是不行的：anyio 的取消作用域用 `call_soon` 自循环，每个事件
    循环周期重投一次取消，生成器 finally 里那个 await（httpx 在这里关连接）做到一半就被打断
    —— 实测要么永远等不到 `closed`，要么半关。收尾因此放进独立任务（不属于那个作用域，没人再
    取消它），再尽量当场等它做完。这里钉住「停在 yield 上被取消」这一种：帧没在自己内部被撕开，
    正是 `_close_stream` 负责的那一段。
    """

    async def test_cleanup_await_completes_inside_a_cancelled_scope(self):
        done = []

        async def upstream():
            try:
                yield "data: x\n\n"          # 停在 yield 上被关：真实场景是「两段之间」
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
