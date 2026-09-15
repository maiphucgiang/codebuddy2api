#!/usr/bin/env python3
"""下游断连在 `stream=false` 的聚合窗口里没人监听：上游要被取消、名额要归还、审计要如实。

流式端点有这层保护：Starlette 的 `StreamingResponse.__call__` 在 `spec_version < 2.4`（uvicorn
的两个 HTTP 协议都是 2.3）把 `stream_response` 和 `listen_for_disconnect` 放进同一个任务组，
客户端走了，挂着的那次上游读就一起被取消。非流式端点没有对应机制 —— 端点直接 `await` 完整
聚合，拿到 `JSONResponse` 之后才第一次 `send`，于是整个聚合窗口里没有任何人消费
`http.disconnect`：客户端已经挂断，这枪上游照旧跑完（最长到 300s 读超时），额度照旧烧，
`ConcurrencyLimitMiddleware` 的名额照旧占着（名额满了网关就对所有人回 503），审计照旧记成
`outcome=success`，因为连 `send` 都是往一个已经死掉的连接里写。

这里钉住：
  - 挂断之后，聚合中的那次上游读确实被取消；
  - 取消之后并发名额立刻可用，而不是继续把新请求挡在 503；
  - 挂断的请求审计为 `cancelled`，不是 `success`；
  - 两条对照：客户端没走的聚合请求照常完成（不许误伤），流式的同一场景本来就成立。
运行：.venv/bin/python -B -m unittest -v tests/test_nonstream_disconnect.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import asyncio
import json
import unittest
from contextlib import suppress

import httpx
from fastapi import FastAPI

import converter
from app.audit_store import AuditStore
from app.inbound_limits import ConcurrencyLimitMiddleware
from app.observability import AuditMiddleware
from tests import test_region_routing as fixtures

HANG_MARKER = "synthetic-hang-up"
# 客户端已经不在了，回什么都只是往死连接里写；什么都不发或一个不成体的 204 都可以接受，
# 唯一不可接受的是一个看起来正常的推理响应。
QUIET_STATUSES = (None, 204)
STEP = 2


class _HangingBody(httpx.AsyncByteStream):
    """永不结束的上游 SSE：占住整个聚合窗口，并报告自己有没有真的被取消。"""

    def __init__(self, closed):
        self.closed = closed

    async def __aiter__(self):
        try:
            await asyncio.Event().wait()        # 第一段永远不来，等价于上游卡住
            yield b""
        finally:
            self.closed.set()


class NonStreamDisconnectTests(fixtures.RegionRoutingTests):
    """自己驱动 ASGI：需要一个能随时吐出 `http.disconnect` 的 receive，TestClient 给不了。"""

    def setUp(self):
        super().setUp()
        self.allowed_profiles = set(fixtures.PROFILES)
        self.upstream_seen = asyncio.Event()
        self.upstream_closed = asyncio.Event()

    # --- 上游夹具：带标记的那一枪卡在首段之前 ---
    def handle_upstream(self, request):
        if HANG_MARKER in request.content.decode("utf-8", "replace"):
            self.upstream_seen.set()
            return httpx.Response(200, content=_HangingBody(self.upstream_closed),
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

    def scope(self, path="/v1/chat/completions"):
        return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "server": ("testserver", 80), "client": ("127.0.0.1", 55555),
                "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")]}

    def receive(self, payload, hangup):
        """一份正常请求体；之后把 `http.disconnect` 攥在手里，等这个「客户端」真的挂断。"""
        pending = [{"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}]

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

    def hang_payload(self, *, stream=False):
        return self.payload(text=HANG_MARKER, stream=stream)

    async def drain_records(self, store, expected=1):
        for _ in range(int(STEP * 20)):
            records = store.list_records()["items"]
            if len(records) >= expected:
                return records
            await asyncio.sleep(0.05)
        self.fail(f"审计没有落库：{store.list_records()}")

    # --- 盲区本体 ---
    def test_hangup_during_aggregation_cancels_the_upstream_call(self):
        sent, hangup = [], asyncio.Event()

        async def scenario():
            gate = self.gated(1)
            task = asyncio.ensure_future(
                gate(self.scope(), self.receive(self.hang_payload(), hangup), self.collect(sent)))
            await asyncio.wait_for(self.upstream_seen.wait(), STEP)
            self.assertFalse(self.upstream_closed.is_set(), "客户端还没走，这枪不该结束")
            hangup.set()
            await asyncio.wait_for(task, STEP)      # 修复前：没人监听断连，这一句必然超时

        asyncio.run(scenario())
        self.assertTrue(self.upstream_closed.is_set(), "挂断之后，挂着的上游读要被取消")
        statuses = self.started_statuses(sent)
        self.assertTrue(all(status in QUIET_STATUSES for status in statuses), statuses)

    def test_hangup_frees_the_slot_that_the_dead_client_held(self):
        """一个已经没有客户端的请求，不许把唯一的并发名额守成 503。"""
        seen = {}

        async def scenario():
            gate = self.gated(1)
            hangup = asyncio.Event()
            dead = asyncio.ensure_future(
                gate(self.scope(), self.receive(self.hang_payload(), hangup),
                     self.collect(seen.setdefault("dead", []))))
            await asyncio.wait_for(self.upstream_seen.wait(), STEP)

            refused = []
            await asyncio.wait_for(gate(self.scope(), self.receive(self.payload(), asyncio.Event()),
                                        self.collect(refused)), STEP)
            self.assertEqual(self.started_statuses(refused), [503], refused)
            self.assertIn(b"concurrency_limit",
                          b"".join(m.get("body", b"") for m in refused))

            hangup.set()
            await asyncio.wait_for(dead, STEP)      # 修复前：这个任务永不结束，卡在名额上

            recovered = []
            await asyncio.wait_for(gate(self.scope(), self.receive(self.payload(), asyncio.Event()),
                                        self.collect(recovered)), STEP)
            seen["recovered"] = self.started_statuses(recovered)

        asyncio.run(scenario())
        self.assertEqual(seen["recovered"], [200], "挂断之后名额必须立刻可用")

    def test_hangup_is_audited_as_cancelled_not_success(self):
        """审计要分得清「模型答完了」和「没人听」：后者不能记成一次成功推理。"""
        store = AuditStore(self.root / "hangup-audit.sqlite3")
        self.addCleanup(store.close)
        hangup = asyncio.Event()

        async def scenario():
            gate = self.gated(1, store)
            task = asyncio.ensure_future(
                gate(self.scope(), self.receive(self.hang_payload(), hangup), self.collect([])))
            await asyncio.wait_for(self.upstream_seen.wait(), STEP)
            hangup.set()
            await asyncio.wait_for(task, STEP)
            await self.drain_records(store)

        asyncio.run(scenario())
        record = store.list_records()["items"][0]
        self.assertEqual(record["outcome"], "cancelled", record)

    # --- 对照一：客户端没走，聚合请求必须照常完成（新监听不许误伤） ---
    def test_aggregation_completes_while_the_client_is_still_there(self):
        sent = []

        async def scenario():
            gate = self.gated(4)
            await asyncio.wait_for(gate(self.scope(), self.receive(self.payload(), asyncio.Event()),
                                        self.collect(sent)), STEP)

        asyncio.run(scenario())
        self.assertEqual(self.started_statuses(sent), [200], sent)
        body = b"".join(m.get("body", b"") for m in sent)
        self.assertIn(b"ok", body)
        self.assertFalse(self.upstream_closed.is_set() and not sent, "正常请求不该被断连监听打断")

    # --- 对照二：流式的同一场景在修复之前就已经成立 ---
    def test_streaming_hangup_already_closes_the_upstream(self):
        sent, hangup = [], asyncio.Event()

        async def scenario():
            gate = self.gated(1)
            task = asyncio.ensure_future(
                gate(self.scope(), self.receive(self.hang_payload(stream=True), hangup),
                     self.collect(sent)))
            await asyncio.wait_for(self.upstream_seen.wait(), STEP)
            hangup.set()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, STEP)

        asyncio.run(scenario())
        self.assertTrue(self.upstream_closed.is_set(), "Starlette 的断连监听管的就是这一段")
        # 挂断落在第一个字节之前就不该有任何响应头（#18 之后如此，之前是已经开了头的 200）；
        # 两种都算对，唯一不可接受的是把一个说完了的流交给已经不存在的客户端。
        self.assertIn(self.started_statuses(sent), ([], [200]), sent)
        self.assertNotIn(b"[DONE]", b"".join(m.get("body", b"") for m in sent))


if __name__ == "__main__":
    unittest.main()
