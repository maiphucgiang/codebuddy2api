#!/usr/bin/env python3
"""Chat SSE 边界回归；仅使用内存数据和 MockTransport。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import json
import unittest
from unittest.mock import patch

import httpx

from app import upstream_io
from app.upstream_io import ChatSSEAccumulator, UpstreamResponseError


def event(delta=None, finish=None, **fields):
    return {"choices": [{"delta": {} if delta is None else delta,
                         "finish_reason": finish}], **fields}


def feed(tracker, *chunks, done=True):
    for chunk in chunks:
        tracker.feed_line("data:" + json.dumps(chunk, ensure_ascii=False))
    if done:
        tracker.feed_line("data: [DONE]")
    return tracker.result()


class AccumulatorTests(unittest.TestCase):
    def test_empty_terminal_choices_fail_with_both_collection_modes(self):
        for collect in (True, False):
            for finish in (None, "stop", "tool_calls", "length"):
                for delta in ({}, {"role": "assistant"}, {"content": ""},
                              {"reasoning_content": ""}, {"refusal": ""},
                              {"tool_calls": []}, {"tool_calls": [{}]},
                              {"tool_calls": [{"index": 0, "type": "function"}]}):
                    with self.subTest(collect=collect, finish=finish, delta=delta):
                        with self.assertRaises(UpstreamResponseError) as caught:
                            feed(ChatSSEAccumulator(collect=collect), event(delta, finish))
                        self.assertEqual(caught.exception.status, 502)
                        self.assertEqual(json.loads(caught.exception.raw)["error"]["code"], "empty_response")

    def test_empty_objects_and_usage_only_are_not_output(self):
        for chunks in ((), ({},), ({"choices": []},), ({"choices": None},),
                       ({"choices": [], "usage": {"total_tokens": 1}},),
                       (event(finish="stop"), {"usage": {"total_tokens": 1}})):
            with self.subTest(chunks=chunks):
                with self.assertRaises((httpx.RemoteProtocolError, UpstreamResponseError)):
                    feed(ChatSSEAccumulator(), *chunks)

    def test_finish_without_done_still_requires_output(self):
        with self.assertRaises(UpstreamResponseError):
            feed(ChatSSEAccumulator(), event(finish="stop"), done=False)

    def test_normal_sse_and_usage_trailer(self):
        tracker = ChatSSEAccumulator()
        for line in (": keepalive", "event: message", "", "id: synthetic"):
            tracker.feed_line(line)
        usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                 "completion_tokens_details": {"reasoning_tokens": 1}}
        result = feed(tracker, event({"role": "assistant"}, model="synthetic"),
                      event({"content": "你好"}), event({"content": "世界"}, "stop"),
                      {"choices": [], "usage": usage})
        self.assertEqual(result["content"], "你好世界")
        self.assertEqual(result["model"], "synthetic")
        self.assertEqual(result["finish_reason"], "stop")
        self.assertEqual(result["usage"], usage)
        self.assertIsNone(result["reasoning_content"])
        self.assertIsNone(result["tool_calls"])

    def test_finish_with_content_without_done_remains_valid(self):
        for collect in (True, False):
            with self.subTest(collect=collect):
                tracker = ChatSSEAccumulator(collect=collect)
                result = feed(tracker, event({"content": "ok"}, "stop"), done=False)
                self.assertEqual(result["content"], "ok" if collect else "")
                self.assertFalse(tracker.done)

    def test_done_with_content_without_finish_remains_valid(self):
        result = feed(ChatSSEAccumulator(), event({"content": "ok"}))
        self.assertEqual(result["content"], "ok")
        self.assertIsNone(result["finish_reason"])

    def test_reasoning_only_is_valid(self):
        for collect in (True, False):
            with self.subTest(collect=collect):
                tracker = ChatSSEAccumulator(collect=collect)
                result = feed(tracker, event({"reasoning_content": "思考"}),
                              event({"reasoning_content": "完毕"}, "stop"))
                self.assertEqual(result["content"], "")
                self.assertEqual(result["reasoning_content"], "思考完毕" if collect else None)
                self.assertTrue(tracker.saw_output)

    def test_tool_only_is_valid_and_arguments_are_collected_conditionally(self):
        for collect in (True, False):
            with self.subTest(collect=collect):
                tracker = ChatSSEAccumulator(collect=collect)
                result = feed(tracker,
                              event({"tool_calls": [{"index": 0, "id": "call-1", "function": {
                                  "name": "synthetic_tool", "arguments": "{"}}]}),
                              event({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}),
                              event(finish="tool_calls"))
                self.assertTrue(tracker.saw_output)
                self.assertEqual(result["content"], "")
                self.assertEqual(result["tool_calls"], [{"id": "call-1", "type": "function",
                                 "function": {"name": "synthetic_tool",
                                              "arguments": "{}" if collect else ""}}])

    def test_tool_metadata_completeness_remains_callers_responsibility(self):
        result = feed(ChatSSEAccumulator(), event({"tool_calls": [{
            "function": {"arguments": "{"}}]}, "tool_calls"))
        self.assertEqual(result["tool_calls"][0]["function"]["arguments"], "{")
        self.assertIsNone(result["tool_calls"][0]["id"])

    def test_collect_false_does_not_buffer_text(self):
        tracker = ChatSSEAccumulator(collect=False)
        feed(tracker, event({"content": "body", "reasoning_content": "reason",
                             "refusal": "rejection"}, "stop"))
        self.assertTrue(tracker.saw_output)
        self.assertEqual((tracker.content, tracker.reasoning, tracker.refusal), ([], [], []))

    def test_eof_without_any_completion_marker_is_always_error(self):
        for collect in (True, False):
            for chunks in ((), ({},), (event({"content": "partial"}),),
                           (event({"reasoning_content": "partial"}),),
                           (event({"refusal": "partial"}),),
                           (event({"tool_calls": [{"id": "partial"}]}),)):
                with self.subTest(collect=collect, chunks=chunks):
                    with self.assertRaisesRegex(httpx.RemoteProtocolError, "completion marker"):
                        feed(ChatSSEAccumulator(collect=collect), *chunks, done=False)

    def test_lines_after_done_are_ignored(self):
        tracker = ChatSSEAccumulator()
        result = feed(tracker, event({"content": "ok"}, "stop"))
        tracker.feed_line("data: not-json")
        self.assertEqual(tracker.result(), result)

    def test_invalid_json_and_top_level_objects_are_protocol_errors(self):
        for raw in ("", "not-json", "null", "[]", "true", "1", '"text"'):
            with self.subTest(raw=raw):
                with self.assertRaises(httpx.RemoteProtocolError):
                    ChatSSEAccumulator().feed_line("data: " + raw)

    def test_malformed_field_types_are_controlled_protocol_errors(self):
        invalid = [True, False, 0, 1, "", "bad", [], [1]]
        chunks = [{"choices": None}, {"choices": {}}, {"model": {}},
                  {"choices": [{"delta": None, "finish_reason": "stop"}]}]
        chunks += [{key: value} for key in ("choices", "usage", "model")
                  for value in invalid
                  if not isinstance(value, {"choices": list, "usage": dict, "model": str}[key])]
        chunks += [{"choices": [value]} for value in [None, *invalid]]
        chunks += [event(value, "stop") for value in invalid]
        chunks += [event({key: value}, "stop") for key in ("content", "reasoning_content", "refusal")
                   for value in (False, 0, [], {})]
        chunks += [event({"tool_calls": value}, "stop") for value in (None, False, 0, "", {})]
        chunks += [event({"tool_calls": [value]}, "stop") for value in [None, *invalid]]
        chunks += [event({"tool_calls": [{"function": value}]}, "stop") for value in [None, *invalid]]
        chunks += [event({"tool_calls": [{"index": value}]}, "stop")
                   for value in (None, True, -1, "0", 0.5, [])]
        chunks += [event({"tool_calls": [{"id": value}]}, "stop") for value in (False, 0, [], {})]
        chunks += [event({"tool_calls": [{"function": {key: value}}]}, "stop")
                   for key in ("name", "arguments") for value in (False, 0, [], {})]
        chunks += [event({"content": "ok"}, value) for value in (False, 0, [], {})]
        for collect in (True, False):
            for chunk in chunks:
                with self.subTest(collect=collect, chunk=chunk):
                    with self.assertRaises(httpx.RemoteProtocolError):
                        feed(ChatSSEAccumulator(collect=collect), chunk)

    def test_malformed_usage_nested_fields_are_protocol_errors(self):
        usages = [{key: value} for key in ("prompt_tokens_details", "completion_tokens_details")
                  for value in (False, 0, "", "bad", [], [1], {"reasoning_tokens": "bad"})]
        usages += [{key: value} for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                           "input_tokens", "output_tokens", "cache_read_input_tokens",
                                           "cache_creation_input_tokens")
                   for value in (None, True, -1, 1.5, "1", [], {})]
        for collect in (True, False):
            for usage in usages:
                with self.subTest(collect=collect, usage=usage):
                    with self.assertRaises(httpx.RemoteProtocolError):
                        feed(ChatSSEAccumulator(collect=collect), event({"content": "ok"}, "stop"),
                             {"usage": usage, "choices": []})

    def test_error_events_preserve_payload_and_never_succeed(self):
        for error in ({"message": "synthetic rejection"}, {}, [], "failure", "", False, 0):
            for collect in (True, False):
                with self.subTest(error=error, collect=collect):
                    with self.assertRaises(UpstreamResponseError) as caught:
                        feed(ChatSSEAccumulator(collect=collect), event({"content": "partial"}),
                             {"error": error})
                    self.assertEqual(caught.exception.status, 502)
                    self.assertEqual(json.loads(caught.exception.raw), {"error": error})

    def test_null_error_and_optional_fields_remain_compatible(self):
        result = feed(ChatSSEAccumulator(), {"error": None, "choices": [], "usage": None},
                      event({"content": None, "reasoning_content": None, "refusal": None}),
                      event({"content": "ok"}, "stop"))
        self.assertEqual(result["content"], "ok")

    def test_explicit_rejection_without_text_is_structured_error(self):
        for finish in ("content_filter", "refusal"):
            for collect in (True, False):
                for done in (True, False):
                    with self.subTest(finish=finish, collect=collect, done=done):
                        with self.assertRaises(UpstreamResponseError) as caught:
                            feed(ChatSSEAccumulator(collect=collect), event(finish=finish), done=done)
                        self.assertEqual(caught.exception.status, 502)
                        self.assertEqual(json.loads(caught.exception.raw)["error"]["code"], finish)

    def test_refusal_text_and_content_filter_text_are_preserved(self):
        for finish in ("stop", "content_filter", "refusal"):
            for collect in (True, False):
                with self.subTest(finish=finish, collect=collect):
                    result = feed(ChatSSEAccumulator(collect=collect), event({"refusal": "不能"}),
                                  event({"refusal": "协助"}, finish))
                    self.assertEqual(result["refusal"], "不能协助" if collect else None)
                    self.assertEqual(result["finish_reason"], finish)
        result = feed(ChatSSEAccumulator(), event({"content": "合法拒绝文本"}, "content_filter"))
        self.assertEqual(result["content"], "合法拒绝文本")
        self.assertEqual(result["finish_reason"], "content_filter")


class OutputBudgetTests(unittest.TestCase):
    """聚合收集预算与错误体有界读取。"""

    def test_collect_budget_aborts_oversized_aggregation(self):
        acc = ChatSSEAccumulator(max_collect_bytes=10)  # 两片各 8B，第二片超预算
        acc.feed_line('data: {"choices":[{"index":0,"delta":{"content":"12345678"}}]}')
        with self.assertRaises(UpstreamResponseError) as caught:
            acc.feed_line('data: {"choices":[{"index":0,"delta":{"content":"12345678"}}]}')
        self.assertEqual(caught.exception.status, 502)
        self.assertIn(b"response_too_large", caught.exception.raw)
        # 预算内不受影响
        acc = ChatSSEAccumulator(max_collect_bytes=1024)
        acc.feed_line('data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}')
        acc.feed_line("data: [DONE]")
        self.assertEqual(acc.result()["content"], "ok")

    def test_error_body_read_is_bounded(self):
        import asyncio

        class BigError:
            async def aiter_bytes(self):
                for _ in range(8):
                    yield b"x" * 1024 * 1024

        raw = asyncio.run(upstream_io.read_bounded_error(BigError(), limit=1024))
        self.assertEqual(len(raw), 1024)


class BoundedErrorReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_filling_the_limit_never_pulls_another_chunk_and_closes_response(self):
        for chunks in ((b"abcdefgh",), (b"abcd", b"efgh"), (b"abcd", b"efgh-tail")):
            with self.subTest(chunks=chunks):
                class CappedStream(httpx.AsyncByteStream):
                    closed = False

                    async def __aiter__(self):
                        for chunk in chunks:
                            yield chunk
                        raise AssertionError("reader waited for data after reaching its budget")

                    async def aclose(self):
                        self.closed = True

                stream = CappedStream()
                transport = httpx.MockTransport(lambda request: httpx.Response(500, stream=stream))
                async with httpx.AsyncClient(transport=transport) as client:
                    async with client.stream("POST", "https://synthetic.invalid") as response:
                        raw = await upstream_io.read_bounded_error(response, limit=8)
                        self.assertEqual(raw, b"abcdefgh")
                    self.assertTrue(stream.closed)

    async def test_short_error_body_is_preserved(self):
        response = httpx.Response(400, content=b"short")
        self.assertEqual(await upstream_io.read_bounded_error(response, limit=8), b"short")

    async def test_zero_budget_does_not_open_the_iterator(self):
        class NoRead:
            def aiter_bytes(self):
                raise AssertionError("zero budget must not read upstream")

        self.assertEqual(await upstream_io.read_bounded_error(NoRead(), limit=0), b"")



class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_or_malformed_stream_never_replays_post(self):
        raw_cases = [b"", b"data: {}\n\ndata: [DONE]\n\n",
                     b'data: {"error": {"message": "synthetic rejection"}}\n\n',
                     b'data: {"choices": [1]}\n\n',
                     ("data: " + json.dumps(event(finish="stop")) + "\n\ndata: [DONE]\n\n").encode()]
        real_client = httpx.AsyncClient
        for collect in (True, False):
            for raw in raw_cases:
                with self.subTest(collect=collect, raw=raw):
                    requests = []

                    def handler(request):
                        requests.append(request)
                        return httpx.Response(200, content=raw)

                    transport = httpx.MockTransport(handler)
                    with patch.object(upstream_io.httpx, "AsyncClient",
                                      side_effect=lambda **kw: real_client(transport=transport, **kw)):
                        with self.assertRaises((httpx.RemoteProtocolError, UpstreamResponseError)):
                            async with upstream_io.open_backend_stream("https://synthetic.invalid", {}, {}) as response:
                                tracker = ChatSSEAccumulator(collect=collect)
                                async for line in response.aiter_lines():
                                    tracker.feed_line(line)
                                    if tracker.done:
                                        break
                                tracker.result()
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].method, "POST")

    async def test_body_not_accepted_is_replayed_on_a_fresh_connection(self):
        """建连失败 / 建连超时：上游手里没有正文，换新连接重放一次不会重复计费。"""
        real_client = httpx.AsyncClient
        for error_type in (httpx.ConnectError, httpx.ConnectTimeout):
            with self.subTest(error=error_type.__name__):
                attempts = []

                def handler(request):
                    attempts.append(request)
                    if len(attempts) == 1:
                        raise error_type("synthetic failure before the body was accepted")
                    return httpx.Response(200, content=b"data: [DONE]\n\n")

                transport = httpx.MockTransport(handler)
                with patch.object(upstream_io.httpx, "AsyncClient",
                                  side_effect=lambda **kw: real_client(transport=transport, **kw)):
                    async with upstream_io.open_backend_stream("https://synthetic.invalid", {}, {}) as response:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(await response.aread(), b"data: [DONE]\n\n")
                self.assertEqual(len(attempts), 2, "只允许重放一次")
                self.assertEqual([request.method for request in attempts], ["POST", "POST"])

    async def test_write_timeout_is_not_replayed_without_the_opt_in(self):
        """写超时只证明正文没写完，证不了上游没处理已收到的部分：默认不重放。"""
        real_client = httpx.AsyncClient
        for error_type in upstream_io.WRITE_TIMEOUT:
            with self.subTest(error=error_type.__name__):
                attempts = []

                def handler(request):
                    attempts.append(request)
                    raise error_type("synthetic write timeout")

                transport = httpx.MockTransport(handler)
                with patch.object(upstream_io.httpx, "AsyncClient",
                                  side_effect=lambda **kw: real_client(transport=transport, **kw)):
                    with self.assertRaises(error_type):
                        async with upstream_io.open_backend_stream("https://synthetic.invalid", {}, {}) as response:
                            await response.aread()
                self.assertEqual(len(attempts), 1, "默认必须一次都不重放")

    async def test_write_timeout_is_replayed_only_when_opted_in(self):
        """`retry_write_timeout=True` 是运维显式承担计费歧义，重放行为与建连失败一致。"""
        real_client = httpx.AsyncClient
        for error_type in upstream_io.WRITE_TIMEOUT:
            with self.subTest(error=error_type.__name__):
                attempts = []

                def handler(request):
                    attempts.append(request)
                    if len(attempts) == 1:
                        raise error_type("synthetic write timeout")
                    return httpx.Response(200, content=b"data: [DONE]\n\n")

                transport = httpx.MockTransport(handler)
                with patch.object(upstream_io.httpx, "AsyncClient",
                                  side_effect=lambda **kw: real_client(transport=transport, **kw)):
                    async with upstream_io.open_backend_stream(
                            "https://synthetic.invalid", {}, {}, retry_write_timeout=True) as response:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(await response.aread(), b"data: [DONE]\n\n")
                self.assertEqual(len(attempts), 2, "只允许重放一次")
                self.assertEqual([request.method for request in attempts], ["POST", "POST"])

    async def test_ambiguous_transport_failures_never_replay_post(self):
        """请求体已经发出（甚至响应已经开始）的失败有计费歧义，一律交给调用方按协议返回。"""
        real_client = httpx.AsyncClient
        for error_type in (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                           httpx.RemoteProtocolError):
            with self.subTest(error=error_type.__name__):
                attempts = []

                def handler(request):
                    attempts.append(request)
                    raise error_type("synthetic failure after the body was sent")

                transport = httpx.MockTransport(handler)
                with patch.object(upstream_io.httpx, "AsyncClient",
                                  side_effect=lambda **kw: real_client(transport=transport, **kw)):
                    with self.assertRaises(error_type):
                        async with upstream_io.open_backend_stream("https://synthetic.invalid", {}, {}) as response:
                            await response.aread()
                self.assertEqual(len(attempts), 1, "歧义请求不得重放 POST")


if __name__ == "__main__":
    unittest.main(verbosity=2)
