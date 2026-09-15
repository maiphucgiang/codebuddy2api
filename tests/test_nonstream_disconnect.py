#!/usr/bin/env python3
"""Test non-streaming disconnect cancellation, capacity release and auditing across all protocols."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

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
# Disconnected clients must not receive a successful inference response.
QUIET_STATUSES = (None, 204)
STEP = 2
ENDPOINTS = fixtures.GENERATIONS
FAILOVER_LOG = "换凭证重放"


def error_body(message="synthetic rejection", code="rate_limit"):
    return {"error": {"message": message, "type": "upstream_error", "code": code}}


@contextmanager
def allow_failover(times: int):
    """Enable the requested credential failover budget."""
    with patch.dict(converter.CONFIG, {"failover_max": times}):
        yield


class _HangingStream(httpx.AsyncByteStream):
    """Keep upstream SSE pending and independently record read cancellation and connection closure."""

    def __init__(self, read_cancelled, closed):
        self.read_cancelled = read_cancelled
        self.closed = closed

    async def __aiter__(self):
        try:
            await asyncio.Event().wait()        # Keep the first upstream segment pending.
            yield b""
        finally:
            self.read_cancelled.set()

    async def aclose(self):
        self.closed.set()
        await super().aclose()


class NonStreamDisconnectTests(fixtures.RegionRoutingTests):
    """Drive ASGI directly to control downstream disconnect timing."""

    def setUp(self):
        super().setUp()
        self.allowed_profiles = set(fixtures.PROFILES)
        self.reset_upstream()

    def reset_upstream(self):
        """Reset upstream state independently for each protocol subtest."""
        self.upstream_seen = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.hang_attempts = 0
        self.hang_uids = []
        self.fail_over_first = False

    # Inject a pending upstream read, optionally preceded by a quota rejection.
    def handle_upstream(self, request):
        if HANG_MARKER in request.content.decode("utf-8", "replace"):
            self.hang_attempts += 1
            self.hang_uids.append(request.headers.get("x-user-id"))
            if self.hang_attempts == 1 and self.fail_over_first:
                return httpx.Response(429, json=error_body(),
                                      headers={"content-type": "application/json"})
            self.upstream_seen.set()
            # stream= preserves the custom aclose callback without wrapping the content.
            return httpx.Response(200, stream=_HangingStream(self.read_cancelled,
                                                             self.stream_closed),
                                  headers={"content-type": "text/event-stream"})
        return super().handle_upstream(request)

    def gated(self, limit, store=None):
        """Create a private concurrency gate so test cases cannot share admission state."""
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
        """Provide a normal request body and a controllable disconnect event."""
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
        """Disconnect after the non-streaming request blocks on upstream output."""
        hangup = hangup or asyncio.Event()
        task = asyncio.ensure_future(
            gate(self.scope(endpoint), self.receive(self.hang_payload(endpoint), hangup),
                 self.collect(sent)))
        await asyncio.wait_for(self.upstream_seen.wait(), STEP)
        self.assertFalse(self.read_cancelled.is_set(), "客户端还没走，这枪不该结束")
        hangup.set()
        await asyncio.wait_for(task, STEP)       # Disconnect must finish within the deadline.
        return task

    async def succeed_after(self, gate, endpoint, sent):
        """Verify a subsequent request can reuse the released concurrency slot."""
        await asyncio.wait_for(
            gate(self.scope(endpoint), self.receive(self.payload(endpoint), asyncio.Event()),
                 self.collect(sent)), STEP)

    def store_for(self, name):
        store = AuditStore(self.root / (name + ".sqlite3"))
        self.addCleanup(store.close)
        return store

    # Non-streaming disconnects across all protocols
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

    # Failover remains cancellable as one request.
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

    # Caller cancellation must fully close upstream resources.
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

    # Connected clients continue to receive complete responses.
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

    # Streaming disconnect control case
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
