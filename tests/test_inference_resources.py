"""Verify bounded connection reuse, identity isolation and cancellation-safe account leases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

import converter as gateway
from app import inference_resources as resources, upstream_io
from app.settings import SCHEMA, apply_persisted_settings, resolve_settings, validate_settings
import test_api_flow as fixtures

URL = "https://copilot.tencent.com/v2/chat/completions"
SSE = fixtures.fixtures.success_sse()
REAL_CLIENT = httpx.AsyncClient


class ResourceOwnershipTests(unittest.TestCase):
    def test_atomic_capacity_and_idempotent_release(self):
        capacity = resources.AccountCapacity()
        with ThreadPoolExecutor(max_workers=12) as executor:
            leases = list(executor.map(lambda _: capacity.acquire("account", 3, object(), 1), range(50)))
        accepted = [lease for lease in leases if lease is not None]
        self.assertEqual(len(accepted), 3)
        self.assertEqual(capacity.count("account"), 3)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda lease: lease.release(), accepted * 5))
        self.assertEqual(capacity.count("account"), 0)
        self.assertEqual(capacity._counts, {})

    def test_scope_releases_late_and_duplicate_leases(self):
        capacity = resources.AccountCapacity()
        scope = resources.RequestResources()
        lease = capacity.acquire("account", 1, object(), 1)
        scope.add(lease)
        scope.add(lease)
        scope.close()
        scope.close()
        self.assertEqual(capacity.count("account"), 0)
        late = capacity.acquire("account", 1, object(), 2)
        with self.assertRaises(asyncio.CancelledError):
            scope.add(late)
        self.assertEqual(capacity._counts, {})

    def test_unlimited_requests_remain_counted_when_limit_changes(self):
        capacity = resources.AccountCapacity()
        leases = [capacity.acquire("account", 0, object(), 1) for _ in range(3)]
        self.assertIsNone(capacity.acquire("account", 1, object(), 1))
        for lease in leases:
            lease.release()
        lease = capacity.acquire("account", 1, object(), 2)
        self.assertIsInstance(lease, tuple)
        self.assertEqual(lease[1], 2)
        lease.release()


class ClientPoolTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, clients=None, *, url=URL, headers=None):
        async with upstream_io.open_backend_stream(url, headers or {}, {}, clients=clients) as response:
            return await response.aread()

    async def test_clients_are_reused_without_cookie_or_authorization_bleed(self):
        seen, clients = [], []
        def handle(request):
            seen.append(request)
            return httpx.Response(200, content=SSE, headers={"Set-Cookie": "session=synthetic; Path=/"})
        def create(**kwargs):
            limits = kwargs["limits"]
            self.assertEqual((limits.max_connections, limits.max_keepalive_connections, limits.keepalive_expiry), (64, 16, 30))
            client = REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs)
            clients.append(client)
            return client
        pool = resources.UpstreamClients()
        try:
            with patch.object(httpx, "AsyncClient", side_effect=create):
                for identity in ("A", "B"):
                    await self.collect(pool, headers={"Authorization": "Bearer synthetic-" + identity,
                                                      "X-User-Id": identity, "X-Tenant-Id": identity})
                await self.collect(pool, url="https://www.workbuddy.ai/v2/chat/completions")
            self.assertEqual(len(clients), 2)
            self.assertEqual([request.headers["X-User-Id"] for request in seen[:2]], ["A", "B"])
            self.assertEqual(seen[1].headers["Authorization"], "Bearer synthetic-B")
            self.assertNotIn("Authorization", seen[2].headers)
            self.assertTrue(all("Cookie" not in request.headers for request in seen))
            self.assertTrue(all(not client.cookies for client in clients))
            self.assertTrue(all(client.trust_env for client in clients))
            self.assertTrue(all(request.extensions["timeout"] == {"connect": 15, "read": 300, "write": 60, "pool": 15}
                                for request in seen))
            self.assertIsNone(pool.get("https://untrusted.invalid/v2/chat/completions"))
        finally:
            await pool.aclose()
        self.assertTrue(all(client.is_closed for client in clients))
        self.assertIsNone(pool.get(URL))
        await pool.aclose()

    async def test_only_safe_connection_failure_uses_one_fresh_client_retry(self):
        for failure in (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                        httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout, httpx.PoolTimeout):
            with self.subTest(failure=failure.__name__):
                seen, made = [], []
                def handle(request):
                    seen.append(request)
                    if len(seen) == 1:
                        raise failure("synthetic transport failure")
                    return httpx.Response(200, content=SSE)
                def create(**kwargs):
                    client = REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs)
                    made.append(client)
                    return client
                pool = resources.UpstreamClients()
                try:
                    with patch.object(httpx, "AsyncClient", side_effect=create):
                        if failure in (httpx.ConnectError, httpx.ConnectTimeout):
                            self.assertEqual(await self.collect(pool), SSE)
                            self.assertEqual(len(made), 2)
                            self.assertTrue(made[1].is_closed)
                        else:
                            with self.assertRaises(failure):
                                await self.collect(pool)
                            self.assertEqual(len(made), 1)
                        self.assertFalse(made[0].is_closed)
                finally:
                    await pool.aclose()

    async def test_cancellation_closes_response_but_preserves_pool_for_next_request(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        class HangingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""
            async def aclose(self):
                closed.set()
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, stream=HangingBody()) if len(calls) == 1 else httpx.Response(200, content=SSE)
        pool = resources.UpstreamClients()
        try:
            with patch.object(httpx, "AsyncClient", side_effect=lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handle), **kw)):
                task = asyncio.create_task(self.collect(pool))
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(closed.is_set())
                self.assertEqual(await self.collect(pool), SSE)
                self.assertEqual(len(pool._clients), 1)
        finally:
            await pool.aclose()

    async def test_real_http_connections_are_reused_only_when_enabled(self):
        accepted, writers, handlers = [], [], []
        async def handle(reader, writer):
            accepted.append(writer)
            writers.append(writer)
            handlers.append(asyncio.current_task())
            try:
                while True:
                    headers = await reader.readuntil(b"\r\n\r\n")
                    length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:"))
                    await reader.readexactly(length)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(SSE)).encode() + b"\r\n\r\n" + SSE)
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            with patch.dict(resources.PROFILE_ENDPOINTS, {"fixture": base}, clear=True), patch.dict(os.environ, {"NO_PROXY": "127.0.0.1"}):
                pool = resources.UpstreamClients()
                try:
                    for _ in range(2):
                        self.assertEqual(await self.collect(pool, url=base), SSE)
                    self.assertEqual(len(accepted), 1)
                finally:
                    await pool.aclose()
                for _ in range(2):
                    self.assertEqual(await self.collect(url=base), SSE)
                self.assertEqual(len(accepted), 3)
        finally:
            server.close()
            await server.wait_closed()
            for writer in writers:
                writer.close()
            await asyncio.wait_for(asyncio.gather(*handlers), 2)

    async def test_lifespan_closes_clients_and_never_reuses_a_previous_owner(self):
        async with resources.inference_lifespan(None) as state:
            first = state["upstream_clients"]
            self.assertIsInstance(first, resources.UpstreamClients)
        self.assertTrue(first._closed)
        async with resources.inference_lifespan(None) as state:
            self.assertIsNot(first, state["upstream_clients"])


class PoolCapacityTests(fixtures.GatewayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(gateway.CONFIG, {"max_inflight_per_account": 1}))
        self.fx.configure(profiles=("cn-cli", "cn-work"))

    def acquire(self, session="same-session"):
        picked = self.fx.pool.headers_for(session, "shared-model", with_generation=True, with_capacity=True)
        self.assertIsNotNone(picked)
        lease, _ = picked
        self.addCleanup(lease.release)
        return lease

    def test_busy_sticky_and_early_expiry_account_spills_within_same_cost_tier(self):
        with patch.object(self.fx.pool, "_expiry_rank", side_effect=lambda e: (False, 1 if e["uid"] == "cn-cli" else 2)):
            first, second = self.acquire(), self.acquire()
            self.assertEqual(first[0], self.fx.entries["cn-cli"]["cm"])
            self.assertEqual(second[0], self.fx.entries["cn-work"]["cm"])
            with self.assertRaises(HTTPException) as error:
                self.acquire()
            self.assertEqual(error.exception.status_code, 503)
            self.assertEqual(error.exception.headers["Retry-After"], "3")
            self.assertEqual(error.exception.detail["error"]["code"], "credential_concurrency_limit")
            first.release()
            self.assertIs(self.acquire()[0], first[0])

    def test_full_free_tier_does_not_fall_through_to_paid_accounts(self):
        self.fx.account_catalogs({
            "cn-cli": [fixtures.fixtures.model("shared-model", "x0.00")],
            "cn-work": [fixtures.fixtures.model("shared-model", "x1.00")]})
        self.acquire()
        with self.assertRaises(HTTPException) as error:
            self.acquire("different-session")
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(self.fx.pool._capacity.count(self.fx.entries["cn-work"]["account_key"]), 0)

    def test_strict_binding_remains_strict_when_its_account_is_full(self):
        identity = self.fx.entries["cn-cli"]["account_key"]
        store = SimpleNamespace(snapshot=lambda: {"revision": 1, "credentials": {},
                                "models": {"shared-model": {"credential_ids": [identity]}}})
        with patch.dict(gateway.CONFIG, {"control_store": store}):
            self.acquire()
            with self.assertRaises(HTTPException) as error:
                self.acquire("different-session")
            self.assertEqual(error.exception.detail["error"]["code"], "credential_concurrency_limit")

    def test_atomic_header_reservations_never_overbook_accounts(self):
        def attempt(index):
            try:
                return self.fx.pool.headers_for(str(index), "shared-model", with_generation=True, with_capacity=True)[0]
            except HTTPException as error:
                self.assertEqual(error.status_code, 503)
                return None
        with ThreadPoolExecutor(max_workers=12) as executor:
            leases = [lease for lease in executor.map(attempt, range(30)) if lease is not None]
        try:
            self.assertEqual(len(leases), 2)
            rows = self.fx.pool.snapshot()
            self.assertTrue(all(row["in_flight"] == row["max_in_flight"] == 1 for row in rows))
        finally:
            for lease in leases:
                lease.release()
        self.assertEqual(self.fx.pool._capacity._counts, {})

    def test_reimport_or_delete_does_not_reset_existing_account_capacity(self):
        self.fx.configure(profiles=("cn-cli",))
        lease = self.acquire()
        cm = lease[0]
        cm.invalidate()
        self.fx.pool.reload([cm.path])
        with self.assertRaises(HTTPException):
            self.acquire()
        content = cm.path.read_bytes()
        self.fx.pool.remove_file(cm.path.name)
        cm.path.write_bytes(content)
        self.fx.pool.reload([cm.path])
        with self.assertRaises(HTTPException):
            self.acquire()
        lease.release()
        self.assertIsNotNone(self.acquire())


class RequestCapacityTests(fixtures.GatewayFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(gateway.CONFIG, {"max_inflight_per_account": 1}))
        self.fx.configure(profiles=("cn-cli",))

    async def test_gateway_reuses_clients_across_protocols_and_same_origin_accounts(self):
        self.fx.add_account("account-B", "cn-cli")
        self.fx.allowed_profiles.add("account-B")
        self.fx.configure(profiles=("cn-cli", "account-B"))
        factory = httpx.AsyncClient
        before = factory.call_count
        with patch.dict(gateway.CONFIG, {"upstream_keepalive": True}):
            for route in fixtures.fixtures.GENERATIONS:
                for stream in (False, True):
                    response = self.fx.client.post("/v1/" + route, json=self.fx.payload(route, stream=stream))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(self.fx.pool._capacity._counts, {})
        self.assertEqual(factory.call_count - before, 1)
        self.assertEqual({request.headers["X-User-Id"] for request in self.fx.requests}, {"cn-cli", "account-B"})

    async def test_disabled_keepalive_preserves_fresh_client_behavior(self):
        factory = httpx.AsyncClient
        before = factory.call_count
        for _ in range(2):
            response = self.fx.client.post("/v1/responses", json=self.fx.payload("responses"))
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(factory.call_count - before, 2)
        self.assertEqual(self.fx.pool._capacity._counts, {})


    async def test_full_account_rejects_then_recovers_after_cancellation(self):
        for route in fixtures.fixtures.GENERATIONS:
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    entered, closed = asyncio.Event(), asyncio.Event()
                    class HangingBody(httpx.AsyncByteStream):
                        async def __aiter__(self):
                            entered.set()
                            await asyncio.Event().wait()
                            yield b""
                        async def aclose(self):
                            closed.set()
                    with self.responder(lambda request: httpx.Response(200, stream=HangingBody())):
                        async with REAL_CLIENT(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test") as client:
                            task = asyncio.create_task(client.post("/v1/" + route, json=self.fx.payload(route, stream=stream)))
                            try:
                                await asyncio.wait_for(entered.wait(), 2)
                                second = await client.post("/v1/" + route, json=self.fx.payload(route, stream=stream))
                                self.assertEqual(second.status_code, 503, second.text)
                                self.assertEqual(second.headers["Retry-After"], "3")
                                self.assertEqual(second.json()["error"]["code"], "credential_concurrency_limit")
                            finally:
                                task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await task
                    self.assertTrue(closed.is_set())
                    self.assertEqual(self.fx.pool._capacity._counts, {})
                    response = self.fx.client.post("/v1/" + route, json=self.fx.payload(route, stream=stream))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(self.fx.pool._capacity._counts, {})

    async def test_late_worker_reservation_after_disconnect_is_released_without_upstream(self):
        original = gateway._route_chat
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()
        with patch.object(gateway, "_route_chat", side_effect=delayed):
            async with REAL_CLIENT(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test") as client:
                task = asyncio.create_task(client.post("/v1/responses", json=self.fx.payload("responses")))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
                    self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual(self.fx.pool._capacity._counts, {})
        self.assertEqual(self.fx.requests, [])

    async def test_failover_releases_previous_capacity_before_next_attempt(self):
        for route in fixtures.fixtures.GENERATIONS:
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    self.fx.configure(profiles=("cn-cli", "cn-work"))
                    seen = []
                    def respond(request):
                        seen.append(request)
                        self.assertEqual(sum(self.fx.pool._capacity._counts.values()), 1)
                        return httpx.Response(503, json={"error": {"message": "synthetic failure"}}) if len(seen) == 1 else httpx.Response(200, content=SSE)
                    with self.responder(respond), patch.dict(gateway.CONFIG, {"failover_max": 1}):
                        response = self.fx.client.post("/v1/" + route, json=self.fx.payload(route, stream=stream))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(len(seen), 2)
                    self.assertEqual(self.fx.pool._capacity._counts, {})


class ConfigurationTests(unittest.TestCase):
    def test_defaults_precedence_validation_and_ui_modes(self):
        self.assertFalse(SCHEMA["upstream_keepalive"]["default"])
        self.assertEqual(SCHEMA["max_inflight_per_account"]["default"], 0)
        store = SimpleNamespace(snapshot=lambda: {"settings": {"upstream_keepalive": True, "max_inflight_per_account": 2}})
        env = {"CODEBUDDY2API_UPSTREAM_KEEPALIVE": "false", "CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT": "3"}
        config = {"control_store": store, "max_inflight_per_account": 1}
        apply_persisted_settings(config, explicit=("max_inflight_per_account",), environ=env)
        self.assertEqual((config["upstream_keepalive"], config["max_inflight_per_account"]), (False, 1))
        items = {item["key"]: item for item in resolve_settings(config)}
        self.assertEqual(items["upstream_keepalive"]["mode"], "restart")
        self.assertTrue(items["upstream_keepalive"]["locked"])
        self.assertEqual(items["max_inflight_per_account"]["mode"], "hot")
        for values in ({"max_inflight_per_account": -1}, {"max_inflight_per_account": True}, {"upstream_keepalive": "true"}):
            with self.assertRaises(ValueError):
                validate_settings(values)


if __name__ == "__main__":
    unittest.main()
