#!/usr/bin/env python3
"""Test that a fragmented tool_call keeps its name on every delta without delaying the stream."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import json
import unittest

from app.tool_delta_normalize import ToolDeltaNormalizer


def chunk(delta, finish=None):
    payload = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return "data: " + json.dumps(payload, ensure_ascii=False)


def parse(line):
    return json.loads(line[5:])


class ToolDeltaNormalizerTests(unittest.TestCase):
    def test_first_fragment_passes_through_unchanged(self):
        normalizer = ToolDeltaNormalizer()
        line = chunk({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                                      "function": {"name": "bash", "arguments": ""}}]})
        self.assertEqual(normalizer.feed(line), [line], "首片必须原样、原时序转发")

    def test_argument_fragment_gets_the_known_name(self):
        normalizer = ToolDeltaNormalizer()
        normalizer.feed(chunk({"tool_calls": [
            {"index": 0, "id": "c", "function": {"name": "bash", "arguments": ""}}]}))
        emitted = normalizer.feed(chunk({"tool_calls": [
            {"index": 0, "function": {"name": "", "arguments": '{"cmd": "ls"}'}}]}))
        self.assertEqual(len(emitted), 1, "碎片必须立即转发，不得缓冲")
        call = parse(emitted[0])["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "bash")
        self.assertEqual(call["function"]["arguments"], '{"cmd": "ls"}')

    def test_parallel_calls_are_tracked_per_index(self):
        normalizer = ToolDeltaNormalizer()
        normalizer.feed(chunk({"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "first", "arguments": ""}},
            {"index": 1, "id": "b", "function": {"name": "second", "arguments": ""}}]}))
        emitted = normalizer.feed(chunk({"tool_calls": [
            {"index": 1, "function": {"arguments": "{}"}},
            {"index": 0, "function": {"arguments": "{}"}}]}))
        calls = parse(emitted[0])["choices"][0]["delta"]["tool_calls"]
        self.assertEqual([call["function"]["name"] for call in calls], ["second", "first"])

    def test_unknown_index_without_a_name_is_left_alone(self):
        normalizer = ToolDeltaNormalizer()
        line = chunk({"tool_calls": [{"index": 7, "function": {"arguments": "{}"}}]})
        self.assertEqual(normalizer.feed(line), [line])

    def test_content_and_finish_lines_are_untouched(self):
        normalizer = ToolDeltaNormalizer()
        for line in (chunk({"content": "hi"}), chunk({}, finish="tool_calls"), "data: [DONE]",
                     "", "event: ping", 'data: {"error": {"message": "boom"}}'):
            self.assertEqual(normalizer.feed(line), [line])

    def test_flush_never_has_a_tail(self):
        normalizer = ToolDeltaNormalizer()
        normalizer.feed(chunk({"tool_calls": [
            {"index": 0, "function": {"name": "bash", "arguments": "{}"}}]}))
        self.assertEqual(normalizer.flush(), [])

    def test_disabled_normalizer_forwards_everything(self):
        normalizer = ToolDeltaNormalizer(enabled=False)
        normalizer.feed(chunk({"tool_calls": [
            {"index": 0, "function": {"name": "bash", "arguments": ""}}]}))
        fragment = chunk({"tool_calls": [{"index": 0, "function": {"name": "", "arguments": "{}"}}]})
        self.assertEqual(normalizer.feed(fragment), [fragment])

    def test_malformed_lines_are_forwarded_unparsed(self):
        normalizer = ToolDeltaNormalizer()
        for line in ("data: not json", "data: [1,2,3]"):
            self.assertEqual(normalizer.feed(line), [line])


if __name__ == "__main__":
    unittest.main()
