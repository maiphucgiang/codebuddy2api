"""Verify mixed Chat/Anthropic history normalization with synthetic requests only."""
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi import HTTPException
import converter as gateway
import test_runtime_endpoints as fixtures
import test_api_flow as routing


IMAGE = {"type": "image_url", "image_url": {"url": "https://synthetic.invalid/image.png", "detail": "high"}}
ANTHROPIC_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


def call(identifier="call_1", **changes):
    return {"type": "tool_use", "id": identifier, "name": "lookup", "input": {"q": "synthetic"}, **changes}


def result(identifier="call_1", **changes):
    return {"type": "tool_result", "tool_use_id": identifier, "content": "synthetic result", **changes}


def history():
    return [{"role": "user", "content": "look up data"},
            {"role": "assistant", "content": [call()]},
            {"role": "user", "content": [result()]}]


class ChatInputTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(gateway.CONFIG, model_guard=False, desensitize=False,
                                     max_request_bytes=32 * 1024 * 1024))

    def prepare(self, messages):
        body = {"model": "auto", "messages": [{"role": "system", "content": "system"}, *messages]}
        return gateway._prepare_chat_body(body)["messages"][1:]

    def assert_invalid(self, messages):
        before = deepcopy(messages)
        with self.assertRaises(HTTPException) as error:
            self.prepare(messages)
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(error.exception.detail["error"]["type"], "invalid_request_error")
        self.assertTrue(error.exception.detail["error"]["param"].startswith("messages["))
        self.assertNotIn("private-synthetic", str(error.exception.detail))
        self.assertEqual(messages, before)

    def test_tool_history_keeps_arguments_ids_and_input(self):
        messages = history()
        arguments = {"text": "中文\nquotes: \"", "nested": {"type": "tool_use", "input": [1, 2]}, "empty": {}}
        messages[1]["content"][0]["input"] = arguments
        before = deepcopy(messages)
        prepared = self.prepare(messages)
        self.assertIsNone(prepared[1]["content"])
        tool_call = prepared[1]["tool_calls"][0]
        self.assertEqual(tool_call["id"], "call_1")
        self.assertEqual(tool_call["type"], "function")
        self.assertEqual(tool_call["function"]["name"], "lookup")
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), arguments)
        self.assertEqual(prepared[2], {"role": "tool", "tool_call_id": "call_1", "content": "synthetic result"})
        self.assertEqual(messages, before)
        self.assertEqual(self.prepare(prepared), prepared)

    def test_parallel_results_precede_followup_and_preserve_images_and_errors(self):
        messages = [{"role": "assistant", "name": "assistant_name", "content": [
            {"type": "text", "text": "before"}, deepcopy(IMAGE), call(), call("call_2"),
            {"type": "text", "text": "after"}]},
            {"role": "user", "content": [
                result(content=[{"type": "text", "text": "failed"}, deepcopy(IMAGE), deepcopy(ANTHROPIC_IMAGE)], is_error=True),
                result("call_2", content=""), {"type": "text", "text": "continue"}, deepcopy(ANTHROPIC_IMAGE)]}]
        before = deepcopy(messages)
        prepared = self.prepare(messages)
        self.assertEqual([m["role"] for m in prepared], ["assistant", "tool", "tool", "user"])
        self.assertEqual(prepared[0]["name"], "assistant_name")
        self.assertEqual(prepared[0]["content"], [messages[0]["content"][0], IMAGE, messages[0]["content"][-1]])
        self.assertEqual([c["id"] for c in prepared[0]["tool_calls"]], ["call_1", "call_2"])
        self.assertEqual(prepared[1]["content"], [
            {"type": "text", "text": "[tool execution failed]"}, {"type": "text", "text": "failed"}, IMAGE,
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}])
        self.assertEqual(prepared[2]["content"], "")
        self.assertEqual(prepared[3]["content"][0], {"type": "text", "text": "continue"})
        self.assertEqual(prepared[3]["content"][1]["image_url"]["url"], "data:image/png;base64,YQ==")
        self.assertEqual(messages, before)

    def test_thinking_is_reasoning_not_visible_content(self):
        messages = [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "first", "signature": "private-synthetic-signature"},
            {"type": "thinking", "thinking": "second"}, {"type": "text", "text": "answer"}, call()]}]
        prepared = self.prepare(messages)
        self.assertEqual(prepared[0]["reasoning_content"], "firstsecond")
        self.assertEqual(prepared[0]["content"], [{"type": "text", "text": "answer"}])
        self.assertNotIn("private-synthetic", json.dumps(prepared))
        self.assertEqual(len(prepared[0]["tool_calls"]), 1)
        thinking_only = self.prepare([{"role": "assistant", "content": [{"type": "thinking", "thinking": "reason"}]}])
        self.assertEqual(thinking_only, [{"role": "assistant", "content": None, "reasoning_content": "reason"}])

    def test_native_chat_fields_and_opaque_json_are_unchanged(self):
        messages = [{"role": "user", "name": "user_name", "content": [deepcopy(IMAGE), {"type": "text", "text": "tool_use"}]},
                    {"role": "assistant", "content": None, "reasoning_content": "existing", "tool_calls": [{
                        "id": "native", "type": "function", "function": {"name": "lookup", "arguments": '{"type":"tool_use"}'}}]},
                    {"role": "tool", "tool_call_id": "native", "content": "{\"type\":\"tool_result\"}"}]
        before = deepcopy(messages)
        self.assertEqual(self.prepare(messages), before)
        self.assertEqual(messages, before)
        self.assertEqual(self.prepare([{"role": "assistant", "content": [], "tool_calls": []}]),
                         [{"role": "assistant", "content": [], "tool_calls": []}])

    def test_converted_results_can_follow_native_calls_and_native_results_can_follow_converted_calls(self):
        messages = history()
        native = self.prepare(messages)
        mixed = [messages[0], native[1], messages[2]]
        self.assertEqual(self.prepare(mixed), native)
        mixed = [messages[0], messages[1], native[2]]
        self.assertEqual(self.prepare(mixed), native)

    def test_error_and_omitted_empty_result_content(self):
        for is_error in (False, True):
            messages = history()
            messages[-1]["content"][0] = {"type": "tool_result", "tool_use_id": "call_1", "is_error": is_error}
            prepared = self.prepare(messages)
            self.assertEqual(prepared[-1]["content"], "[tool execution failed]\n" if is_error else "")

    def test_malformed_and_conflicting_blocks_fail_closed(self):
        bad_calls = [call(id=""), call(id=42), call(name=" "), call(input="private-synthetic"),
                     call(input=None), call(input=[]), {"type": "tool_use", "id": "call_1", "name": "lookup"}]
        for block in bad_calls:
            with self.subTest(block=block):
                self.assert_invalid([{"role": "assistant", "content": [block]}])
        for blocks in ([call(), call()], [call(), {"type": "unknown", "text": "private-synthetic"}], [call(), 3],
                       [{"type": "thinking", "thinking": 3}], [{"type": "redacted_thinking", "data": "private-synthetic"}]):
            with self.subTest(blocks=blocks):
                self.assert_invalid([{"role": "assistant", "content": blocks}])
        for role, block in (("user", call()), ("system", call()), ("assistant", result()),
                            ("tool", result()), ("user", {"type": "thinking", "thinking": "private-synthetic"})):
            self.assert_invalid([{"role": role, "content": [block]}])
        for extra in ({"tool_calls": [{"id": "native"}]}, {"function_call": {"name": "lookup"}}):
            self.assert_invalid([{"role": "assistant", "content": [call()], **extra}])
        self.assert_invalid([{"role": "assistant", "content": [{"type": "thinking", "thinking": "new"}],
                              "reasoning_content": "private-synthetic"}])

    def test_invalid_results_and_unrepresentable_images_fail_closed(self):
        for block in (result(tool_use_id=""), result(tool_use_id=12), result(content=None), result(content={}),
                      result(is_error="false"), result(content=[call()]), result(content=[{"type": "text", "text": 3}]),
                      result(content=[{"type": "image", "source": {"type": "file", "file_id": "private-synthetic"}}]),
                      result(content=[{"type": "image", "source": {"type": "url", "url": ""}}])):
            with self.subTest(block=block):
                messages = history()
                messages[-1]["content"] = [block]
                self.assert_invalid(messages)
        messages = history()
        messages[-1]["name"] = "private-synthetic"
        self.assert_invalid(messages)

    def test_user_content_before_or_between_tool_results_is_rejected(self):
        for ordinary in ({"type": "text", "text": "keep this order"}, IMAGE, ANTHROPIC_IMAGE):
            for position in (0, 1):
                with self.subTest(kind=ordinary["type"], position=position):
                    messages = [{"role": "assistant", "content": [call(), call("call_2")]},
                                {"role": "user", "content": [result(), result("call_2")]}]
                    messages[-1]["content"].insert(position, deepcopy(ordinary))
                    self.assert_invalid(messages)


    def test_separate_result_messages_and_empty_blocks_keep_call_boundaries(self):
        messages = [{"role": "assistant", "content": [{"type": "text", "text": ""}, call(), call("call_2")]},
                    {"role": "user", "content": [result()]},
                    {"role": "user", "content": [result("call_2", content=[]), {"type": "text", "text": "next"}]}]
        prepared = self.prepare(messages)
        self.assertEqual([message["role"] for message in prepared], ["assistant", "tool", "tool", "user"])
        self.assertEqual(prepared[0]["content"], [{"type": "text", "text": ""}])
        self.assertEqual(prepared[2]["content"], "")
        messages[1]["content"].append({"type": "text", "text": "too early"})
        self.assert_invalid(messages)

    def test_late_system_is_hoisted_before_matching_tool_results(self):
        for role in ("system", "developer"):
            with self.subTest(role=role):
                messages = history()
                messages.insert(2, {"role": role, "content": "late instructions"})
                before = deepcopy(messages)
                prepared = gateway._prepare_chat_body({"model": "auto", "messages": messages})["messages"]
                self.assertEqual(prepared[0], {"role": "system", "content": "late instructions"})
                self.assertEqual([m["role"] for m in prepared], ["system", "user", "assistant", "tool"])
                self.assertEqual(prepared[2]["tool_calls"][0]["id"], prepared[3]["tool_call_id"])
                self.assertEqual(messages, before)
                # Only the first system is hoisted; a remaining instruction still interrupts the tool run.
                self.assert_invalid([{"role": "system", "content": "already first"}, *messages])

    def test_content_error_paths_keep_original_indices_after_system_placement(self):
        for role, position in ((None, None), ("system", 0), ("system", 2), ("developer", 2)):
            with self.subTest(role=role, position=position):
                messages = history()
                if role:
                    messages.insert(position, {"role": role, "content": "instructions"})
                index = next(i for i, message in enumerate(messages) if message["role"] == "assistant")
                messages[index]["content"][0]["id"] = ""
                with self.assertRaises(HTTPException) as error:
                    gateway._prepare_chat_body({"model": "auto", "messages": messages})
                self.assertEqual(error.exception.status_code, 400)
                self.assertEqual(error.exception.detail["error"]["param"], f"messages[{index}].content[0].id")


    def test_thinking_alongside_native_calls_and_empty_reasoning_field(self):
        native = self.prepare(history())[1]
        native["reasoning_content"] = ""
        native["content"] = [{"type": "thinking", "thinking": "reason"}]
        prepared = self.prepare([native])[0]
        self.assertEqual(prepared["tool_calls"], native["tool_calls"])
        self.assertEqual(prepared["reasoning_content"], "reason")
        self.assertIsNone(prepared["content"])

    def test_invalid_argument_json_does_not_echo_values(self):
        for value in (float("nan"), float("inf"), {1, 2}):
            with self.subTest(value=value):
                self.assert_invalid([{"role": "assistant", "content": [call(input={"private-synthetic": value})]}])


    def test_orphan_duplicate_and_out_of_order_results_are_rejected(self):
        self.assert_invalid([{"role": "user", "content": [result()]}])
        messages = history()
        messages[-1]["content"] = [result(), result()]
        self.assert_invalid(messages)
        messages[-1]["content"] = [result("unknown")]
        self.assert_invalid(messages)
        messages = history()
        messages.insert(2, {"role": "user", "content": "intervening message"})
        self.assert_invalid(messages)


class ChatInputEndpointTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.EndpointTests()
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()

    def test_mixed_history_is_normalized_before_upstream_in_both_modes(self):
        def reply(request):
            body = routing.request_json(request)
            for message in body["messages"]:
                if isinstance(message.get("content"), list):
                    for index, block in enumerate(message["content"]):
                        if block.get("type") not in ("text", "image_url"):
                            return httpx.Response(400, json={"code": 11101, "msg":
                                f"Parse message failed: unsupported content type at index {index}: {block.get('type')}"})
            return httpx.Response(200, content=fixtures.sse())
        self.fx.respond = reply
        for stream, desensitize, thinking in product((False, True), repeat=3):
            with self.subTest(stream=stream, desensitize=desensitize, thinking=thinking), patch.dict(
                    gateway.CONFIG, desensitize=desensitize):
                self.fx.requests.clear()
                messages = history()
                if thinking:
                    messages[1]["content"].insert(0, {"type": "thinking", "thinking": "synthetic reasoning"})
                response = self.fx.client.post("/v1/chat/completions", json={
                    "model": "auto", "messages": messages, "tools": TOOLS, "stream": stream})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(self.fx.requests), 1)
                body = routing.request_json(self.fx.requests[0])
                assistant_index = next(index for index, message in enumerate(body["messages"])
                                       if message["role"] == "assistant" and message.get("tool_calls"))
                assistant = body["messages"][assistant_index]
                self.assertEqual(assistant.get("reasoning_content"), "synthetic reasoning" if thinking else None)
                self.assertEqual(assistant["tool_calls"][0]["id"], body["messages"][assistant_index + 1]["tool_call_id"])
                self.assertEqual(body["tools"][0]["function"]["name"], "lookup")

    def test_late_system_and_developer_tool_history_reaches_upstream(self):
        for role, stream in product(("system", "developer"), (False, True)):
            with self.subTest(role=role, stream=stream):
                self.fx.requests.clear()
                messages = history()
                messages.insert(2, {"role": role, "content": "late instructions"})
                response = self.fx.client.post("/v1/chat/completions", json={
                    "model": "auto", "messages": messages, "stream": stream})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(self.fx.requests), 1)
                sent = routing.request_json(self.fx.requests[0])["messages"]
                self.assertEqual(sent[0], {"role": "system", "content": "late instructions"})
                self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "tool"])
                self.assertEqual(sent[2]["tool_calls"][0]["id"], sent[3]["tool_call_id"])


    def test_invalid_history_is_rejected_before_credential_selection(self):
        for stream in (False, True):
            self.fx.credentials.reset_mock()
            self.fx.requests.clear()
            messages = history()
            messages[1]["content"][0]["id"] = ""
            response = self.fx.client.post("/v1/chat/completions", json={"model": "auto", "messages": messages, "stream": stream})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["type"], "invalid_request_error")
            self.fx.credentials.assert_not_called()
            self.assertEqual(self.fx.requests, [])

    def test_reordered_user_content_is_rejected_without_an_upstream_attempt(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.fx.credentials.reset_mock()
                self.fx.requests.clear()
                messages = history()
                messages[-1]["content"].insert(0, {"type": "text", "text": "before the result"})
                response = self.fx.client.post("/v1/chat/completions", json={
                    "model": "auto", "messages": messages, "stream": stream})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "invalid_chat_content")
                self.fx.credentials.assert_not_called()
                self.assertEqual(self.fx.requests, [])


    def test_nested_image_policy_precedes_conversion(self):
        messages = history()
        messages[-1]["content"][0]["content"] = [deepcopy(ANTHROPIC_IMAGE), deepcopy(IMAGE)]
        for policy, status in (("truncate", 200), ("error", 413)):
            self.fx.requests.clear()
            with patch.dict(gateway.CONFIG, max_images=1, image_policy=policy):
                response = self.fx.client.post("/v1/chat/completions", json={"model": "auto", "messages": messages})
            self.assertEqual(response.status_code, status, response.text)
            if policy == "truncate":
                body = routing.request_json(self.fx.requests[0])
                self.assertEqual(body["messages"][-1]["content"], [IMAGE])
            else:
                self.assertEqual(self.fx.requests, [])

    def test_expanded_tool_arguments_are_subject_to_wire_size_limit(self):
        messages = history()
        messages[1]["content"][0]["input"] = {"escaped": '"' * 2000}
        payload = {"model": "auto", "messages": messages}
        before = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        self.fx.credentials.reset_mock()
        with patch.dict(gateway.CONFIG, max_request_bytes=before + 500):
            response = self.fx.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(response.status_code, 413, response.text)
        self.fx.credentials.assert_not_called()
        self.assertEqual(self.fx.requests, [])


class ChatInputRoutingTests(routing.GatewayFixture, unittest.TestCase):
    def test_converted_history_reaches_each_profile_and_releases_capacity(self):
        for profile in routing.fixtures.PROFILES:
            for stream in (False, True):
                with self.subTest(profile=profile, stream=stream):
                    self.fx.configure(profiles=(profile,))
                    payload = self.fx.payload("chat/completions", stream=stream)
                    payload.update(messages=history(), tools=TOOLS)
                    self.fx.post_ok("chat/completions", payload, {profile})
                    body = routing.request_json(self.fx.requests[-1])
                    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
                    tool = next(m for m in body["messages"] if m["role"] == "tool")
                    self.assertEqual(assistant["tool_calls"][0]["id"], tool["tool_call_id"])
                    self.assertEqual(self.fx.pool._capacity._counts, {})



if __name__ == "__main__":
    unittest.main(verbosity=2)
