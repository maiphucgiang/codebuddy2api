#!/usr/bin/env python3
"""Interface-level regression tests for Responses multi-agent tools and namespace identity mapping."""

from copy import deepcopy
import json
import re
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient

import converter
from app import upstream_io


def _chat_completion_response(tool_calls=None, content="hello"):
    """Build a standard non-streaming Chat completions JSON response."""
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl_test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "auto",
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _chat_sse_stream(tool_calls=None, content=None):
    """Build Chat SSE streaming lines for buffered or realtime streams."""
    lines = []
    # Initial chunk
    lines.append("data: " + json.dumps({
        "id": "chatcmpl_chunk",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": "auto",
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }) + "\n\n")

    if content:
        lines.append("data: " + json.dumps({
            "id": "chatcmpl_chunk",
            "object": "chat.completion.chunk",
            "created": 1234567890,
            "model": "auto",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }) + "\n\n")

    if tool_calls:
        for idx, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            lines.append("data: " + json.dumps({
                "id": "chatcmpl_chunk",
                "object": "chat.completion.chunk",
                "created": 1234567890,
                "model": "auto",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": idx,
                            "id": tc.get("id", f"call_{idx}"),
                            "type": "function",
                            "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "{}")},
                        }]
                    },
                    "finish_reason": None,
                }],
            }) + "\n\n")

        lines.append("data: " + json.dumps({
            "id": "chatcmpl_chunk",
            "object": "chat.completion.chunk",
            "created": 1234567890,
            "model": "auto",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }) + "\n\n")
    else:
        lines.append("data: " + json.dumps({
            "id": "chatcmpl_chunk",
            "object": "chat.completion.chunk",
            "created": 1234567890,
            "model": "auto",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }) + "\n\n")

    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _parse_responses_sse_events(raw_text: str) -> list[dict]:
    """Parse SSE event stream from /v1/responses."""
    events = []
    for block in raw_text.strip().split("\n\n"):
        if not block:
            continue
        data_str = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), None)
        if data_str and data_str != "[DONE]":
            events.append(json.loads(data_str))
    return events


class ResponsesMultiAgentInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "model_guard": False,
            "max_images": 16, "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
            "log_body_limit": 65536, "log_path": None, "desensitize": False, "no_compact": False,
            "stream_mode": "compatible",
        }))
        self.enterContext(patch.object(converter, "_cred_for", return_value=(None, {})))
        self.enterContext(patch.object(converter, "_log"))
        self.enterContext(patch.object(converter, "_note_cred_status"))

        self.upstream_requests = []
        self.mock_response_generator = None

        def handle_request(request: httpx.Request):
            self.upstream_requests.append(request)
            if self.mock_response_generator:
                return self.mock_response_generator(request)
            return httpx.Response(200, json=_chat_completion_response())

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handle_request)
        self.enterContext(patch.object(upstream_io.httpx, "AsyncClient",
                                       side_effect=lambda **kw: real_client(transport=transport, **kw)))
        self.client = self.enterContext(TestClient(converter.app))

    def _sample_multiagent_payload(self, stream: bool = False):
        return {
            "model": "auto",
            "stream": stream,
            "tools": [
                {
                    "type": "function",
                    "name": "collaboration__spawn_agent",
                    "description": "Standard ordinary tool without namespace",
                    "parameters": {"type": "object", "properties": {"task": {"type": "string"}}},
                },
                {
                    "type": "namespace",
                    "name": "ns1",
                    "tools": [
                        {
                            "type": "function",
                            "name": "search",
                            "description": "Search in ns1",
                            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                        }
                    ],
                },
                {
                    "type": "namespace",
                    "name": "ns2",
                    "tools": [
                        {
                            "type": "function",
                            "name": "search",
                            "description": "Search in ns2",
                            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                        }
                    ],
                },
                {
                    "type": "namespace",
                    "name": "custom_ns",
                    "tools": [
                        {
                            "type": "function",
                            "name": "exec__command",
                            "description": "Tool whose name contains double underscores",
                            "parameters": {
                                "type": "object",
                                "properties": {"cmd": {"type": "string", "encrypted": True}},
                            },
                        }
                    ],
                },
            ],
            "input": [{"role": "user", "content": "Execute multi-agent subtasks"}],
        }

    def test_nonstream_tools_isolation_identity_and_metadata_sanitation(self):
        """Cover non-streaming mode: verify identity restoration, ordinary tool preservation, and metadata purity."""
        # 1. Test upstream call to ns1.search
        tc_ns1 = [{"id": "call_1", "type": "function", "function": {"name": "ns1__search", "arguments": '{"q":"weather"}'}}]
        self.mock_response_generator = lambda req: httpx.Response(200, content=_chat_sse_stream(tool_calls=tc_ns1))

        res = self.client.post("/v1/responses", json=self._sample_multiagent_payload(stream=False))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        output = data.get("output", [])
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["name"], "search")
        self.assertEqual(output[0]["namespace"], "ns1")

        # Verify internal metadata was not sent to upstream
        upstream_req = self.upstream_requests[-1]
        upstream_json = json.loads(upstream_req.content.decode("utf-8"))
        self.assertNotIn("_tool_registry", upstream_json)
        self.assertNotIn("_tool_namespaces", upstream_json)
        self.assertFalse(any(k.startswith("_") for k in upstream_json.keys()))

        # Verify upstream tools contains both ns1__search and ns2__search
        declared = [t["function"]["name"] for t in upstream_json.get("tools", [])]
        self.assertIn("ns1__search", declared)
        self.assertIn("ns2__search", declared)
        self.assertIn("collaboration__spawn_agent", declared)
        self.assertIn("custom_ns__exec__command", declared)

        # 2. Test upstream call to ns2.search
        tc_ns2 = [{"id": "call_2", "type": "function", "function": {"name": "ns2__search", "arguments": '{"q":"news"}'}}]
        self.mock_response_generator = lambda req: httpx.Response(200, content=_chat_sse_stream(tool_calls=tc_ns2))
        res2 = self.client.post("/v1/responses", json=self._sample_multiagent_payload(stream=False))
        self.assertEqual(res2.status_code, 200)
        out2 = res2.json().get("output", [])
        self.assertEqual(out2[0]["name"], "search")
        self.assertEqual(out2[0]["namespace"], "ns2")

        # 3. Test upstream call to plain tool collaboration__spawn_agent (MUST NOT be altered)
        tc_collab = [{"id": "call_3", "type": "function", "function": {"name": "collaboration__spawn_agent", "arguments": '{"task":"inspect"}'}}]
        self.mock_response_generator = lambda req: httpx.Response(200, content=_chat_sse_stream(tool_calls=tc_collab))
        res3 = self.client.post("/v1/responses", json=self._sample_multiagent_payload(stream=False))
        self.assertEqual(res3.status_code, 200)
        out3 = res3.json().get("output", [])
        self.assertEqual(out3[0]["name"], "collaboration__spawn_agent")
        self.assertNotIn("namespace", out3[0])

        # 4. Test upstream call to tool with __ in name
        tc_custom = [{"id": "call_4", "type": "function", "function": {"name": "custom_ns__exec__command", "arguments": '{"cmd":"ls"}'}}]
        self.mock_response_generator = lambda req: httpx.Response(200, content=_chat_sse_stream(tool_calls=tc_custom))
        res4 = self.client.post("/v1/responses", json=self._sample_multiagent_payload(stream=False))
        self.assertEqual(res4.status_code, 200)
        out4 = res4.json().get("output", [])
        self.assertEqual(out4[0]["name"], "exec__command")
        self.assertEqual(out4[0]["namespace"], "custom_ns")

    def test_buffered_stream_tools_isolation_identity_and_metadata_sanitation(self):
        """Cover buffered streaming mode (compatible): verify events and tool identities."""
        tc = [
            {"id": "call_a", "type": "function", "function": {"name": "ns1__search", "arguments": '{"q":"a"}'}},
            {"id": "call_b", "type": "function", "function": {"name": "collaboration__spawn_agent", "arguments": '{"task":"b"}'}},
            {"id": "call_c", "type": "function", "function": {"name": "custom_ns__exec__command", "arguments": '{"cmd":"c"}'}},
        ]
        self.mock_response_generator = lambda req: httpx.Response(
            200,
            content=_chat_sse_stream(tool_calls=tc),
            headers={"Content-Type": "text/event-stream"},
        )

        with patch.dict(converter.CONFIG, {"stream_mode": "compatible"}):
            res = self.client.post("/v1/responses", json=self._sample_multiagent_payload(stream=True))
            self.assertEqual(res.status_code, 200)
            events = _parse_responses_sse_events(res.text)

            # Check output_item.done events
            done_items = [e["item"] for e in events if e.get("type") == "response.output_item.done" and e.get("item", {}).get("type") == "function_call"]
            self.assertEqual(len(done_items), 3)

            # 1. ns1.search
            self.assertEqual(done_items[0]["name"], "search")
            self.assertEqual(done_items[0]["namespace"], "ns1")

            # 2. collaboration__spawn_agent
            self.assertEqual(done_items[1]["name"], "collaboration__spawn_agent")
            self.assertNotIn("namespace", done_items[1])

            # 3. custom_ns.exec__command
            self.assertEqual(done_items[2]["name"], "exec__command")
            self.assertEqual(done_items[2]["namespace"], "custom_ns")

            # Check response.completed terminal
            completed = next(e for e in events if e.get("type") == "response.completed")
            outputs = completed["response"]["output"]
            self.assertEqual(len(outputs), 3)
            self.assertEqual(outputs[0]["name"], "search")
            self.assertEqual(outputs[0]["namespace"], "ns1")
            self.assertEqual(outputs[1]["name"], "collaboration__spawn_agent")
            self.assertNotIn("namespace", outputs[1])
            self.assertEqual(outputs[2]["name"], "exec__command")
            self.assertEqual(outputs[2]["namespace"], "custom_ns")

            # Verify no internal metadata sent upstream
            upstream_req = self.upstream_requests[-1]
            upstream_json = json.loads(upstream_req.content.decode("utf-8"))
            self.assertNotIn("_tool_registry", upstream_json)
            self.assertNotIn("_tool_namespaces", upstream_json)
            self.assertFalse(any(k.startswith("_") for k in upstream_json.keys()))

    def test_realtime_stream_tools_isolation_identity_and_metadata_sanitation(self):
        """Cover realtime streaming mode: verify incremental events and tool identities."""
        tc = [
            {"id": "call_1", "type": "function", "function": {"name": "ns2__search", "arguments": '{"q":"live"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "collaboration__spawn_agent", "arguments": '{"task":"live"}'}},
            {"id": "call_3", "type": "function", "function": {"name": "custom_ns__exec__command", "arguments": '{"cmd":"live"}'}},
        ]
        self.mock_response_generator = lambda req: httpx.Response(
            200,
            content=_chat_sse_stream(tool_calls=tc),
            headers={"Content-Type": "text/event-stream"},
        )

        with patch.dict(converter.CONFIG, {"stream_mode": "realtime"}):
            res = self.client.post("/v1/responses", json=self._sample_multiagent_payload(stream=True))
            self.assertEqual(res.status_code, 200)
            events = _parse_responses_sse_events(res.text)

            # Check output_item.added events
            added_items = [e["item"] for e in events if e.get("type") == "response.output_item.added" and e.get("item", {}).get("type") == "function_call"]
            self.assertEqual(len(added_items), 3)

            # 1. ns2.search
            self.assertEqual(added_items[0]["name"], "search")
            self.assertEqual(added_items[0]["namespace"], "ns2")

            # 2. collaboration__spawn_agent
            self.assertEqual(added_items[1]["name"], "collaboration__spawn_agent")
            self.assertNotIn("namespace", added_items[1])

            # 3. custom_ns.exec__command
            self.assertEqual(added_items[2]["name"], "exec__command")
            self.assertEqual(added_items[2]["namespace"], "custom_ns")

            # Check response.completed terminal
            completed = next(e for e in events if e.get("type") == "response.completed")
            outputs = completed["response"]["output"]
            self.assertEqual(len(outputs), 3)
            self.assertEqual(outputs[0]["name"], "search")
            self.assertEqual(outputs[0]["namespace"], "ns2")
            self.assertEqual(outputs[1]["name"], "collaboration__spawn_agent")
            self.assertNotIn("namespace", outputs[1])
            self.assertEqual(outputs[2]["name"], "exec__command")
            self.assertEqual(outputs[2]["namespace"], "custom_ns")

            # Verify no internal metadata sent upstream
            upstream_req = self.upstream_requests[-1]
            upstream_json = json.loads(upstream_req.content.decode("utf-8"))
            self.assertNotIn("_tool_registry", upstream_json)
            self.assertNotIn("_tool_namespaces", upstream_json)
            self.assertFalse(any(k.startswith("_") for k in upstream_json.keys()))

    def test_historical_tool_calls_and_tool_choice_integration(self):
        """Verify historical function_call and tool_choice with namespace are correctly mapped to upstream."""
        payload = {
            "model": "auto",
            "stream": False,
            "tools": [
                {
                    "type": "namespace",
                    "name": "collaboration",
                    "tools": [{"type": "function", "name": "spawn_agent", "parameters": {"type": "object"}}],
                }
            ],
            "tool_choice": {"type": "function", "name": "spawn_agent", "namespace": "collaboration"},
            "input": [
                {
                    "type": "agent_message",
                    "content": [
                        {"type": "input_text", "text": "Task: "},
                        {"type": "encrypted_content", "encrypted_content": "Run analysis worker"},
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call_hist_1",
                    "name": "spawn_agent",
                    "namespace": "collaboration",
                    "arguments": '{"task_name": "worker_1"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_hist_1",
                    "output": "worker spawned",
                },
            ],
        }

        tc = [{"id": "call_resp_1", "type": "function", "function": {"name": "collaboration__spawn_agent", "arguments": '{"task_name":"worker_2"}'}}]
        self.mock_response_generator = lambda req: httpx.Response(200, content=_chat_sse_stream(tool_calls=tc))
        res = self.client.post("/v1/responses", json=payload)
        self.assertEqual(res.status_code, 200)

        upstream_req = self.upstream_requests[-1]
        upstream_json = json.loads(upstream_req.content.decode("utf-8"))

        # 1. tool_choice mapped
        self.assertEqual(upstream_json.get("tool_choice"), "required")
        # And tools filtered to that single tool by _normalize_tool_choice
        self.assertEqual(upstream_json["tools"][0]["function"]["name"], "collaboration__spawn_agent")

        # 2. Historical tool call in assistant message mapped
        messages = upstream_json["messages"]
        asst = next(m for m in messages if m["role"] == "assistant" and "tool_calls" in m)
        self.assertEqual(asst["tool_calls"][0]["function"]["name"], "collaboration__spawn_agent")

        # 3. Encrypted content extracted into user message
        user = next(m for m in messages if m["role"] == "user")
        self.assertIn("Task: ", user["content"])
        self.assertIn("Run analysis worker", user["content"])

        # 4. Response returned by gateway has namespace and name restored
        out = res.json().get("output", [])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "spawn_agent")
        self.assertEqual(out[0]["namespace"], "collaboration")


    def _boundary_request(self, payload, stream, mode, projection="balanced"):
        self.upstream_requests.clear()

        def respond(request):
            body = json.loads(request.content)
            names = [tool["function"]["name"] for tool in body.get("tools", [])]
            names.extend(call["function"]["name"] for message in body["messages"] for call in message.get("tool_calls", []))
            if any(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None for name in names):
                return httpx.Response(400, json={"error": {"message": "Invalid Chat function name", "type": "invalid_request_error"}})
            calls = None
            if body.get("tool_choice") == "required" and body.get("tools"):
                calls = [{"id": "next_call", "function": {
                    "name": body["tools"][0]["function"]["name"], "arguments": "{}"}}]
            return httpx.Response(200, content=_chat_sse_stream(tool_calls=calls, content=None if calls else "OK"))

        self.mock_response_generator = respond
        body = {"input": [{"role": "user", "content": "Use the tool"}],
                **deepcopy(payload), "model": "auto", "stream": stream}
        with patch.dict(converter.CONFIG, {"stream_mode": mode, "responses_projection_mode": projection,
                                           "keep_tool_metadata": True}):
            response = self.client.post("/v1/responses", json=body)
        sent = json.loads(self.upstream_requests[-1].content) if self.upstream_requests else None
        return response, sent

    def test_schema_cleanup_preserves_names_and_instance_data(self):
        literal = {"encrypted": True, "payload": "fixture",
                   "metadata": {"properties": {"encrypted": {"encrypted": False}}}}
        schema = {
            "type": "object", "encrypted": True,
            "properties": {"encrypted": {"type": "boolean", "encrypted": True},
                           "payload": {"type": "string", "encrypted": False},
                           "metadata": {"type": "object", "additionalProperties": True}},
            "required": ["encrypted", "payload"], "additionalProperties": False,
            "$defs": {"encrypted": {"type": "boolean", "encrypted": True}},
            "allOf": [{"encrypted": True, "properties": {"encrypted": {"$ref": "#/$defs/encrypted"}}}],
            "default": literal, "const": literal, "enum": [literal], "examples": [literal],
        }
        expected = deepcopy(schema)
        del expected["encrypted"]
        del expected["properties"]["encrypted"]["encrypted"]
        del expected["properties"]["payload"]["encrypted"]
        del expected["$defs"]["encrypted"]["encrypted"]
        del expected["allOf"][0]["encrypted"]
        for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
            for projection in ("balanced", "passthrough"):
                with self.subTest(stream=stream, mode=mode, projection=projection):
                    response, sent = self._boundary_request({"tools": [{
                        "type": "function", "name": "save", "parameters": schema}]}, stream, mode, projection)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(sent["tools"][0]["function"]["parameters"], expected)

    def test_schema_cleanup_handles_nested_schema_positions(self):
        nested = {"type": "object", "encrypted": True,
                  "properties": {"encrypted": {"type": "boolean", "encrypted": True}}}
        cleaned = {"type": "object", "properties": {"encrypted": {"type": "boolean"}}}
        schema = {
            "type": "object", "definitions": {"encrypted": nested},
            "patternProperties": {"encrypted": nested}, "dependentSchemas": {"encrypted": nested},
            "dependencies": {"encrypted": ["payload"], "payload": nested},
            "additionalProperties": nested, "propertyNames": nested, "unevaluatedProperties": nested,
            "items": nested, "contains": nested, "additionalItems": nested, "unevaluatedItems": nested,
            "contentSchema": nested, "not": nested, "if": nested, "then": nested, "else": nested,
            "anyOf": [nested], "oneOf": [nested], "prefixItems": [nested],
        }
        response, sent = self._boundary_request({"tools": [{"type": "function", "function": {
            "name": "save", "parameters": schema}}]}, False, "compatible")
        self.assertEqual(response.status_code, 200, response.text)
        actual = sent["tools"][0]["function"]["parameters"]
        self.assertEqual(actual["definitions"]["encrypted"], cleaned)
        self.assertEqual(actual["patternProperties"]["encrypted"], cleaned)
        self.assertEqual(actual["dependentSchemas"]["encrypted"], cleaned)
        self.assertEqual(actual["dependencies"], {"encrypted": ["payload"], "payload": cleaned})
        for key in ("additionalProperties", "propertyNames", "unevaluatedProperties", "items", "contains",
                    "additionalItems", "unevaluatedItems", "contentSchema", "not", "if", "then", "else"):
            self.assertEqual(actual[key], cleaned, key)
        for key in ("anyOf", "oneOf", "prefixItems"):
            self.assertEqual(actual[key], [cleaned], key)
        response, sent = self._boundary_request({"tools": [{"type": "function", "name": "tuple",
            "parameters": {"type": "array", "items": [nested, False]}}]}, False, "compatible")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sent["tools"][0]["function"]["parameters"]["items"], [cleaned, False])

    def test_explicit_choices_require_exact_current_identity(self):
        function = {"type": "function", "name": "read", "parameters": {"type": "object"}}
        namespaced = {"type": "namespace", "name": "vault", "tools": [function]}
        cases = [
            ([{**function, "name": "vault__read"}], {"type": "function", "namespace": "vault", "name": "read"}),
            ([function], {"type": "custom", "name": "read"}),
            ([function], {"name": "read"}),
            ([namespaced], {"type": "function", "name": "read"}),
            ([namespaced], {"type": "function", "namespace": "missing", "name": "read"}),
        ]
        for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
            for tools, choice in cases:
                with self.subTest(stream=stream, mode=mode, choice=choice):
                    response, _ = self._boundary_request({"tools": tools, "tool_choice": choice}, stream, mode)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(self.upstream_requests, [])

    def test_history_collisions_preserve_reasoning_and_agent_messages(self):
        cases = [((None, "vault__read"), ("vault", "read")),
                 (("vault", "read"), (None, "vault__read")),
                 (("vault", "inner__read"), ("vault__inner", "read"))]
        for current, prior in cases:
            namespace, name = current
            tool = {"type": "function", "name": name, "parameters": {"type": "object"}}
            if namespace is not None:
                tool = {"type": "namespace", "name": namespace, "tools": [tool]}
            history = [
                {"role": "user", "content": "Earlier request"},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "prior thought"}]},
                {"type": "function_call", "call_id": "prior_call", "namespace": prior[0],
                 "name": prior[1], "arguments": "{}"},
                {"type": "function_call_output", "call_id": "prior_call", "output": "prior result"},
                {"type": "additional_tools", "tools": [tool]},
                {"type": "agent_message", "content": [{"type": "encrypted_content",
                                                       "encrypted_content": "next task"}]},
            ]
            for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
                for projection in ("balanced", "passthrough"):
                    with self.subTest(current=current, prior=prior, stream=stream, mode=mode, projection=projection):
                        payload = {"input": history, "tool_choice": {"type": "function", "name": name, "namespace": namespace}}
                        response, sent = self._boundary_request(payload, stream, mode, projection)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(len(sent["tools"]), 1)
                        active_name = sent["tools"][0]["function"]["name"]
                        previous = next(message for message in sent["messages"] if message.get("tool_calls"))
                        previous_name = previous["tool_calls"][0]["function"]["name"]
                        self.assertNotEqual(previous_name, active_name)
                        if prior[0] is None:
                            self.assertEqual(previous_name, prior[1])
                        self.assertEqual(previous["reasoning_content"], "prior thought")
                        self.assertEqual(sent["messages"][-1]["content"], "next task")
                        self.assertFalse(any(key.startswith("_") for key in sent))
                        data = (next(event["response"] for event in _parse_responses_sse_events(response.text)
                                     if event["type"] == "response.completed") if stream else response.json())
                        call = next(item for item in data["output"] if item["type"] == "function_call")
                        self.assertEqual((call.get("namespace"), call["name"]), current)

    def test_retired_tools_are_not_exposed_or_selectable(self):
        history = [{"role": "user", "content": "Earlier request"},
                   {"type": "function_call", "call_id": "past", "name": "read", "namespace": "retired", "arguments": "{}"},
                   {"type": "function_call_output", "call_id": "past", "output": "prior result"},
                   {"role": "user", "content": "Continue"}]
        for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
            with self.subTest(stream=stream, mode=mode):
                response, sent = self._boundary_request({"input": history}, stream, mode)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("tools", sent)
                self.assertNotIn("_tool_registry", sent)
                response, _ = self._boundary_request({"input": history, "tool_choice": {
                    "type": "function", "name": "read", "namespace": "retired"}}, stream, mode)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.upstream_requests, [])

    def test_upstream_retired_identity_cannot_become_an_executable_call(self):
        payload = {"model": "auto",
                   "tools": [{"type": "namespace", "name": "probe", "tools": [{"type": "function",
                              "name": "record", "parameters": {"type": "object"}}]}],
                   "tool_choice": {"type": "function", "name": "record", "namespace": "probe"},
                   "input": [{"role": "user", "content": "Earlier request"},
                             {"type": "function_call", "call_id": "past", "name": "probe__record", "arguments": "{}"},
                             {"type": "function_call_output", "call_id": "past", "output": "done"},
                             {"role": "user", "content": "Use the new tool"}]}
        calls = [{"id": "invalid_call", "function": {"name": "probe__record", "arguments": "{}"}}]
        self.mock_response_generator = lambda request: httpx.Response(200, content=_chat_sse_stream(tool_calls=calls))
        for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
            with self.subTest(stream=stream, mode=mode), patch.dict(converter.CONFIG, {
                "stream_mode": mode, "tool_call_max_retry": 0}):
                response = self.client.post("/v1/responses", json={**payload, "stream": stream})
                if not stream or response.status_code != 200:
                    self.assertEqual(response.status_code, 502, response.text)
                    continue
                events = _parse_responses_sse_events(response.text)
                self.assertTrue(any(event.get("type") in ("error", "response.failed") or event.get("error") for event in events))
                self.assertFalse(any(event.get("type") in ("response.completed", "response.function_call_arguments.done") for event in events))
                self.assertFalse(any(event.get("item", {}).get("type") == "function_call" for event in events))


    def test_malformed_namespaces_return_400_without_upstream(self):
        for namespace in ([], {}, True, 7, ""):
            with self.subTest(namespace=namespace):
                response, _ = self._boundary_request({"tools": [{"type": "function", "name": "read"}],
                    "tool_choice": {"type": "function", "name": "read", "namespace": namespace}}, False, "compatible")
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.upstream_requests, [])



    def test_request_budget_counts_wire_bytes_without_tool_registry(self):
        payloads = [self._sample_multiagent_payload(), {
            "input": [{"role": "user", "content": "Previous task"},
                      {"type": "function_call", "call_id": "old", "namespace": "retired",
                       "name": "lookup", "arguments": "{}"},
                      {"type": "function_call_output", "call_id": "old", "output": "done"},
                      {"role": "user", "content": "汉字" * 800}]}]
        payloads[0]["input"] = [{"role": "user", "content": "汉字" * 800}]
        payloads[0]["tool_choice"] = {"type": "function", "namespace": "ns2", "name": "search"}
        for index, payload in enumerate(payloads):
            for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
                with self.subTest(payload=index, stream=stream, mode=mode):
                    response, _ = self._boundary_request(payload, stream, mode)
                    self.assertEqual(response.status_code, 200, response.text)
                    wire_size = len(self.upstream_requests[-1].content)
                    with patch.dict(converter.CONFIG, {"max_request_bytes": wire_size}):
                        response, sent = self._boundary_request(payload, stream, mode)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(len(self.upstream_requests[-1].content), wire_size)
                    self.assertNotIn("_tool_registry", sent)
                    data = (next(event["response"] for event in _parse_responses_sse_events(response.text)
                                 if event["type"] == "response.completed") if stream else response.json())
                    if index == 0:
                        call = next(item for item in data["output"] if item["type"] == "function_call")
                        self.assertEqual((call.get("namespace"), call["name"]), ("ns2", "search"))
                    with patch.dict(converter.CONFIG, {"max_request_bytes": wire_size - 1}):
                        response, _ = self._boundary_request(payload, stream, mode)
                    self.assertEqual(response.status_code, 413, response.text)
                    self.assertEqual(self.upstream_requests, [])



    def test_request_budget_rechecks_routed_model_without_registry(self):
        payload = self._sample_multiagent_payload()
        payload["input"] = [{"role": "user", "content": "汉字" * 800}]
        for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
            with self.subTest(stream=stream, mode=mode), patch.object(converter, "_upstream_model",
                return_value="routed-" + "model" * 50):
                response, _ = self._boundary_request(payload, stream, mode)
                self.assertEqual(response.status_code, 200, response.text)
                size = len(self.upstream_requests[-1].content)
                with patch.dict(converter.CONFIG, {"max_request_bytes": size}):
                    response, sent = self._boundary_request(payload, stream, mode)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(self.upstream_requests[-1].content), size)
                self.assertNotIn("_tool_registry", sent)
                with patch.dict(converter.CONFIG, {"max_request_bytes": size - 1}):
                    response, _ = self._boundary_request(payload, stream, mode)
                self.assertEqual(response.status_code, 413, response.text)
                self.assertEqual(self.upstream_requests, [])



    def test_nested_and_long_namespace_aliases_round_trip(self):
        cases = [(["parent", "child"], "tool"), (["space group", "中文"], "run-command"),
                 (["n" * 40], "f" * 40), (["n" * 31], "f" * 31)]
        for parts, name in cases:
            namespace = ".".join(parts)
            tools = [{"type": "function", "name": name, "parameters": {"type": "object"}}]
            for part in reversed(parts):
                tools = [{"type": "namespace", "name": part, "tools": tools}]
            payload = {"tool_choice": {"type": "function", "namespace": namespace, "name": name},
                       "input": [{"role": "user", "content": "Previous task"},
                                 {"type": "function_call", "namespace": namespace, "name": name,
                                  "call_id": "past", "arguments": "{}"},
                                 {"type": "function_call_output", "call_id": "past", "output": "done"},
                                 {"type": "additional_tools", "tools": tools},
                                 {"role": "user", "content": "Call the tool again"}]}
            original = deepcopy(payload)
            for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
                with self.subTest(namespace=namespace, name=name, stream=stream, mode=mode):
                    request = deepcopy(payload)
                    for turn in range(2):
                        response, sent = self._boundary_request(request, stream, mode)
                        self.assertEqual(response.status_code, 200, response.text)
                        alias = sent["tools"][0]["function"]["name"]
                        self.assertRegex(alias, r"\A[A-Za-z0-9_-]{1,64}\Z")
                        historical = [call for message in sent["messages"] for call in message.get("tool_calls", [])]
                        self.assertEqual(len(historical), turn + 1)
                        self.assertTrue(all(call["function"]["name"] == alias for call in historical))
                        self.assertEqual(sent["tool_choice"], "required")
                        self.assertNotIn("_tool_registry", sent)
                        data = (next(event["response"] for event in _parse_responses_sse_events(response.text)
                                     if event["type"] == "response.completed") if stream else response.json())
                        calls = [item for item in data["output"] if item["type"] == "function_call"]
                        self.assertEqual(len(calls), 1)
                        self.assertEqual((calls[0].get("namespace"), calls[0]["name"]), (namespace, name))
                        request["input"].extend([*data["output"], {"type": "function_call_output",
                            "call_id": calls[0]["call_id"], "output": "done"}, {"role": "user", "content": "Again"}])
            self.assertEqual(payload, original)

    def test_alias_collisions_remain_bounded_and_preserve_global_names(self):
        from app.adapters.responses_adapter import ToolRegistry
        for namespace, name in (("n" * 31, "f" * 31), ("parent.child" * 8, "function" * 8)):
            base = ToolRegistry().register(namespace, name)
            reserved = [base, *(base[:64 - len(f"_{index}")] + f"_{index}" for index in range(1, 13))]
            for order in (reserved, list(reversed(reserved))):
                with self.subTest(namespace=namespace, order=order):
                    registry = ToolRegistry()
                    for global_name in order:
                        self.assertEqual(registry.register(None, global_name), global_name)
                    alias = registry.register(namespace, name)
                    self.assertRegex(alias, r"\A[A-Za-z0-9_-]{1,64}\Z")
                    self.assertNotIn(alias, reserved)
                    self.assertEqual(registry.get_identity(alias), (namespace, name))
                    self.assertEqual(registry.register(namespace, name), alias)
                    restored = ToolRegistry.from_dict(registry.to_dict())
                    self.assertEqual(restored.get_upstream_name(namespace, name), alias)
                    for global_name in reserved:
                        self.assertEqual(restored.get_identity(global_name), (None, global_name))

    def test_encoded_aliases_distinguish_normalized_and_truncated_identities(self):
        from app.adapters.responses_adapter import ToolRegistry
        identities = [("parent.child", "tool"), ("parent_child", "tool"), ("parent/child", "tool"),
                      ("n" * 70 + "first", "tool"), ("n" * 70 + "second", "tool"),
                      ("n" * 70, "__tool"), ("n" * 70 + "__", "tool")]
        registry = ToolRegistry()
        aliases = [registry.register(*identity) for identity in identities]
        self.assertEqual(len(set(aliases)), len(identities))
        for identity, alias in zip(identities, aliases):
            self.assertRegex(alias, r"\A[A-Za-z0-9_-]{1,64}\Z")
            self.assertEqual(ToolRegistry().register(*identity), alias)
            self.assertEqual(registry.get_identity(alias), identity)


    def test_bounded_aliases_do_not_reuse_retired_global_names(self):
        from app.adapters.responses_adapter import ToolRegistry
        for namespace, name in (("n" * 31, "f" * 31), ("parent.child" * 8, "tool")):
            global_name = ToolRegistry().register(namespace, name)
            payload = {"tools": [{"type": "namespace", "name": namespace, "tools": [{"type": "function", "name": name}]}],
                       "tool_choice": {"type": "function", "namespace": namespace, "name": name},
                       "input": [{"role": "user", "content": "Previous task"},
                                 {"type": "function_call", "name": global_name, "call_id": "past", "arguments": "{}"},
                                 {"type": "function_call_output", "call_id": "past", "output": "done"},
                                 {"role": "user", "content": "Use the namespaced tool"}]}
            for stream, mode in ((False, "compatible"), (True, "compatible"), (True, "realtime")):
                with self.subTest(namespace=namespace, stream=stream, mode=mode):
                    response, sent = self._boundary_request(payload, stream, mode)
                    self.assertEqual(response.status_code, 200, response.text)
                    alias = sent["tools"][0]["function"]["name"]
                    self.assertRegex(alias, r"\A[A-Za-z0-9_-]{1,64}\Z")
                    self.assertNotEqual(alias, global_name)
                    history = next(message["tool_calls"] for message in sent["messages"] if message.get("tool_calls"))
                    self.assertEqual(history[0]["function"]["name"], global_name)
                    data = (next(event["response"] for event in _parse_responses_sse_events(response.text)
                                 if event["type"] == "response.completed") if stream else response.json())
                    call = next(item for item in data["output"] if item["type"] == "function_call")
                    self.assertEqual((call.get("namespace"), call["name"]), (namespace, name))


if __name__ == "__main__":
    unittest.main()
