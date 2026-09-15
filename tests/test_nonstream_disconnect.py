#!/usr/bin/env python3
"""`stream=false` 的聚合窗口监听下游断连：取消上游、归还名额、审计如实，三协议一致。

流式端点由 Starlette 的 `listen_for_disconnect` 兜住，非流式端点没有对应机制。这里钉住
聚合路径的边界，并覆盖换凭证重放途中挂断、以及调用方自己取消外层任务两种情形。

运行：.venv/bin/python -B -m unittest -v tests/test_nonstream_disconnect.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import asyncio
import json
import unittest
from contextlib import contextmanager, suppress
from unittest.mock import patch

import httpx
from fastapi import FastAPI

import converter
from app.audit_store import AuditStore
from app.inbound_limits import ConcurrencyLimitMiddleware
from app.observability import AuditMiddleware
from tests import test_region_routing as fixtures

HANG_MARKER = "synthetic-hang-up"
# 客户端已经不在了：唯一不可接受的是一个看起来正常的推理响应
QUIET_STATUSES = (None, 204)
STEP = 2
ENDPOINTS = fixtures.GENERATIONS
FAILOVER_LOG = "换凭证重放"


def error_body(message="synthetic rejection", code="rate_limit"):
    return {"error": {"message": message, "type": "upstream_error", "code": code}}


@contextmanager
def allow_failover(times: int):
    """打开换凭证重放开关（等价于 --failover-max N）。"""
    with patch.dict(converter.CONFIG, {"failover_max": times}):
        yield


class _HangingStream(httpx.AsyncByteStream):
    """永不结束的上游 SSE：占住整个聚合窗口。

    两个独立标记：`read_cancelled` 只在 `__aiter__` 的 `finally` 置位，证明读取被取消；
    `stream_closed` 由 httpx 的 `Response.aclose()` 调到底，证明连接真的还回去了。
    """

    def __init__(self, read_cancelled, closed):
        self.read_cancelled = read_cancelled
        self.closed = closed

    async def __aiter__(self):
        try:
            await asyncio.Event().wait()        # 第一段永远不来，等价于上游卡住
            yield b""
        finally:
            self.read_cancelled.set()

    async def aclose(self):
        self.closed.set()
        await super().aclose()


class NonStreamDisconnectTests(fixtures.RegionRoutingTests):
    """自己驱动 ASGI：需要一个能随时吐出 `http.disconnect` 的 receive，TestClient 给不了。"""

    def setUp(self):
        super().setUp()
        self.allowed_profiles = set(fixtures.PROFILES)
        self.reset_upstream()

    def reset_upstream(self):
        """每个协议子例各自从干净的上游状态起（subTest 共用同一次 setUp）。"""
        self.upstream_seen = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.hang_attempts = 0
        self.hang_uids = []
        self.fail_over_first = False

    # --- 上游夹具：带标记的那一枪卡在首段之前；被点名时先让第一枪吃 429 ---
    def handle_upstream(self, request):
        if HANG_MARKER in request.content.decode("utf-8", "replace"):
            self.hang_attempts += 1
            self.hang_uids.append(request.headers.get("x-user-id"))
            if self.hang_attempts == 1 and self.fail_over_first:
                return httpx.Response(429, json=error_body(),
                                      headers={"content-type": "application/json"})
            self.upstream_seen.set()
            # 用 stream= 而不是 content=：后者会把流再包一层，自定义 aclose 收不到关闭回调
            return httpx.Response(200, stream=_HangingStream(self.read_cancelled,
                                                             self.stream_closed),
                                  headers={"content-type": "text/event-stream"})
        return super().handle_upstream(request)

    # --- 驱动 ---
    def gated(self, limit, store=None):
        """本用例私有的名额闸：`converter.app` 自带的那个跨用例复用，会把状态串到别的测试上。"""
        application = converter.app
        if store is not None:
            application = FastAPI()
            application.router.routes = list(converter.app.router.routes)
            application.add_middleware(AuditMiddleware, {"audit_store": store})
        return ConcurrencyLimitMiddleware(application, {"max_concurrent": limit})

    def scope(self, endpoint="chat/completions"):
        path = "/v1/" + endpoint
        return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "server": ("testserver", 80), "client": ("127.0.0.1", 55555),
                "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")]}

    def receive(self, payload, hangup):
        """一份正常请求体；之后把 `http.disconnect` 攥在手里，等这个「客户端」真的挂断。"""
        pending = [{"type": "http.request", "body": json.dumps(payload).encode(),
                    "more_body": False}]

        async def receive():
            if pending:
                return pending.pop(0)
            await hangup.wait()
            return {"type": "http.disconnect"}
        return receive

    @staticmethod
    def collect(sent):
        async def send(message):
            sent.append(message)
        return send

    def started_statuses(self, sent):
        return [m.get("status") for m in sent if m.get("type") == "http.response.start"]

    def hang_payload(self, endpoint="chat/completions", *, stream=False):
        return self.payload(endpoint, text=HANG_MARKER, stream=stream)

    async def drain_records(self, store, expected=1):
        for _ in range(int(STEP * 20)):
            records = store.list_records()["items"]
            if len(records) >= expected:
                return records
            await asyncio.sleep(0.05)
        self.fail(f"审计没有落库：{store.list_records()}")

    async def hang_up(self, gate, endpoint, sent, hangup=None):
        """发出聚合请求，等它卡在上游之后再让「客户端」挂断。"""
        hangup = hangup or asyncio.Event()
        task = asyncio.ensure_future(
            gate(self.scope(endpoint), self.receive(self.hang_payload(endpoint), hangup),
                 self.collect(sent)))
        await asyncio.wait_for(self.upstream_seen.wait(), STEP)
        self.assertFalse(self.read_cancelled.is_set(), "客户端还没走，这枪不该结束")
        hangup.set()
        await asyncio.wait_for(task, STEP)       # 修复前：没人监听断连，这一句必然超时
        return task

    async def succeed_after(self, gate, endpoint, sent):
        """同一个名额闸上再打一发正常请求：名额必须已经还给网关。"""
        await asyncio.wait_for(
            gate(self.scope(endpoint), self.receive(self.payload(endpoint), asyncio.Event()),
                 self.collect(sent)), STEP)

    def store_for(self, name):
        store = AuditStore(self.root / (name + ".sqlite3"))
        self.addCleanup(store.close)
        return store

    # --- 盲区本体：三个协议的聚合端点同一口径 ---
    def test_hangup_during_aggregation_cancels_and_closes_the_upstream_call(self):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.reset_upstream()
                sent = []

                async def scenario():
                    await self.hang_up(self.gated(1), endpoint, sent)

                asyncio.run(scenario())
                self.assertTrue(self.read_cancelled.is_set(), "挂断之后，挂着的上游读要被取消")
                self.assertTrue(self.stream_closed.is_set(),
                                "响应的异步 aclose() 也要跑完，连接才不留半开")
                statuses = self.started_statuses(sent)
                self.assertTrue(all(status in QUIET_STATUSES for status in statuses), statuses)

    def test_hangup_is_audited_as_cancelled_and_frees_the_slot(self):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.reset_upstream()
                store = self.store_for("hangup-" + endpoint)

                hung_up = []

                async def scenario():
                    gate = self.gated(1, store)
                    await self.hang_up(gate, endpoint, [])
                    hung_up.append((await self.drain_records(store))[0])
                    await self.succeed_after(gate, endpoint, [])

                asyncio.run(scenario())
                record = hung_up[0]
                self.assertEqual(record["outcome"], "cancelled", record)
                self.assertFalse(record["streaming"], record)

    # --- 换凭证重放途中挂断：整段重放是一个可取消单元 ---
    def test_hangup_after_credential_failover_cancels_the_whole_sequence(self):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.reset_upstream()
                self.fail_over_first = True
                store = self.store_for("failover-" + endpoint)
                sent, follow_up, hung_up = [], [], []

                async def scenario():
                    gate = self.gated(1, store)
                    with allow_failover(1):
                        await self.hang_up(gate, endpoint, sent)
                        hung_up.append((await self.drain_records(store))[0])
                    await self.succeed_after(gate, endpoint, follow_up)

                asyncio.run(scenario())
                record = hung_up[0]
                self.assertEqual(self.hang_attempts, 2, "第一枪 429 之后应恰好换凭证重放一次")
                self.assertEqual(len(set(self.hang_uids)), 2,
                                 "重放必须换凭证：" + str(self.hang_uids))
                self.assertTrue(self.read_cancelled.is_set())
                self.assertTrue(self.stream_closed.is_set(), "取消要等重放那一枪的响应也关干净")
                self.assertTrue(all(status in QUIET_STATUSES
                                    for status in self.started_statuses(sent)), sent)
                self.assertEqual(record["outcome"], "cancelled", record)
                self.assertNotIn("failover_recovered", json.dumps(record["attempts"] or []),
                                 "没换回结果就不能标成已恢复")
                self.assertEqual(self.started_statuses(follow_up), [200],
                                 "挂断之后名额必须立刻可用")

    # --- 调用方自己取消外层任务：同样不许把上游留在半关状态 ---
    def test_outer_task_cancellation_cancels_and_closes_the_upstream_call(self):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.reset_upstream()
                store = self.store_for("outer-" + endpoint)
                sent, follow_up, hung_up = [], [], []

                async def scenario():
                    gate = self.gated(1, store)
                    task = asyncio.ensure_future(
                        gate(self.scope(endpoint),
                             self.receive(self.hang_payload(endpoint), asyncio.Event()),
                             self.collect(sent)))
                    await asyncio.wait_for(self.upstream_seen.wait(), STEP)
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    hung_up.append((await self.drain_records(store))[0])
                    await self.succeed_after(gate, endpoint, follow_up)

                asyncio.run(scenario())
                self.assertTrue(self.read_cancelled.is_set(), "外层取消要传到挂着的上游读")
                self.assertTrue(self.stream_closed.is_set())
                self.assertEqual(hung_up[0]["outcome"], "cancelled", hung_up[0])
                self.assertEqual(self.started_statuses(follow_up), [200], follow_up)

    # --- 对照一：客户端没走，聚合请求必须照常完成（新监听不许误伤） ---
    def test_aggregation_completes_while_the_client_is_still_there(self):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.reset_upstream()
                sent = []

                async def scenario():
                    await asyncio.wait_for(
                        self.gated(4)(self.scope(endpoint),
                                      self.receive(self.payload(endpoint), asyncio.Event()),
                                      self.collect(sent)), STEP)

                asyncio.run(scenario())
                self.assertEqual(self.started_statuses(sent), [200], sent)
                self.assertIn(b"ok", b"".join(m.get("body", b"") for m in sent))
                self.assertFalse(self.read_cancelled.is_set(), "正常请求不该被断连监听打断")

    # --- 对照二：流式的同一场景在修复之前就已经成立 ---
    def test_streaming_hangup_already_closes_the_upstream(self):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.reset_upstream()
                sent = []

                async def scenario():
                    gate = self.gated(1)
                    hangup = asyncio.Event()
                    task = asyncio.ensure_future(
                        gate(self.scope(endpoint),
                             self.receive(self.hang_payload(endpoint, stream=True), hangup),
                             self.collect(sent)))
                    await asyncio.wait_for(self.upstream_seen.wait(), STEP)
                    hangup.set()
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait_for(task, STEP)

                asyncio.run(scenario())
                self.assertTrue(self.read_cancelled.is_set(),
                                "Starlette 的断连监听管的就是这一段")
                self.assertIn(self.started_statuses(sent), ([], [200]), sent)
                self.assertNotIn(b"[DONE]", b"".join(m.get("body", b"") for m in sent))


if __name__ == "__main__":
    unittest.main()
