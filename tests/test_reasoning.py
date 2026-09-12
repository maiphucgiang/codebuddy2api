#!/usr/bin/env python3
"""reasoning_content（思考）透传回归测试：聚合、伪流式重放、Anthropic/Responses 映射。

直接运行：python3 tests/test_reasoning.py
"""

import asyncio
import json
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import httpx

from converter import _collect_stream, _merge_chat_sse_text, _chat_result_to_sse_lines
from app.adapters.anthropic_adapter import AnthropicStreamConverter
from app.adapters.responses_adapter import ResponsesStreamConverter

_SSE = (
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":""},"finish_reason":null}]}\n'
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"reasoning_content":"思考一"},"finish_reason":null}]}\n'
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"reasoning_content":"思考二"},"finish_reason":null}]}\n'
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"content":"正文"},"finish_reason":null}]}\n'
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,'
    '"completion_tokens_details":{"reasoning_tokens":7}}}\n'
    'data: [DONE]\n'
)


def _parse_sse_events(raw: str) -> list[dict]:
    """把 SSE 文本解析为事件 dict 列表（兼容有无 event: 行）。"""
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        for line in block.strip().split("\n"):
            if line.startswith("data: ") and line[6:] != "[DONE]":
                events.append(json.loads(line[6:]))
    return events


class TestChatAggregation(unittest.TestCase):
    """Chat SSE 聚合必须保留 reasoning_content。"""

    def test_collect_stream_keeps_reasoning(self):
        resp = httpx.Response(200, content=_SSE.encode("utf-8"))
        collected = asyncio.run(_collect_stream(resp))
        msg = collected["choices"][0]["message"]
        self.assertEqual(msg.get("reasoning_content"), "思考一思考二")
        self.assertEqual(msg.get("content"), "正文")

    def test_merge_chat_sse_text_keeps_reasoning(self):
        merged = _merge_chat_sse_text(_SSE)
        self.assertEqual(merged["reasoning_content"], "思考一思考二")
        self.assertEqual(merged["content"], "正文")
        self.assertEqual(merged["finish_reason"], "stop")

    def test_merge_without_reasoning_is_none(self):
        text = ('data: {"id":"c1","choices":[{"index":0,"delta":{"content":"hi"},'
                '"finish_reason":"stop"}]}\ndata: [DONE]\n')
        merged = _merge_chat_sse_text(text)
        self.assertIsNone(merged["reasoning_content"])

    def test_sse_replay_reasoning_before_content(self):
        merged = _merge_chat_sse_text(_SSE)
        lines = _chat_result_to_sse_lines(merged)
        reasoning = content = ""
        for line in lines:
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            choices = json.loads(line[6:]).get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            reasoning += delta.get("reasoning_content") or ""
            content += delta.get("content") or ""
        self.assertEqual(reasoning, "思考一思考二")
        self.assertIn("正文", content)
        # reasoning 分片必须先于 content 分片
        first_reasoning = next(i for i, l in enumerate(lines) if "reasoning_content" in l)
        first_content = next(i for i, l in enumerate(lines) if '"content": "正' in l or '"content":"正' in l)
        self.assertLess(first_reasoning, first_content)


class TestAnthropicThinking(unittest.TestCase):
    """reasoning_content 必须映射为 Anthropic thinking content block。"""

    def test_stream_thinking_events(self):
        conv = AnthropicStreamConverter(model="m")
        raw = "".join(conv.feed_line(line) for line in _SSE.splitlines())
        raw += conv.finish()
        events = _parse_sse_events(raw)
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual(starts[0]["content_block"]["type"], "thinking")
        self.assertEqual(starts[0]["index"], 0)
        self.assertEqual(starts[1]["content_block"]["type"], "text")
        self.assertEqual(starts[1]["index"], 1)
        deltas = [e for e in events if e["type"] == "content_block_delta"
                  and e["delta"]["type"] == "thinking_delta"]
        self.assertEqual("".join(d["delta"]["thinking"] for d in deltas), "思考一思考二")
        # thinking 块在 text 块开始前已关闭
        stops = [e for e in events if e["type"] == "content_block_stop"]
        self.assertEqual(stops[0]["index"], 0)

    def test_nonstream_thinking_block(self):
        conv = AnthropicStreamConverter(model="m")
        for line in _SSE.splitlines():
            conv.feed_line(line)
        conv.finish()
        resp = conv.get_nonstream_response()
        self.assertEqual(resp["content"][0], {"type": "thinking", "thinking": "思考一思考二"})
        self.assertEqual(resp["content"][1], {"type": "text", "text": "正文"})

    def test_no_reasoning_no_thinking_block(self):
        conv = AnthropicStreamConverter(model="m")
        for line in ('data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}',
                     'data: [DONE]'):
            conv.feed_line(line)
        conv.finish()
        resp = conv.get_nonstream_response()
        self.assertEqual(resp["content"], [{"type": "text", "text": "hi"}])


class TestResponsesReasoning(unittest.TestCase):
    """reasoning_content 必须映射为 Responses reasoning item（位于 message 之前）。"""

    def test_stream_reasoning_item(self):
        conv = ResponsesStreamConverter(model="m")
        raw = "".join(conv.feed_line(line) for line in _SSE.splitlines())
        raw += conv.finish()
        events = _parse_sse_events(raw)
        added = [e for e in events if e["type"] == "response.output_item.added"]
        self.assertEqual(added[0]["item"]["type"], "reasoning")
        self.assertEqual(added[0]["output_index"], 0)
        self.assertEqual(added[1]["item"]["type"], "message")
        self.assertEqual(added[1]["output_index"], 1)
        deltas = [e for e in events if e["type"] == "response.reasoning_summary_text.delta"]
        self.assertEqual("".join(d["delta"] for d in deltas), "思考一思考二")
        text_delta = [e for e in events if e["type"] == "response.output_text.delta"][0]
        self.assertEqual(text_delta["output_index"], 1)

    def test_nonstream_response_output_order(self):
        conv = ResponsesStreamConverter(model="m")
        for line in _SSE.splitlines():
            conv.feed_line(line)
        conv.finish()
        resp = conv.get_nonstream_response()
        self.assertEqual(resp["output"][0]["type"], "reasoning")
        self.assertEqual(resp["output"][0]["summary"][0]["text"], "思考一思考二")
        self.assertEqual(resp["output"][1]["type"], "message")
        self.assertEqual(resp["usage"]["output_tokens_details"]["reasoning_tokens"], 7)

    def test_no_reasoning_keeps_message_at_zero(self):
        conv = ResponsesStreamConverter(model="m")
        for line in ('data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}',
                     'data: [DONE]'):
            conv.feed_line(line)
        conv.finish()
        resp = conv.get_nonstream_response()
        self.assertEqual(len(resp["output"]), 1)
        self.assertEqual(resp["output"][0]["type"], "message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
