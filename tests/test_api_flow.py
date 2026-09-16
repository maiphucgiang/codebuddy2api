"""Keep request preparation responsive and preserve retry hints and explicit cache keys."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from copy import deepcopy
from email.utils import formatdate
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx

import converter as gateway
from app import upstream_io
from app.adapters.responses_adapter import responses_request_to_chat
import test_region_routing as fixtures

REAL_ASYNC_CLIENT = httpx.AsyncClient


class RetryAfterParsingTests(unittest.TestCase):
    def test_standard_seconds_and_http_dates(self):
        now = 1_700_000_000
        cases = (("0", 0), ("2", 2), (" 17 ", 17), ("86400", 86400),
                 (formatdate(now + 30, usegmt=True), 30))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(upstream_io.parse_retry_after(value, now=now), expected)
        self.assertEqual(upstream_io.parse_retry_after(formatdate(now + 1, usegmt=True),
                                                       now=now + 0.25), 1)

    def test_invalid_expired_and_excessive_values_are_ignored(self):
        now = 1_700_000_000
        values = (None, "", "-1", "+2", "1.5", "nan", "inf", "two", "2, 3", "86401",
                  "9" * 200, "２", "2\r\nSet-Cookie: bad", "2\n", "2\x00",
                  formatdate(now - 60, usegmt=True), formatdate(now + 86401, usegmt=True))
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(upstream_io.parse_retry_after(value, now=now))


class GatewayFixture:
    def setUp(self):
        super().setUp()
        self.fx = fixtures.RegionRoutingTests()
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.fx.allowed_profiles = set(fixtures.PROFILES)
        self.enterContext(patch.object(gateway, "_log"))

    def responder(self, callback):
        transport = httpx.MockTransport(callback)
        return patch.object(httpx, "AsyncClient", side_effect=lambda **kw:
                            REAL_ASYNC_CLIENT(transport=transport, **kw))


class RetryAfterEndpointTests(GatewayFixture, unittest.TestCase):
    def test_retry_hint_reaches_all_protocols_and_only_cools_the_selected_model(self):
        for profile in fixtures.PROFILES:
            for route in fixtures.GENERATIONS:
                for stream in (False, True):
                    for status in (429, 503):
                        with self.subTest(profile=profile, route=route, stream=stream, status=status):
                            self.fx.configure(profiles=(profile,))
                            seen = []

                            def reject(request):
                                seen.append(request)
                                return httpx.Response(status, headers={"Retry-After": "17", "X-Private": "hidden"},
                                                      json={"error": {"message": "synthetic rejection", "code": "quota"}})

                            before = time.time()
                            with self.responder(reject):
                                response = self.fx.client.post("/v1/" + route,
                                    json=self.fx.payload(route, stream=stream))
                            self.assertEqual(response.status_code, status, response.text)
                            self.assertEqual(response.headers.get("Retry-After"), "17")
                            self.assertNotIn("X-Private", response.headers)
                            self.assertEqual(response.json()["error"]["code"], "quota")
                            self.assertEqual(len(seen), 1, "A retry hint must not enable replay")
                            entry = self.fx.entries[profile]
                            self.assertEqual(entry["fail_until"], 0)
                            if status == 429:
                                until = self.fx.pool._model_fail[(entry["id"], "shared-model")]
                                self.assertGreaterEqual(until, before + 17)
                                self.assertLessEqual(until, time.time() + 17)
                                self.assertTrue(self.fx.pool._model_healthy(entry, "other-model"))
                            else:
                                self.assertEqual(self.fx.pool._model_fail, {})

    def test_http_date_is_normalized_to_seconds(self):
        self.fx.configure(profiles=("cn-cli",))
        date = formatdate(time.time() + 60, usegmt=True)
        with self.responder(lambda request: httpx.Response(429, headers={"Retry-After": date},
                           json={"error": {"message": "slow down"}})):
            response = self.fx.client.post("/v1/responses", json=self.fx.payload("responses", stream=True))
        self.assertEqual(response.status_code, 429)
        self.assertLessEqual(int(response.headers["Retry-After"]), 60)
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 55)

    def test_invalid_header_falls_back_to_body_or_default_cooldown(self):
        for reset_seconds in (None, -60, 90):
            with self.subTest(reset_seconds=reset_seconds):
                self.fx.configure(profiles=("cn-cli",))
                before = time.time()
                message = "slow down"
                if reset_seconds is not None:
                    message += time.strftime(" %Y-%m-%d %H:%M:%S UTC+0", time.gmtime(before + reset_seconds))
                with self.responder(lambda request: httpx.Response(429, headers={"Retry-After": "999999999"},
                                   json={"error": {"message": message}})):
                    response = self.fx.client.post("/v1/chat/completions", json=self.fx.payload())
                self.assertEqual(response.status_code, 429)
                self.assertNotIn("Retry-After", response.headers)
                until = next(iter(self.fx.pool._model_fail.values()))
                expected = 90 if reset_seconds == 90 else gateway.MODEL_COOLDOWN
                self.assertAlmostEqual(until - before, expected, delta=2)

    def test_pool_generated_429_includes_remaining_wait_without_sending_again(self):
        self.fx.configure(profiles=("cn-cli",))
        entry = self.fx.entries["cn-cli"]
        self.fx.pool.note_status(entry["cm"], 429, model="shared-model", retry_after=17)
        for route in fixtures.GENERATIONS:
            with self.subTest(route=route):
                response = self.fx.client.post("/v1/" + route, json=self.fx.payload(route, stream=True))
                self.assertEqual(response.status_code, 429, response.text)
                self.assertGreater(int(response.headers["Retry-After"]), 0)
                self.assertLessEqual(int(response.headers["Retry-After"]), 17)
        self.assertEqual(self.fx.requests, [])

    def test_explicit_zero_does_not_invent_a_cooldown(self):
        self.fx.configure(profiles=("cn-cli",))
        entry = self.fx.entries["cn-cli"]
        self.fx.pool.note_status(entry["cm"], 429, model="shared-model", retry_after=0)
        self.assertTrue(self.fx.pool._model_healthy(entry, "shared-model"))

    def test_stale_lease_cannot_apply_retry_hint_to_replaced_credentials(self):
        self.fx.configure(profiles=("cn-cli",))
        cm = self.fx.pool.first()
        old = (cm, cm._generation)
        cm.invalidate()
        gateway._note_cred_status(old, 429, model="shared-model", retry_after=17)
        self.assertEqual(self.fx.pool._model_fail, {})

    def test_concurrent_shorter_hints_cannot_clear_an_active_cooldown(self):
        self.fx.configure(profiles=("cn-cli",))
        cm = self.fx.pool.first()
        self.fx.pool.note_status(cm, 429, model="shared-model", retry_after=31)
        original = dict(self.fx.pool._model_fail)
        for delay in (2, 0):
            self.fx.pool.note_status(cm, 429, model="shared-model", retry_after=delay)
            self.assertEqual(self.fx.pool._model_fail, original)

    def test_exhausted_pool_keeps_the_original_upstream_hint(self):
        for route in fixtures.GENERATIONS:
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    self.fx.configure(profiles=("cn-cli",))
                    with self.responder(lambda request: httpx.Response(429, headers={"Retry-After": "17"},
                                       json={"error": {"message": "original rejection"}})), patch.dict(gateway.CONFIG, {"failover_max": 1}):
                        response = self.fx.client.post("/v1/" + route, json=self.fx.payload(route, stream=stream))
                    self.assertEqual(response.status_code, 429)
                    self.assertEqual(response.headers["Retry-After"], "17")
                    self.assertEqual(response.json()["error"]["message"], "original rejection")

    def test_success_headers_do_not_leak_into_sse_failures_or_enable_replay(self):
        data = (b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
                b'data: {"error":{"message":"late rejection","code":429}}\n\n')
        for route in fixtures.GENERATIONS:
            with self.subTest(route=route):
                self.fx.configure()
                seen = []

                def respond(request):
                    seen.append(request)
                    return httpx.Response(200, headers={"Retry-After": "17"}, content=data)

                with self.responder(respond), patch.dict(gateway.CONFIG, {"failover_max": 1}):
                    response = self.fx.client.post("/v1/" + route, json=self.fx.payload(route, stream=True))
                self.assertEqual(response.status_code, 502 if route == "responses" else 200)
                self.assertIn("late rejection", response.text)
                self.assertNotIn("Retry-After", response.headers)
                self.assertEqual(len(seen), 1)


    def test_failover_discards_old_hint_on_success_and_preserves_last_failure_hint(self):
        for final_status in (200, 429):
            for route in fixtures.GENERATIONS:
                for stream in (False, True):
                    with self.subTest(final_status=final_status, route=route, stream=stream):
                        self.fx.configure()
                        seen = []

                        def respond(request):
                            seen.append(request)
                            if len(seen) == 2 and final_status == 200:
                                return httpx.Response(200, content=fixtures.success_sse())
                            return httpx.Response(429, headers={"Retry-After": "17" if len(seen) == 1 else "31"},
                                                  json={"error": {"message": "slow down"}})

                        with self.responder(respond), patch.dict(gateway.CONFIG, {"failover_max": 1}):
                            response = self.fx.client.post("/v1/" + route,
                                json=self.fx.payload(route, stream=stream))
                        self.assertEqual(response.status_code, final_status, response.text)
                        self.assertEqual(len(seen), 2)
                        self.assertEqual(response.headers.get("Retry-After"), "31" if final_status == 429 else None)


class CacheKeyTests(GatewayFixture, unittest.TestCase):
    def test_responses_adapter_preserves_explicit_key_without_mutating_input(self):
        for key in ("client-cache-key", "", None):
            with self.subTest(key=key):
                payload = {"input": "hi", "prompt_cache_key": key}
                original = deepcopy(payload)
                self.assertEqual(responses_request_to_chat(payload).get("prompt_cache_key"), key)
                self.assertIn("prompt_cache_key", responses_request_to_chat(payload))
                self.assertEqual(payload, original)
        self.assertNotIn("prompt_cache_key", responses_request_to_chat({"input": "hi"}))

    def test_chat_and_responses_preserve_keys_through_projection_and_routing(self):
        for profile in fixtures.PROFILES:
            self.fx.configure(profiles=(profile,))
            for route in ("chat/completions", "responses"):
                for stream in (False, True):
                    with self.subTest(profile=profile, route=route, stream=stream):
                        payload = self.fx.payload(route, stream=stream)
                        payload["prompt_cache_key"] = "synthetic-cache"
                        response = self.fx.client.post("/v1/" + route, json=payload)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(json.loads(self.fx.requests[-1].content)["prompt_cache_key"], "synthetic-cache")


class PreparationResponsivenessTests(GatewayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_credential_lock_during_preparation_does_not_block_event_loop(self):
        self.fx.configure(profiles=("cn-cli",))
        cm = self.fx.pool.first()
        async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test") as client:
            for route in fixtures.GENERATIONS:
                for stream in (False, True):
                    with self.subTest(route=route, stream=stream):
                        owned, release, expired = threading.Event(), threading.Event(), threading.Event()

                        def hold_refresh_lock():
                            with cm._lock:
                                owned.set()
                                if not release.wait(2):
                                    expired.set()

                        thread = threading.Thread(target=hold_refresh_lock)
                        thread.start()
                        try:
                            self.assertTrue(await asyncio.to_thread(owned.wait, 1))
                            heartbeat = asyncio.get_running_loop().call_later(0.05, release.set)
                            try:
                                response = await asyncio.wait_for(client.post("/v1/" + route,
                                    json=self.fx.payload(route, stream=stream)), 4)
                            finally:
                                heartbeat.cancel()
                            self.assertFalse(expired.is_set(), "The event loop could not release the credential lock")
                            self.assertEqual(response.status_code, 200, response.text)
                        finally:
                            release.set()
                            await asyncio.to_thread(thread.join, 2)
                        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
