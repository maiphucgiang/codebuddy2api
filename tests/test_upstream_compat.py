#!/usr/bin/env python3
"""Test upstream tool-schema and channel compatibility behavior."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

from copy import deepcopy
import gzip
import json
import time
import unittest
from unittest.mock import patch

import httpx

import converter
from app import buddy_task, growth_events, rate_measurement
from app.upstream_compat import channel_fallback_url, is_channel_check_error, normalize_tool_schemas
from tests import test_region_routing as fixtures

CHANNEL_ERROR = {
    "message": "{'code': 11128, 'msg': 'Illegal API invocation from an unapproved channel', "
               "'displayTips': {'en': 'This call failed channel verification, possibly because it was not sent "
               "from an official client.'}, 'displayMsg': {'zh': '请求被拦截，请重新发送'}}",
    "type": "error",
}
MODEL_MISSING = {"code": 11102, "msg": "model [shared-model] service info not found"}


def request_json(request):
    raw = gzip.decompress(request.content) if request.headers.get("Content-Encoding") == "gzip" else request.content
    return json.loads(raw)


class ToolPatternTests(unittest.TestCase):
    def test_nested_patterns_are_normalized_without_mutating_input_or_instance_data(self):
        schema = {
            "type": "object",
            "properties": {"runId": {"type": "string", "pattern": r"^agent\_run\_"}},
            "patternProperties": {r"^a\_b": {"pattern": r"x\_y"}},
            "anyOf": [{"properties": {"code": {"pattern": r"x\_z"}}}],
            "default": {"pattern": r"keep\_default"},
            "const": r"keep\_const",
            "examples": [{"pattern": r"keep\_example"}],
        }
        body = {
            "messages": [{"role": "user", "content": r"keep\_message"}],
            "tools": [{"type": "function", "function": {"name": "agent_run", "parameters": schema}}],
        }
        before = deepcopy(body)
        result = normalize_tool_schemas(body)
        actual = result["tools"][0]["function"]["parameters"]
        self.assertEqual(body, before)
        self.assertEqual(actual["properties"]["runId"]["pattern"], "^agent_run_")
        self.assertEqual(actual["patternProperties"]["^a_b"]["pattern"], "x_y")
        self.assertEqual(actual["anyOf"][0]["properties"]["code"]["pattern"], "x_z")
        self.assertEqual(actual["default"], {"pattern": r"keep\_default"})
        self.assertEqual(actual["const"], r"keep\_const")
        self.assertEqual(actual["examples"], [{"pattern": r"keep\_example"}])
        self.assertEqual(result["messages"], body["messages"])

    def test_legacy_and_bare_parameters_are_covered(self):
        body = {
            "tools": [{"name": "bare", "parameters": {"pattern": r"a\_b"}}],
            "functions": [{"name": "legacy", "parameters": {"pattern": r"c\_d"}}],
        }
        result = normalize_tool_schemas(body)
        self.assertEqual(result["tools"][0]["parameters"]["pattern"], "a_b")
        self.assertEqual(result["functions"][0]["parameters"]["pattern"], "c_d")

    def test_escape_parity_and_pattern_property_collisions_are_preserved(self):
        body = {"tools": [{"name": "bare", "parameters": {
            "patternProperties": {r"a\_b": {"const": "original"}, "a_b": {"const": "existing"}},
            "properties": {
                "single": {"pattern": r"a\_b"},
                "double": {"pattern": r"a\\_b"},
                "triple": {"pattern": r"a\\\_b"},
            },
        }}]}
        result = normalize_tool_schemas(body)
        schema = result["tools"][0]["parameters"]
        self.assertEqual(schema["patternProperties"]["a_b"],
                         {"allOf": [{"const": "original"}, {"const": "existing"}]})
        self.assertEqual(schema["properties"]["single"]["pattern"], "a_b")
        self.assertEqual(schema["properties"]["double"]["pattern"], r"a\\_b")
        self.assertEqual(schema["properties"]["triple"]["pattern"], r"a\\_b")

    def test_boolean_pattern_property_collisions_are_merged(self):
        body = {"tools": [{"name": "bare", "parameters": {
            "patternProperties": {r"^a\_b$": False, "^a_b$": {"type": "string"}}}}]}
        result = normalize_tool_schemas(body)
        self.assertEqual(result["tools"][0]["parameters"]["patternProperties"],
                         {"^a_b$": {"allOf": [False, {"type": "string"}]}})

    def test_bodies_without_escapes_are_not_copied(self):
        body = {"tools": [{"name": "bare", "parameters": {"pattern": "a_b"}}],
                "response_format": {"type": "json_object"}}
        result = normalize_tool_schemas(body)
        self.assertIs(result, body)
        self.assertIs(result["tools"], body["tools"])

    def test_response_format_json_schema_is_normalized(self):
        body = {"response_format": {"type": "json_schema", "json_schema": {
            "name": "result", "schema": {"properties": {"runId": {"pattern": r"^agent\_run\_"}}}}}}
        result = normalize_tool_schemas(body)
        schema = result["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["runId"]["pattern"], "^agent_run_")
        self.assertEqual(body["response_format"]["json_schema"]["schema"]["properties"]["runId"]["pattern"], r"^agent\_run\_")


class ChannelResponseTests(unittest.TestCase):
    def test_channel_detection_requires_code_and_text(self):
        self.assertTrue(is_channel_check_error(json.dumps(CHANNEL_ERROR).encode()))
        self.assertTrue(is_channel_check_error(
            '{"code":"11-128","displayTips":{"en":"not sent from an official client"}}'))
        self.assertFalse(is_channel_check_error('{"code":11128,"msg":"blocked by security policy"}'))
        self.assertFalse(is_channel_check_error('{"msg":"channel verification"}'))
        self.assertFalse(is_channel_check_error('{"requestId":"11128","msg":"official client"}'))
        self.assertFalse(is_channel_check_error('{"code":111280,"msg":"official client"}'))

    def test_channel_detection_accepts_json_embedded_as_an_escaped_string(self):
        inner = json.dumps({"code": 11128, "msg": "Illegal API invocation from an unapproved channel"})
        self.assertTrue(is_channel_check_error(json.dumps({"message": inner})))
        self.assertTrue(is_channel_check_error(json.dumps({"message": json.dumps({"message": inner})})))

    def test_only_primary_chat_url_has_console_fallback(self):
        self.assertEqual(channel_fallback_url("https://synthetic.invalid/v2/chat/completions"),
                         "https://synthetic.invalid/console/chat/completions")
        for url in ("https://synthetic.invalid/console/chat/completions",
                    "https://synthetic.invalid/v2/chat/completions/extra",
                    "https://synthetic.invalid/agenttool/v1/search"):
            self.assertIsNone(channel_fallback_url(url))


class UpstreamCompatEndpointTests(fixtures.RegionRoutingTests):
    def setUp(self):
        super().setUp()
        self.channel_mode = None
        self.channel_uid = None

    def record(self, request):
        uid = request.headers.get("x-user-id")
        self.assertIn(uid, self.allowed_profiles)
        self.requests.append(request)
        return uid

    def handle_upstream(self, request):
        path = request.url.path
        if path == "/agenttool/v1/search":
            uid = self.record(request)
            if self.channel_mode == "always":
                return httpx.Response(400, json=CHANNEL_ERROR)
            return httpx.Response(200, json={"query": "synthetic", "type": "text2text", "provider": "1",
                                             "results": [{"title": "t", "url": "https://example.com/",
                                                          "snippet": "s", "site": "example.com"}],
                                             "total_results": 1, "response_time_ms": 1})
        if self.channel_mode and path in ("/v2/chat/completions", "/console/chat/completions"):
            uid = self.record(request)
            if self.channel_mode in ("first", "model_missing") and self.channel_uid is None:
                self.channel_uid = uid
            if path == "/console/chat/completions":
                if self.channel_mode == "model_missing":
                    return httpx.Response(400, json=MODEL_MISSING)
                if self.channel_mode == "always" or (self.channel_mode == "first" and uid == self.channel_uid):
                    return httpx.Response(400, json=CHANNEL_ERROR)
                return httpx.Response(200, content=fixtures.success_sse(),
                                      headers={"Content-Type": "text/event-stream"})
            if self.channel_mode in ("primary", "always") or uid == self.channel_uid:
                return httpx.Response(400, json=CHANNEL_ERROR)
            return httpx.Response(200, content=fixtures.success_sse(),
                                  headers={"Content-Type": "text/event-stream"})
        return super().handle_upstream(request)

    def configure_profiles(self, *profiles):
        self.configure(profiles=profiles)
        self.allowed_profiles = set(profiles)
        self.channel_uid = None
        self.requests.clear()

    def current_entry(self, uid):
        return next(entry for entry in self.pool.entries() if entry["cm"].summary()["uid"] == uid)

    def test_tool_patterns_are_normalized_across_protocols(self):
        self.configure_profiles("cn-cli")
        for endpoint in fixtures.GENERATIONS:
            with self.subTest(endpoint=endpoint):
                payload = self.payload(endpoint)
                schema = {"type": "object", "properties": {"runId": {"pattern": r"^agent\_run\_"}}}
                if endpoint == "messages":
                    payload["tools"] = [{"name": "agent_run", "input_schema": schema}]
                elif endpoint == "responses":
                    payload["tools"] = [{"type": "function", "name": "agent_run", "parameters": schema}]
                else:
                    payload["tools"] = [{"type": "function", "function": {
                        "name": "agent_run", "parameters": schema}}]
                response = self.client.post("/v1/" + endpoint, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                sent = request_json(self.requests[-1])
                pattern = sent["tools"][0]["function"]["parameters"]["properties"]["runId"]["pattern"]
                self.assertEqual(pattern, "^agent_run_")

    def test_response_format_patterns_are_normalized_end_to_end(self):
        self.configure_profiles("cn-cli")
        for endpoint in ("chat/completions", "responses"):
            with self.subTest(endpoint=endpoint):
                payload = self.payload(endpoint)
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "result", "schema": {"properties": {"runId": {"pattern": r"^agent\_run\_"}}}}}
                response = self.client.post("/v1/" + endpoint, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                sent = request_json(self.requests[-1])
                schema = sent["response_format"]["json_schema"]["schema"]
                self.assertEqual(schema["properties"]["runId"]["pattern"], "^agent_run_")

    def test_channel_check_falls_back_for_every_protocol_and_stream_mode(self):
        self.configure_profiles("cn-cli")
        self.channel_mode = "primary"
        for endpoint in fixtures.GENERATIONS:
            for stream in (False, True):
                with self.subTest(endpoint=endpoint, stream=stream):
                    self.requests.clear()
                    response = self.client.post("/v1/" + endpoint, json=self.payload(endpoint, stream=stream))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual([request.url.path for request in self.requests],
                                     ["/v2/chat/completions", "/console/chat/completions"])

    def test_regular_400_never_tries_console(self):
        self.configure_profiles("cn-cli")
        self.response_status = 400
        response = self.client.post("/v1/chat/completions", json=self.payload())
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual([request.url.path for request in self.requests], ["/v2/chat/completions"])

    def test_rejection_on_both_endpoints_does_not_cool_the_account_or_fail_over(self):
        self.configure_profiles("cn-cli", "cn-work")
        self.channel_mode = "always"
        with patch.dict(converter.CONFIG, {"failover_max": 2}):
            response = self.client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual([request.url.path for request in self.requests],
                         ["/v2/chat/completions", "/console/chat/completions"])
        first_uid = self.requests[0].headers["x-user-id"]
        self.assertEqual(self.requests[1].headers["x-user-id"], first_uid)
        self.assertLessEqual(self.current_entry(first_uid)["fail_until"], time.time())

    def test_channel_rejection_moves_system_to_user_once_on_same_account(self):
        self.configure_profiles("cn-cli")
        self.channel_mode = "always"
        body = self.payload()
        body["messages"].insert(0, {"role": "system", "content": "private system instructions"})
        response = self.client.post("/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual([request.url.path for request in self.requests], [
            "/v2/chat/completions", "/console/chat/completions", "/console/chat/completions"])
        self.assertEqual(len({request.headers["x-user-id"] for request in self.requests}), 1)
        moved = request_json(self.requests[-1])
        self.assertEqual(moved["messages"][0], {"role": "system", "content": "You are a helpful assistant."})
        self.assertEqual(moved["messages"][1]["role"], "user")
        self.assertIn("<instructions>\nprivate system instructions", moved["messages"][1]["content"])
        self.assertLessEqual(self.current_entry(self.requests[0].headers["x-user-id"])["fail_until"], time.time())
    def test_console_model_missing_cools_only_this_account_model(self):
        self.configure_profiles("cn-cli")
        self.channel_mode = "model_missing"
        response = self.client.post("/v1/chat/completions", json=self.payload())
        self.assertEqual(response.status_code, 400, response.text)
        entry = self.current_entry("cn-cli")
        self.assertLessEqual(entry["fail_until"], time.time())
        self.assertFalse(self.pool._model_healthy(entry, "shared-model"))
        # The shared domestic endpoint block stays clear for accounts that still reach v2.
        self.assertTrue(self.pool._model_servable(entry, "shared-model"))

    def test_console_model_missing_can_fail_over_without_cooling_the_account(self):
        self.configure_profiles("cn-cli", "cn-work")
        self.channel_mode = "model_missing"
        with patch.dict(converter.CONFIG, {"failover_max": 1}):
            response = self.client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.requests), 3)
        first_uid = self.requests[0].headers["x-user-id"]
        self.assertEqual(self.requests[1].headers["x-user-id"], first_uid)
        self.assertNotEqual(self.requests[2].headers["x-user-id"], first_uid)
        self.assertLessEqual(self.current_entry(first_uid)["fail_until"], time.time())

    def test_search_channel_rejection_stops_without_cooling(self):
        self.configure_profiles("cn-cli", "cn-work")
        self.channel_mode = "always"
        with patch.dict(converter.CONFIG, {"failover_max": 1}):
            response = self.client.post("/v1/search", json={"query": "synthetic", "max_results": 3})
        first_uid = self.requests[0].headers["x-user-id"]
        self.assertEqual([request.url.path for request in self.requests], ["/agenttool/v1/search"])
        # Search has no console detour; cooling would also stop chat that could still recover there.
        self.assertLessEqual(self.current_entry(first_uid)["fail_until"], time.time())
        self.assertEqual(response.status_code, 400, response.text)


class AutomationCompatTests(unittest.TestCase):
    def transport(self):
        calls = []

        def handle(request):
            calls.append(request)
            if request.url.path == "/v2/chat/completions":
                return httpx.Response(400, json=CHANNEL_ERROR)
            return httpx.Response(200, content=fixtures.success_sse(),
                                  headers={"Content-Type": "text/event-stream"})

        return calls, httpx.MockTransport(handle)

    def test_buddy_chat_uses_console_after_channel_rejection(self):
        calls, transport = self.transport()
        with httpx.Client(transport=transport) as client:
            total = buddy_task._chat(client, {}, {"conversation_id": "conv", "request_id": "req"},
                                     {"id": "shared-model", "name": "Shared"})
        self.assertEqual(total, 3)
        self.assertEqual([request.url.path for request in calls],
                         ["/v2/chat/completions", "/console/chat/completions"])

    def test_growth_chat_never_falls_back_so_the_reported_id_stays_valid(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(400, json=CHANNEL_ERROR)

        transport = httpx.MockTransport(handle)
        real_client = growth_events.httpx.Client
        with patch.object(growth_events.httpx, "Client",
                          side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            with self.assertRaises(growth_events.Failure):
                growth_events.desktop_chat("https://synthetic.invalid", {})
        self.assertEqual([request.url.path for request in calls], ["/v2/chat/completions"])
        self.assertEqual(calls[0].headers["X-Agent-Type"], "auxiliary")
        self.assertEqual(calls[0].headers["X-Agent-Runtime"], "local-agent")
        self.assertEqual(calls[0].headers["X-IDE-Version"], "5.9.3")

    def test_rate_probe_uses_console_after_channel_rejection(self):
        calls = []

        def handle(request):
            calls.append(request)
            if request.url.path == "/v2/chat/completions":
                return httpx.Response(400, json=CHANNEL_ERROR)
            content = (b'data: {"usage":{"credit":0.5,"input_tokens":4,"output_tokens":2}}\n\n'
                       b"data: [DONE]\n\n")
            return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})

        transport = httpx.MockTransport(handle)
        real_client = rate_measurement.httpx.Client
        with patch.object(rate_measurement.httpx, "Client",
                          side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            result = rate_measurement.probe_once("https://synthetic.invalid/v2/chat/completions", {},
                                                 "shared", timeout=5)
        self.assertEqual(result["credit"], 0.5)
        self.assertEqual([request.url.path for request in calls],
                         ["/v2/chat/completions", "/console/chat/completions"])

    def probe_with(self, handle):
        real_client = rate_measurement.httpx.Client
        with patch.object(rate_measurement.httpx, "Client",
                          side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handle),
                                                                   **kwargs)):
            with self.assertRaises(rate_measurement.ProbeError) as caught:
                rate_measurement.probe_once("https://synthetic.invalid/v2/chat/completions", {}, "shared",
                                            timeout=5)
        return caught.exception

    def test_rate_probe_reports_the_console_error_not_the_channel_error(self):
        def handle(request):
            if request.url.path == "/v2/chat/completions":
                return httpx.Response(400, json=CHANNEL_ERROR)
            return httpx.Response(400, json=MODEL_MISSING)

        error = self.probe_with(handle)
        self.assertEqual((error.status_code, error.code), (400, "11102"))

    def test_rate_probe_handles_an_empty_consumed_400_body(self):
        error = self.probe_with(lambda request: httpx.Response(400, content=b""))
        self.assertEqual((error.status_code, error.code), (400, None))

    def test_rate_probe_surfaces_the_console_error_without_retrying(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(400, json={"code": 11102, "msg": "model service info not found"})

        real_client = rate_measurement.httpx.Client
        with patch.object(rate_measurement.httpx, "Client",
                          side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handle),
                                                                   **kwargs)):
            with self.assertRaises(rate_measurement.ProbeError) as caught:
                rate_measurement.probe_once("https://synthetic.invalid/v2/chat/completions", {}, "shared",
                                            timeout=5)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.code, "11102")
        self.assertEqual([request.url.path for request in calls], ["/v2/chat/completions"])


if __name__ == "__main__":
    unittest.main()
