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
                    error = response.json()["detail"]["error"]
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
        self.assertEqual(response.json()["detail"]["error"]["code"], "request_too_large")
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


class ConfigurationTests(unittest.TestCase):
    def configure(self, env=None, flags=(), invalid=False):
        with contextlib.ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
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
            if invalid:
                with self.assertRaises(SystemExit) as caught:
                    converter.main()
                self.assertEqual(caught.exception.code, 2)
                seed.assert_not_called()
                server.assert_not_called()
                return
            converter.main()
            server.assert_called_once()
            return {key: converter.CONFIG[key] for key in (
                "max_images", "image_policy", "max_request_bytes", "log_body_limit")}

    def test_defaults(self):
        self.assertEqual(self.configure(), {"max_images": 16, "image_policy": "truncate",
                                           "max_request_bytes": 33554432, "log_body_limit": 65536})

    def test_environment_and_explicit_cli_precedence(self):
        env = {"CODEBUDDY2API_MAX_IMAGES": "8", "CODEBUDDY2API_IMAGE_POLICY": "error",
               "CODEBUDDY2API_MAX_REQUEST_BYTES": "100000", "CODEBUDDY2API_LOG_BODY_LIMIT": "0"}
        self.assertEqual(self.configure(env), {"max_images": 8, "image_policy": "error",
                                               "max_request_bytes": 100000, "log_body_limit": 0})
        env["CODEBUDDY2API_IMAGE_POLICY"] = "invalid-overridden"
        self.assertEqual(self.configure(env, ("--max-images", "0", "--image-policy", "truncate"))["max_images"], 0)

    def test_invalid_config_fails_before_side_effects(self):
        for env in ({"CODEBUDDY2API_MAX_IMAGES": "-1"}, {"CODEBUDDY2API_MAX_IMAGES": "1.5"},
                    {"CODEBUDDY2API_IMAGE_POLICY": "drop"}, {"CODEBUDDY2API_MAX_REQUEST_BYTES": "0"},
                    {"CODEBUDDY2API_LOG_BODY_LIMIT": "-1"}):
            with self.subTest(env=env):
                self.configure(env, invalid=True)
        self.configure(flags=("--max-images", "-1"), invalid=True)


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
