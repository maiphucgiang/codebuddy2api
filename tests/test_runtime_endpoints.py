"""网关限额、协议集成、网络失败与配置回归；所有上游调用均由 MockTransport 接管。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from itertools import product

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import converter
from app import upstream_io


ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
TOOLS = [{"type": "function", "function": {"name": "synthetic_tool", "parameters": {"type": "object"}}}]


def sse(delta=None, finish="stop"):
    return ("data: " + json.dumps({"model": "auto", "choices": [{
        "index": 0, "delta": delta or {"content": "ok", "reasoning_content": "reason"},
        "finish_reason": finish}], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
        + "\n\ndata: [DONE]\n\n").encode()


def payload_for(route, count, stream=True):
    parts = [{"type": "input_text" if route == "/v1/responses" else "text", "text": "keep this text"}]
    for index in range(count):
        url = f"https://synthetic.invalid/image-{index}.png"
        if route == "/v1/responses":
            part = {"type": "input_image", "image_url": url}
        elif route == "/v1/messages":
            part = {"type": "image", "source": {"type": "url", "url": url}}
        else:
            part = {"type": "image_url", "image_url": {"url": url}}
        parts.append(part)
    field = "input" if route == "/v1/responses" else "messages"
    return {"model": "auto", "stream": stream, field: [{"role": "user", "content": parts}]}


def image_urls(body):
    return [part["image_url"]["url"] for message in body["messages"]
            if isinstance(message.get("content"), list) for part in message["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"]


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "model_guard": False,
            "max_images": 16, "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
            "log_body_limit": 65536, "log_path": None, "desensitize": False, "no_compact": False}))
        self.credentials = self.enterContext(patch.object(converter, "_cred_for", return_value=(None, {})))
        self.logs = self.enterContext(patch.object(converter, "_log"))
        self.requests = []
        self.respond = lambda request: httpx.Response(200, content=sse())
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.handle)
        self.enterContext(patch.object(upstream_io.httpx, "AsyncClient",
                                       side_effect=lambda **kw: real_client(transport=transport, **kw)))
        self.client = self.enterContext(TestClient(converter.app))

    def handle(self, request):
        self.requests.append(request)
        return self.respond(request)

    def test_stateful_responses_fields_are_rejected(self):
        """previous_response_id/conversation 依赖服务端历史：本网关无状态，必须显式 400。"""
        for field, value in (("previous_response_id", "resp_abc"), ("conversation", "conv_abc")):
            with self.subTest(field=field):
                self.requests.clear()
                response = self.client.post("/v1/responses", json={
                    "model": "auto", "input": [{"role": "user", "content": "hi"}], field: value})
                self.assertEqual(response.status_code, 400, response.text)
                body = response.json()
                error = body.get("error") or body["detail"]["error"]
                self.assertEqual(error["param"], field)
                self.assertEqual(len(self.requests), 0)

    def test_multiple_candidates_are_rejected_before_reaching_upstream(self):
        """聚合路径无法保持多候选独立：n 只能缺省或恰为 1。"""
        for n in (2, 0, "2", True, 1.5):
            with self.subTest(n=n):
                self.requests.clear()
                response = self.client.post("/v1/chat/completions", json={
                    "model": "auto", "messages": [{"role": "user", "content": "hi"}], "n": n})
                self.assertEqual(response.status_code, 400, response.text)
                body = response.json()
                error = body.get("error") or body["detail"]["error"]
                self.assertEqual(error["param"], "n")
                self.assertEqual(len(self.requests), 0)
        self.requests.clear()
        response = self.client.post("/v1/chat/completions", json={
            "model": "auto", "messages": [{"role": "user", "content": "hi"}], "n": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.requests), 1)

    def test_tool_arguments_must_be_objects_of_declared_tools(self):
        """解析成功不等于正确：非对象参数或未声明的函数名都不健康。"""
        body = {"tools": TOOLS}
        call = lambda name, args: [{"id": "c1", "function": {"name": name, "arguments": args}}]
        healthy = converter._tool_calls_healthy
        self.assertTrue(healthy(None, body))
        self.assertTrue(healthy(call("synthetic_tool", "{}"), body))
        self.assertTrue(healthy(call("synthetic_tool", "{\"x\": 1}"), body))
        for bad_args in ("null", "[]", "42", "\"text\"", "{"):
            with self.subTest(args=bad_args):
                self.assertFalse(healthy(call("synthetic_tool", bad_args), body))
        self.assertFalse(healthy(call("undeclared", "{}"), body))
        self.assertFalse(healthy(call("", "{}"), body))
        # 未声明任何工具的请求不做名称核对：合法 JSON 对象的工具调用仍算健康
        self.assertTrue(healthy(call("anything", "{}"), {"tools": []}))
        self.assertTrue(healthy(call("anything", "{}"), None))

    def test_count_tokens_estimates_instead_of_constant_zero(self):
        """计数端点返回随输入增长的估算值，而不是伪装精确的常量 0。"""
        short = self.client.post("/v1/messages/count_tokens", json={
            "model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(short.status_code, 200, short.text)
        small = short.json()["input_tokens"]
        self.assertGreater(small, 0)
        long = self.client.post("/v1/messages/count_tokens", json={
            "model": "auto", "system": "s" * 400,
            "messages": [{"role": "user", "content": "x" * 4000}]})
        big = long.json()["input_tokens"]
        self.assertGreater(big, small)
        cjk = self.client.post("/v1/messages/count_tokens", json={
            "model": "auto", "messages": [{"role": "user", "content": "汉" * 100}]})
        self.assertGreaterEqual(cjk.json()["input_tokens"], 100)  # 非 ASCII 不按 4 字符折算低估
        bad = self.client.post("/v1/messages/count_tokens", content=b"{ not json",
                               headers={"Content-Type": "application/json"})
        self.assertEqual(bad.status_code, 400)

    def test_inference_errors_follow_the_client_protocol_shape(self):
        """OpenAI 路由顶层 error；Anthropic 路由 error 对象；/admin 保持 detail 包装。"""
        converter.CONFIG["api_key"] = "secret"
        try:
            for route in ("/v1/chat/completions", "/v1/responses"):
                with self.subTest(route=route):
                    response = self.client.post(route, json={})
                    self.assertEqual(response.status_code, 401, response.text)
                    body = response.json()
                    self.assertIn("error", body)
                    self.assertNotIn("detail", body)
                    self.assertEqual(body["error"]["type"], "auth_error")
            response = self.client.post("/v1/messages", json={})
            self.assertEqual(response.status_code, 401, response.text)
            body = response.json()
            self.assertEqual(body["type"], "error")
            self.assertEqual(body["error"]["type"], "authentication_error")
            # Anthropic 约定 404 → not_found_error，即使内层写的是 invalid_request_error
            converter.CONFIG["model_guard"] = True
            try:
                missing = self.client.post("/v1/messages", json={
                    "model": "no-such-model", "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer secret"})
                self.assertEqual(missing.status_code, 404, missing.text)
                self.assertEqual(missing.json()["error"]["type"], "not_found_error")
            finally:
                converter.CONFIG["model_guard"] = False
            response = self.client.get("/admin/credentials")
            self.assertEqual(response.status_code, 401, response.text)
            self.assertIn("detail", response.json())
        finally:
            converter.CONFIG["api_key"] = ""

    def test_omitted_stream_defaults_to_nonstream_and_bad_type_rejected(self):
        """省略 stream 按协议默认非流式返回完整 JSON；非布尔 stream 显式 400。"""
        bodies = {
            "/v1/chat/completions": {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            "/v1/responses": {"model": "auto", "input": [{"role": "user", "content": "hi"}]},
            "/v1/messages": {"model": "auto", "max_tokens": 64,
                             "messages": [{"role": "user", "content": "hi"}]},
        }
        for route, body in bodies.items():
            with self.subTest(route=route):
                self.requests.clear()
                response = self.client.post(route, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["content-type"], "application/json")
                self.assertNotIn("data:", response.text)
                response = self.client.post(route, json={**body, "stream": "true"})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["param"], "stream")
                response = self.client.post(route, json={**body, "stream": False})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["content-type"], "application/json")

    def test_discarded_tool_generations_are_recorded_with_usage(self):
        """损坏工具调用触发的额外生成：每次丢弃都带用量记入 attempts；预算可配。"""
        good = {"tool_calls": [{"index": 0, "id": "ok", "type": "function",
                                "function": {"name": "synthetic_tool", "arguments": "{}"}}]}
        bad = {"tool_calls": [{"index": 0, "id": "bad", "type": "function",
                               "function": {"name": "synthetic_tool", "arguments": "{"}}]}
        attempts = []
        with patch.object(converter, "observe_attempt",
                          side_effect=lambda stage, **kw: attempts.append((stage, kw))):
            calls = {"n": 0}
            def flaky(request):
                calls["n"] += 1
                return httpx.Response(200, content=sse(bad if calls["n"] == 1 else good, "tool_calls"))
            self.respond = flaky
            self.requests.clear()
            payload = payload_for(ROUTES[0], 0)
            payload["tools"] = TOOLS
            response = self.client.post(ROUTES[0], json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(calls["n"], 2)
            retries = [kw for stage, kw in attempts if stage == "tool_args_retry"]
            self.assertEqual(len(retries), 1)
            self.assertEqual(retries[0]["attempt"], 1)
            self.assertIn("total_tokens", retries[0])  # 被丢弃的生成用量不再消失

            converter.CONFIG["tool_call_max_retry"] = 0
            try:
                self.respond = lambda request: httpx.Response(200, content=sse(bad, "tool_calls"))
                self.requests.clear()
                nonstream = dict(payload, stream=False)  # 非流式：错误直接体现为 HTTP 状态码
                response = self.client.post(ROUTES[0], json=nonstream)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(len(self.requests), 1)  # 预算 0：不重试
                # 耗尽预算的末次生成也必须带着用量出现在 attempts 里
                exhausted = [kw for stage, kw in attempts if stage == "tool_args_exhausted"]
                self.assertEqual(len(exhausted), 1)
                self.assertIn("total_tokens", exhausted[0])
            finally:
                converter.CONFIG["tool_call_max_retry"] = 3

    def test_credential_selection_runs_off_the_event_loop(self):
        """_route_chat 内含线程锁/文件锁/同步刷新：三个端点都必须经线程池调用它。"""
        import inspect
        import re
        src = inspect.getsource(converter)
        direct = re.findall(r"^\s+(?:body|chat_body), cred, headers, url = _route_chat\(", src, re.M)
        pooled = re.findall(r"await run_in_threadpool\(_route_chat", src)
        self.assertEqual(direct, [])
        # 三个端点各一次，另外换凭证重放（_routed_stream / _routed_fetch）还要再路由一次：
        # 只要没有任何直调（direct 为空），线程池约束就仍然成立。
        self.assertGreaterEqual(len(pooled), 3)

    def test_tool_metadata_policy_reaches_all_protocols(self):
        description = "Read sandbox data without destructive changes."
        schema = {"type": "object", "title": "Lookup inputs", "properties": {
            "path": {"type": "string", "title": "Data path", "description": description, "enum": ["sandbox", "local"]}},
            "required": ["path"]}
        function = {"name": "lookup_data", "description": description, "parameters": schema}
        for route, keep, desensitize, no_compact, stream in product(ROUTES, (False, True), (False, True), (False, True), (False, True)):
            with self.subTest(route=route, keep=keep, desensitize=desensitize, no_compact=no_compact, stream=stream):
                converter.CONFIG.update(keep_tool_metadata=keep, desensitize=desensitize, no_compact=no_compact)
                payload = payload_for(route, 0, stream)
                if route == "/v1/messages":
                    payload["tools"] = [{"name": function["name"], "description": description, "input_schema": schema}]
                elif route == "/v1/responses":
                    payload["tools"] = [{"type": "function", **function}]
                else:
                    payload["tools"] = [{"type": "function", "function": function}]
                self.requests.clear()
                response = self.client.post(route, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(self.requests), 1)
                sent = json.loads(self.requests[0].content)["tools"][0]["function"]
                retained = keep or (not desensitize and route != "/v1/responses")
                self.assertEqual("description" in sent, retained)
                self.assertEqual("title" in sent["parameters"], retained)
                prop = sent["parameters"]["properties"]["path"]
                self.assertEqual("description" in prop, retained)
                self.assertEqual("title" in prop, retained)
                self.assertEqual(sent["name"], function["name"])
                self.assertEqual(sent["parameters"]["required"], ["path"])
                self.assertEqual(prop["enum"], ["sandbox", "local"])
                if retained:
                    self.assertEqual(sent["description"].replace("\u200b", ""), description)
                    self.assertEqual("\u200b" in sent["description"], desensitize)

    def test_kept_tool_descriptions_still_obey_request_size_budget(self):
        converter.CONFIG.update(keep_tool_metadata=True, desensitize=True, max_request_bytes=2048)
        for route in ROUTES:
            with self.subTest(route=route):
                payload = payload_for(route, 0, False)
                function = {"name": "lookup_data", "description": "x" * 4096, "parameters": {"type": "object"}}
                if route == "/v1/messages":
                    payload["tools"] = [{"name": function["name"], "description": function["description"], "input_schema": function["parameters"]}]
                elif route == "/v1/responses":
                    payload["tools"] = [{"type": "function", **function}]
                else:
                    payload["tools"] = [{"type": "function", "function": function}]
                response = self.client.post(route, json=payload)
                self.assertEqual(response.status_code, 413, response.text)
                self.assertEqual(response.json()["error"]["code"], "request_too_large")
        self.credentials.assert_not_called()
        self.assertFalse(self.requests)

    def test_default_truncates_to_newest_16_for_all_routes_and_stream_flags(self):
        for route in ROUTES:
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    response = self.client.post(route, json=payload_for(route, 17, stream))
                    self.assertEqual(response.status_code, 200, response.text)
                    urls = image_urls(json.loads(self.requests[-1].content))
                    self.assertEqual(urls, [f"https://synthetic.invalid/image-{i}.png" for i in range(1, 17)])

    def test_custom_limit_and_desensitization_preserve_retained_images(self):
        converter.CONFIG.update(max_images=2, desensitize=True)
        for route in ROUTES:
            with self.subTest(route=route):
                response = self.client.post(route, json=payload_for(route, 4))
                self.assertEqual(response.status_code, 200, response.text)
                body = json.loads(self.requests[-1].content)
                self.assertEqual(image_urls(body), [f"https://synthetic.invalid/image-{i}.png" for i in (2, 3)])
                self.assertIn("keep this text", json.dumps(body))

    def test_error_policy_rejects_before_conversion_credentials_and_upstream(self):
        converter.CONFIG["image_policy"] = "error"
        for route in ROUTES:
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    response = self.client.post(route, json=payload_for(route, 17, stream))
                    self.assertEqual(response.status_code, 413)
                    error = response.json()["error"]
                    self.assertEqual((error["code"], error["image_count"], error["max_images"]),
                                     ("too_many_images", 17, 16))
        self.credentials.assert_not_called()
        self.assertFalse(self.requests)
        self.assertFalse(any("synthetic.invalid" in c.args[0] for c in self.logs.call_args_list))

    def test_exact_limit_and_text_only_pass_error_policy(self):
        converter.CONFIG["image_policy"] = "error"
        for route in ROUTES:
            for count in (0, 16):
                with self.subTest(route=route, count=count):
                    self.assertEqual(self.client.post(route, json=payload_for(route, count)).status_code, 200)

    def test_zero_limit_means_no_images_for_both_policies(self):
        converter.CONFIG["max_images"] = 0
        for route in ROUTES:
            converter.CONFIG["image_policy"] = "truncate"
            self.assertEqual(self.client.post(route, json=payload_for(route, 1)).status_code, 200)
            self.assertEqual(image_urls(json.loads(self.requests[-1].content)), [])
            converter.CONFIG["image_policy"] = "error"
            self.assertEqual(self.client.post(route, json=payload_for(route, 1)).status_code, 413)
            self.assertEqual(self.client.post(route, json=payload_for(route, 0)).status_code, 200)

    def test_byte_limit_rejects_remaining_large_image(self):
        converter.CONFIG["max_request_bytes"] = 1024
        payload = payload_for(ROUTES[0], 1)
        payload["messages"][0]["content"][1]["image_url"]["url"] = "data:image/png;base64," + "A" * 2000
        response = self.client.post(ROUTES[0], json=payload)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")
        self.credentials.assert_not_called()
        self.assertFalse(self.requests)

    def test_image_truncation_precedes_byte_limit(self):
        converter.CONFIG.update(max_images=1, max_request_bytes=1024)
        payload = payload_for(ROUTES[0], 2)
        payload["messages"][0]["content"][1]["image_url"]["url"] = "data:image/png;base64," + "A" * 2000
        response = self.client.post(ROUTES[0], json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(image_urls(json.loads(self.requests[-1].content)), ["https://synthetic.invalid/image-1.png"])

    def test_request_byte_budget_uses_actual_utf8_encoding(self):
        body = {"messages": [{"role": "user", "content": "汉字"}]}
        size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
        converter.CONFIG["max_request_bytes"] = size
        converter._guard_request_size(body)
        converter.CONFIG["max_request_bytes"] = size - 1
        with self.assertRaises(converter.HTTPException) as caught:
            converter._guard_request_size(body)
        self.assertEqual(caught.exception.status_code, 413)

    def test_bad_payloads_and_auth_fail_without_upstream(self):
        for route in ROUTES:
            for value in (None, [], "not an object"):
                self.assertEqual(self.client.post(route, json=value).status_code, 400)
        converter.CONFIG["api_key"] = "synthetic-key"
        response = self.client.post(ROUTES[0], json=payload_for(ROUTES[0], 17))
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.requests)

    def test_retained_responses_file_id_returns_400(self):
        payload = payload_for(ROUTES[1], 0)
        payload["input"][0]["content"].append({"type": "input_image", "file_id": "synthetic-file"})
        self.assertEqual(self.client.post(ROUTES[1], json=payload).status_code, 400)
        self.assertFalse(self.requests)

    def test_named_tool_choice_is_required_and_keeps_only_named_function(self):
        self.respond = lambda request: httpx.Response(200, content=sse({"tool_calls": [{
            "index": 0, "id": "synthetic-id", "type": "function",
            "function": {"name": "chosen", "arguments": "{}"}}]}, "tool_calls"))
        for route in ROUTES:
            payload = payload_for(route, 0, False)
            if route == "/v1/messages":
                payload["tools"] = [{"name": name, "input_schema": {"type": "object"}} for name in ("other", "chosen")]
                payload["tool_choice"] = {"type": "tool", "name": "chosen"}
            elif route == "/v1/responses":
                payload["tools"] = [{"type": "function", "name": name, "parameters": {"type": "object"}} for name in ("other", "chosen")]
                payload["tool_choice"] = {"type": "function", "name": "chosen"}
            else:
                payload["tools"] = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}} for name in ("other", "chosen")]
                payload["tool_choice"] = {"type": "function", "function": {"name": "chosen"}}
            original = json.dumps(payload)
            response = self.client.post(route, json=payload)
            self.assertEqual(response.status_code, 200, f"{route}: {response.text}")
            sent = json.loads(self.requests[-1].content)
            self.assertEqual(sent["tool_choice"], "required")
            self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["chosen"])
            self.assertEqual(json.dumps(payload), original)

    def test_invalid_or_ambiguous_named_choice_is_rejected_locally(self):
        for tools, choice in (([], {"type": "function", "function": {"name": "missing"}}),
                              (TOOLS * 2, {"type": "function", "function": {"name": "synthetic_tool"}}),
                              (TOOLS, {"type": "other"}), (TOOLS, {"type": "function", "function": 42})):
            payload = payload_for(ROUTES[0], 0, False)
            payload.update(tools=tools, tool_choice=choice)
            self.assertEqual(self.client.post(ROUTES[0], json=payload).status_code, 400)
        self.credentials.assert_not_called()
        self.assertFalse(self.requests)


    def test_required_choice_never_turns_missing_or_wrong_tool_into_success(self):
        for delta in ({"content": "ignored tool choice"}, {"tool_calls": [{"index": 0, "id": "synthetic-id",
                      "type": "function", "function": {"name": "undeclared", "arguments": "{}"}}]}):
            self.requests.clear()
            self.respond = lambda request: httpx.Response(200, content=sse(delta))
            payload = payload_for(ROUTES[0], 0, False)
            payload.update(tools=TOOLS, tool_choice="required")
            self.assertEqual(self.client.post(ROUTES[0], json=payload).status_code, 502)
            self.assertEqual(len(self.requests), 4)


    def test_connection_failure_retries_only_once_with_finite_timeouts(self):
        def respond(request):
            if len(self.requests) == 1:
                raise httpx.ConnectError("synthetic connect failure", request=request)
            return httpx.Response(200, content=sse())
        self.respond = respond
        response = self.client.post(ROUTES[0], json=payload_for(ROUTES[0], 0, False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0].content, self.requests[1].content)
        self.assertTrue(all(value is not None for value in self.requests[0].extensions["timeout"].values()))

    def test_connection_retry_exhaustion_is_bounded(self):
        def respond(request):
            raise httpx.ConnectTimeout("", request=request)
        self.respond = respond
        response = self.client.post(ROUTES[0], json=payload_for(ROUTES[0], 0, False))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("ConnectTimeout", response.text)

    def test_ambiguous_disconnect_and_read_timeout_never_replay_post(self):
        for route in ROUTES:
            for stream in (False, True):
                for error_type in (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.WriteError):
                    with self.subTest(route=route, stream=stream, error_type=error_type):
                        self.requests.clear()
                        def respond(request):
                            raise error_type("", request=request)
                        self.respond = respond
                        payload = payload_for(route, 0, stream)
                        payload["tools"] = TOOLS
                        response = self.client.post(route, json=payload)
                        self.assertEqual(len(self.requests), 1)
                        self.assertIn(error_type.__name__, response.text)
                        self.assertNotIn("response.completed", response.text)
                        self.assertNotIn("message_stop", response.text)

    def test_http_errors_never_retry(self):
        for status in (400, 401, 403, 413, 429, 503):
            with self.subTest(status=status):
                self.requests.clear()
                self.respond = lambda request: httpx.Response(status, json={"error": {"message": "synthetic rejection"}})
                response = self.client.post(ROUTES[0], json=payload_for(ROUTES[0], 0, False))
                self.assertEqual(response.status_code, status)
                self.assertEqual(len(self.requests), 1)

    def test_partial_stream_error_does_not_emit_success_or_retry(self):
        class Partial(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                raise httpx.ReadError("synthetic disconnect")
        self.respond = lambda request: httpx.Response(200, stream=Partial())
        for route in ROUTES:
            with self.subTest(route=route):
                self.requests.clear()
                self.logs.reset_mock()
                response = self.client.post(route, json=payload_for(route, 0))
                self.assertEqual(len(self.requests), 1)
                self.assertIn("ReadError", response.text)
                self.assertNotIn("response.completed", response.text)
                self.assertNotIn("message_stop", response.text)
                self.assertFalse(any("◀ RESPONSE" in c.args[0] for c in self.logs.call_args_list))

    def test_empty_malformed_and_unterminated_streams_fail_closed(self):
        for raw in (b"", b"data: not-json\n\n", b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'):
            with self.subTest(raw=raw):
                self.requests.clear()
                self.respond = lambda request: httpx.Response(200, content=raw)
                response = self.client.post(ROUTES[0], json=payload_for(ROUTES[0], 0, False))
                self.assertEqual(response.status_code, 502)
                self.assertEqual(len(self.requests), 1)

    def test_done_marker_stops_reading_without_waiting_for_connection_close(self):
        class DoneThenFailure(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse()
                raise AssertionError("must not read beyond DONE")
        self.respond = lambda request: httpx.Response(200, stream=DoneThenFailure())
        for route in ROUTES:
            for stream in (False, True):
                with self.subTest(route=route, stream=stream):
                    response = self.client.post(route, json=payload_for(route, 0, stream))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("ok", response.text)

    def test_invalid_upstream_field_types_return_502(self):
        chunks = [
            {"choices": "bad"}, {"choices": [1]}, {"usage": True},
            {"choices": [{"delta": {"content": 1}, "finish_reason": "stop"}]},
            {"choices": [{"delta": {"tool_calls": [{"index": "bad"}]}, "finish_reason": "stop"}]},
            {"choices": [{"delta": {"tool_calls": [{"function": {"name": 1}}]}, "finish_reason": "stop"}]},
            {"choices": [{"delta": {}, "finish_reason": []}]},
            {"error": {"message": "synthetic SSE failure"}},
        ]
        for chunk in chunks:
            with self.subTest(chunk=chunk):
                self.requests.clear()
                raw = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                self.respond = lambda request: httpx.Response(200, content=raw)
                response = self.client.post(ROUTES[0], json=payload_for(ROUTES[0], 0, False))
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(len(self.requests), 1)


    def test_invalid_tool_calls_exhaust_retries_without_returning_bad_tool(self):
        bad = {"tool_calls": [{"index": 0, "id": "bad-call", "function": {
            "name": "synthetic_tool", "arguments": "{"}}]}
        self.respond = lambda request: httpx.Response(200, content=sse(bad, "tool_calls"))
        for route in ROUTES:
            with self.subTest(route=route):
                self.requests.clear()
                payload = payload_for(route, 0)
                payload["tools"] = TOOLS
                response = self.client.post(route, json=payload)
                self.assertEqual(len(self.requests), 4)
                self.assertIn("Invalid upstream tool_calls", response.text)
                self.assertNotIn("bad-call", response.text)
                self.assertNotIn("response.completed", response.text)
                self.assertNotIn("message_stop", response.text)


class TransportBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_is_not_retried(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            raise asyncio.CancelledError()

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch.object(upstream_io.httpx, "AsyncClient",
                          side_effect=lambda **kw: real_client(transport=transport, **kw)):
            with self.assertRaises(asyncio.CancelledError):
                async with upstream_io.open_backend_stream("https://synthetic.invalid", {}, {}):
                    self.fail("cancelled request must not open a response")
        self.assertEqual(calls, 1)


class InboundBodyLimitTests(unittest.TestCase):
    """入站原始字节限量：解析前 413，chunked 同样受限，/admin 不受影响。"""

    def _app(self, limit):
        from app.inbound_limits import InboundBodyLimitMiddleware
        from fastapi import Request
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def inference(request: Request):
            return {"size": len(await request.body())}

        @app.post("/admin/x")
        async def admin(request: Request):
            return {"size": len(await request.body())}

        app.add_middleware(InboundBodyLimitMiddleware, config={"max_inbound_bytes": limit})
        return TestClient(app)

    def test_over_limit_rejected_before_parsing_and_under_limit_passes(self):
        client = self._app(1024)
        ok = client.post("/v1/chat/completions", json={"messages": []})
        self.assertEqual(ok.status_code, 200, ok.text)
        big = client.post("/v1/chat/completions", content=b"x" * 2048,
                          headers={"Content-Type": "application/json"})
        self.assertEqual(big.status_code, 413)
        self.assertEqual(big.json()["error"]["code"], "request_too_large")
        self.assertNotIn("detail", big.json())
        big_admin = client.post("/admin/x", content=b"x" * 2048)
        self.assertEqual(big_admin.status_code, 200)  # 管理路由不在此限量范围

    def test_chunked_body_is_counted_and_rejected(self):
        from app.inbound_limits import InboundBodyLimitMiddleware
        reached = []

        async def app(scope, receive, send):
            reached.append(True)

        middleware = InboundBodyLimitMiddleware(app, {"max_inbound_bytes": 10})
        chunks = [{"type": "http.request", "body": b"12345678", "more_body": True},
                  {"type": "http.request", "body": b"9" * 8, "more_body": False}]
        sent = []

        async def receive():
            return chunks.pop(0) if chunks else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        import asyncio
        asyncio.run(middleware({"type": "http", "method": "POST", "path": "/v1/chat/completions"}, receive, send))
        self.assertFalse(reached)  # 超限请求不进入下游
        self.assertEqual(sent[0]["status"], 413)


class InboundStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_stream(self, path, disconnect=False):
        from app.inbound_limits import ConcurrencyLimitMiddleware, InboundBodyLimitMiddleware
        from starlette.responses import StreamingResponse

        disconnected = asyncio.Event()
        closed = asyncio.Event()
        chunks = [{"type": "http.request", "body": b"{", "more_body": True},
                  {"type": "http.request", "body": b"}", "more_body": False}]
        sent = []

        async def receive():
            if chunks:
                return chunks.pop(0)
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if disconnect and message["type"] == "http.response.body" and message.get("body"):
                disconnected.set()

        async def stream():
            try:
                yield b"data: first\n\n"
                if disconnect:
                    await asyncio.Event().wait()
                else:
                    await asyncio.sleep(0)
                    yield b"data: [DONE]\n\n"
            finally:
                closed.set()

        async def app(scope, receive, send):
            request = await receive()
            self.assertEqual(request["body"], b"{}")
            self.assertFalse(request["more_body"])
            await StreamingResponse(stream(), media_type="text/event-stream")(scope, receive, send)

        config = {"max_inbound_bytes": 1024, "max_concurrent": 1}
        middleware = ConcurrencyLimitMiddleware(InboundBodyLimitMiddleware(app, config), config)
        scope = {"type": "http", "method": "POST", "path": path,
                 "asgi": {"version": "3.0", "spec_version": "2.3"}}
        await asyncio.wait_for(middleware(scope, receive, send), 2)
        self.assertTrue(closed.is_set())
        self.assertFalse(middleware._gate().locked())
        body = b"".join(m.get("body", b"") for m in sent)
        self.assertIn(b"data: first", body)
        if disconnect:
            self.assertNotIn(b"[DONE]", body)
        else:
            self.assertIn(b"[DONE]", body)
            self.assertFalse(sent[-1].get("more_body", False))

    async def test_buffered_requests_keep_streaming_until_completion(self):
        for path in ROUTES:
            with self.subTest(path=path):
                await self.exercise_stream(path)

    async def test_real_disconnect_closes_the_stream_and_releases_capacity(self):
        await self.exercise_stream("/v1/messages", disconnect=True)



class ConcurrencyLimitTests(unittest.IsolatedAsyncioTestCase):
    """并发上限：名额占满立即 503（含 Retry-After），释放后恢复。"""

    async def test_full_gate_returns_503_and_recovers(self):
        from app.inbound_limits import ConcurrencyLimitMiddleware
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_app(scope, receive, send):
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        mw = ConcurrencyLimitMiddleware(slow_app, {"max_concurrent": 1})
        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions"}

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        first = asyncio.create_task(mw(scope, receive, send))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for path in ROUTES:
                with self.subTest(path=path):
                    sent.clear()
                    await mw({**scope, "path": path}, receive, send)
                    self.assertEqual(sent[0]["status"], 503)
                    headers = dict(sent[0]["headers"])
                    self.assertEqual(headers[b"retry-after"], b"3")
                    body = sent[1]["body"]
                    self.assertEqual(int(headers[b"content-length"]), len(body))
                    payload = json.loads(body)
                    self.assertEqual(payload["error"]["code"], "concurrency_limit")
                    if path == "/v1/messages":
                        self.assertEqual(payload["type"], "error")
                        self.assertEqual(payload["error"]["type"], "api_error")
                    else:
                        self.assertNotIn("type", payload)
                        self.assertEqual(payload["error"]["type"], "rate_limit_error")
        finally:
            release.set()
            await asyncio.wait_for(first, 2)
        sent.clear()
        await mw(scope, receive, send)
        self.assertEqual(sent[0]["status"], 200)


class AuxiliaryCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_saturated_generation_gate_does_not_block_token_counting(self):
        from app.inbound_limits import ConcurrencyLimitMiddleware

        middleware = ConcurrencyLimitMiddleware(converter.app, {"max_concurrent": 1})
        gate = middleware._gate()
        await gate.acquire()
        try:
            with patch.dict(converter.CONFIG, {"api_key": ""}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware),
                                             base_url="http://test") as client:
                    response = await client.post("/v1/messages/count_tokens", json={
                        "model": "auto", "messages": [{"role": "user", "content": "hello"}]})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertGreater(response.json()["input_tokens"], 0)
                    unknown = await client.post("/v1/messages/unknown", json={})
                    self.assertEqual(unknown.status_code, 404)
                    wrong_method = await client.get("/v1/messages")
                    self.assertEqual(wrong_method.status_code, 405)
            self.assertTrue(gate.locked())
        finally:
            gate.release()



class ConfigurationTests(unittest.TestCase):
    def configure(self, env=None, flags=(), invalid=False, stored=None, expected_host=None):
        with contextlib.ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            if stored:
                from app.control_store import ControlStore
                store = ControlStore(Path(directory) / "control.sqlite3")
                try:
                    store.update_settings(stored, store.snapshot()["revision"])
                finally:
                    store.close()
            stack.enter_context(patch.object(converter, "managed_auth_dir", return_value=Path(directory)))
            stack.enter_context(patch.object(converter, "app", FastAPI()))
            stack.enter_context(patch.dict(os.environ, env or {}, clear=True))
            stack.enter_context(patch.dict(converter.CONFIG))
            stack.enter_context(patch("sys.argv", ["converter.py", "--skip-check", *flags]))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            seed = stack.enter_context(patch.object(converter, "seed_credentials"))
            stack.enter_context(patch.object(converter, "CredentialPool", return_value=Mock(first=lambda: None)))
            stack.enter_context(patch.object(converter, "credits_mod", None))
            stack.enter_context(patch.object(converter.threading, "Thread"))
            server = stack.enter_context(patch.object(converter.uvicorn, "run"))
            from app import runtime_management
            close = stack.enter_context(patch.object(runtime_management, "close", wraps=runtime_management.close))
            if invalid:
                with self.assertRaises(SystemExit) as caught:
                    converter.main()
                self.assertEqual(caught.exception.code, 2)
                seed.assert_not_called()
                server.assert_not_called()
                if stored:
                    close.assert_called_once_with(converter.CONFIG)
                return
            converter.main()
            server.assert_called_once()
            if expected_host is not None:
                self.assertEqual(server.call_args.kwargs["host"], expected_host)
            return {key: converter.CONFIG[key] for key in (
                "max_images", "image_policy", "max_request_bytes", "log_body_limit", "admin_csrf", "keep_tool_metadata")}

    def test_defaults(self):
        self.assertEqual(self.configure(), {"max_images": 16, "image_policy": "truncate",
                                           "max_request_bytes": 33554432, "log_body_limit": 65536,
                                           "admin_csrf": True, "keep_tool_metadata": False})

    def test_open_binding_without_key_requires_explicit_opt_in(self):
        # 非回环 + 空 key：默认拒启（SystemExit 2）
        self.configure(flags=("--host", "0.0.0.0"), invalid=True)
        # 显式放行环境变量后可启动
        self.configure(env={"CODEBUDDY2API_ALLOW_OPEN_NOAUTH": "true"}, flags=("--host", "0.0.0.0"))
        # 非回环但设了 key：正常
        self.configure(env={"CODEBUDDY2API_KEY": "k"}, flags=("--host", "0.0.0.0"))

    def test_persisted_host_is_validated_after_configuration_resolution(self):
        for host in ("0.0.0.0", "::"):
            with self.subTest(host=host):
                self.configure(stored={"host": host}, invalid=True)
                self.configure(stored={"host": host}, env={"CODEBUDDY2API_KEY": "k"}, expected_host=host)
                self.configure(stored={"host": host}, env={"CODEBUDDY2API_ALLOW_OPEN_NOAUTH": "true"},
                               expected_host=host)
        self.configure(stored={"host": "0.0.0.0"}, flags=("--host", "127.0.0.1"), expected_host="127.0.0.1")
        self.configure(stored={"host": "127.0.0.1"}, flags=("--host", "0.0.0.0"), invalid=True)
        self.configure(stored={"host": "0.0.0.0"}, env={"CODEBUDDY2API_KEY": ""},
                       flags=("--api-key", "k"), expected_host="0.0.0.0")


    def test_environment_and_explicit_cli_precedence(self):
        env = {"CODEBUDDY2API_MAX_IMAGES": "8", "CODEBUDDY2API_IMAGE_POLICY": "error",
               "CODEBUDDY2API_MAX_REQUEST_BYTES": "100000", "CODEBUDDY2API_LOG_BODY_LIMIT": "0"}
        self.assertEqual(self.configure(env), {"max_images": 8, "image_policy": "error",
                                               "max_request_bytes": 100000, "log_body_limit": 0,
                                               "admin_csrf": True, "keep_tool_metadata": False})
        env["CODEBUDDY2API_IMAGE_POLICY"] = "invalid-overridden"
        self.assertEqual(self.configure(env, ("--max-images", "0", "--image-policy", "truncate"))["max_images"], 0)

    def test_admin_csrf_startup_flag_and_environment_precedence(self):
        cases = [
            ({}, ("--admin-csrf", "false"), False),
            ({"CODEBUDDY2API_ADMIN_CSRF": "false"}, (), False),
            ({"CODEBUDDY2API_ADMIN_CSRF": "true"}, (), True),
            ({"CODEBUDDY2API_ADMIN_CSRF": "true"}, ("--admin-csrf", "false"), False),
            ({"CODEBUDDY2API_ADMIN_CSRF": "false"}, ("--admin-csrf", "true"), True),
            ({"CODEBUDDY2API_ADMIN_CSRF": "false"}, ("--admin-csrf",), True),
            ({"CODEBUDDY2API_ADMIN_CSRF": "invalid-overridden"}, ("--admin-csrf", "true"), True),
        ]
        for env, flags, expected in cases:
            with self.subTest(env=env, flags=flags):
                self.assertIs(self.configure(env, flags)["admin_csrf"], expected)

    def test_tool_metadata_flag_and_environment_precedence(self):
        key = "CODEBUDDY2API_KEEP_TOOL_METADATA"
        cases = [
            ({}, ("--keep-tool-metadata",), True),
            ({}, ("--keep-tool-metadata", "false"), False),
            ({key: "true"}, (), True),
            ({key: "false"}, (), False),
            ({key: "true"}, ("--keep-tool-metadata", "false"), False),
            ({key: "false"}, ("--keep-tool-metadata", "true"), True),
            ({key: "invalid-overridden"}, ("--keep-tool-metadata",), True),
        ]
        for env, flags, expected in cases:
            with self.subTest(env=env, flags=flags):
                self.assertIs(self.configure(env, flags)["keep_tool_metadata"], expected)
        self.configure({key: "invalid"}, invalid=True)
        self.configure({key: ""}, invalid=True)
        self.configure(flags=("--keep-tool-metadata", "invalid"), invalid=True)

    def test_invalid_config_fails_before_side_effects(self):
        for env in ({"CODEBUDDY2API_MAX_IMAGES": "-1"}, {"CODEBUDDY2API_MAX_IMAGES": "1.5"},
                    {"CODEBUDDY2API_IMAGE_POLICY": "drop"}, {"CODEBUDDY2API_MAX_REQUEST_BYTES": "0"},
                    {"CODEBUDDY2API_LOG_BODY_LIMIT": "-1"}, {"CODEBUDDY2API_ADMIN_CSRF": "invalid"}):
            with self.subTest(env=env):
                self.configure(env, invalid=True)
        self.configure(flags=("--max-images", "-1"), invalid=True)
        self.configure(flags=("--admin-csrf", "invalid"), invalid=True)


class LogIntegrationTests(unittest.TestCase):
    def test_log_rotation_is_bounded_and_thread_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.log"
            with patch.dict(converter.CONFIG, {"log_path": str(path), "log_body_limit": 256}), \
                    patch.object(converter, "LOG_MAX_BYTES", 2048):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(converter._log, [f"event={i} " + "汉字" * 1000 for i in range(30)]))
            files = list(Path(directory).glob("test.log*"))
            self.assertLessEqual(len(files), 3)
            self.assertTrue(all(file.stat().st_size <= 2048 for file in files))
            for file in files:
                file.read_text(encoding="utf-8", errors="strict")

    def test_body_previews_do_not_log_image_data_or_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.log"
            with patch.dict(converter.CONFIG, {"log_path": str(path), "log_body_limit": 1024}):
                converter._log_json("body", {"accessToken": "synthetic-private", "image": "data:image/png;base64," + "Z" * 10000})
                converter._log("ReadTimeout: Authorization: Bearer synthetic-secret")
            text = path.read_text()
            self.assertNotIn("synthetic-private", text)
            self.assertNotIn("synthetic-secret", text)
            self.assertNotIn("Z" * 20, text)
            self.assertIn("ReadTimeout", text)
            self.assertLess(path.stat().st_size, 1500)

    def test_zero_body_budget_keeps_summary_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.log"
            with patch.dict(converter.CONFIG, {"log_path": str(path), "log_body_limit": 0}):
                converter._log_json("body", {"text": "must-not-be-logged"})
                converter._log_text_body("raw", "must-not-be-logged")
                converter._log("request summary")
            self.assertIn("request summary", path.read_text())
            self.assertNotIn("must-not-be-logged", path.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
