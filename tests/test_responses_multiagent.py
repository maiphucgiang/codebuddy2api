#!/usr/bin/env python3
"""Interface-level regression tests for Responses multi-agent tools and namespace identity mapping."""

import json
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


if __name__ == "__main__":
    unittest.main()
