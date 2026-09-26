"""Verify shared reasoning controls, protocol round trips and account-owned defaults."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copy import deepcopy
from itertools import permutations, product
import json
import unittest
from unittest.mock import patch

import httpx
import converter as gateway
from app.control_store import ControlStore
import test_api_flow as fixtures
import test_runtime_endpoints as runtime

MODES = ((False, "compatible"), (True, "compatible"), (True, "realtime"))
PROTOCOLS = ("chat/completions", "messages", "responses")


def model(effort="medium", **extra):
    return {"id": "shared-model", "credits": "x0.00", "supportsReasoning": True,
            "supportsToolCall": True, "canDisableThinking": True,
            "reasoning": {"defaultEffort": effort, "supportedEfforts": ["low", "medium", "high", "xhigh", "max"]}, **extra}


def payload(protocol, **extra):
    body = {"model": "shared-model", "max_tokens": 2048}
    body["input" if protocol == "responses" else "messages"] = [{"role": "user", "content": "question"}]
    body.update(extra)
    return body


def reasoning(text):
    return {"type": "reasoning", "summary": [{"type": "summary_text", "text": text}]}


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]


def output(response, protocol, stream):
    if not stream:
        return response.json()
    parsed = events(response)
    if protocol == "responses":
        return next(event["response"] for event in parsed if event.get("type") == "response.completed")
    blocks = []
    for event in parsed:
        if event["type"] == "content_block_start":
            blocks.append(deepcopy(event["content_block"]))
        elif event["type"] == "content_block_delta":
            block, delta = blocks[event["index"]], event["delta"]
            if delta["type"] == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "input_json_delta":
                block["_arguments"] = block.get("_arguments", "") + delta["partial_json"]
    for block in blocks:
        if "_arguments" in block:
            block["input"] = json.loads(block.pop("_arguments"))
    return {"content": blocks}


class ReasoningEndpointTests(unittest.TestCase):
    def setUp(self):
        self.fx = runtime.EndpointTests("test_stateful_responses_fields_are_rejected")
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.metadata = self.enterContext(patch.object(gateway.model_capabilities, "entry_model", return_value=model()))
        self.fx.respond = self.respond

    def respond(self, request):
        body = json.loads(request.content)
        delta = {"content": "answer"}
        if body.get("reasoning_effort") not in (None, "none"):
            delta["reasoning_content"] = "new reasoning"
        return httpx.Response(200, content=runtime.sse(delta))

    def post(self, protocol, body, *, stream=False, mode="compatible", projection="balanced", desensitize=False):
        original = deepcopy(body)
        before = len(self.fx.requests)
        with patch.dict(gateway.CONFIG, stream_mode=mode, responses_projection_mode=projection, desensitize=desensitize):
            response = self.fx.client.post("/v1/" + protocol, json={**body, "stream": stream})
        self.assertEqual(body, original)
        upstream = [json.loads(request.content) for request in self.fx.requests[before:]]
        return response, upstream

    def test_controls_and_output_in_all_modes(self):
        cases = [
            ("chat/completions", {"reasoning_effort": "high"}, "high"),
            ("responses", {"reasoning": {"effort": "high"}}, "high"),
            ("responses", {"reasoning": {"effort": "high"}, "reasoning_effort": "low"}, "low"),
            ("responses", {"reasoning": {"effort": "none"}}, "none"),
            ("messages", {"thinking": {"type": "enabled", "budget_tokens": 1024}}, "medium"),
            ("messages", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}, "low"),
            ("messages", {"thinking": {"type": "enabled", "budget_tokens": 1024}, "output_config": {"effort": "high"}}, "high"),
            ("messages", {"thinking": {"type": "disabled"}, "output_config": {"effort": "high"}}, "none"),
            ("messages", {"reasoning_effort": "xhigh", "output_config": {"effort": "low"}}, "xhigh"),
            ("messages", {"output_config": {"effort": "high"}}, "high"),
        ] + [(protocol, {}, None) for protocol in PROTOCOLS]
        for (protocol, fields, expected), (stream, mode) in product(cases, MODES):
            with self.subTest(protocol=protocol, fields=fields, stream=stream, mode=mode):
                response, sent = self.post(protocol, payload(protocol, **fields), stream=stream, mode=mode)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0].get("reasoning_effort"), expected)
                self.assertEqual(sent[0]["max_tokens"], 2048)
                self.assertTrue({"thinking", "output_config", "reasoning", "budget_tokens"}.isdisjoint(sent[0]))
                self.assertEqual("new reasoning" in response.text, expected not in (None, "none"))

    def test_readable_history_is_separate_from_visible_text(self):
        thoughts = [{"type": "thinking", "thinking": "first", "signature": "signature-canary"},
                    {"type": "thinking", "thinking": "second"}]
        for protocol, (stream, mode), only, projection, desensitize in product(
                PROTOCOLS, MODES, (False, True), ("balanced", "passthrough"), (False, True)):
            with self.subTest(protocol=protocol, stream=stream, mode=mode, only=only, projection=projection, desensitize=desensitize):
                body = payload(protocol)
                history = [{"role": "user", "content": "question"}]
                if protocol == "responses":
                    history += [reasoning("first"), reasoning("second")]
                    if not only:
                        history.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]})
                else:
                    history.append({"role": "assistant", "content": deepcopy(thoughts) + ([] if only else [{"type": "text", "text": "answer"}])})
                history.append({"role": "user", "content": "continue"})
                body["input" if protocol == "responses" else "messages"] = history
                response, sent = self.post(protocol, body, stream=stream, mode=mode, projection=projection, desensitize=desensitize)
                self.assertEqual(response.status_code, 200, response.text)
                assistants = [m for m in sent[0]["messages"] if m["role"] == "assistant"]
                self.assertEqual(len(assistants), 1)
                self.assertEqual(assistants[0]["reasoning_content"], "firstsecond")
                self.assertNotIn("first", json.dumps(assistants[0]["content"]))
                self.assertNotIn("signature-canary", json.dumps(sent))

    def test_gateway_output_roundtrips_through_tool_results(self):
        call = {"index": 0, "id": "call_shared", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        first = runtime.sse({"reasoning_content": "firstsecond", "tool_calls": [call]}, finish="tool_calls")
        for protocol, (stream, mode), projection in product(("messages", "responses"), MODES, ("balanced", "passthrough")):
            with self.subTest(protocol=protocol, stream=stream, mode=mode, projection=projection):
                self.fx.respond = lambda request: httpx.Response(200, content=first)
                body = payload(protocol)
                body["tools"] = ([{"name": "lookup", "input_schema": {"type": "object", "properties": {}}}]
                                 if protocol == "messages" else [{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}}])
                response, _ = self.post(protocol, body, stream=stream, mode=mode, projection=projection)
                self.assertEqual(response.status_code, 200, response.text)
                result = output(response, protocol, stream)
                if protocol == "messages":
                    body["messages"] += [{"role": "assistant", "content": result["content"]},
                                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_shared", "content": "43"}]}]
                else:
                    body["input"] += result["output"] + [{"type": "function_call_output", "call_id": "call_shared", "output": "43"}]
                self.fx.respond = self.respond
                response, sent = self.post(protocol, body, stream=stream, mode=mode, projection=projection)
                self.assertEqual(response.status_code, 200, response.text)
                messages = sent[0]["messages"]
                assistant = next(m for m in messages if m["role"] == "assistant")
                self.assertEqual(assistant["reasoning_content"], "firstsecond")
                self.assertEqual(assistant["tool_calls"][0]["id"], "call_shared")
                self.assertEqual(messages[messages.index(assistant) + 1], {"role": "tool", "tool_call_id": "call_shared", "content": "43"})

    def test_realtime_tool_first_output_preserves_one_assistant_turn(self):
        deltas = [{"tool_calls": [{"index": 0, "id": "call_first", "type": "function",
                                  "function": {"name": "lookup", "arguments": "{}"}}]},
                  {"reasoning_content": "late reasoning"}, {"content": "after tool"}]
        chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
        self.fx.respond = lambda request: httpx.Response(200, content=raw)
        body = payload("responses", tools=[{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}}])
        response, _ = self.post("responses", body, stream=True, mode="realtime")
        self.assertEqual(response.status_code, 200, response.text)
        result = output(response, "responses", True)
        self.assertEqual([item["type"] for item in result["output"]], ["function_call", "reasoning", "message"])
        body["input"] += result["output"] + [{"type": "function_call_output", "call_id": "call_first", "output": "43"}]
        self.fx.respond = self.respond
        response, sent = self.post("responses", body, stream=True, mode="realtime")
        self.assertEqual(response.status_code, 200, response.text)
        messages = sent[0]["messages"]
        assistants = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["reasoning_content"], "late reasoning")
        self.assertEqual(assistants[0]["content"], "after tool")
        self.assertEqual(assistants[0]["tool_calls"][0]["id"], "call_first")
        self.assertEqual(messages[messages.index(assistants[0]) + 1]["tool_call_id"], "call_first")

    def test_responses_keeps_upstream_declared_effort_extensions(self):
        self.metadata.return_value = model(reasoning={"supportedEfforts": ["ultra"]})
        response, sent = self.post("responses", payload("responses", reasoning={"effort": "ultra"}))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sent[0]["reasoning_effort"], "ultra")
        response, sent = self.post("responses", payload("responses", reasoning={"effort": "low"}))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "unsupported_reasoning_effort")
        self.assertEqual(sent, [])

    def test_responses_assistant_item_orders_keep_tools_and_reasoning_together(self):
        items = [reasoning("same turn"),
                 {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
                 {"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}"}]
        for ordered in permutations(items):
            with self.subTest(order=[item["type"] for item in ordered]):
                body = payload("responses", input=[{"role": "user", "content": "question"}, *ordered,
                               {"type": "function_call_output", "call_id": "call_1", "output": "43"}])
                response, sent = self.post("responses", body)
                self.assertEqual(response.status_code, 200, response.text)
                assistants = [m for m in sent[0]["messages"] if m["role"] == "assistant"]
                self.assertEqual(len(assistants), 1)
                self.assertEqual(assistants[0]["reasoning_content"], "same turn")
                self.assertEqual(assistants[0]["content"], "answer")
                self.assertEqual(assistants[0]["tool_calls"][0]["id"], "call_1")

    def test_responses_keeps_reasoning_with_its_assistant_turn(self):
        body = payload("responses", input=[
            {"role": "user", "content": "question"}, reasoning("before answer"),
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
            {"role": "user", "content": "look it up next"},
            reasoning("before tool"), {"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "43"},
            reasoning("after tool"), {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
            {"role": "user", "content": "continue"},
        ])
        response, sent = self.post("responses", body)
        self.assertEqual(response.status_code, 200, response.text)
        assistants = [m for m in sent[0]["messages"] if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], ["before answer", "before tool", "after tool"])
        self.assertEqual([m["content"] for m in assistants], ["answer", "", "done"])

    def test_responses_prefers_readable_content_over_its_summary(self):
        item = {**reasoning("summary"), "content": [{"type": "reasoning_text", "text": "full "}, {"type": "text", "text": "content"}]}
        body = payload("responses", input=[{"role": "user", "content": "question"}, item,
                                           {"role": "assistant", "content": "answer"}, {"role": "user", "content": "continue"}])
        response, sent = self.post("responses", body)
        self.assertEqual(response.status_code, 200, response.text)
        assistant = next(m for m in sent[0]["messages"] if m["role"] == "assistant")
        self.assertEqual(assistant["reasoning_content"], "full content")

    def test_opaque_or_malformed_reasoning_is_rejected_without_upstream(self):
        blocks = [{"type": "redacted_thinking", "data": "opaque-canary"}, {"type": "thinking", "thinking": 7},
                  {"type": "thinking", "thinking": "", "signature": "opaque-canary"}]
        for protocol in ("chat/completions", "messages"):
            for block in blocks:
                body = payload(protocol, messages=[{"role": "assistant", "content": [block]}])
                response, sent = self.post(protocol, body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(sent, [])
                self.assertNotIn("opaque-canary", response.text)
        for item in ({**reasoning("readable summary"), "encrypted_content": "opaque-canary"},
                     {"type": "reasoning", "summary": "opaque-canary"},
                     {"type": "reasoning", "summary": [{"type": "summary_text", "text": 7}]}):
            response, sent = self.post("responses", payload("responses", input=[item]))
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(sent, [])
            self.assertNotIn("opaque-canary", response.text)
        self.assertEqual(self.fx.credentials.call_count, 0)

    def test_invalid_controls_fail_before_routing(self):
        for controls in ({"thinking": []}, {"thinking": {"type": "unknown"}},
                         {"thinking": {"type": "enabled", "budget_tokens": True}},
                         {"thinking": {"type": "enabled", "budget_tokens": 100}},
                         {"thinking": {"type": "adaptive", "budget_tokens": 1024}},
                         {"thinking": {"type": "adaptive", "display": "omitted"}},
                         {"output_config": {"effort": "unknown"}}, {"output_config": []}):
            response, sent = self.post("messages", payload("messages", **controls))
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(sent, [])
        for control in ([], {"effort": True}, {"effort": " "}):
            response, sent = self.post("responses", payload("responses", reasoning=control))
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(sent, [])
        self.assertEqual(self.fx.credentials.call_count, 0)

    def test_activation_uses_legacy_defaults_and_skips_disabled_defaults(self):
        for metadata, expected in (({"reasoning": {"effort": "low"}}, "low"),
                                   ({"reasoning": {"defaultEffort": "low", "supportedEfforts": []}}, "low"),
                                   ({"reasoning": {"defaultEffort": "none", "supportedEfforts": ["low", "high"]}}, "high"),
                                   ({"reasoning": {"supportedEfforts": ["low"]}}, "low"), ({}, "high")):
            self.metadata.return_value = metadata
            response, sent = self.post("messages", payload("messages", thinking={"type": "adaptive"}))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(sent[0]["reasoning_effort"], expected)

    def test_disabled_preserves_capability_checks_and_does_not_request_reasoning(self):
        self.metadata.return_value = model(supportsReasoning=False)
        body = payload("messages", thinking={"type": "disabled"}, output_config={"effort": "high"})
        response, sent = self.post("messages", body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sent[0]["reasoning_effort"], "none")
        self.metadata.return_value = model(canDisableThinking=False)
        response, sent = self.post("messages", body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "reasoning_required")
        self.assertEqual(sent, [])

    def test_reasoning_counts_toward_request_size_limit(self):
        for protocol in ("messages", "responses"):
            body = payload(protocol)
            if protocol == "messages":
                body["messages"] = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x" * 2048}]}]
            else:
                body["input"] = [reasoning("x" * 2048)]
            with patch.dict(gateway.CONFIG, max_request_bytes=1024):
                response, sent = self.post(protocol, body)
            self.assertEqual(response.status_code, 413, response.text)
            self.assertEqual(sent, [])


class ReasoningRoutingTests(fixtures.GatewayFixture, unittest.TestCase):
    def test_each_account_uses_its_own_default_even_within_one_profile(self):
        self.fx.add_account("second", "intl-cli")
        self.fx.configure(profiles=("intl-cli", "second"))
        self.fx.account_catalogs({"intl-cli": [model("low")], "second": [model("high")]})
        seen = set()
        for guard in (True, False):
            for _ in range(4):
                body = self.fx.payload("messages")
                body["thinking"] = {"type": "adaptive"}
                with patch.dict(gateway.CONFIG, model_capability_guard=guard):
                    request, sent = self.fx.post_ok("messages", body, {"intl-cli", "second"})
                uid = request.headers["x-user-id"]
                seen.add(uid)
                self.assertEqual(sent["reasoning_effort"], {"intl-cli": "low", "second": "high"}[uid])
        self.assertEqual(seen, {"intl-cli", "second"})
        body["output_config"] = {"effort": "medium"}
        _, sent = self.fx.post_ok("messages", body, seen)
        self.assertEqual(sent["reasoning_effort"], "medium")

    def test_failover_recomputes_defaults_without_mutating_canonical_input(self):
        for stream, mode in MODES:
            with self.subTest(stream=stream, mode=mode):
                self.fx.configure(profiles=("cn-cli", "intl-work"))
                self.fx.account_catalogs({"cn-cli": [model("low")], "intl-work": [model("high")]})
                seen = []
                def respond(request):
                    seen.append(request)
                    if len(seen) == 1:
                        return httpx.Response(429, json={"error": {"message": "synthetic quota"}})
                    return httpx.Response(200, content=fixtures.fixtures.success_sse())
                body = self.fx.payload("messages", stream=stream)
                body["thinking"] = {"type": "adaptive"}
                original = deepcopy(body)
                with self.responder(respond), patch.dict(gateway.CONFIG, failover_max=1, stream_mode=mode):
                    response = self.fx.client.post("/v1/messages", json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(body, original)
                self.assertEqual(len(seen), 2)
                self.assertNotEqual(seen[0].headers["x-user-id"], seen[1].headers["x-user-id"])
                for request in seen:
                    expected = {"cn-cli": "low", "intl-work": "high"}[request.headers["x-user-id"]]
                    self.assertEqual(json.loads(request.content)["reasoning_effort"], expected)
                self.assertEqual(self.fx.pool._capacity._counts, {})

    def test_activation_cannot_escape_free_tier_or_strict_binding(self):
        self.fx.configure(profiles=("cn-cli", "intl-work"))
        self.fx.account_catalogs({"intl-work": [model(supportsReasoning=False)], "cn-cli": [model(credits="x1.00")]})
        body = self.fx.payload("messages")
        body["thinking"] = {"type": "adaptive"}
        response = self.fx.client.post("/v1/messages", json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fx.requests, [])
        self.fx.account_catalogs({"intl-work": [model(supportsReasoning=False)], "cn-cli": [model()]})
        store = ControlStore(self.fx.root / "reasoning-control.sqlite3")
        self.addCleanup(store.close)
        store.update_model("shared-model", {"profile": "intl-work"}, store.snapshot()["revision"], {"shared-model"})
        with patch.dict(gateway.CONFIG, control_store=store):
            response = self.fx.client.post("/v1/messages", json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fx.requests, [])
        self.assertEqual(self.fx.pool._capacity._counts, {})


if __name__ == "__main__":
    unittest.main()
