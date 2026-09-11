#!/usr/bin/env python3
"""Local synthetic image-policy/adapter/projection regression tests.

Run: .venv/bin/python -B test_request_limits.py
No converter import, account access, image decoding or upstream requests.
"""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from anthropic_adapter import anthropic_request_to_chat
from request_limits import ImageLimitError, apply_image_policy
from responses_adapter import responses_request_to_chat
from responses_projection import project_responses_chat_body


def chat_image(index=0):
    return {"type": "image_url", "image_url": {"url": f"https://example.invalid/{index}.png"}}


def response_image(index=0):
    return {"type": "input_image", "image_url": f"https://example.invalid/{index}.png"}


def anthropic_image(index=0):
    return {"type": "image", "source": {"type": "url", "url": f"https://example.invalid/{index}.png"}}


def text(value="Keep this text", kind="text"):
    return {"type": kind, "text": value}


class ImagePolicyTests(unittest.TestCase):
    def test_default_boundaries_and_both_policies(self):
        for field, make_image in (("messages", chat_image), ("messages", anthropic_image),
                                  ("input", response_image)):
            for count in (0, 16, 17):
                for policy in ("truncate", "error"):
                    with self.subTest(field=field, image=make_image.__name__, count=count, policy=policy):
                        body = {field: [{"role": "user", "content": [make_image(i) for i in range(count)]}]}
                        before = deepcopy(body)
                        if count == 17 and policy == "error":
                            with self.assertRaises(ImageLimitError) as raised:
                                apply_image_policy(body, field=field, policy=policy)
                            self.assertEqual((raised.exception.count, raised.exception.limit), (17, 16))
                        else:
                            out, stats = apply_image_policy(body, field=field, policy=policy)
                            self.assertEqual(stats, {"count": count, "retained": min(count, 16),
                                                     "dropped": max(0, count - 16)})
                            if count <= 16:
                                self.assertIs(out, body)
                            else:
                                self.assertEqual(out[field][0]["content"], before[field][0]["content"][1:])
                        self.assertEqual(body, before)

    def test_custom_limit_newest_across_messages_and_blocks_copy_on_write(self):
        body = {
            "model": "synthetic",
            "tools": [{"type": "function", "function": {"name": "noop"}}],
            "messages": [
                {"role": "user", "content": [chat_image(0), text("old text"), chat_image(1)]},
                {"role": "assistant", "content": "Answer", "reasoning_content": "Keep reasoning"},
                {"role": "user", "content": [chat_image(2), text("new text"), chat_image(3)]},
            ],
        }
        before = deepcopy(body)
        out, stats = apply_image_policy(body, max_images=1)
        self.assertEqual(stats, {"count": 4, "retained": 1, "dropped": 3})
        self.assertEqual(out["messages"][0]["content"], [text("old text")])
        self.assertEqual(out["messages"][2]["content"], [text("new text"), chat_image(3)])
        self.assertIsNot(out, body)
        self.assertIsNot(out["messages"], body["messages"])
        self.assertIsNot(out["messages"][0], body["messages"][0])
        self.assertIs(out["messages"][1], body["messages"][1])
        self.assertIs(out["messages"][2]["content"][-1], body["messages"][2]["content"][-1])
        self.assertIs(out["tools"], body["tools"])
        self.assertEqual(body, before)
        again, _ = apply_image_policy(out, max_images=1)
        self.assertIs(again, out)

    def test_duplicate_urls_count_individually(self):
        image = chat_image()
        body = {"messages": [{"role": "user", "content": [image] * 17}]}
        out, stats = apply_image_policy(body)
        self.assertEqual(stats, {"count": 17, "retained": 16, "dropped": 1})
        self.assertEqual(len(out["messages"][0]["content"]), 16)
        self.assertEqual(len(body["messages"][0]["content"]), 17)

    def test_zero_limit_placeholders_and_tool_calls_remain(self):
        for field, make_image, placeholder in (("messages", chat_image, "text"),
                                               ("messages", anthropic_image, "text"),
                                               ("input", response_image, "input_text")):
            with self.subTest(field=field, image=make_image.__name__):
                body = {field: [{"role": "user", "content": [make_image()]}]}
                before = deepcopy(body)
                out, stats = apply_image_policy(body, field=field, max_images=0)
                self.assertEqual(stats, {"count": 1, "retained": 0, "dropped": 1})
                self.assertEqual(len(out[field]), 1)
                self.assertEqual(out[field][0]["content"][0]["type"], placeholder)
                self.assertTrue(out[field][0]["content"][0]["text"])
                with self.assertRaises(ImageLimitError) as raised:
                    apply_image_policy(body, field=field, max_images=0, policy="error")
                self.assertEqual((raised.exception.count, raised.exception.limit), (1, 0))
                self.assertEqual(body, before)
        call = {"id": "call_1", "type": "function", "function": {"name": "view", "arguments": "{}"}}
        body = {"messages": [
            {"role": "assistant", "content": [chat_image()], "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_1", "content": [chat_image()]},
        ]}
        out, _ = apply_image_policy(body, max_images=0)
        self.assertEqual(len(out["messages"]), 2)
        self.assertEqual(out["messages"][0]["tool_calls"], [call])
        self.assertEqual(out["messages"][1]["tool_call_id"], "call_1")
        self.assertTrue(out["messages"][1]["content"][0]["text"])

    def test_anthropic_nested_tool_results_follow_block_order(self):
        tool_use = {"type": "tool_use", "id": "call_1", "name": "view", "input": {}}
        body = {"messages": [
            {"role": "assistant", "content": [tool_use]},
            {"role": "user", "content": [
                anthropic_image(0),
                {"type": "tool_result", "tool_use_id": "call_1", "is_error": False,
                 "content": [anthropic_image(1), text(), anthropic_image(2)]},
                anthropic_image(3),
            ]},
        ]}
        before = deepcopy(body)
        out, stats = apply_image_policy(body, max_images=2)
        self.assertEqual(stats, {"count": 4, "retained": 2, "dropped": 2})
        blocks = out["messages"][1]["content"]
        self.assertEqual(blocks[0]["content"], [text(), anthropic_image(2)])
        self.assertEqual(blocks[0]["tool_use_id"], "call_1")
        self.assertIs(blocks[0]["is_error"], False)
        self.assertEqual(blocks[1], anthropic_image(3))
        self.assertIs(out["messages"][0], body["messages"][0])
        self.assertEqual(body, before)
        image_only = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": [anthropic_image()]}
        ]}]}
        limited, _ = apply_image_policy(image_only, max_images=0)
        result = limited["messages"][0]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["content"][0]["type"], "text")

    def test_responses_tool_outputs_url_and_file_id_count(self):
        body = {"input": [
            {"role": "user", "content": [response_image(0)]},
            {"type": "function_call", "call_id": "call_1", "name": "view", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [
                {"type": "input_image", "file_id": "file_synthetic"}, response_image(2)]},
            {"role": "user", "content": [response_image(3)]},
        ]}
        before = deepcopy(body)
        out, stats = apply_image_policy(body, field="input", max_images=2)
        self.assertEqual(stats, {"count": 4, "retained": 2, "dropped": 2})
        self.assertEqual(out["input"][2]["output"], [response_image(2)])
        self.assertIs(out["input"][1], body["input"][1])
        empty, _ = apply_image_policy(body, field="input", max_images=0)
        self.assertEqual(empty["input"][2]["output"][0]["type"], "input_text")
        self.assertEqual(empty["input"][2]["call_id"], "call_1")
        self.assertEqual(len(empty["input"]), len(body["input"]))
        self.assertEqual(body, before)

    def test_responses_assistant_empty_placeholder(self):
        body = {"input": [{"type": "message", "role": "assistant", "content": [response_image()]}]}
        out, _ = apply_image_policy(body, field="input", max_images=0)
        self.assertEqual(out["input"][0]["content"][0]["type"], "output_text")
        self.assertTrue(responses_request_to_chat(out)["messages"][0]["content"])

    def test_no_false_positives_in_schema_arguments_json_or_text(self):
        sample = {"content": [chat_image(), response_image(), anthropic_image()]}
        body = {
            "tools": [{"input_schema": sample, "parameters": sample}],
            "metadata": sample,
            "messages": [
                {"role": "user", "content": json.dumps(sample)},
                {"role": "user", "content": [text(json.dumps(sample)), {"type": "json", "data": sample}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call_a", "input": sample}],
                 "tool_calls": [{"function": {"arguments": sample}}]},
                {"role": "tool", "content": sample, "tool_call_id": "call_a"},
            ],
            "input": [
                {"type": "function_call", "arguments": sample},
                {"type": "function_call_output", "output": sample},
                {"type": "function_call_output", "output": json.dumps(sample)},
                {"type": "reasoning", "content": [response_image()]},
            ],
        }
        before = deepcopy(body)
        for field in ("messages", "input"):
            out, stats = apply_image_policy(body, field=field, max_images=0, policy="error")
            self.assertIs(out, body)
            self.assertEqual(stats, {"count": 0, "retained": 0, "dropped": 0})
        self.assertEqual(body, before)

    def test_invalid_configuration_is_rejected_even_without_images(self):
        for limit in (-1, True, False, 1.5, "16", None, [], {}):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                apply_image_policy({}, max_images=limit)
        for policy in ("drop", "TRUNCATE", "", None, [], {}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                apply_image_policy({}, policy=policy)
        for body in ({}, {"messages": []}, {"input": "An image_url example in plain text"}):
            out, stats = apply_image_policy(body, max_images=0)
            self.assertIs(out, body)
            self.assertEqual(stats["count"], 0)


class ImageAdapterTests(unittest.TestCase):
    def test_anthropic_url_base64_and_tool_images_preserved(self):
        base64_image = {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "synthetic-not-decoded"}}
        body = {"messages": [
            {"role": "user", "content": [text("before"), anthropic_image(0), text("after"), base64_image]},
            {"role": "assistant", "content": [text("inspect"), anthropic_image(1),
                {"type": "tool_use", "id": "call_1", "name": "view", "input": {"x": 1}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1",
                "content": [text("tool text"), base64_image]}]},
        ]}
        before = deepcopy(body)
        with patch("base64.b64decode", side_effect=AssertionError("must not decode")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("must not fetch")), \
             patch("socket.create_connection", side_effect=AssertionError("must not connect")):
            limited, stats = apply_image_policy(body)
            chat = anthropic_request_to_chat(limited)
            projected, _ = project_responses_chat_body(chat)
        self.assertEqual(stats["count"], 4)
        messages = projected["messages"]
        self.assertEqual(messages[0]["content"][:3], [text("before"), chat_image(0), text("after")])
        data_url = {"type": "image_url", "image_url": {"url": "data:image/png;base64,synthetic-not-decoded"}}
        self.assertEqual(messages[0]["content"][3], data_url)
        self.assertEqual(messages[1]["content"], [text("inspect"), chat_image(1)])
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(json.loads(messages[1]["tool_calls"][0]["function"]["arguments"]), {"x": 1})
        self.assertEqual(messages[2], {"role": "tool", "tool_call_id": "call_1", "content": [text("tool text"), data_url]})
        self.assertEqual(body, before)

    def test_anthropic_unsupported_source_reports_error(self):
        empty_text = anthropic_request_to_chat({"messages": [{"role": "assistant", "content": [text("")]}]})
        self.assertEqual(empty_text["messages"][0]["content"], "")
        for source in (None, {"type": "file", "file_id": "file_synthetic"},
                       {"type": "url", "url": ""}, {"type": "base64", "data": "abc"}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                anthropic_request_to_chat({"messages": [{"role": "user", "content": [
                    {"type": "image", "source": source}]}]})

    def test_responses_url_detail_data_url_and_tool_images_preserved(self):
        image = {**response_image(0), "detail": "high"}
        data_image = {"type": "input_image", "image_url": "data:image/jpeg;base64,synthetic"}
        body = {"input": [
            {"role": "user", "content": [text("before", "input_text"), image, text("after", "input_text")]},
            {"type": "message", "role": "assistant", "content": [text("inspect", "output_text"), response_image(1)]},
            {"type": "function_call", "call_id": "call_1", "name": "view", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [text("result", "input_text"), data_image]},
            {"role": "assistant", "content": [response_image(2)]},
        ]}
        before = deepcopy(body)
        limited, stats = apply_image_policy(body, field="input")
        chat = responses_request_to_chat(limited)
        messages = chat["messages"]
        self.assertEqual(stats["count"], 4)
        self.assertEqual(messages[0]["content"], [text("before"),
            {"type": "image_url", "image_url": {"url": image["image_url"], "detail": "high"}}, text("after")])
        self.assertEqual(messages[1]["content"], [text("inspect"), chat_image(1)])
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(messages[2]["tool_call_id"], "call_1")
        self.assertEqual(messages[2]["content"], [text("result"),
            {"type": "image_url", "image_url": {"url": data_image["image_url"]}}])
        self.assertEqual(messages[3]["content"], [chat_image(2)])
        self.assertEqual(body, before)

    def test_responses_file_id_explicitly_rejected_unless_removed_by_policy(self):
        file_image = {"type": "input_image", "file_id": "file_synthetic"}
        for item in (
            {"role": "user", "content": [file_image]},
            {"type": "message", "role": "assistant", "content": [file_image]},
            {"type": "function_call_output", "call_id": "call_1", "output": [file_image]},
        ):
            with self.subTest(item=item):
                body = {"input": [item]}
                before = deepcopy(body)
                with self.assertRaisesRegex(ValueError, "file_id is not supported"):
                    responses_request_to_chat(body)
                limited, stats = apply_image_policy(body, field="input", max_images=0)
                chat = responses_request_to_chat(limited)
                self.assertTrue(chat["messages"][0]["content"])
                self.assertEqual(stats["dropped"], 1)
                self.assertEqual(body, before)

    def test_truncation_survives_adapters_and_projection(self):
        for field, factory, adapter in (("messages", anthropic_image, anthropic_request_to_chat),
                                        ("input", response_image, responses_request_to_chat)):
            with self.subTest(field=field):
                body = {field: [{"role": "user", "content": [factory(i)]} for i in range(17)]}
                limited, stats = apply_image_policy(body, field=field)
                projected, _ = project_responses_chat_body(adapter(limited))
                images = [block for msg in projected["messages"] if isinstance(msg["content"], list)
                          for block in msg["content"] if block.get("type") == "image_url"]
                self.assertEqual(images, [chat_image(i) for i in range(1, 17)])
                self.assertEqual(stats["retained"], 16)
                self.assertEqual(len(projected["messages"]), 17)
                self.assertTrue(projected["messages"][0]["content"])

    def test_projection_preserves_old_images_harness_and_full_tool_chain(self):
        large_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 20000}}
        call = {"id": "call_old", "type": "function", "function": {"name": "view_image", "arguments": "{}"}}
        body = {"messages": [
            {"role": "system", "content": [text("You are a coding agent running in the Codex CLI"), large_image]},
            {"role": "user", "content": [text("# AGENTS.md instructions"), chat_image(0)]},
            {"role": "assistant", "content": [text("inspect"), chat_image(1)], "tool_calls": [call],
             "reasoning_content": "unchanged reasoning"},
            {"role": "tool", "tool_call_id": "call_old", "content": [text("x" * 5000), chat_image(2)]},
            *[{"role": "user", "content": "later text"} for _ in range(12)],
            {"role": "developer", "content": [chat_image(3)]},
        ], "tools": [{"type": "function", "function": {"name": "view_image", "description": "long metadata",
            "parameters": {"type": "object", "properties": {}}}}]}
        before = deepcopy(body)
        out, stats = project_responses_chat_body(body)
        self.assertEqual(stats["mode"], "conservative")
        self.assertEqual(len(out["messages"]), len(body["messages"]))
        for index in (0, 1, 2, 3, 16):
            self.assertEqual(out["messages"][index]["content"][-1], body["messages"][index]["content"][-1])
        self.assertLess(len(out["messages"][3]["content"][0]["text"]), 5000)
        self.assertEqual(out["messages"][2]["tool_calls"], [call])
        self.assertEqual(out["messages"][2]["reasoning_content"], "unchanged reasoning")
        self.assertEqual(out["messages"][3]["tool_call_id"], "call_old")
        self.assertNotIn("description", out["tools"][0]["function"])
        self.assertEqual(body, before)


if __name__ == "__main__":
    unittest.main()
