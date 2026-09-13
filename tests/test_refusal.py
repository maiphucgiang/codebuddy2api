#!/usr/bin/env python3
"""拒绝文本与空终止回归；仅内存 SSE、MockTransport，不读凭据或写日志。

运行：python3 tests/test_refusal.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from app.adapters.anthropic_adapter import AnthropicStreamConverter
from app.adapters.responses_adapter import ResponsesStreamConverter


REFUSAL_PARTS = ("抱歉，我不能协助这项请求。\n", "I cannot help with that request.")
REFUSAL = "".join(REFUSAL_PARTS)
ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages")


def chunk(delta=None, finish=None):
    return "data: " + json.dumps({"model": "auto", "choices": [{
        "index": 0, "delta": {} if delta is None else delta,
        "finish_reason": finish}]}, ensure_ascii=False)


def refusal_lines(finish="stop"):
    return [chunk({"role": "assistant", "content": None, "refusal": ""}),
            *(chunk({"refusal": part}) for part in REFUSAL_PARTS),
            chunk(finish=finish), "data: [DONE]"]


def events(raw):
    return [json.loads(line[5:].strip()) for line in raw.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


def adapted_text(response, anthropic):
    if anthropic:
        return "".join(part["text"] for part in response["content"] if part["type"] == "text")
    return "".join(part["text"] for item in response["output"] if item["type"] == "message"
                   for part in item["content"] if part["type"] == "output_text")


def delta_text(parsed, anthropic):
    if anthropic:
        return "".join(event["delta"]["text"] for event in parsed
                       if event["type"] == "content_block_delta"
                       and event["delta"]["type"] == "text_delta")
    return "".join(event["delta"] for event in parsed if event["type"] == "response.output_text.delta")


class AdapterRefusalTests(unittest.TestCase):
    def test_refusal_only_stream_and_nonstream_preserve_exact_text(self):
        for adapter in (AnthropicStreamConverter, ResponsesStreamConverter):
            for finish in ("stop", "content_filter", "refusal"):
                with self.subTest(adapter=adapter.__name__, finish=finish):
                    converter = adapter(model="auto")
                    raw = "".join(converter.feed_line(line) for line in refusal_lines(finish))
                    raw += converter.finish()
                    anthropic = adapter is AnthropicStreamConverter
                    self.assertEqual(delta_text(events(raw), anthropic), REFUSAL)
                    self.assertEqual(adapted_text(converter.get_nonstream_response(), anthropic), REFUSAL)
                    if anthropic:
                        self.assertEqual(events(raw)[-1]["type"], "message_stop")
                    else:
                        final = events(raw)[-1]
                        self.assertEqual(final["type"], "response.completed")
                        self.assertEqual(adapted_text(final["response"], False), REFUSAL)
                        self.assertEqual(final["response"]["output"][0]["content"], [
                            {"type": "output_text", "text": REFUSAL, "annotations": []}])

    def test_content_and_refusal_in_same_delta_preserve_both(self):
        for adapter in (AnthropicStreamConverter, ResponsesStreamConverter):
            with self.subTest(adapter=adapter.__name__):
                converter = adapter()
                raw = converter.feed_line(chunk({"content": "说明：", "refusal": REFUSAL}, "stop"))
                raw += converter.finish()
                anthropic = adapter is AnthropicStreamConverter
                self.assertEqual(delta_text(events(raw), anthropic), "说明：" + REFUSAL)
                self.assertEqual(adapted_text(converter.get_nonstream_response(), anthropic), "说明：" + REFUSAL)

    def test_reasoning_before_refusal_is_preserved(self):
        for adapter in (AnthropicStreamConverter, ResponsesStreamConverter):
            with self.subTest(adapter=adapter.__name__):
                converter = adapter()
                raw = converter.feed_line(chunk({"reasoning_content": "思考"}))
                raw += "".join(converter.feed_line(line) for line in refusal_lines())
                raw += converter.finish()
                response = converter.get_nonstream_response()
                anthropic = adapter is AnthropicStreamConverter
                self.assertEqual(delta_text(events(raw), anthropic), REFUSAL)
                self.assertEqual(adapted_text(response, anthropic), REFUSAL)
                if anthropic:
                    self.assertEqual(response["content"][0], {"type": "thinking", "thinking": "思考"})
                    parsed = events(raw)
                    stop = next(i for i, event in enumerate(parsed)
                                if event["type"] == "content_block_stop" and event["index"] == 0)
                    text_start = next(i for i, event in enumerate(parsed)
                                      if event["type"] == "content_block_start"
                                      and event["content_block"]["type"] == "text")
                    self.assertLess(stop, text_start)
                else:
                    self.assertEqual(response["output"][0]["summary"], [{"type": "summary_text", "text": "思考"}])

    def test_null_or_empty_refusal_does_not_change_text_reasoning_or_tools(self):
        for adapter in (AnthropicStreamConverter, ResponsesStreamConverter):
            for refusal in (None, ""):
                with self.subTest(adapter=adapter.__name__, refusal=refusal):
                    baseline = adapter()
                    # Compare independent states with identical IDs/timestamps.
                    converters = [baseline, deepcopy(baseline)]
                    deltas = [{"reasoning_content": "思考"}, {"content": "正文"},
                              {"tool_calls": [{"index": 0, "id": "call_mock", "function": {
                                  "name": "mock_tool", "arguments": "{}"}}]}]
                    outputs = []
                    # Responses allocates function-call IDs while processing deltas.
                    with patch("os.urandom", return_value=b"x" * 12):
                        for index, converter in enumerate(converters):
                            raw = "".join(converter.feed_line(chunk(
                                delta if index == 0 else {**delta, "refusal": refusal})) for delta in deltas)
                            raw += converter.feed_line(chunk(finish="tool_calls")) + converter.finish()
                            outputs.append((raw, converter.get_nonstream_response()))
                    self.assertEqual(outputs[0], outputs[1])


class EmptyTerminationTests(unittest.TestCase):
    def test_empty_stop_done_is_structured_upstream_error(self):
        from app.upstream_io import ChatSSEAccumulator, UpstreamResponseError

        for collect in (False, True):
            with self.subTest(collect=collect):
                tracker = ChatSSEAccumulator(collect=collect)
                with self.assertRaises(UpstreamResponseError) as caught:
                    tracker.feed_line(chunk(finish="stop"))
                    tracker.feed_line("data: [DONE]")
                    tracker.result()
                self.assertEqual(caught.exception.status, 502)
                self.assertIn("error", json.loads(caught.exception.raw))


class EndpointRefusalTests(unittest.TestCase):
    def setUp(self):
        import httpx
        from fastapi.testclient import TestClient
        import converter
        from app import upstream_io

        self.httpx = httpx
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "ledger": None,
            "model_guard": False, "model_cache": None, "models_remote": None,
            "max_images": 16, "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
            "log_body_limit": 0, "log_path": None, "desensitize": False, "no_compact": False}))
        self.enterContext(patch.object(converter, "_cred_for", return_value=(None, {})))
        self.enterContext(patch.object(converter, "_log"))
        self.enterContext(patch.object(httpx.HTTPTransport, "handle_request",
                                      side_effect=AssertionError("real network forbidden")))
        self.enterContext(patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                                      side_effect=AssertionError("real network forbidden")))
        self.requests = []
        self.raw = b""
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.handle)
        self.enterContext(patch.object(upstream_io.httpx, "AsyncClient",
                                      side_effect=lambda **kw: real_client(transport=transport, **kw)))
        self.client = self.enterContext(TestClient(converter.app))

    def handle(self, request):
        self.requests.append(request)
        return self.httpx.Response(200, content=self.raw, headers={"content-type": "text/event-stream"})

    def request(self, route, stream, tools):
        self.requests.clear()
        field = "input" if route == "/v1/responses" else "messages"
        body = {"model": "auto", "stream": stream, "max_tokens": 128,
                field: [{"role": "user", "content": "synthetic test request"}]}
        if tools:
            function = {"name": "mock_tool", "parameters": {"type": "object"}}
            if route == "/v1/responses":
                body["tools"] = [{"type": "function", **function}]
            elif route == "/v1/messages":
                body["tools"] = [{"name": "mock_tool", "input_schema": {"type": "object"}}]
            else:
                body["tools"] = [{"type": "function", "function": function}]
        response = self.client.post(route, json=body)
        self.assertEqual(len(self.requests), 1, "HTTP 200 rejection/empty output must not replay POST")
        self.assertEqual(self.requests[0].method, "POST")
        return response

    def test_refusal_text_survives_all_six_paths_without_post_replay(self):
        for finish in ("stop", "content_filter", "refusal"):
            self.raw = ("\n\n".join(refusal_lines(finish)) + "\n\n").encode()
            for route in ROUTES:
                for stream in (False, True):
                    for tools in (False, True):
                        with self.subTest(finish=finish, route=route, stream=stream, tools=tools):
                            response = self.request(route, stream, tools)
                            self.assertEqual(response.status_code, 200, response.text)
                            if not stream:
                                result = response.json()
                                if route == ROUTES[0]:
                                    self.assertEqual(result["choices"][0]["message"].get("refusal"), REFUSAL)
                                else:
                                    self.assertEqual(adapted_text(result, route == ROUTES[2]), REFUSAL)
                                continue
                            parsed = events(response.text)
                            self.assertFalse(any("error" in event for event in parsed), response.text)
                            if route == ROUTES[0]:
                                text = "".join(choice.get("delta", {}).get("refusal", "") or ""
                                               for event in parsed for choice in event.get("choices", []))
                                self.assertEqual(text, REFUSAL)
                                self.assertIn("data: [DONE]", response.text)
                            else:
                                self.assertEqual(delta_text(parsed, route == ROUTES[2]), REFUSAL)
                                expected = "message_stop" if route == ROUTES[2] else "response.completed"
                                self.assertEqual(parsed[-1]["type"], expected)

    def test_empty_stop_done_errors_on_all_six_paths_without_normal_completion_or_replay(self):
        self.raw = (chunk(finish="stop") + "\n\ndata: [DONE]\n\n").encode()
        for route in ROUTES:
            for stream in (False, True):
                for tools in (False, True):
                    with self.subTest(route=route, stream=stream, tools=tools):
                        response = self.request(route, stream, tools)
                        if not stream:
                            self.assertEqual(response.status_code, 502, response.text)
                            self.assertIn("error", response.json().get("detail", response.json()))
                            continue
                        parsed = events(response.text)
                        self.assertTrue(any("error" in event for event in parsed), response.text)
                        self.assertNotIn("data: [DONE]", response.text)
                        self.assertFalse(any(choice.get("finish_reason") for event in parsed
                                             for choice in event.get("choices", [])), response.text)
                        self.assertFalse(any(event.get("type") in (
                            "response.completed", "response.output_item.done", "response.output_text.done",
                            "message_stop", "message_delta") for event in parsed), response.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
