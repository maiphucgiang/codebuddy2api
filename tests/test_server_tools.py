#!/usr/bin/env python3
"""Test gateway-executed web tools: spec splitting, the chat loop and protocol blocks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import json
import unittest

import httpx

from app import server_tools
from app.adapters.anthropic_adapter import AnthropicStreamConverter
from app.adapters.responses_adapter import ResponsesStreamConverter
from tests import test_region_routing as fixtures

QUERY = "latest rust release"
URL = "https://example.com/guide"
SEARCH_RESULT = {"title": "Example", "url": URL, "snippet": "Snippet.", "site": "example.com"}


def sse(chunks):
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode()


def tool_call_result(name, arguments):
    """One merged Chat completion whose only choice issues a single tool call."""
    return {"choices": [{"index": 0, "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments)}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}


def tool_call_round(name, arguments):
    return sse([
        {"id": "round", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}]},
            "finish_reason": None}]},
        {"id": "round", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
    ])


def final_round(text="ok"):
    return sse([
        {"id": "round", "choices": [{"index": 0,
         "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
        {"id": "round", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
    ])


class SplitTests(unittest.TestCase):
    def test_anthropic_types_are_extracted(self):
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3,
                  "allowed_domains": ["example.com"]},
                 {"type": "web_fetch_20250910", "name": "web_fetch", "max_content_tokens": 1000},
                 {"name": "client_tool", "input_schema": {"type": "object"}}]
        plain, spec = server_tools.split_server_tools(tools)
        self.assertEqual([t["name"] for t in plain], ["client_tool"])
        self.assertEqual(spec["WebSearch"]["max_uses"], 3)
        self.assertEqual(spec["WebSearch"]["allowed_domains"], ["example.com"])
        self.assertEqual(spec["WebSearch"]["max_results"], 5)
        self.assertEqual(spec["WebFetch"]["max_length"], 4000)

    def test_openai_types_and_filters(self):
        tools = [{"type": "web_search", "search_context_size": "high",
                  "filters": {"allowed_domains": ["a.com"], "blocked_domains": ["b.com"]}},
                 {"type": "web_search_preview"},
                 {"type": "function", "function": {"name": "f"}}]
        plain, spec = server_tools.split_server_tools(tools, openai_types=True)
        self.assertEqual([t["type"] for t in plain], ["function"])
        self.assertEqual(spec["WebSearch"]["max_results"], 8)
        self.assertEqual(spec["WebSearch"]["allowed_domains"], ["a.com"])
        # allowed wins when both are present, mirroring the mutual exclusion rule.
        self.assertEqual(spec["WebSearch"]["blocked_domains"], [])

    def test_options_config(self):
        config = server_tools.search_config_from_options({"search_context_size": "low"})
        self.assertEqual(config["max_results"], 3)

    def test_injected_tools_skip_client_owned_names(self):
        spec = {"WebSearch": {}, "WebFetch": {}}
        injected = server_tools.injected_chat_tools(spec, {"WebSearch"})
        self.assertEqual([t["function"]["name"] for t in injected], ["WebFetch"])


class LoopTests(unittest.TestCase):
    def run_loop(self, rounds, spec=None):
        body = {"messages": [{"role": "user", "content": "q"}], "tools": []}
        state = {"rounds": list(rounds)}

        async def fetch_chat(tool_body):
            return state["rounds"].pop(0)

        async def search_fn(query, count, allowed, freshness):
            self.queries.append((query, count, allowed, freshness))
            return [dict(SEARCH_RESULT)]

        async def fetch_fn(url, prompt, max_length):
            self.fetches.append((url, prompt, max_length))
            return {"url": url, "title": "Example", "content": "body"}

        self.queries, self.fetches = [], []
        spec = spec if spec is not None else {"WebSearch": server_tools.SEARCH_FUNCTION and
                                              {"max_uses": None, "allowed_domains": [],
                                               "blocked_domains": [], "max_results": 5}}
        result, records = _run(server_tools.run_chat_tool_loop(
            fetch_chat, body, spec, search_fn=search_fn, fetch_fn=fetch_fn))
        return body, result, records

    def test_search_round_then_final(self):
        body, result, records = self.run_loop([
            tool_call_result("WebSearch", {"query": QUERY}),
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ])
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual([record["kind"] for record in records], ["search"])
        self.assertEqual(records[0]["results"][0]["url"], URL)
        roles = [message["role"] for message in body["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        self.assertEqual(body["messages"][2]["tool_call_id"], "call_1")

    def test_blocked_domains_fold_into_query(self):
        _, _, records = self.run_loop([
            tool_call_result("WebSearch", {"query": QUERY, "blocked_domains": ["spam.com"]}),
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ])
        self.assertIn("-site:spam.com", records[0]["input"]["query"])
        self.assertEqual(records[0]["results"], [SEARCH_RESULT])

    def test_max_uses_exceeded(self):
        spec = {"WebSearch": {"max_uses": 1, "allowed_domains": [], "blocked_domains": [],
                              "max_results": 5}}
        _, _, records = self.run_loop([
            tool_call_result("WebSearch", {"query": QUERY}),
            tool_call_result("WebSearch", {"query": QUERY}),
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ], spec=spec)
        self.assertEqual([record.get("error") for record in records], [None, "max_uses_exceeded"])

    def test_fetch_round_uses_prompt(self):
        _, _, records = self.run_loop([
            tool_call_result("WebFetch", {"url": URL, "prompt": "summarize"}),
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ], spec={"WebFetch": {"max_uses": None, "allowed_domains": [], "blocked_domains": [],
                              "max_length": None}})
        self.assertEqual([record["kind"] for record in records], ["fetch"])
        self.assertEqual(self.fetches, [(URL, "summarize", None)])


def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


class SeedTests(unittest.TestCase):
    RECORDS = [
        {"kind": "search", "name": "web_search", "id": "srv_1", "input": {"query": QUERY},
         "results": [dict(SEARCH_RESULT)], "error": None},
        {"kind": "fetch", "name": "web_fetch", "id": "srv_2", "input": {"url": URL},
         "content": {"url": URL, "title": "Example", "content": "body"}, "error": None},
    ]

    def feed(self, converter, seeded=True):
        if seeded:
            getattr(converter, "seed_server_tools",
                    lambda r: None)(self.RECORDS) if hasattr(converter, "seed_server_tools") \
                else converter.seed_web_search_calls(self.RECORDS)
        events = []
        for chunk in json.loads(json.dumps([
                {"id": "x", "choices": [{"index": 0, "delta": {"role": "assistant",
                                                               "content": "ok"},
                                         "finish_reason": None}]},
                {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}])):
            events.append(converter.feed_line("data: " + json.dumps(chunk)))
        events.append(converter.finish())
        return "".join(events)

    def test_anthropic_blocks_lead_the_message(self):
        converter = AnthropicStreamConverter(model="m")
        converter.seed_server_tools(self.RECORDS)
        events = self.feed(converter, seeded=False)
        self.assertIn('"server_tool_use"', events)
        self.assertIn('"web_search_tool_result"', events)
        self.assertIn('"web_fetch_tool_result"', events)
        self.assertIn('"web_result"', events)
        content = converter.get_nonstream_response()["content"]
        self.assertEqual([block["type"] for block in content],
                         ["server_tool_use", "web_search_tool_result",
                          "server_tool_use", "web_fetch_tool_result", "text"])
        self.assertEqual(content[1]["tool_use_id"], "srv_1")
        self.assertEqual(content[1]["content"][0]["url"], URL)
        self.assertEqual(content[3]["content"][0]["content"], "body")

    def test_anthropic_error_record_shape(self):
        converter = AnthropicStreamConverter(model="m")
        converter.seed_server_tools([{"kind": "search", "name": "web_search", "id": "srv_e",
                                      "input": {"query": "q"}, "results": None,
                                      "error": "max_uses_exceeded"}])
        self.feed(converter, seeded=False)
        block = converter.get_nonstream_response()["content"][1]
        self.assertEqual(block["error"]["type"], "max_uses_exceeded")
        self.assertIsNone(block["content"])

    def test_responses_search_call_items_lead(self):
        converter = ResponsesStreamConverter(model="m")
        converter.seed_web_search_calls(self.RECORDS)
        events = self.feed(converter, seeded=False)
        self.assertIn("web_search_call", events)
        self.assertIn("response.output_item.added", events)
        output = converter.get_nonstream_response()["output"]
        self.assertEqual(output[0]["type"], "web_search_call")
        self.assertEqual(output[0]["action"]["queries"], [QUERY])
        self.assertEqual(output[1]["type"], "message")


class ServerToolEndpointTests(fixtures.RegionRoutingTests):
    """Drive the three protocol endpoints with declared server web tools."""

    def setUp(self):
        super().setUp()
        self.allowed_profiles = set(fixtures.PROFILES)
        self.chat_rounds = []
        self.chat_requests = []
        self.tool_requests = []

    def handle_upstream(self, request):
        if request.url.path in ("/agenttool/v1/search", "/agenttool/v1/webfetch"):
            self.tool_requests.append(request)
            if request.url.path == "/agenttool/v1/search":
                return httpx.Response(200, json={"results": [dict(SEARCH_RESULT)],
                                                 "total_results": 1})
            return httpx.Response(200, json={"url": URL, "title": "Example",
                                             "content": "fetched body",
                                             "content_type": "text/markdown",
                                             "truncated": False, "status_code": 200})
        if request.url.path.endswith("/chat/completions"):
            self.chat_requests.append(request)
            # Run the base handler for its identity assertions and request log;
            # scripted rounds only override the body on the success path.
            base_response = super().handle_upstream(request)
            if self.response_status != 200:
                return base_response
            body = self.chat_rounds.pop(0) if self.chat_rounds else fixtures.success_sse()
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "text/event-stream"})
        return super().handle_upstream(request)

    def posted_tools(self):
        return [json.loads(request.content).get("tools") for request in self.chat_requests]

    def test_messages_web_search_roundtrip(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                             "max_uses": 2, "allowed_domains": ["example.com"]}]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        types = [block["type"] for block in data["content"]]
        self.assertEqual(types, ["server_tool_use", "web_search_tool_result", "text"])
        self.assertEqual(data["content"][0]["name"], "web_search")
        self.assertEqual(data["content"][1]["content"][0]["url"], URL)
        self.assertEqual(data["content"][2]["text"], "ok")
        # Upstream saw the injected CLI-shaped tool, never the internal spec key.
        self.assertIn("WebSearch", [t["function"]["name"] for t in self.posted_tools()[0]])
        for tools in self.posted_tools():
            self.assertFalse(any(t["function"]["name"].startswith("_") for t in tools))
        # The agenttool search carries the declared domain filter.
        sent = json.loads(self.tool_requests[0].content)
        self.assertEqual(sent["query"], QUERY)
        self.assertEqual(sent["allowed_domains"], ["example.com"])
        # Round two carries the executed tool result back upstream.
        second = json.loads(self.chat_requests[1].content)
        self.assertEqual(second["messages"][-1]["role"], "tool")
        self.assertIn(URL, second["messages"][-1]["content"])

    def test_messages_web_fetch_stream_roundtrip(self):
        self.chat_rounds = [tool_call_round("WebFetch", {"url": URL, "prompt": "summarize"}),
                            final_round()]
        payload = self.payload(endpoint="messages", stream=True)
        payload["tools"] = [{"type": "web_fetch_20250910", "name": "web_fetch"}]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        text = response.text
        for fragment in ('"type": "server_tool_use"', '"name": "web_fetch"',
                         '"type": "web_fetch_tool_result"', '"type": "web_result"',
                         '"type": "text_delta"', "message_stop"):
            self.assertIn(fragment, text)
        sent = json.loads(self.tool_requests[0].content)
        self.assertEqual(sent["url"], URL)
        self.assertEqual(sent["prompt"], "summarize")

    def test_responses_web_search_tool(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="responses")
        payload["tools"] = [{"type": "web_search"}]
        response = self.client.post("/v1/responses", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["output"][0]["type"], "web_search_call")
        self.assertEqual(data["output"][0]["action"]["queries"], [QUERY])
        self.assertEqual(data["output"][1]["content"][0]["text"], "ok")
        # The hosted tool type is never forwarded upstream.
        for tools in self.posted_tools():
            self.assertNotIn("web_search", [t.get("type") for t in tools])

    def test_chat_web_search_options(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload()
        payload["web_search_options"] = {"search_context_size": "low"}
        response = self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")
        self.assertNotIn("tool_calls", data["choices"][0]["message"])
        self.assertIn("WebSearch", [t["function"]["name"] for t in self.posted_tools()[0]])
        self.assertEqual(json.loads(self.tool_requests[0].content)["max_results"], 3)

    def test_no_declaration_keeps_tools_untouched(self):
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"name": "client_tool", "description": "d",
                             "input_schema": {"type": "object", "properties": {}}}]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        sent = json.loads(self.chat_requests[0].content)
        self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["client_tool"])
        self.assertEqual(self.tool_requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
