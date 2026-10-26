#!/usr/bin/env python3
"""Test the REST /v1/fetch endpoint and the MCP web_fetch tool surface."""
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

URL = "https://example.com/guide"
FETCH_BODY = {"url": URL}
LEGACY_HEADERS = {"MCP-Protocol-Version": "2025-11-25"}
FETCH_UPSTREAM_PATH = "/agenttool/v1/webfetch"
CONTENT = {"url": URL, "title": "Example Guide", "content": "# Example Guide\n\nBody.",
           "content_type": "text/markdown", "content_length": 24, "truncated": False,
           "status_code": 200}


def rpc(identifier, method, params=None):
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


class WebFetchTests(fixtures.RegionRoutingTests):
    """Route fetch traffic through a mocked upstream agenttool webfetch."""

    def setUp(self):
        super().setUp()
        self.fetch_requests = []
        self.fail_first = 0
        self.fetch_status = 200

    def handle_upstream(self, request):
        if request.url.path == FETCH_UPSTREAM_PATH:
            return self.handle_fetch(request)
        return super().handle_upstream(request)

    def handle_fetch(self, request):
        self.fetch_requests.append(request)
        uid = request.headers.get("x-user-id")
        profile = self.account_profiles[uid]
        self.assertEqual(request.url.host, fixtures.HOSTS[profile])
        self.assertEqual(request.headers["authorization"], f"Bearer synthetic-access-{uid}")
        payload = json.loads(request.content)
        self.assertEqual(payload["format"], "markdown")
        self.assertEqual(payload["timeout"], 20)
        if self.fail_first:
            self.fail_first -= 1
            return httpx.Response(429, json={"error": {"message": "synthetic quota"}})
        if self.fetch_status != 200:
            return httpx.Response(self.fetch_status, json={"code": 15003, "msg": "synthetic rejection"})
        if payload.get("url") != URL:
            return httpx.Response(400, json={"code": 15003, "msg": "synthetic rejection"})
        return httpx.Response(200, json=dict(CONTENT))

    # -- REST endpoint -------------------------------------------------------

    def test_rest_roundtrip(self):
        response = self.client.post("/v1/fetch", json=FETCH_BODY)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.fetch_requests), 1)
        sent = json.loads(self.fetch_requests[0].content)
        self.assertEqual(sent["url"], URL)
        self.assertEqual(sent["max_length"], 20000)
        self.assertIn("prompt", sent)
        body = response.json()
        self.assertEqual(body["object"], "fetch.result")
        self.assertEqual(body["content"], CONTENT["content"])
        self.assertEqual(body["title"], CONTENT["title"])

    def test_rest_forwards_prompt_and_length(self):
        response = self.client.post("/v1/fetch", json={"url": URL, "prompt": "summarize",
                                                       "max_length": 500})
        self.assertEqual(response.status_code, 200, response.text)
        sent = json.loads(self.fetch_requests[-1].content)
        self.assertEqual(sent["prompt"], "summarize")
        self.assertEqual(sent["max_length"], 500)

    def test_rest_rejects_bad_urls(self):
        for payload in ({}, {"url": ""}, {"url": "ftp://x"}, {"url": 7}, {"url": URL, "max_length": 0},
                        {"url": URL, "max_length": True}, {"url": URL, "prompt": 5}):
            with self.subTest(payload=payload):
                response = self.client.post("/v1/fetch", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["type"], "invalid_request_error")
        self.assertEqual(self.fetch_requests, [])

    def test_rest_failover_replays_on_another_credential(self):
        self.fail_first = 1
        with patch.dict(converter.CONFIG, {"failover_max": 1}):
            response = self.client.post("/v1/fetch", json=FETCH_BODY)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.fetch_requests), 2)
        self.assertNotEqual(self.fetch_requests[0].headers["x-user-id"],
                            self.fetch_requests[1].headers["x-user-id"])

    def test_rest_upstream_error_is_mapped(self):
        self.fetch_status = 400
        response = self.client.post("/v1/fetch", json=FETCH_BODY)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["message"], "synthetic rejection")

    # -- MCP surface ---------------------------------------------------------

    def test_mcp_lists_both_tools(self):
        self.client.post("/v1/mcp", json=rpc(1, "initialize", {"protocolVersion": "2025-11-25",
                                                               "capabilities": {},
                                                               "clientInfo": {"name": "s", "version": "0"}}))
        listed = self.client.post("/v1/mcp", json=rpc(2, "tools/list"),
                                  headers=LEGACY_HEADERS).json()["result"]
        self.assertEqual([tool["name"] for tool in listed["tools"]], ["web_search", "web_fetch"])

    def test_mcp_fetch_call_returns_markdown(self):
        message = rpc("f1", "tools/call", {"name": "web_fetch",
                                           "arguments": {"url": URL, "prompt": "summarize"}})
        response = self.client.post("/v1/mcp", json=message, headers=LEGACY_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("# Example Guide", text)
        self.assertIn(URL, text)
        sent = json.loads(self.fetch_requests[-1].content)
        self.assertEqual(sent["prompt"], "summarize")

    def test_mcp_fetch_invalid_arguments(self):
        for arguments in ({"prompt": "x"}, {"url": "notaurl"}, {"url": URL, "prompt": 3}):
            with self.subTest(arguments=arguments):
                message = rpc("f2", "tools/call", {"name": "web_fetch", "arguments": arguments})
                response = self.client.post("/v1/mcp", json=message, headers=LEGACY_HEADERS)
                result = response.json()["result"]
                self.assertTrue(result["isError"])
                self.assertIn("invalid arguments", result["content"][0]["text"])
        self.assertEqual(self.fetch_requests, [])

    def test_mcp_upstream_failure_is_a_tool_error(self):
        self.fetch_status = 429
        message = rpc("f3", "tools/call", {"name": "web_fetch", "arguments": {"url": URL}})
        response = self.client.post("/v1/mcp", json=message, headers=LEGACY_HEADERS)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "synthetic rejection")

    # -- Auditing ------------------------------------------------------------

    def test_audit_records_rest_fetch_protocol(self):
        store = AuditStore(self.root / "fetch-audit.sqlite3")
        self.addCleanup(store.close)
        application = FastAPI()
        application.router.routes = list(converter.app.router.routes)
        application.add_middleware(AuditMiddleware, store)
        with TestClient(application) as client:
            response = client.post("/v1/fetch", json=FETCH_BODY)
        self.assertEqual(response.status_code, 200, response.text)
        records = store.list_records()["items"]
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["protocol"], "fetch")
        self.assertEqual(records[0]["public_model"], "web-fetch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
