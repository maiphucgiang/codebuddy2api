"""Verify request/session isolation and tracing with synthetic credentials and offline upstreams."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi.testclient import TestClient
import converter as gateway
from app.audit_store import AuditStore
from app.observability import AuditMiddleware, observe_attempt
from app.request_context import (RequestContext, RequestContextMiddleware, SessionIdentifierError,
                                 current_context, ensure_context)
from app.adapters.responses_adapter import responses_request_to_chat
from app.adapters.anthropic_adapter import anthropic_request_to_chat
import test_api_flow as fixtures
import test_workbuddy_filter as filters

REAL_CLIENT = fixtures.REAL_ASYNC_CLIENT
USER = {"role": "user", "content": "first question"}


def context(payload, *, protocol="chat", headers=(), messages=None):
    ctx = RequestContext(protocol, "scoped", headers)
    ctx.bind_session(payload, payload.get("messages", [USER]) if messages is None else messages)
    return ctx


class SessionTests(unittest.TestCase):
    def test_explicit_aliases_and_header_share_a_key_without_disclosing_the_value(self):
        value = "synthetic-private-conversation"
        cases = [{"metadata": {key: value}} for key in ("conversation_id", "conversationId")]
        cases += [{key: value} for key in ("conversation_id", "conversationId")]
        keys = [context(body).session_key for body in cases]
        keys.append(context({}, headers=[(b"X-Codebuddy-Session-ID", value.encode())]).session_key)
        self.assertEqual(len(set(keys)), 1)
        self.assertNotIn(value, keys[0])
        self.assertNotEqual(keys[0], context({"conversation_id": "different"}).session_key)
        self.assertNotEqual(keys[0], context(cases[0], protocol="responses").session_key)

    def test_conflicting_and_malformed_identifiers_are_rejected_without_echo(self):
        with self.assertRaises(SessionIdentifierError):
            context({"metadata": {"conversation_id": "one"}, "conversationId": "two"})
        with self.assertRaises(SessionIdentifierError):
            context({}, headers=[(b"x-codebuddy-session-id", b"one"), (b"x-codebuddy-session-id", b"two")])
        for value in (True, 1, [], {}, "canary\nprivate", "x" * 513, "é" * 257, "\ud800"):
            with self.subTest(value=repr(value)[:20]), self.assertRaises(SessionIdentifierError) as error:
                context({"conversation_id": value})
            self.assertNotIn("canary", str(error.exception))
        a = context({"conversation_id": " same ", "metadata": {"conversationId": "same"}})
        self.assertEqual(a.session_key, context({"conversation_id": "same"}).session_key)
        self.assertEqual(context({"conversation_id": None}).session_key, context({}).session_key)

    def test_user_identity_and_cache_key_do_not_become_session_identity(self):
        baseline = context({}).session_key
        for payload in ({"user": "different"}, {"metadata": {"user_id": "different"}},
                        {"prompt_cache_key": "different"}):
            self.assertEqual(context(payload).session_key, baseline)

    def test_adapted_instructions_participate_for_each_protocol(self):
        for protocol, adapt, field in (("responses", responses_request_to_chat, "instructions"),
                                       ("messages", anthropic_request_to_chat, "system")):
            keys = []
            for value in ("A", "B"):
                body = {"input": "first question", "messages": [USER], field: value}
                keys.append(context(body, protocol=protocol, messages=adapt(body)["messages"]).session_key)
            self.assertNotEqual(*keys)
        first = context({"messages": [{"role": "system", "content": "A"}, USER]})
        second = context({"messages": [{"role": "developer", "content": [{"type": "text", "text": "A"}]}, USER]})
        self.assertEqual(first.session_key, second.session_key)

    def test_followup_turns_preserve_key_but_instruction_boundaries_do_not_collapse(self):
        first = {"messages": [{"role": "system", "content": "A"}, USER]}
        continued = deepcopy(first)
        continued["messages"] += [{"role": "assistant", "content": "answer"}, {"role": "user", "content": "next"}]
        self.assertEqual(context(first).session_key, context(continued).session_key)
        a = [{"role": "system", "content": x} for x in ("a", "bc")] + [USER]
        b = [{"role": "system", "content": x} for x in ("ab", "c")] + [USER]
        self.assertNotEqual(context({}, messages=a).session_key, context({}, messages=b).session_key)

    def test_images_order_and_contents_are_hashed_without_mutation(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}
        text = {"type": "text", "text": "inspect"}
        keys = []
        for blocks in ([image], [{**image, "image_url": {"url": "data:image/png;base64,Qg=="}}],
                       [image, text], [text, image]):
            body = {"messages": [{"role": "user", "content": blocks}]}
            original = deepcopy(body)
            ctx = context(body)
            self.assertEqual(ctx.session_source, "fingerprint")
            self.assertEqual(body, original)
            keys.append(ctx.session_key)
        self.assertEqual(len(set(keys)), 4)

    def test_no_reliable_input_uses_temporary_session_and_binding_happens_once(self):
        a, b = context({}, messages=[]), context({}, messages=[])
        self.assertEqual(a.session_source, "temporary")
        self.assertNotEqual(a.session_key, b.session_key)
        original = a.session_key
        a.bind_session({"conversation_id": "late"}, [USER])
        self.assertEqual(a.session_key, original)

    def test_legacy_does_not_parse_hints_and_account_conversations_are_isolated(self):
        ctx = RequestContext("chat", "legacy", [(b"x-codebuddy-session-id", b"bad\nvalue")])
        ctx.bind_session({"conversation_id": []}, [USER])
        self.assertEqual(ctx.session_source, "legacy")
        ctx = context({"conversation_id": "same"})
        ids = [ctx.conversation_id(profile, account) for profile in fixtures.fixtures.PROFILES for account in ("A", "B")]
        self.assertEqual(len(set(ids)), 8)

    def test_attempt_ids_are_unique_and_request_mode_is_a_snapshot(self):
        scope = {"path": "/v1/responses", "headers": [(b"x-request-id", b"untrusted")]}
        config = {"request_context_mode": "scoped"}
        ctx = ensure_context(scope, config)
        config["request_context_mode"] = "legacy"
        self.assertIs(ensure_context(scope, config), ctx)
        self.assertTrue(ctx.scoped)
        self.assertNotEqual(ctx.request_id, "untrusted")
        with ThreadPoolExecutor(max_workers=4) as executor:
            attempts = list(executor.map(lambda _: ctx.start_attempt(), range(32)))
        self.assertEqual({a.index for a in attempts}, set(range(1, 33)))
        self.assertEqual(len({a.id for a in attempts}), 32)
        base = {"Authorization": "synthetic", "X-Conversation-ID": "conversation"}
        for attempt in attempts:
            headers = ctx.attempt_headers(base, attempt)
            self.assertEqual(headers["X-Root-Request-ID"], ctx.request_id)
            self.assertEqual(headers["X-Request-ID"], attempt.id)
            self.assertEqual(headers["traceparent"], f"00-{ctx.request_id}-{attempt.span}-01")
            self.assertEqual(headers["X-B3-SpanId"], attempt.span)
        self.assertEqual(base, {"Authorization": "synthetic", "X-Conversation-ID": "conversation"})


class EndpointContextTests(fixtures.GatewayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(gateway.CONFIG, {"request_context_mode": "scoped", "max_inflight_per_account": 1}))

    def assert_attempts(self, response, seen, *, same_account=True):
        self.assertEqual(response.status_code, 200, response.text)
        root = response.headers["X-Request-ID"]
        self.assertRegex(root, r"^[0-9a-f]{32}$")
        self.assertEqual({r.headers["X-Root-Request-ID"] for r in seen}, {root})
        self.assertEqual(len({r.headers["X-Request-ID"] for r in seen}), len(seen))
        self.assertEqual(len({r.headers["X-Conversation-ID"] for r in seen}), 1 if same_account else len(seen))
        for request in seen:
            headers = request.headers
            self.assertEqual(headers["X-Conversation-Request-ID"], root)
            self.assertEqual(headers["X-Trace-ID"], root)
            self.assertEqual(headers["X-Request-ID"], headers["X-Conversation-Message-ID"])
            self.assertEqual(headers["traceparent"], f'00-{root}-{headers["X-B3-SpanId"]}-01')
        self.assertEqual(self.fx.pool._capacity._counts, {})

    def test_profiles_protocols_and_modes_preserve_body_ids_and_isolate_requests(self):
        for profile in fixtures.fixtures.PROFILES:
            self.fx.configure(profiles=(profile,))
            for route in fixtures.fixtures.GENERATIONS:
                for stream in (False, True):
                    with self.subTest(profile=profile, route=route, stream=stream):
                        payload = self.fx.payload(route, stream=stream, text="same")
                        payload["metadata"] = {"conversation_id": "explicit"}
                        first = self.fx.client.post("/v1/" + route, json=payload)
                        request = self.fx.requests[-1]
                        self.assert_attempts(first, [request])
                        second = self.fx.client.post("/v1/" + route, json=payload)
                        self.assert_attempts(second, [self.fx.requests[-1]])
                        self.assertNotEqual(first.headers["X-Request-ID"], second.headers["X-Request-ID"])
                        self.assertEqual(request.headers["X-Conversation-ID"], self.fx.requests[-1].headers["X-Conversation-ID"])
                        self.assertIn("synthetic-completion" if route == "chat/completions" and stream else "ok", first.text)
                        if not stream:
                            prefix = {"chat/completions": "chatcmpl-", "responses": "resp_", "messages": "msg_"}[route]
                            self.assertTrue(first.json()["id"].startswith(prefix))
                            self.assertNotEqual(first.json()["id"], first.headers["X-Request-ID"])
                        self.assertNotIn("metadata", json.loads(request.content))

    def test_pooling_and_capacity_settings_do_not_change_context_contract(self):
        for keepalive in (False, True):
            for capacity in (0, 1):
                for mode in ("legacy", "scoped"):
                    with self.subTest(keepalive=keepalive, capacity=capacity, mode=mode), patch.dict(gateway.CONFIG, {
                        "upstream_keepalive": keepalive, "max_inflight_per_account": capacity, "request_context_mode": mode}):
                        self.fx.configure(profiles=("cn-cli",))
                        response = self.fx.client.post("/v1/responses", json=self.fx.payload("responses", text="same"))
                        request = self.fx.requests[-1]
                        if mode == "scoped":
                            self.assert_attempts(response, [request])
                        else:
                            self.assertEqual(response.status_code, 200)
                            self.assertNotEqual(response.headers["X-Request-ID"], request.headers["X-Root-Request-ID"])
                            self.assertEqual(self.fx.pool._capacity._counts, {})

    def test_hint_errors_do_not_send_upstream_and_legacy_ignores_them(self):
        for route in fixtures.fixtures.GENERATIONS:
            body = self.fx.payload(route)
            body["metadata"] = {"conversation_id": "private-hint"}
            response = self.fx.client.post("/v1/" + route, json=body,
                                           headers={"X-Codebuddy-Session-ID": "different-private-hint"})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn("private-hint", response.text)
            self.assertIn("X-Request-ID", response.headers)
        self.assertEqual(self.fx.requests, [])
        with patch.dict(gateway.CONFIG, {"request_context_mode": "legacy"}):
            body = self.fx.payload()
            body["conversation_id"] = []
            response = self.fx.client.post("/v1/chat/completions", json=body)
            self.assertEqual(response.status_code, 200, response.text)

    def test_protocol_instructions_and_explicit_ids_change_conversation(self):
        self.fx.configure(profiles=("cn-cli",))
        for route in fixtures.fixtures.GENERATIONS:
            for field in ("instructions", "explicit"):
                ids = []
                for value in ("A", "B"):
                    body = self.fx.payload(route, text="same", system=value if field == "instructions" else "constant")
                    if field == "explicit":
                        body["metadata"] = {"conversation_id": value}
                    response = self.fx.client.post("/v1/" + route, json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    ids.append(self.fx.requests[-1].headers["X-Conversation-ID"])
                self.assertNotEqual(*ids, (route, field))

    def test_responses_fingerprint_is_bound_before_projection(self):
        self.fx.configure(profiles=("cn-cli",))
        original = gateway.project_responses_chat_body
        seen = []
        def projection(body, **kwargs):
            projected, stats = original(body, **kwargs)
            seen.append(len(seen))
            return {**projected, "messages": [{"role": "user", "content": str(len(seen))}]}, stats
        ids = []
        with patch.object(gateway, "project_responses_chat_body", side_effect=projection):
            for _ in range(2):
                response = self.fx.client.post("/v1/responses", json=self.fx.payload("responses", text="same", system="original"))
                self.assertEqual(response.status_code, 200, response.text)
                ids.append(self.fx.requests[-1].headers["X-Conversation-ID"])
        self.assertEqual(*ids)
        self.assertEqual(len(seen), 2)

    def test_failover_keeps_root_and_rotates_account_conversation_and_attempt(self):
        for route in fixtures.fixtures.GENERATIONS:
            for stream in (False, True):
                self.fx.configure(profiles=("cn-cli", "cn-work"))
                seen = []
                def respond(request):
                    seen.append(request)
                    return httpx.Response(429, json={"error": {"message": "quota"}}) if len(seen) == 1 else httpx.Response(200, content=fixtures.fixtures.success_sse())
                with self.responder(respond), patch.dict(gateway.CONFIG, {"failover_max": 1}):
                    response = self.fx.client.post("/v1/" + route, json=self.fx.payload(route, stream=stream))
                self.assertEqual(len(seen), 2)
                self.assert_attempts(response, seen, same_account=False)

    def test_scoped_hints_cannot_escape_a_full_strict_binding_or_free_tier(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                self.fx.configure(profiles=("cn-cli", "cn-work"))
                if strict:
                    identity = self.fx.entries["cn-cli"]["account_key"]
                    store = SimpleNamespace(snapshot=lambda: {"revision": 1, "credentials": {},
                        "models": {"shared-model": {"credential_ids": [identity]}}})
                else:
                    store = None
                    self.fx.account_catalogs({
                        "cn-cli": [fixtures.fixtures.model("shared-model", "x0.00")],
                        "cn-work": [fixtures.fixtures.model("shared-model", "x1.00")]})
                with patch.dict(gateway.CONFIG, {"control_store": store}):
                    lease, _ = self.fx.pool.headers_for("held-session", "shared-model", with_capacity=True)
                    before = len(self.fx.requests)
                    try:
                        for route in fixtures.fixtures.GENERATIONS:
                            body = self.fx.payload(route)
                            body["metadata"] = {"conversation_id": "different-session"}
                            response = self.fx.client.post("/v1/" + route, json=body)
                            self.assertEqual(response.status_code, 503, response.text)
                            self.assertEqual(response.json()["error"]["code"], "credential_concurrency_limit")
                            self.assertEqual(len(self.fx.requests), before)
                    finally:
                        lease.release()
                self.assertEqual(self.fx.pool._capacity._counts, {})

    def test_scoped_tracing_does_not_replay_partial_upstream_failures(self):
        for route in fixtures.fixtures.GENERATIONS:
            seen = []
            def respond(request):
                seen.append(request)
                return httpx.Response(200, content=filters.sse(
                    filters.event({"content": "partial"}), {"error": {"code": 429, "message": "late"}}))
            with self.responder(respond), patch.dict(gateway.CONFIG, {"failover_max": 1}):
                response = self.fx.client.post("/v1/" + route, json=self.fx.payload(route, stream=True))
            self.assertIn("late", response.text)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].headers["X-Root-Request-ID"], response.headers["X-Request-ID"])
            self.assertEqual(self.fx.pool._capacity._counts, {})


    def test_connect_retries_get_new_attempts_but_legacy_headers_remain_unchanged(self):
        self.fx.configure(profiles=("cn-cli",))
        for mode in ("legacy", "scoped"):
            seen = []
            def respond(request):
                seen.append(request)
                if len(seen) == 1:
                    raise httpx.ConnectError("synthetic connect failure")
                return httpx.Response(200, content=fixtures.fixtures.success_sse())
            with self.responder(respond), patch.dict(gateway.CONFIG, {"request_context_mode": mode}), patch.object(gateway.asyncio, "sleep", new=AsyncMock()):
                response = self.fx.client.post("/v1/chat/completions", json=self.fx.payload())
            self.assertEqual(len(seen), 2)
            if mode == "scoped":
                self.assert_attempts(response, seen)
            else:
                self.assertEqual(dict(seen[0].headers), dict(seen[1].headers))

    def test_tool_repair_and_filter_fallback_get_new_attempts(self):
        self.fx.configure(profiles=("cn-cli",))
        for route in filters.ROUTES:
            for filtered in (False, True):
                seen = []
                def respond(request):
                    seen.append(request)
                    if filtered:
                        return filters.reply() if len(seen) == 1 else filters.reply("ok")
                    call = deepcopy(filters.TOOL)
                    if len(seen) == 1:
                        call["function"]["arguments"] = "{"
                    return httpx.Response(200, content=filters.sse(filters.event({"tool_calls": [call]}, "tool_calls")))
                body = filters.payload(route, tools=not filtered)
                body["model"] = "shared-model"
                with self.responder(respond), patch.dict(gateway.CONFIG, {"desensitize": True, "no_compact": True}):
                    response = self.fx.client.post(route, json=body)
                self.assertEqual(len(seen), 2, response.text)
                self.assert_attempts(response, seen)
                if not filtered:
                    self.assertIn("call-test", response.text)

    def test_inflight_mode_does_not_change_during_a_retry(self):
        self.fx.configure(profiles=("cn-cli",))
        seen = []
        def respond(request):
            seen.append(request)
            if len(seen) == 1:
                gateway.CONFIG["request_context_mode"] = "legacy"
                raise httpx.ConnectError("synthetic")
            return httpx.Response(200, content=fixtures.fixtures.success_sse())
        with self.responder(respond), patch.object(gateway.asyncio, "sleep", new=AsyncMock()):
            first = self.fx.client.post("/v1/chat/completions", json=self.fx.payload())
            self.assert_attempts(first, seen)
            second = self.fx.client.post("/v1/chat/completions", json=self.fx.payload())
            self.assertEqual(second.status_code, 200)
            self.assertNotEqual(second.headers["X-Request-ID"], seen[-1].headers["X-Root-Request-ID"])

    def test_response_ids_match_audit_without_deduplicating_client_supplied_ids(self):
        self.fx.configure(profiles=("cn-cli",))
        store = AuditStore(self.fx.root / "context-audit.sqlite3")
        self.addCleanup(store.close)
        app = AuditMiddleware(gateway.app, {**gateway.CONFIG, "audit_store": store})
        roots = []
        with TestClient(app) as client:
            for _ in range(2):
                body = self.fx.payload(text="same")
                body["metadata"] = {"conversation_id": "private-session-canary"}
                response = client.post("/v1/chat/completions", json=body, headers={"X-Request-ID": "client-reused-id"})
                roots.append(response.headers["X-Request-ID"])
        rows = store.list_records()["items"]
        self.assertEqual({row["id"] for row in rows}, set(roots))
        self.assertEqual(len(set(roots)), 2)
        self.assertEqual(store.dashboard()["summary"]["requests"], 2)
        for row in rows:
            attempt = next(a for a in row["attempts"] if a["stage"] == "upstream_attempt")
            self.assertEqual(attempt["request_id"], row["id"])
            self.assertEqual(attempt["attempt_index"], 1)
            self.assertEqual(attempt["attempt_id"], attempt["upstream_request_id"])
        self.assertNotIn("private-session-canary", json.dumps(rows))
        self.assertNotIn("synthetic-access", json.dumps(rows))
        self.assertNotIn("client-reused-id", json.dumps(rows))


class AsyncContextTests(fixtures.GatewayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_parallel_requests_and_cancellation_leave_no_context_or_capacity(self):
        self.fx.configure(profiles=("cn-cli",))
        reached, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def respond(request):
            seen.append((request, current_context().request_id))
            if len(seen) == 4:
                reached.set()
            await release.wait()
            return httpx.Response(200, content=fixtures.fixtures.success_sse())
        with self.responder(respond), patch.dict(gateway.CONFIG, {"request_context_mode": "scoped"}):
            async with REAL_CLIENT(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test") as client:
                tasks = [asyncio.create_task(client.post("/v1/responses", json=self.fx.payload("responses"))) for _ in range(4)]
                try:
                    await asyncio.wait_for(reached.wait(), 2)
                    self.assertEqual(sum(self.fx.pool._capacity._counts.values()), 4)
                    tasks[0].cancel()
                    await asyncio.gather(tasks[0], return_exceptions=True)
                    release.set()
                    responses = await asyncio.gather(*tasks[1:])
                finally:
                    release.set()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        roots = {root for _, root in seen}
        self.assertEqual(len(roots), 4)
        self.assertTrue(all(request.headers["X-Root-Request-ID"] == root for request, root in seen))
        self.assertTrue(all(r.status_code == 200 and r.headers["X-Request-ID"] in roots for r in responses))
        self.assertEqual(self.fx.pool._capacity._counts, {})
        self.assertIsNone(current_context())

    async def test_bounded_audit_overflow_and_excluded_routes(self):
        store = AuditStore(self.fx.root / "overflow.sqlite3")
        self.addCleanup(store.close)
        async def app(scope, receive, send):
            for i in range(40):
                observe_attempt("probe", attempt=i, token="private-canary")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{}'})
        wrapped = RequestContextMiddleware(AuditMiddleware(app, {"audit_store": store}), {})
        sent = []
        async def receive():
            raise AssertionError("Middleware must not read the request body")
        async def send(message):
            sent.append(message)
        await wrapped({"type": "http", "method": "POST", "path": "/v1/responses"}, receive, send)
        row = store.list_records()["items"][0]
        self.assertEqual(row["id"].encode(), dict(sent[0]["headers"])[b"x-request-id"])
        self.assertEqual(len(row["attempts"]), 32)
        self.assertEqual(row["attempts"][-1], {"stage": "attempts_truncated", "dropped": 9})
        self.assertNotIn("private-canary", json.dumps(row))
        sent.clear()
        await wrapped({"type": "http", "method": "POST", "path": "/admin/credentials"}, receive, send)
        self.assertNotIn(b"x-request-id", dict(sent[0]["headers"]))
        self.assertEqual(len(store.list_records()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
