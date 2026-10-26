#!/usr/bin/env python3
"""Test the generic REST search endpoint and the stateless MCP web_search surface."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import json
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import converter
from app.audit_store import AuditStore
from app.observability import AuditMiddleware
from tests import test_region_routing as fixtures

QUERY = "Rust edition guide"
SEARCH_BODY = {"query": QUERY}
LEGACY_HEADERS = {"MCP-Protocol-Version": "2025-11-25"}
MODERN_HEADERS = {"MCP-Protocol-Version": "2026-07-28"}
MODERN_META = {"_meta": {"protocolVersion": "2026-07-28"}}
SEARCH_UPSTREAM_PATH = "/agenttool/v1/search"
RESULT = {"title": "Rust Edition Guide", "url": "https://doc.rust-lang.org/edition-guide/",
          "snippet": "Choose between editions.", "site": "doc.rust-lang.org"}


def rpc(identifier, method, params=None):
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


class WebSearchTests(fixtures.RegionRoutingTests):
    """Route search traffic to the four synthetic profiles through a mocked upstream."""

    def setUp(self):
        super().setUp()
        self.logs = []
        self.search_requests = []
        self.fail_first = 0
        self.search_status = 200
        self.search_reply = None
        self.enterContext(patch.object(converter, "_log", side_effect=self.capture_log))

    def capture_log(self, message, *args, **kwargs):
        self.logs.append(str(message))

    def handle_upstream(self, request):
        if request.url.path == SEARCH_UPSTREAM_PATH:
            return self.handle_search(request)
        return super().handle_upstream(request)

    def handle_search(self, request):
        self.search_requests.append(request)
        uid = request.headers.get("x-user-id")
        profile = self.account_profiles[uid]
        self.assertEqual(request.url.host, fixtures.HOSTS[profile])
        self.assertEqual(request.headers["authorization"], f"Bearer synthetic-access-{uid}")
        self.assertEqual(request.headers["x-domain"], self.credentials[uid]["auth"]["domain"])
        payload = json.loads(request.content)
        self.assertEqual(payload["type"], "text2text")
        if self.search_reply is not None:
            return self.search_reply
        if self.fail_first:
            self.fail_first -= 1
            return httpx.Response(429, json={"error": {"message": "synthetic quota"}})
        if self.search_status != 200:
            return httpx.Response(self.search_status, json={"code": 15003, "msg": "synthetic rejection"})
        if payload.get("query") != QUERY:
            return httpx.Response(400, json={"code": 15003, "msg": "synthetic rejection"})
        return httpx.Response(200, json={"query": payload["query"], "type": "text2text", "provider": "1",
                                         "results": [dict(RESULT)], "total_results": 1, "response_time_ms": 42})

    # -- REST endpoint -------------------------------------------------------

    def test_rest_roundtrip(self):
        response = self.client.post("/v1/search", json=SEARCH_BODY)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.search_requests), 1)
        self.assertEqual(json.loads(self.search_requests[0].content)["query"], QUERY)
        self.assertEqual(json.loads(self.search_requests[0].content)["max_results"], 5)
        body = response.json()
        self.assertEqual(body["object"], "search.results")
        self.assertEqual(body["query"], QUERY)
        self.assertEqual(body["total_results"], 1)
        self.assertEqual(body["results"], [RESULT])   # 上游条目原样透传（含 site 字段）

    def test_rest_max_results_is_forwarded(self):
        response = self.client.post("/v1/search", json={**SEARCH_BODY, "max_results": 3})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(json.loads(self.search_requests[-1].content)["max_results"], 3)

    def test_rest_requires_query(self):
        for payload in ({}, {"query": ""}, {"query": "   "}, {"query": 7}, []):
            with self.subTest(payload=payload):
                response = self.client.post("/v1/search", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["type"], "invalid_request_error")
        self.assertEqual(self.search_requests, [])

    def test_rest_validates_max_results(self):
        for value in (0, 11, 2.5, "3", True, {}):
            with self.subTest(value=value):
                response = self.client.post("/v1/search", json={**SEARCH_BODY, "max_results": value})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("max_results", response.json()["error"]["message"])
        self.assertEqual(self.search_requests, [])

    def test_rest_bad_json_is_rejected(self):
        response = self.client.post("/v1/search", content=b"{nope",
                                    headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")
        self.assertEqual(self.search_requests, [])

    def test_rest_failover_replays_on_another_credential(self):
        self.fail_first = 1
        with patch.dict(converter.CONFIG, {"failover_max": 1}):
            response = self.client.post("/v1/search", json=SEARCH_BODY)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.search_requests), 2)
        self.assertNotEqual(self.search_requests[0].headers["x-user-id"],
                            self.search_requests[1].headers["x-user-id"])
        self.assertIn("换凭证重放", "\n".join(self.logs))

    def test_rest_deterministic_upstream_error_is_not_replayed(self):
        self.search_status = 400
        response = self.client.post("/v1/search", json=SEARCH_BODY)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(self.search_requests), 1)
        payload = response.json()
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertEqual(payload["error"]["message"], "synthetic rejection")
        self.assertNotIn("type\": \"error", response.text)   # OpenAI 形态，非 Anthropic 信封

    BAD_BODIES = (
        (httpx.Response(200, content=b"<html>"), "搜索响应体不是有效的 JSON"),
        (httpx.Response(200, json=[]), "搜索响应必须是 JSON 对象"),
        (httpx.Response(200, json={"code": 15003, "msg": "boom"}), "搜索失败：boom"),
        (httpx.Response(200, json={"code": 15003}), "搜索失败：15003"),
        (httpx.Response(200, json={}), "搜索响应缺少 results 列表"),
    )

    def test_rest_unusable_upstream_body_is_a_chinese_502(self):
        for reply, message in self.BAD_BODIES:
            with self.subTest(message=message):
                self.search_requests.clear()
                self.search_reply = reply
                with patch.dict(converter.CONFIG, {"failover_max": 1}):
                    response = self.client.post("/v1/search", json=SEARCH_BODY)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(response.json()["error"],
                                 {"message": message, "type": "upstream_error"})
                # A readable but unusable reply is not a failover case.
                self.assertEqual(len(self.search_requests), 1)

    # -- MCP surface ---------------------------------------------------------

    def mcp_post(self, message, headers=None):
        return self.client.post("/v1/mcp", json=message, headers=headers or {})

    def test_mcp_legacy_session_flow(self):
        response = self.mcp_post(rpc(1, "initialize", {"protocolVersion": "2025-06-18",
                                                       "capabilities": {},
                                                       "clientInfo": {"name": "synthetic", "version": "0"}}))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "codebuddy2api")
        self.assertNotIn("resultType", result)
        self.assertEqual(self.mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
        listed = self.mcp_post(rpc(2, "tools/list"), LEGACY_HEADERS).json()["result"]
        self.assertEqual(listed["tools"][0]["name"], "web_search")
        self.assertEqual(self.search_requests, [])

    def test_mcp_tools_call_runs_search(self):
        self.mcp_post(rpc(1, "initialize", {"protocolVersion": "2025-11-25",
                                            "capabilities": {}, "clientInfo": {"name": "s", "version": "0"}}))
        message = rpc("abc", "tools/call", {"name": "web_search", "arguments": {"query": QUERY, "count": 3}})
        response = self.mcp_post(message, LEGACY_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        self.assertIn(f"1. [{RESULT['title']}]({RESULT['url']})", result["content"][0]["text"])
        self.assertIn(f"[1] {RESULT['snippet']}", result["content"][0]["text"])
        self.assertEqual(json.loads(self.search_requests[-1].content)["max_results"], 3)

    def test_mcp_modern_request_headers(self):
        message = rpc(7, "tools/call", {"name": "web_search", "arguments": {"query": QUERY}, **MODERN_META})
        response = self.mcp_post(message, MODERN_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["resultType"], "complete")
        mismatch = self.mcp_post(message, LEGACY_HEADERS)
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json()["error"]["code"], -32020)

    def test_mcp_unsupported_meta_version(self):
        message = rpc(3, "tools/call", {"name": "web_search", "arguments": {"query": QUERY},
                                        "_meta": {"protocolVersion": "2025-03-26"}})
        response = self.mcp_post(message, LEGACY_HEADERS)
        self.assertEqual(response.status_code, 400, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], -32022)
        self.assertIn("2026-07-28", error["message"])

    def test_mcp_unknown_methods(self):
        legacy = self.mcp_post(rpc(4, "resources/list"), LEGACY_HEADERS)
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.json()["error"]["code"], -32601)
        modern = self.mcp_post(rpc(5, "resources/list", {**MODERN_META}), MODERN_HEADERS)
        self.assertEqual(modern.status_code, 404)
        self.assertEqual(modern.json()["error"]["code"], -32601)

    def test_mcp_invalid_tool_arguments_never_reach_upstream(self):
        self.mcp_post(rpc(1, "initialize", {"protocolVersion": "2025-11-25",
                                            "capabilities": {}, "clientInfo": {"name": "s", "version": "0"}}))
        for arguments in ({"count": 3}, {"query": "  "}, {"query": QUERY, "count": 0},
                          {"query": QUERY, "count": True}):
            with self.subTest(arguments=arguments):
                message = rpc(6, "tools/call", {"name": "web_search", "arguments": arguments})
                response = self.mcp_post(message, LEGACY_HEADERS)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["result"]
                self.assertTrue(result["isError"])
                self.assertIn("invalid arguments", result["content"][0]["text"])
        self.assertEqual(self.search_requests, [])

    def test_mcp_unknown_tool(self):
        message = rpc(8, "tools/call", {"name": "other", "arguments": {}})
        response = self.mcp_post(message, LEGACY_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["error"]["code"], -32601)

    def test_mcp_non_string_tool_name_is_an_unknown_tool(self):
        for name in (["x"], {}, {"a": 1}, 7, None):
            with self.subTest(name=name):
                response = self.mcp_post(rpc(8, "tools/call", {"name": name, "arguments": {}}),
                                         LEGACY_HEADERS)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["error"]["code"], -32601)
        self.assertEqual(self.search_requests, [])

    def test_mcp_unusable_upstream_body_is_a_tool_error(self):
        self.search_reply = httpx.Response(200, content=b"<html>")
        message = rpc(9, "tools/call", {"name": "web_search", "arguments": {"query": QUERY}})
        result = self.mcp_post(message, LEGACY_HEADERS).json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "搜索响应体不是有效的 JSON")

    def test_mcp_upstream_failure_is_a_tool_error(self):
        self.search_status = 429
        message = rpc(9, "tools/call", {"name": "web_search", "arguments": {"query": QUERY}})
        response = self.mcp_post(message, LEGACY_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "synthetic rejection")
        self.assertEqual(len(self.search_requests), 1)

    def test_mcp_rejects_browser_origins(self):
        response = self.mcp_post(rpc(1, "initialize", {"protocolVersion": "2025-11-25",
                                                       "capabilities": {},
                                                       "clientInfo": {"name": "s", "version": "0"}}),
                                 {"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.search_requests, [])

    def test_mcp_requires_api_key_when_configured(self):
        with patch.dict(converter.CONFIG, {"api_key": "secret-key"}):
            response = self.mcp_post(rpc(1, "initialize", {"protocolVersion": "2025-11-25",
                                                           "capabilities": {},
                                                           "clientInfo": {"name": "s", "version": "0"}}))
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["message"], "invalid api key")

    def test_get_and_delete_are_rejected(self):
        self.assertEqual(self.client.get("/v1/mcp").status_code, 405)
        self.assertEqual(self.client.delete("/v1/mcp").status_code, 405)

    # -- Auditing ------------------------------------------------------------

    def audited_client(self):
        store = AuditStore(self.root / "search-audit.sqlite3")
        self.addCleanup(store.close)
        application = FastAPI()
        application.router.routes = list(converter.app.router.routes)
        application.add_middleware(AuditMiddleware, store)
        return store, self.enterContext(TestClient(application))

    def test_audit_records_only_executing_requests(self):
        store, client = self.audited_client()
        client.post("/v1/mcp", json=rpc(1, "initialize", {"protocolVersion": "2025-11-25",
                                                          "capabilities": {},
                                                          "clientInfo": {"name": "s", "version": "0"}}))
        client.post("/v1/mcp", json=rpc(2, "tools/list"), headers=LEGACY_HEADERS)
        client.post("/v1/mcp", json=rpc("c", "tools/call",
                                        {"name": "web_search", "arguments": {"query": QUERY}}),
                    headers=LEGACY_HEADERS)
        records = store.list_records()["items"]
        self.assertEqual(len(records), 1, records)
        record = records[0]
        self.assertEqual(record["protocol"], "mcp")
        self.assertEqual(record["public_model"], "web-search")
        self.assertEqual(record["outcome"], "success")
        # 搜索不调用模型，usage 恒为空，不污染 token 统计。
        self.assertIsNone(record["input_tokens"])
        self.assertIsNone(record["output_tokens"])

    def test_unusable_upstream_body_is_recorded_with_its_failure_code(self):
        store, client = self.audited_client()
        self.search_reply = httpx.Response(200, json={})
        response = client.post("/v1/search", json=SEARCH_BODY)
        self.assertEqual(response.status_code, 502, response.text)
        records = store.list_records()["items"]
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["error_code"], "bad_search_response")

    def test_audit_records_rest_search_protocol(self):
        store, client = self.audited_client()
        response = client.post("/v1/search", json=SEARCH_BODY)
        self.assertEqual(response.status_code, 200, response.text)
        records = store.list_records()["items"]
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["protocol"], "search")
        self.assertEqual(records[0]["public_model"], "web-search")


if __name__ == "__main__":
    unittest.main(verbosity=2)
