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
from app import web_fetch
from app.audit_store import AuditStore
from app.observability import AuditMiddleware
from tests import test_region_routing as fixtures

URL = "https://example.com/guide"
INTERNAL_URLS = ("http://localhost:8787/", "http://127.0.0.1/", "http://169.254.169.254/latest",
                 "http://[::1]/", "http://10.0.0.1/", "http://router.local/",
                 "http://127.0.0.1\\@example.com/", "http://%6c%6f%63%61%6c%68%6f%73%74/")
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


class ValidateUrlTests(unittest.TestCase):
    def test_public_urls_are_accepted_and_stripped(self):
        for url in ("https://example.com/x", "https://8.8.8.8/", "http://example.com:8080/a?b=c",
                    "https://[2606:4700:4700::1111]/", "http://例子.中国/", "https://münchen.de/x"):
            with self.subTest(url=url):
                self.assertEqual(web_fetch.validate_url("  " + url + " \n"), url)

    def test_internal_and_malformed_urls_are_rejected(self):
        rejected = (
            "http://localhost", "http://localhost./", "http://app.localhost/", "http://router.local",
            "http://db.internal/", "http://127.0.0.1/", "http://127.1/", "http://2130706433/",
            "http://0x7f000001/", "http://0.0.0.0/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
            "http://10.0.0.1", "http://192.168.1.1", "http://169.254.169.254",
            "http://100.64.0.1", "http://a:b@127.0.0.1/", "ftp://example.com", "http:///x",
            "example.com", "http://[::1", "", "   ", None, 7)
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    web_fetch.validate_url(url)
                self.assertIn("url", str(caught.exception))
                self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in str(caught.exception)))


    def test_parser_differential_hosts_are_rejected(self):
        # WHATWG parsers resolve these to an internal host while urlsplit alone does not.
        for url in ("http://127.0.0.1\\@example.com/", "http://127.0.0.1\\.example.com/",
                    "http://%31%32%37.0.0.1/", "http://%6c%6f%63%61%6c%68%6f%73%74/",
                    "http://localhost%2e/", "http://１２７.０.０.１/", "http://127。0。0。1/",
                    "http://ｌｏｃａｌｈｏｓｔ/", "http://router．local/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    web_fetch.validate_url(url)
                self.assertEqual(str(caught.exception), "url 不能指向内网地址")

    def test_unusable_hosts_fail_closed(self):
        for url in ("http://127.0.0.1%00.evil.com/", "http://127.0.0.1%20x/", "http://a..com/",
                    "http://" + "a" * 64 + ".com/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    web_fetch.validate_url(url)

    def test_canonical_host_follows_whatwg(self):
        self.assertEqual(web_fetch.canonical_host("http://例子.中国/"), "xn--fsqu00a.xn--fiqs8s")
        self.assertEqual(web_fetch.canonical_host("https://Example.COM.:8443/x"), "example.com")
        self.assertEqual(web_fetch.canonical_host("https://u:p@[::1]:80/"), "::1")
        self.assertEqual(web_fetch.canonical_host("https://evil.com\\@docs.example.com/"), "evil.com")


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

    def test_rest_rejects_internal_urls(self):
        for url in INTERNAL_URLS:
            with self.subTest(url=url):
                response = self.client.post("/v1/fetch", json={"url": url})
                self.assertEqual(response.status_code, 400, response.text)
                error = response.json()["error"]
                self.assertEqual(error["type"], "invalid_request_error")
                self.assertEqual(error["message"], "url 不能指向内网地址")
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

    def test_mcp_fetch_rejects_internal_urls(self):
        for url in INTERNAL_URLS:
            with self.subTest(url=url):
                message = rpc("f4", "tools/call", {"name": "web_fetch", "arguments": {"url": url}})
                response = self.client.post("/v1/mcp", json=message, headers=LEGACY_HEADERS)
                result = response.json()["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(result["content"][0]["text"],
                                 "invalid arguments: url 不能指向内网地址")
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
