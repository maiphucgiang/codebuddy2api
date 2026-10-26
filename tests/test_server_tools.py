#!/usr/bin/env python3
"""Test gateway-executed web tools: spec splitting, the chat loop and protocol blocks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import asyncio
import base64
import copy
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import converter
from app import server_tools
from app.adapters.anthropic_adapter import AnthropicStreamConverter, anthropic_request_to_chat
from app.adapters.responses_adapter import ResponsesStreamConverter, responses_request_to_chat
from app.audit_store import AuditStore
from app.observability import AuditMiddleware
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


def double_call_round(server_name, server_args, client_name, client_args):
    return sse([
        {"id": "round", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": server_name, "arguments": json.dumps(server_args)}},
            {"index": 1, "id": "call_2", "type": "function",
             "function": {"name": client_name, "arguments": json.dumps(client_args)}}]},
            "finish_reason": None}]},
        {"id": "round", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
    ])


def double_call_result(server_name, server_args, client_name, client_args):
    """One merged completion issuing one gateway call and one client call."""
    return {"choices": [{"index": 0, "message": {
        "role": "assistant", "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": server_name, "arguments": json.dumps(server_args)}},
            {"id": "call_2", "type": "function",
             "function": {"name": client_name, "arguments": json.dumps(client_args)}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}


def sse_events(text):
    """Decode the JSON data payloads of an SSE body, skipping the [DONE] marker."""
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                events.append(json.loads(line[6:]))
    return events


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
        self.assertIsNone(spec["WebSearch"]["blocked_domains"])

    def test_dated_openai_types_are_extracted(self):
        for kind in ("web_search", "web_search_preview", "web_search_preview_2025_03_11",
                     "web_search_2025_08_26"):
            with self.subTest(kind=kind):
                tools = [{"type": kind, "search_context_size": "low"},
                         {"type": "function", "function": {"name": "f"}}]
                plain, spec = server_tools.split_server_tools(tools, openai_types=True)
                self.assertEqual([t["type"] for t in plain], ["function"])
                self.assertEqual(spec["WebSearch"]["max_results"], 3)

    def test_openai_search_type_predicate(self):
        for kind in ("web_search", "web_search_preview", "web_search_preview_2025_03_11",
                     "web_search_2025_08_26"):
            self.assertTrue(server_tools.is_openai_search_type(kind), kind)
        for kind in ("web_search_20250305", "web_search_preview_2025_03", "web_search_2025_08_26_x",
                     "web_searches", "web_fetch_2025_08_26", "function", "", None, 7):
            self.assertFalse(server_tools.is_openai_search_type(kind), kind)

    def test_anthropic_dated_types_keep_working_with_openai_types_enabled(self):
        tools = [{"type": "web_search_20250305", "name": "web_search"},
                 {"type": "web_fetch_20250910", "name": "web_fetch"}]
        plain, spec = server_tools.split_server_tools(tools, openai_types=True)
        self.assertEqual(plain, [])
        self.assertEqual(set(spec), {"WebSearch", "WebFetch"})

    def test_options_config(self):
        config = server_tools.search_config_from_options({"search_context_size": "low"})
        self.assertEqual(config["max_results"], 3)

    def test_injected_tools_skip_client_owned_names(self):
        spec = {"WebSearch": {}, "WebFetch": {}}
        injected = server_tools.injected_chat_tools(spec, {"WebSearch"})
        self.assertEqual([t["function"]["name"] for t in injected], ["WebFetch"])


    def test_client_owned_names_drop_spec_entries(self):
        tools = [{"type": "web_search_20250305", "name": "web_search"},
                 {"name": "WebSearch", "input_schema": {"type": "object"}}]
        plain, spec = server_tools.split_server_tools(tools)
        # The client's own WebSearch wins; no gateway execution for that name.
        self.assertEqual(spec, {})
        merged, active = server_tools.merge_injected(
            [{"type": "function", "function": {"name": "WebSearch"}}], spec)
        self.assertEqual(len(merged), 1)
        self.assertEqual(active, {})

    def test_merge_injected_filters_active_spec(self):
        spec = {"WebSearch": {"max_uses": 2}, "WebFetch": {"max_uses": 1}}
        merged, active = server_tools.merge_injected([], spec)
        self.assertEqual([t["function"]["name"] for t in merged], ["WebSearch", "WebFetch"])
        self.assertEqual(set(active), {"WebSearch", "WebFetch"})
        # A client-owned WebFetch drops out of both injection and execution.
        client_tools = [{"type": "function", "function": {"name": "WebFetch"}}]
        merged, active = server_tools.merge_injected(client_tools, spec)
        self.assertEqual([t["function"]["name"] for t in merged],
                         ["WebFetch", "WebSearch"])
        self.assertEqual(set(active), {"WebSearch"})


class LoopTests(unittest.TestCase):
    def run_loop(self, rounds, spec=None):
        body = {"messages": [{"role": "user", "content": "q"}], "tools": []}
        state = {"rounds": list(rounds)}

        async def fetch_chat(tool_body):
            self.sent_messages.append(copy.deepcopy(tool_body["messages"]))
            return state["rounds"].pop(0)

        async def search_fn(query, count, allowed, freshness):
            self.queries.append((query, count, allowed, freshness))
            return [dict(SEARCH_RESULT)]

        async def fetch_fn(url, prompt, max_length):
            self.fetches.append((url, prompt, max_length))
            return {"url": url, "title": "Example", "content": "body"}

        self.queries, self.fetches, self.sent_messages = [], [], []
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
        # What the continuation carried; the caller's own list never sees the tool turns.
        self.assertEqual([message["role"] for message in self.sent_messages[0]], ["user"])
        continuation = self.sent_messages[1]
        self.assertEqual([message["role"] for message in continuation], ["user", "assistant", "tool"])
        self.assertEqual(continuation[2]["tool_call_id"], "call_1")
        self.assertEqual([message["role"] for message in body["messages"]], ["user"])

    def test_usage_is_summed_over_every_round(self):
        first = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                 "prompt_tokens_details": {"cached_tokens": 4},
                 "completion_tokens_details": {"reasoning_tokens": 1}, "flag": True, "model": "m"}
        second = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25,
                  "prompt_tokens_details": {"cached_tokens": 6}, "credit": 1.5}
        _, result, _ = self.run_loop([
            tool_call_result("WebSearch", {"query": QUERY}) | {"usage": first},
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": second},
        ])
        # Only numeric leaves add up; flags and labels are not usage counters.
        self.assertEqual(result["usage"], {
            "prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37,
            "prompt_tokens_details": {"cached_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 1}, "credit": 1.5})
        self.assertEqual(first["prompt_tokens"], 10)

    def test_usage_is_summed_when_client_tools_end_the_loop(self):
        _, result, _ = self.run_loop([
            double_call_result("WebSearch", {"query": QUERY}, "client_tool", {"x": 1}),
        ])
        self.assertEqual(result["usage"], {"prompt_tokens": 2, "completion_tokens": 1,
                                           "total_tokens": 3})

    def test_result_without_usage_stays_untouched(self):
        _, result, _ = self.run_loop([
            tool_call_result("WebSearch", {"query": QUERY}) | {"usage": None},
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ])
        self.assertNotIn("usage", result)

    def test_search_tool_message_carries_snippets(self):
        self.run_loop([
            tool_call_result("WebSearch", {"query": QUERY}),
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ])
        self.assertEqual(self.sent_messages[1][2]["content"],
                         f"1. [Example]({URL})\n\n[1] Snippet.")

    def test_search_tool_message_empty_and_error_texts(self):
        self.assertEqual(server_tools._search_tool_text({"results": [], "input": {"query": "q"}}),
                         "No search results found.")
        self.assertEqual(server_tools._search_tool_text({"error": "boom"}),
                         "web search failed: boom")
        self.assertEqual(server_tools._search_tool_text(
            {"results": [{"url": URL, "title": None}]}), f"1. [{URL}]({URL})")

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


    def test_mixed_calls_execute_server_and_surface_client(self):
        _, result, records = self.run_loop([
            double_call_result("WebSearch", {"query": QUERY}, "client_tool", {"x": 1}),
            {"choices": [{"message": {"content": "never"}, "finish_reason": "stop"}]},
        ])
        # The gateway ran its search, then handed the client call back untouched.
        self.assertEqual([record["kind"] for record in records], ["search"])
        calls = result["choices"][0]["message"]["tool_calls"]
        self.assertEqual([call["id"] for call in calls], ["call_2"])
        self.assertEqual(calls[0]["function"]["name"], "client_tool")
        # No continuation goes out, so the tool turn is only observable as the record's message.
        self.assertEqual(len(self.sent_messages), 1)
        tool_message = server_tools._tool_message(records[0])
        self.assertEqual((tool_message["role"], tool_message["tool_call_id"]), ("tool", "call_1"))
        self.assertNotIn("[gateway]", tool_message["content"])

    def test_client_only_call_returns_immediately(self):
        body, result, records = self.run_loop([
            tool_call_result("client_tool", {"x": 1}),
            {"choices": [{"message": {"content": "never"}, "finish_reason": "stop"}]},
        ])
        self.assertEqual(records, [])
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(len(self.sent_messages), 1)
        self.assertEqual([message["role"] for message in body["messages"]], ["user"])

    def test_declared_domains_bound_model_arguments(self):
        spec = {"WebSearch": {"max_uses": None, "allowed_domains": ["allowed.com"],
                              "blocked_domains": None, "max_results": 5}}
        _, _, records = self.run_loop([
            tool_call_result("WebSearch", {"query": QUERY,
                                           "allowed_domains": ["evil.com"]}),
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ], spec=spec)
        # The declaration replaces the model-given filter on the wire and on results.
        self.assertEqual(self.queries[0][2], ["allowed.com"])
        self.assertEqual(records[0]["results"], [])

    def test_continuation_keeps_reasoning_content(self):
        body = {"messages": [{"role": "user", "content": "q"}], "tools": []}
        state = {"rounds": [
            {"choices": [{"index": 0, "message": {
                "role": "assistant", "content": None, "reasoning_content": "thinking",
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "WebSearch",
                                             "arguments": json.dumps({"query": QUERY})}}]},
                "finish_reason": "tool_calls"}]},
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}]}

        sent = []

        async def fetch_chat(tool_body):
            sent.append(copy.deepcopy(tool_body["messages"]))
            return state["rounds"].pop(0)

        async def search_fn(query, count, allowed, freshness):
            return [dict(SEARCH_RESULT)]

        async def fetch_fn(url, prompt, max_length):
            return {"url": url, "content": "body"}

        _run(server_tools.run_chat_tool_loop(
            fetch_chat, body, {"WebSearch": {"max_uses": None, "allowed_domains": None,
                                             "blocked_domains": None, "max_results": 5}},
            search_fn=search_fn, fetch_fn=fetch_fn))
        self.assertEqual(sent[1][1]["reasoning_content"], "thinking")

    def test_tool_turns_never_reach_the_callers_message_list(self):
        original = [{"role": "user", "content": "q"}]
        body = {"messages": original, "tools": []}
        sent = []
        rounds = [tool_call_result("WebSearch", {"query": QUERY})]

        async def fetch_chat(tool_body):
            sent.append(tool_body["messages"])
            if not rounds:
                # A failover-safe failure on the continuation round.
                raise RuntimeError("continuation failed")
            return rounds.pop(0)

        async def search_fn(query, count, allowed, freshness):
            return [dict(SEARCH_RESULT)]

        async def fetch_fn(url, prompt, max_length):
            return {"url": url, "content": "body"}

        with self.assertRaises(RuntimeError):
            _run(server_tools.run_chat_tool_loop(
                fetch_chat, body, self.SEARCH_SPEC, search_fn=search_fn, fetch_fn=fetch_fn))
        # The continuation worked on its own list; the original list was restored untouched.
        self.assertEqual(len(sent), 2)
        self.assertIsNot(sent[1], original)
        self.assertEqual([message["role"] for message in sent[1]], ["user", "assistant", "tool"])
        self.assertIs(body["messages"], original)
        self.assertEqual(original, [{"role": "user", "content": "q"}])

    def test_tool_choice_and_tools_restored_after_loop(self):
        body = {"messages": [{"role": "user", "content": "q"}],
                "tools": [{"type": "function", "function": {"name": "WebSearch"}}],
                "tool_choice": "required"}
        state = {"rounds": [tool_call_result("WebSearch", {"query": QUERY}),
                            {"choices": [{"message": {"content": "ok"},
                                          "finish_reason": "stop"}]}]}
        seen = []

        async def fetch_chat(tool_body):
            seen.append(tool_body.get("tool_choice"))
            return state["rounds"].pop(0)

        async def search_fn(query, count, allowed, freshness):
            return [dict(SEARCH_RESULT)]

        async def fetch_fn(url, prompt, max_length):
            return {"url": url, "content": "body"}

        _run(server_tools.run_chat_tool_loop(
            fetch_chat, body, {"WebSearch": {"max_uses": None, "allowed_domains": None,
                                             "blocked_domains": None, "max_results": 5}},
            search_fn=search_fn, fetch_fn=fetch_fn))
        self.assertEqual(seen, ["required", "auto"])
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual([t["function"]["name"] for t in body["tools"]], ["WebSearch"])

    SEARCH_SPEC = {"WebSearch": {"max_uses": None, "allowed_domains": None,
                                 "blocked_domains": None, "max_results": 5}}
    CLIENT_TOOL = {"type": "function", "function": {"name": "client_tool"}}
    ANSWER = {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]}

    def run_traced(self, rounds, body, max_rounds=server_tools.MAX_ROUNDS):
        """Run the loop recording the tool keys each upstream request carried."""
        state = {"rounds": list(rounds)}
        self.sent = []

        async def fetch_chat(tool_body):
            self.sent.append(copy.deepcopy(
                {key: tool_body[key] for key in ("tools", "tool_choice") if key in tool_body}))
            return state["rounds"].pop(0)

        async def search_fn(query, count, allowed, freshness):
            return [dict(SEARCH_RESULT)]

        async def fetch_fn(url, prompt, max_length):
            return {"url": url, "content": "body"}

        result, records = _run(server_tools.run_chat_tool_loop(
            fetch_chat, body, self.SEARCH_SPEC, search_fn=search_fn, fetch_fn=fetch_fn,
            max_rounds=max_rounds))
        return result, records, state["rounds"]

    def traced_body(self, client_tool=False, tool_choice="required"):
        tools = [server_tools.SEARCH_FUNCTION] + ([self.CLIENT_TOOL] if client_tool else [])
        return {"messages": [{"role": "user", "content": "q"}], "tools": tools,
                "tool_choice": tool_choice}

    def test_answer_after_last_round_is_returned_without_extra_request(self):
        rounds = server_tools.MAX_ROUNDS
        searches = [tool_call_result("WebSearch", {"query": QUERY})] * rounds
        result, records, unused = self.run_traced(searches + [self.ANSWER], self.traced_body())
        self.assertEqual(result["choices"][0]["message"]["content"], "answer")
        self.assertEqual(len(records), rounds)
        self.assertEqual(len(self.sent), rounds + 1)
        self.assertEqual(unused, [])

    def test_early_answer_sends_no_extra_request(self):
        result, records, unused = self.run_traced(
            [tool_call_result("WebSearch", {"query": QUERY}), self.ANSWER,
             tool_call_result("WebSearch", {"query": QUERY})], self.traced_body())
        self.assertEqual(result["choices"][0]["message"]["content"], "answer")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(unused), 1)

    def test_exhausted_budget_strips_injected_tools_from_last_request(self):
        keeps_calling = tool_call_result("WebSearch", {"query": QUERY})
        result, records, _ = self.run_traced([keeps_calling] * 3, self.traced_body(), max_rounds=2)
        # Two executions, then one closing request whose result is returned untouched.
        self.assertEqual(len(records), 2)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        for sent in self.sent[:2]:
            self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["WebSearch"])
        # Without client tools the closing request carries neither key.
        self.assertEqual(self.sent[2], {})

    def test_exhausted_budget_keeps_client_tools(self):
        keeps_calling = tool_call_result("WebSearch", {"query": QUERY})
        self.run_traced([keeps_calling] * 3, self.traced_body(client_tool=True), max_rounds=2)
        self.assertEqual(self.sent[2], {"tools": [self.CLIENT_TOOL], "tool_choice": "auto"})
        self.assertEqual(len(self.sent[1]["tools"]), 2)

    def test_tools_and_choice_restored_after_exhausted_budget(self):
        body = self.traced_body(client_tool=True)
        original = copy.deepcopy(body)
        self.run_traced([tool_call_result("WebSearch", {"query": QUERY})] * 3, body, max_rounds=2)
        self.assertEqual(body["tools"], original["tools"])
        self.assertEqual(body["tool_choice"], "required")
        # A body that never carried the keys does not gain them back.
        bare = {"messages": [{"role": "user", "content": "q"}]}
        self.run_traced([tool_call_result("WebSearch", {"query": QUERY})] * 3, bare, max_rounds=2)
        self.assertNotIn("tools", bare)
        self.assertNotIn("tool_choice", bare)


def _run(coro):
    return asyncio.run(coro)


class DomainPolicyTests(unittest.TestCase):
    def test_userinfo_is_not_the_host(self):
        self.assertFalse(server_tools._domain_allowed(
            "https://docs.example.com:x@evil.com/p", ["example.com"], []))
        self.assertTrue(server_tools._domain_allowed(
            "https://evil.com@docs.example.com/p", ["example.com"], []))

    def test_trailing_dot_cannot_bypass_blocklist(self):
        self.assertFalse(server_tools._domain_allowed("https://evil.com./", [], ["evil.com"]))
        self.assertFalse(server_tools._domain_allowed("https://sub.evil.com.:443/", [], ["evil.com"]))

    def test_declared_domains_are_case_insensitive(self):
        self.assertEqual(server_tools._domain_list(["Evil.com", ".Foo.COM.", " ", "."]),
                         ["evil.com", "foo.com"])
        blocked = server_tools._domain_list(["Evil.com"])
        self.assertFalse(server_tools._domain_allowed("https://EVIL.com/", [], blocked))

    def test_port_is_ignored_and_unparseable_hosts_fail_closed(self):
        self.assertTrue(server_tools._domain_allowed(
            "https://docs.example.com:8443/x", ["example.com"], []))
        for url in ("https://[bad/", "/relative/path", "", "mailto:a@example.com"):
            with self.subTest(url=url):
                self.assertFalse(server_tools._domain_allowed(url, ["example.com"], []))
                self.assertFalse(server_tools._domain_allowed(url, [], ["evil.com"]))

    def test_parser_differential_hosts_use_the_fetcher_view(self):
        # A WHATWG parser connects to evil.com for these, not to the example.com suffix.
        for url in ("https://evil.com\\@docs.example.com/", "https://%65vil.com\\.example.com/",
                    "https://%65%76%69%6c.com/"):
            with self.subTest(url=url):
                self.assertFalse(server_tools._domain_allowed(url, ["example.com"], []))
        self.assertFalse(server_tools._domain_allowed(
            "https://%65%76%69%6c.com/", [], ["evil.com"]))
        self.assertTrue(server_tools._domain_allowed("https://docs.%65xample.com/", ["example.com"], []))

    def test_unicode_domains_match_in_punycode_form(self):
        allowed = server_tools._domain_list(["例子.中国"])
        self.assertEqual(allowed, ["xn--fsqu00a.xn--fiqs8s"])
        for url in ("http://例子.中国/x", "http://sub.例子。中国/x", "http://xn--fsqu00a.xn--fiqs8s/x"):
            with self.subTest(url=url):
                self.assertTrue(server_tools._domain_allowed(url, allowed, []))
                self.assertFalse(server_tools._domain_allowed(url, [], allowed))
        self.assertFalse(server_tools._domain_allowed("http://other.中国/", allowed, []))
        # An entry IDNA cannot encode is kept as the lowercased text.
        self.assertEqual(server_tools._domain_list(["A..B"]), ["a..b"])

    def test_search_results_are_filtered_with_the_hardened_host(self):
        urls = ["https://docs.example.com:x@evil.com/p", "https://evil.com./",
                "https://EVIL.com/page", "https://docs.example.com:8443/x"]

        async def search_fn(query, count, allowed, freshness):
            return [{"title": "t", "url": url} for url in urls]

        async def fetch_fn(url, prompt, max_length):
            raise AssertionError("fetch is not part of a search")

        allowed = server_tools._search_config({"allowed_domains": ["Example.com"]})
        blocked = server_tools._search_config({"blocked_domains": ["Evil.com"]})
        for config, expected in ((allowed, urls[3:]), (blocked, urls[3:])):
            record = _run(server_tools.execute_call("WebSearch", {"query": QUERY}, config,
                                                    search_fn=search_fn, fetch_fn=fetch_fn))
            self.assertEqual([item["url"] for item in record["results"]], expected)


class FetchTargetTests(unittest.TestCase):
    CONFIG = {"max_uses": None, "allowed_domains": None, "blocked_domains": None,
              "max_length": None}

    def test_internal_urls_never_reach_the_fetch_function(self):
        fetched = []

        async def fetch_fn(url, prompt, max_length):
            fetched.append(url)
            return {"url": url, "content": "body"}

        async def search_fn(query, count, allowed, freshness):
            return []

        for url in ("http://localhost:8787/", "http://127.1/", "http://[::ffff:127.0.0.1]/",
                    "http://169.254.169.254/latest/meta-data", "http://router.local/"):
            with self.subTest(url=url):
                record = _run(server_tools.execute_call(
                    "WebFetch", {"url": url}, self.CONFIG, search_fn=search_fn, fetch_fn=fetch_fn))
                self.assertEqual(record["error_code"], "invalid_tool_input")
                self.assertIn("url", record["error"])
        self.assertEqual(fetched, [])


class SeedTests(unittest.TestCase):
    RECORDS = [
        {"kind": "search", "name": "web_search", "id": "srv_1", "public_id": "srvtoolu_1",
         "input": {"query": QUERY}, "results": [dict(SEARCH_RESULT)], "error": None},
        {"kind": "fetch", "name": "web_fetch", "id": "srv_2", "public_id": "srvtoolu_2",
         "input": {"url": URL}, "error": None,
         "content": {"url": URL, "title": "Example", "content": "body"}},
    ]

    def feed(self, converter):
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
        events = self.feed(converter)
        self.assertIn('"server_tool_use"', events)
        self.assertIn('"web_search_tool_result"', events)
        self.assertIn('"web_fetch_tool_result"', events)
        self.assertIn('"web_fetch_result"', events)
        content = converter.get_nonstream_response()["content"]
        self.assertEqual([block["type"] for block in content],
                         ["server_tool_use", "web_search_tool_result",
                          "server_tool_use", "web_fetch_tool_result", "text"])
        self.assertEqual(content[1]["tool_use_id"], "srvtoolu_1")
        self.assertEqual(content[1]["content"][0]["url"], URL)
        self.assertEqual(content[3]["content"]["content"]["source"]["data"], "body")

    def test_anthropic_error_record_shape(self):
        converter = AnthropicStreamConverter(model="m")
        converter.seed_server_tools([{"kind": "search", "name": "web_search", "id": "srv_e",
                                      "public_id": "srvtoolu_e", "input": {"query": "q"},
                                      "results": None, "error": "max_uses_exceeded",
                                      "error_code": "max_uses_exceeded"}])
        self.feed(converter)
        block = converter.get_nonstream_response()["content"][1]
        self.assertEqual(block["content"],
                         {"type": "web_search_tool_result_error",
                          "error_code": "max_uses_exceeded"})
        self.assertEqual(block["tool_use_id"], "srvtoolu_e")

    def test_responses_search_call_items_lead(self):
        converter = ResponsesStreamConverter(model="m")
        converter.seed_web_search_calls(self.RECORDS)
        events = self.feed(converter)
        self.assertIn("web_search_call", events)
        self.assertIn("response.output_item.added", events)
        output = converter.get_nonstream_response()["output"]
        self.assertEqual(output[0]["type"], "web_search_call")
        self.assertEqual(output[0]["action"]["queries"], [QUERY])
        self.assertEqual(output[1]["type"], "message")
        self.assertEqual(output[0]["status"], "completed")
        self.assertIn('"status": "completed"', events)
        self.assertIn('"status": "in_progress"', events)

    def feed_reasoning_content_and_tool(self, converter):
        chunks = [
            {"id": "x", "choices": [{"index": 0, "delta": {"role": "assistant",
                                                           "reasoning_content": "thinking"},
                                     "finish_reason": None}]},
            {"id": "x", "choices": [{"index": 0, "delta": {"content": "ok"},
                                     "finish_reason": None}]},
            {"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_9", "type": "function",
                 "function": {"name": "client_tool", "arguments": "{}"}}]},
                "finish_reason": None}]},
            {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ]
        events = [converter.feed_line("data: " + json.dumps(chunk)) for chunk in chunks]
        events.append(converter.finish())
        return "".join(events)

    def test_responses_output_indices_follow_seeded_items(self):
        for searches in (1, 2):
            for realtime in (False, True):
                with self.subTest(searches=searches, realtime=realtime):
                    converter = ResponsesStreamConverter(
                        model="m", realtime=realtime, declared_names=["client_tool"])
                    converter.seed_web_search_calls([
                        dict(self.RECORDS[0], id=f"srv_{n}") for n in range(searches)])
                    events = sse_events(self.feed_reasoning_content_and_tool(converter))
                    output = [event for event in events
                              if event["type"] == "response.completed"][0]["response"]["output"]
                    self.assertEqual([item["type"] for item in output],
                                     ["web_search_call"] * searches
                                     + ["reasoning", "message", "function_call"])
                    positions = {item["id"]: index for index, item in enumerate(output)}
                    checked = 0
                    for event in events:
                        if "output_index" not in event:
                            continue
                        item_id = event.get("item_id") or event["item"]["id"]
                        self.assertEqual(event["output_index"], positions[item_id], event["type"])
                        checked += 1
                    self.assertGreaterEqual(checked, 3 * len(output))

    def test_anthropic_block_indices_follow_seeded_blocks(self):
        converter = AnthropicStreamConverter(model="m")
        converter.seed_server_tools(self.RECORDS)
        events = sse_events(self.feed_reasoning_content_and_tool(converter))
        content = converter.get_nonstream_response()["content"]
        self.assertEqual([block["type"] for block in content],
                         ["server_tool_use", "web_search_tool_result", "server_tool_use",
                          "web_fetch_tool_result", "thinking", "text", "tool_use"])
        delta_types = {"thinking_delta": "thinking", "text_delta": "text",
                       "input_json_delta": "tool_use"}
        started = {}
        for event in events:
            if event["type"] == "content_block_start":
                started[event["index"]] = event["content_block"]["type"]
            elif event["type"] == "content_block_delta":
                self.assertEqual(content[event["index"]]["type"],
                                 delta_types[event["delta"]["type"]])
        self.assertEqual(started, {index: block["type"] for index, block in enumerate(content)})

    def test_responses_search_call_failed_status(self):
        converter = ResponsesStreamConverter(model="m")
        converter.seed_web_search_calls([
            {"kind": "search", "name": "web_search", "id": "srv_e", "public_id": "srvtoolu_e",
             "input": {"query": "q"}, "results": None, "error": "boom",
             "error_code": "unavailable"}])
        self.feed(converter)
        output = converter.get_nonstream_response()["output"]
        self.assertEqual(output[0]["status"], "failed")

def anthropic_blocks(records):
    """Emit records the way the gateway answers, returning the final content blocks."""
    emitter = AnthropicStreamConverter(model="m")
    emitter.seed_server_tools(records)
    for chunk in ({"id": "x", "choices": [{"index": 0, "delta": {"role": "assistant",
                                                                  "content": "answer"},
                                           "finish_reason": None}]},
                  {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}):
        emitter.feed_line("data: " + json.dumps(chunk))
    emitter.finish()
    return emitter.get_nonstream_response()["content"]


def echoed_assistant(blocks):
    """The assistant Chat message a follow-up turn builds from echoed Anthropic blocks."""
    chat = anthropic_request_to_chat({"model": "m", "max_tokens": 10, "messages": [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": blocks},
        {"role": "user", "content": "follow-up"}]})
    return chat["messages"][1]


class HistoryEchoTests(unittest.TestCase):
    RECORDS = SeedTests.RECORDS

    def test_emitted_search_blocks_carry_snippets_in_gateway_owned_values(self):
        result = anthropic_blocks(self.RECORDS)[1]["content"][0]
        self.assertTrue(result["encrypted_content"].startswith("cb2api:"))
        self.assertNotIn("Snippet.", result["encrypted_content"])
        bare = dict(self.RECORDS[0], results=[{"title": "T", "url": URL}])
        self.assertEqual(anthropic_blocks([bare])[1]["content"][0]["encrypted_content"], "")

    def test_search_and_fetch_round_trip_into_text_context(self):
        message = echoed_assistant(anthropic_blocks(self.RECORDS))
        text = message["content"]
        self.assertNotIn("tool_calls", message)
        for fragment in (QUERY, f"[Example]({URL})", "[1] Snippet.", "body", "answer"):
            self.assertIn(fragment, text)
        self.assertLess(text.index(QUERY), text.index("body"))
        self.assertLess(text.index("body"), text.index("answer"))
        self.assertNotIn("server_tool_use", json.dumps(message))
        self.assertIn(f"Source: {URL}", text)
        self.assertIn("# Example", text)

    def test_block_order_is_preserved_and_renderings_stay_on_their_own_lines(self):
        blocks = anthropic_blocks(self.RECORDS[:1])
        blocks.insert(0, {"type": "text", "text": "Searching."})
        text = echoed_assistant(blocks)["content"]
        self.assertLess(text.index("Searching."), text.index(QUERY))
        self.assertLess(text.index(QUERY), text.index("answer"))
        self.assertIn("Searching.\n", text)
        self.assertIn("\nanswer", text)

    def test_foreign_or_malformed_encrypted_content_is_ignored(self):
        for blob in ("EqkBCkYIBxgCKkDfake-real-anthropic-blob", "cb2api:%%%not-base64",
                     "cb2api:" + base64.urlsafe_b64encode(b"[1, 2]").decode(),
                     "cb2api:" + base64.urlsafe_b64encode(b'{"snippet": 5}').decode(), None, 7):
            with self.subTest(blob=blob):
                blocks = anthropic_blocks(self.RECORDS[:1])
                blocks[1]["content"][0]["encrypted_content"] = blob
                text = echoed_assistant(blocks)["content"]
                self.assertIn(URL, text)
                self.assertNotIn("Snippet.", text)
                if isinstance(blob, str):
                    self.assertNotIn(blob, text)

    def test_error_results_become_failure_text(self):
        failed = [{"kind": "search", "name": "web_search", "id": "s", "public_id": "srvtoolu_s",
                   "input": {"query": QUERY}, "results": None, "error": "boom",
                   "error_code": "unavailable"},
                  {"kind": "fetch", "name": "web_fetch", "id": "f", "public_id": "srvtoolu_f",
                   "input": {"url": URL}, "error": "boom", "error_code": "invalid_tool_input"}]
        text = echoed_assistant(anthropic_blocks(failed))["content"]
        self.assertIn(QUERY, text)
        self.assertIn("web search failed: unavailable", text)
        self.assertIn("web fetch failed: invalid_tool_input", text)

    def test_server_blocks_alone_still_form_an_assistant_message(self):
        message = echoed_assistant(anthropic_blocks(self.RECORDS)[:2])
        self.assertIn(URL, message["content"])

    def test_responses_search_call_round_trip_into_text_context(self):
        emitter = ResponsesStreamConverter(model="m")
        emitter.seed_web_search_calls(self.RECORDS)
        for chunk in ({"id": "x", "choices": [{"index": 0, "delta": {"role": "assistant",
                                                                      "content": "answer"},
                                               "finish_reason": None}]},
                      {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}):
            emitter.feed_line("data: " + json.dumps(chunk))
        emitter.finish()
        output = emitter.get_nonstream_response()["output"]
        self.assertEqual(output[0]["action"]["sources"], [{"type": "url", "url": URL}])
        chat = responses_request_to_chat({"model": "m", "input": [
            {"type": "message", "role": "user", "content": "question"}, *output,
            {"type": "message", "role": "user", "content": "follow-up"}]})
        roles = [message["role"] for message in chat["messages"]]
        # The search context and the answer form one assistant turn.
        self.assertEqual(roles, ["user", "assistant", "user"])
        text = chat["messages"][1]["content"]
        self.assertIn(QUERY, text)
        self.assertIn(URL, text)
        self.assertLess(text.index(URL), text.index("answer"))

    def test_responses_search_call_variants(self):
        def convert(item):
            return responses_request_to_chat({"model": "m", "input": [
                {"type": "message", "role": "user", "content": "question"}, item,
                {"type": "message", "role": "user", "content": "follow-up"}]})["messages"]
        official = {"type": "web_search_call", "status": "completed",
                    "action": {"type": "search", "query": QUERY,
                               "sources": [{"type": "url", "url": URL}, {"type": "api", "name": "x"}]}}
        messages = convert(official)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertIn(QUERY, messages[1]["content"])
        self.assertIn(URL, messages[1]["content"])
        failed = convert({**official, "status": "failed", "action": {"type": "search", "queries": [QUERY]}})
        self.assertIn("web search failed", failed[1]["content"])
        for empty in ({"type": "web_search_call", "status": "completed"},
                      {"type": "web_search_call", "action": {"type": "open_page", "url": URL}}):
            self.assertEqual([m["role"] for m in convert(empty)], ["user", "user"])


class ServerToolEndpointTests(fixtures.RegionRoutingTests):
    """Drive the three protocol endpoints with declared server web tools."""

    def setUp(self):
        super().setUp()
        self.allowed_profiles = set(fixtures.PROFILES)
        self.chat_rounds = []
        self.chat_requests = []
        self.tool_requests = []
        self.fetch_content = "fetched body"
        self.search_status = 200
        self.search_fail_uids = set()
        self.tool_held = []
        self.chat_held = []

    def held(self):
        """Per-account in-flight leases at this moment, by synthetic UID."""
        counts = {uid: self.pool._capacity.count(self.pool._capacity_key(entry))
                  for uid, entry in self.entries.items()}
        return {uid: count for uid, count in counts.items() if count}

    def handle_upstream(self, request):
        if request.url.path in ("/agenttool/v1/search", "/agenttool/v1/webfetch"):
            self.tool_requests.append(request)
            self.tool_held.append(self.held())
            if request.url.path == "/agenttool/v1/search":
                if request.headers.get("x-user-id") in self.search_fail_uids:
                    return httpx.Response(503, json={"code": 503, "msg": "synthetic outage"})
                if self.search_status != 200:
                    return httpx.Response(self.search_status,
                                          json={"code": self.search_status, "msg": "synthetic failure"})
                return httpx.Response(200, json={"results": [dict(SEARCH_RESULT)],
                                                 "total_results": 1})
            return httpx.Response(200, json={"url": URL, "title": "Example",
                                             "content": self.fetch_content,
                                             "content_type": "text/markdown",
                                             "truncated": False, "status_code": 200})
        if request.url.path.endswith("/chat/completions"):
            self.chat_requests.append(request)
            self.chat_held.append(self.held())
            # Run the base handler for its identity assertions and request log;
            # scripted rounds only override the body on the success path.
            base_response = super().handle_upstream(request)
            if self.response_status != 200:
                return base_response
            body = self.chat_rounds.pop(0) if self.chat_rounds else fixtures.success_sse()
            if isinstance(body, int):
                # A scripted round may be an upstream error status instead of a body.
                return httpx.Response(body, json={"error": {"message": "synthetic failure"}})
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
                         '"type": "web_fetch_tool_result"', '"type": "web_fetch_result"',
                         '"type": "document"', '"type": "text_delta"', "message_stop"):
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

    def assert_valid_chat_sse(self, text):
        """Events must be framed by blank lines and the stream must end with [DONE]."""
        self.assertTrue(text.endswith("data: [DONE]\n\n"), text[-80:])
        blocks = text.split("\n\n")
        self.assertEqual(blocks.pop(), "")
        self.assertGreater(len(blocks), 2)
        for block in blocks:
            self.assertTrue(block.startswith("data: ") and "\n" not in block, block)
            if block != "data: [DONE]":
                json.loads(block[6:])
        return sse_events(text)

    def test_chat_stream_server_tool_is_framed_as_sse(self):
        declarations = (("web_search_options", {"web_search_options": {}}),
                        ("hosted tool", {"tools": [{"type": "web_search"}]}))
        for label, extra in declarations:
            with self.subTest(label):
                self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
                payload = {**self.payload(stream=True), **extra}
                response = self.client.post("/v1/chat/completions", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                events = self.assert_valid_chat_sse(response.text)
                text = "".join(choice["delta"].get("content") or ""
                               for event in events for choice in event["choices"])
                self.assertEqual(text, "ok")

    def test_chat_hosted_tool_choice_forces_websearch(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload()
        payload["tools"] = [{"type": "web_search"}]
        payload["tool_choice"] = {"type": "web_search"}
        response = self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(self.tool_requests), 1)
        first = json.loads(self.chat_requests[0].content)
        self.assertEqual(first["tool_choice"], "required")
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["WebSearch"])
        self.assertEqual(json.loads(self.chat_requests[1].content)["tool_choice"], "auto")

    def test_chat_hosted_tool_choice_without_hosted_tool_is_auto(self):
        payload = self.payload()
        payload["tools"] = [{"type": "function", "function": {
            "name": "client_tool", "parameters": {"type": "object", "properties": {}}}}]
        payload["tool_choice"] = {"type": "web_search"}
        response = self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        sent = json.loads(self.chat_requests[0].content)
        self.assertEqual(sent["tool_choice"], "auto")
        self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["client_tool"])
        self.assertEqual(self.tool_requests, [])

    def test_messages_fetch_rejects_internal_url(self):
        self.chat_rounds = [tool_call_round("WebFetch", {"url": "http://169.254.169.254/latest"}),
                            final_round()]
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"type": "web_fetch_20250910", "name": "web_fetch"}]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.tool_requests, [])
        result = response.json()["content"][1]
        self.assertEqual(result["type"], "web_fetch_tool_result")
        self.assertEqual(result["content"]["error_code"], "invalid_tool_input")

    def test_no_declaration_keeps_tools_untouched(self):
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"name": "client_tool", "description": "d",
                             "input_schema": {"type": "object", "properties": {}}}]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        sent = json.loads(self.chat_requests[0].content)
        self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["client_tool"])
        self.assertEqual(self.tool_requests, [])


    def test_messages_mixed_calls_surface_client_tool(self):
        self.chat_rounds = [double_call_round("WebSearch", {"query": QUERY},
                                              "client_tool", {"x": 1}), final_round()]
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"},
                            {"name": "client_tool", "description": "d",
                             "input_schema": {"type": "object", "properties": {}}}]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        # The gateway executed its search, then stopped and surfaced the client call.
        self.assertEqual(len(self.chat_requests), 1)
        types = [block["type"] for block in data["content"]]
        self.assertEqual(types, ["server_tool_use", "web_search_tool_result", "tool_use"])
        self.assertEqual(data["content"][2]["name"], "client_tool")
        self.assertEqual(data["stop_reason"], "tool_use")
        self.assertEqual(len(self.tool_requests), 1)

    def test_messages_tool_choice_any_completes(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        payload["tool_choice"] = {"type": "any"}
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["content"][-1]["type"], "text")
        # The continuation round ran with a relaxed choice.
        second = json.loads(self.chat_requests[1].content)
        self.assertEqual(second["tool_choice"], "auto")

    def test_messages_tool_choice_named_web_search(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        payload["tool_choice"] = {"type": "tool", "name": "web_search"}
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        first = json.loads(self.chat_requests[0].content)
        self.assertEqual(first["tool_choice"], "required")
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["WebSearch"])

    def test_responses_hosted_tool_choice(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="responses")
        payload["tools"] = [{"type": "web_search"}]
        payload["tool_choice"] = {"type": "web_search"}
        response = self.client.post("/v1/responses", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        sent = json.loads(self.chat_requests[0].content)
        self.assertEqual(sent["tool_choice"], "required")

    def post_realtime(self, endpoint, tools, rounds):
        self.chat_rounds = rounds
        payload = self.payload(endpoint=endpoint, stream=True)
        payload["tools"] = tools
        with patch.dict(converter.CONFIG, {"stream_mode": "realtime", "max_collect_bytes": 0}):
            response = self.client.post("/v1/" + endpoint, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("event: error", response.text)
        self.assertNotIn('"error"', response.text)
        return response.text

    CLIENT_FUNCTION = {"name": "client_tool", "description": "d",
                       "input_schema": {"type": "object", "properties": {}}}

    def test_realtime_messages_server_tool_with_client_tool(self):
        text = self.post_realtime("messages", [
            {"type": "web_search_20250305", "name": "web_search"}, self.CLIENT_FUNCTION],
            [double_call_round("WebSearch", {"query": QUERY}, "client_tool", {"x": 1}),
             final_round()])
        events = sse_events(text)
        starts = [event["content_block"] for event in events
                  if event["type"] == "content_block_start"]
        self.assertEqual([block["type"] for block in starts],
                         ["server_tool_use", "web_search_tool_result", "tool_use"])
        self.assertEqual(starts[2]["name"], "client_tool")
        stop = [event for event in events if event["type"] == "message_delta"][0]
        self.assertEqual(stop["delta"]["stop_reason"], "tool_use")
        self.assertEqual(len(self.chat_requests), 1)

    def test_realtime_responses_server_tool_with_client_tool(self):
        text = self.post_realtime("responses", [
            {"type": "web_search"},
            {"type": "function", "name": "client_tool", "parameters": {"type": "object"}}],
            [double_call_round("WebSearch", {"query": QUERY}, "client_tool", {"x": 1}),
             final_round()])
        completed = [event for event in sse_events(text)
                     if event["type"] == "response.completed"][0]["response"]
        self.assertEqual([item["type"] for item in completed["output"]],
                         ["web_search_call", "function_call"])
        self.assertEqual(completed["output"][1]["name"], "client_tool")
        for event in sse_events(text):
            if "output_index" in event:
                item_id = event.get("item_id") or event["item"]["id"]
                self.assertEqual(event["output_index"],
                                 [item["id"] for item in completed["output"]].index(item_id))

    def test_realtime_server_tool_round_then_final_text(self):
        rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        text = self.post_realtime("messages", [{"type": "web_search_20250305", "name": "web_search"}],
                                  rounds)
        self.assertIn('"type": "web_search_tool_result"', text)
        self.assertIn('"text": "ok"', text)
        self.assertIn("message_stop", text)
        text = self.post_realtime("responses", [{"type": "web_search"}],
                                  [tool_call_round("WebSearch", {"query": QUERY}), final_round()])
        completed = [event for event in sse_events(text)
                     if event["type"] == "response.completed"][0]["response"]
        self.assertEqual([item["type"] for item in completed["output"]],
                         ["web_search_call", "message"])
        self.assertEqual(completed["output"][1]["content"][0]["text"], "ok")

    def test_client_owned_websearch_name_stays_client(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY})]
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"},
                            {"name": "WebSearch", "description": "client",
                             "input_schema": {"type": "object", "properties": {}}}]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        # No gateway injection or execution; the client's own tool call passes through.
        sent = json.loads(self.chat_requests[0].content)
        self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["WebSearch"])
        self.assertEqual(self.tool_requests, [])
        self.assertEqual(response.json()["stop_reason"], "tool_use")

    def test_history_echo_blocks_are_accepted(self):
        payload = self.payload(endpoint="messages", text="follow-up question")
        payload["messages"] = [
            {"role": "user", "content": "search please"},
            {"role": "assistant", "content": [
                {"type": "server_tool_use", "id": "srvtoolu_x", "name": "web_search",
                 "input": {"query": "q"}},
                {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_x",
                 "content": [{"type": "web_search_result", "url": URL, "title": "T",
                              "encrypted_content": ""}]},
                {"type": "text", "text": "based on results"}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "other",
                 "content": "client result"},
                {"type": "text", "text": "and now?"}]},
        ]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)


    def test_responses_dated_hosted_search_type(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="responses")
        payload["tools"] = [{"type": "web_search_preview_2025_03_11"}]
        response = self.client.post("/v1/responses", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["output"][0]["type"], "web_search_call")
        self.assertEqual(data["output"][1]["content"][0]["text"], "ok")
        self.assertEqual(len(self.tool_requests), 1)
        # The hosted type is gateway-owned: upstream only sees the injected function.
        for tools in self.posted_tools():
            self.assertEqual([t["type"] for t in tools], ["function"])
            self.assertEqual([t["function"]["name"] for t in tools], ["WebSearch"])

    def test_chat_dated_hosted_search_type(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload()
        payload["tools"] = [{"type": "web_search_2025_08_26"}]
        response = self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(self.tool_requests), 1)
        for tools in self.posted_tools():
            self.assertEqual([t["type"] for t in tools], ["function"])
            self.assertEqual([t["function"]["name"] for t in tools], ["WebSearch"])

    def test_dated_hosted_tool_choice_forces_websearch(self):
        for endpoint, tool in (("chat/completions", {"type": "web_search_2025_08_26"}),
                               ("responses", {"type": "web_search_preview_2025_03_11"})):
            with self.subTest(endpoint=endpoint):
                self.chat_requests.clear()
                self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
                payload = self.payload(endpoint=endpoint)
                payload["tools"] = [tool]
                payload["tool_choice"] = dict(tool)
                response = self.client.post("/v1/" + endpoint, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                first = json.loads(self.chat_requests[0].content)
                self.assertEqual(first["tool_choice"], "required")
                self.assertEqual([t["function"]["name"] for t in first["tools"]], ["WebSearch"])

    def test_chat_dated_hosted_tool_choice_without_hosted_tool_is_auto(self):
        payload = self.payload()
        payload["tools"] = [{"type": "function", "function": {
            "name": "client_tool", "parameters": {"type": "object", "properties": {}}}}]
        payload["tool_choice"] = {"type": "web_search_preview_2025_03_11"}
        response = self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(json.loads(self.chat_requests[0].content)["tool_choice"], "auto")

    def test_search_snippets_reach_the_model(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="messages")
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        second = json.loads(self.chat_requests[1].content)
        self.assertEqual(second["messages"][-1]["content"], f"1. [Example]({URL})\n\n[1] Snippet.")

    def last_upstream_messages(self):
        return json.loads(self.chat_requests[-1].content)["messages"]

    def test_messages_history_echo_carries_search_and_fetch_to_the_next_turn(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round("found it")]
        first = self.payload(endpoint="messages")
        first["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        turn_one = self.client.post("/v1/messages", json=first)
        self.assertEqual(turn_one.status_code, 200, turn_one.text)
        search_blocks = turn_one.json()["content"]
        self.fetch_content = "fetched page text"
        self.chat_rounds = [tool_call_round("WebFetch", {"url": URL}), final_round("read it")]
        second = self.payload(endpoint="messages")
        second["tools"] = [{"type": "web_fetch_20250910", "name": "web_fetch"}]
        turn_two = self.client.post("/v1/messages", json=second)
        self.assertEqual(turn_two.status_code, 200, turn_two.text)
        fetch_blocks = turn_two.json()["content"]
        follow_up = self.payload(endpoint="messages", text="and then?")
        follow_up["messages"] = [{"role": "user", "content": "search then fetch"},
                                 {"role": "assistant", "content": search_blocks + fetch_blocks},
                                 {"role": "user", "content": "and then?"}]
        response = self.client.post("/v1/messages", json=follow_up)
        self.assertEqual(response.status_code, 200, response.text)
        sent = json.loads(self.chat_requests[-1].content)
        # Echoed server blocks reach the model as text, never as tool calls.
        self.assertNotIn("tools", sent)
        messages = [m for m in sent["messages"] if m["role"] != "system"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        assistant = messages[1]
        self.assertNotIn("tool_calls", assistant)
        text = assistant["content"]
        for fragment in (QUERY, URL, "[1] Snippet.", "found it", "fetched page text", "read it"):
            self.assertIn(fragment, text)
        self.assertLess(text.index("[1] Snippet."), text.index("fetched page text"))

    def test_responses_history_echo_carries_the_search_to_the_next_turn(self):
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round("found it")]
        first = self.payload(endpoint="responses")
        first["tools"] = [{"type": "web_search"}]
        turn_one = self.client.post("/v1/responses", json=first)
        self.assertEqual(turn_one.status_code, 200, turn_one.text)
        output = turn_one.json()["output"]
        self.assertEqual(output[0]["action"]["sources"], [{"type": "url", "url": URL}])
        follow_up = self.payload(endpoint="responses")
        follow_up["input"] = [{"role": "user", "content": "search please"}, *output,
                              {"role": "user", "content": "and then?"}]
        response = self.client.post("/v1/responses", json=follow_up)
        self.assertEqual(response.status_code, 200, response.text)
        messages = [m for m in self.last_upstream_messages() if m["role"] != "system"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        for fragment in (QUERY, URL, "found it"):
            self.assertIn(fragment, messages[1]["content"])
        self.assertNotIn("tool_calls", messages[1])

    # -- Audit: one record per request -------------------------------------

    def audited(self, profiles=("cn-cli", "intl-cli"), mode="legacy"):
        """Serve the app through the audit middleware."""
        self.configure(profiles=profiles)
        self.allowed_profiles = set(profiles)
        store = AuditStore(self.root / "tool-audit.sqlite3")
        self.addCleanup(store.close)
        client = self.enterContext(TestClient(
            AuditMiddleware(converter.app, {**converter.CONFIG, "audit_store": store,
                                            "request_context_mode": mode})))
        return store, client

    def only_record(self, store):
        records = store.list_records()["items"]
        self.assertEqual(len(records), 1, records)
        return records[0]

    def test_messages_search_is_one_audit_record_with_one_server_tool_attempt(self):
        store, client = self.audited(mode="scoped")
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="messages", selected_model="cn-cli-only")
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        response = client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        # The search borrows the request's own account instead of leasing another.
        self.assertEqual(self.tool_requests[0].headers["x-user-id"], "cn-cli")
        record = self.only_record(store)
        self.assertEqual((record["public_model"], record["profile"]), ("cn-cli-only", "cn-cli"))
        self.assertEqual(record["credential"], self.entries["cn-cli"]["account_key"])
        self.assertEqual(record["outcome"], "success")
        self.assertIsNone(record["error_code"])
        tools = [a for a in record["attempts"] if a["stage"] == "server_tool"]
        self.assertEqual(len(tools), 1, record["attempts"])
        entry = tools[0]
        self.assertEqual(set(entry), {"stage", "model", "outcome", "profile", "credential", "attempt",
                                      "duration_ms", "status_code", "upstream_request_id"})
        self.assertEqual(entry["upstream_request_id"], self.tool_requests[0].headers["x-request-id"])
        self.assertEqual((entry["model"], entry["outcome"], entry["profile"], entry["attempt"],
                          entry["status_code"]), ("web-search", "success", "cn-cli", 1, 200))
        self.assertEqual(entry["credential"], self.entries["cn-cli"]["account_key"])
        self.assertGreaterEqual(entry["duration_ms"], 0)
        self.assertNotIn("error_code", entry)
        # The search left no low-level attempts of its own: only the two model rounds remain.
        stages = [a["stage"] for a in record["attempts"]]
        self.assertEqual(stages.count("upstream_attempt"), 2, stages)
        self.assertEqual(stages.count("upstream_http"), 2, stages)

    def test_failed_search_is_a_tool_error_not_a_request_failure(self):
        store, client = self.audited()
        self.search_status = 500
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        payload = self.payload(endpoint="messages", selected_model="cn-cli-only")
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        response = client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["content"][1]["content"]["type"],
                         "web_search_tool_result_error")
        record = self.only_record(store)
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["status_code"], 200)
        self.assertIsNone(record["error_code"])
        self.assertEqual((record["public_model"], record["profile"]), ("cn-cli-only", "cn-cli"))
        entry = next(a for a in record["attempts"] if a["stage"] == "server_tool")
        self.assertEqual((entry["model"], entry["outcome"], entry["error_code"], entry["status_code"]),
                         ("web-search", "error", "upstream_500", 500))
        self.assertGreaterEqual(entry["attempt"], 1)
        self.assertEqual(sum(a["stage"] == "server_tool" for a in record["attempts"]), 1)
        self.assertFalse(any(a["stage"] == "upstream_error" for a in record["attempts"]))

    def test_messages_fetch_is_a_web_fetch_server_tool_attempt(self):
        store, client = self.audited()
        self.chat_rounds = [tool_call_round("WebFetch", {"url": URL}), final_round()]
        payload = self.payload(endpoint="messages", selected_model="cn-cli-only")
        payload["tools"] = [{"type": "web_fetch_20250910", "name": "web_fetch"}]
        response = client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        record = self.only_record(store)
        self.assertEqual((record["public_model"], record["profile"], record["outcome"]),
                         ("cn-cli-only", "cn-cli", "success"))
        entry = next(a for a in record["attempts"] if a["stage"] == "server_tool")
        self.assertEqual((entry["model"], entry["outcome"], entry["profile"], entry["attempt"]),
                         ("web-fetch", "success", "cn-cli", 1))

    def test_standalone_search_keeps_its_own_route_fields(self):
        store, client = self.audited()
        response = client.post("/v1/search", json={"query": QUERY})
        self.assertEqual(response.status_code, 200, response.text)
        record = self.only_record(store)
        self.assertEqual((record["protocol"], record["public_model"]), ("search", "web-search"))
        self.assertFalse(any(a["stage"] == "server_tool" for a in record["attempts"]))

    def test_usage_is_summed_across_rounds_in_response_and_audit_record(self):
        store, client = self.audited()
        cases = (("messages", False), ("responses", False), ("chat/completions", False),
                 ("messages", True))
        for endpoint, stream in cases:
            with self.subTest(endpoint=endpoint, stream=stream):
                self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
                payload = self.payload(endpoint=endpoint, stream=stream)
                payload["tools"] = ([{"type": "web_search"}] if endpoint != "messages" else
                                    [{"type": "web_search_20250305", "name": "web_search"}])
                response = client.post("/v1/" + endpoint, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                if stream:
                    delta = [e for e in sse_events(response.text) if e["type"] == "message_delta"][0]
                    usage = delta["usage"]
                elif endpoint == "messages":
                    usage = response.json()["usage"]
                else:
                    usage = response.json()["usage"]
                if endpoint == "messages":
                    self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (4, 2))
                elif endpoint == "responses":
                    self.assertEqual((usage["input_tokens"], usage["output_tokens"],
                                      usage["total_tokens"]), (4, 2, 6))
                else:
                    self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"],
                                      usage["total_tokens"]), (4, 2, 6))
        records = store.list_records()["items"]
        self.assertEqual(len(records), len(cases))
        for record in records:
            self.assertEqual((record["input_tokens"], record["output_tokens"], record["total_tokens"]),
                             (4, 2, 6), record["protocol"])

    def test_messages_fetch_continuation_guard(self):
        # The chat endpoint has no hosted fetch tool type; drive the loop through the
        # messages endpoint, which declares web_fetch_* and runs the same guard.
        self.fetch_content = "x" * 3000
        self.chat_rounds = [tool_call_round("WebFetch", {"url": URL}), final_round()]
        with patch.dict(converter.CONFIG, {"max_request_bytes": 2000}):
            payload = self.payload(endpoint="messages")
            payload["tools"] = [{"type": "web_fetch_20250910", "name": "web_fetch"}]
            response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(len(self.chat_requests), 1)


    # -- Borrowed account lease --------------------------------------------

    def search_payload(self, selected_model="shared-model"):
        payload = self.payload(endpoint="messages", selected_model=selected_model)
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        return payload

    def test_search_borrows_the_requests_account_instead_of_leasing_capacity(self):
        self.configure(profiles=("cn-cli",))
        self.allowed_profiles = {"cn-cli"}
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        with patch.dict(converter.CONFIG, {"max_inflight_per_account": 1}):
            response = self.client.post("/v1/messages", json=self.search_payload())
        self.assertEqual(response.status_code, 200, response.text)
        blocks = response.json()["content"]
        self.assertEqual([block["type"] for block in blocks],
                         ["server_tool_use", "web_search_tool_result", "text"])
        self.assertEqual(blocks[1]["content"][0]["url"], URL)
        self.assertEqual(len(self.tool_requests), 1)
        # The request's own lease is still the only one held while the upstream serves the search.
        self.assertEqual(self.tool_held, [{"cn-cli": 1}])
        self.assertEqual(self.held(), {})

    def test_search_follows_the_chat_account_and_leaves_the_sticky_binding_alone(self):
        _, client = self.audited(profiles=("cn-cli", "cn-work"), mode="scoped")
        payload = self.search_payload()
        with patch.dict(converter.CONFIG, {"max_inflight_per_account": 1}), \
                patch.object(self.pool, "pick", wraps=self.pool.pick) as pick:
            # Two turns of one conversation.
            for _ in range(2):
                self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
                response = client.post("/v1/messages", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
        accounts = {request.headers["x-user-id"]
                    for request in [*self.chat_requests, *self.tool_requests]}
        self.assertEqual(len(self.chat_requests), 4)
        self.assertEqual(len(self.tool_requests), 2)
        self.assertEqual(len(accounts), 1, accounts)
        # Only the chat selections touched the pool; the searches never picked an account.
        self.assertEqual([call.args[1] for call in pick.call_args_list], ["shared-model"] * 2)
        self.assertEqual([account for account, _ in self.pool._sticky.values()],
                         [self.entries[accounts.pop()]["id"]])
        self.assertEqual(self.held(), {})

    def test_search_failing_on_the_borrowed_account_fails_over_outside_the_sticky_binding(self):
        store, client = self.audited(profiles=("cn-cli", "cn-work"), mode="scoped")
        self.search_fail_uids = {"cn-cli"}
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        with patch.dict(converter.CONFIG, {"max_inflight_per_account": 1, "failover_max": 1}), \
                patch.object(self.pool, "pick", wraps=self.pool.pick) as pick:
            response = client.post("/v1/messages", json=self.search_payload("cn-cli-only"))
        self.assertEqual(response.status_code, 200, response.text)
        blocks = response.json()["content"]
        self.assertEqual([block["type"] for block in blocks],
                         ["server_tool_use", "web_search_tool_result", "text"])
        self.assertEqual(blocks[1]["content"][0]["url"], URL)
        self.assertEqual([request.headers["x-user-id"] for request in self.tool_requests],
                         ["cn-cli", "cn-work"])
        # The failover lease takes capacity beside the request's own and is released before the
        # continuation; the borrowed lease stays with the request throughout.
        self.assertEqual(self.tool_held, [{"cn-cli": 1}, {"cn-cli": 1, "cn-work": 1}])
        self.assertEqual(self.chat_held, [{"cn-cli": 1}, {"cn-cli": 1}])
        self.assertEqual(self.held(), {})
        # The chat selection is the only sticky one; the failover pick carries no session key.
        self.assertEqual([call.args[:2] for call in pick.call_args_list][1:], [(None, None)])
        self.assertEqual([account for account, _ in self.pool._sticky.values()],
                         [self.entries["cn-cli"]["id"]])
        record = self.only_record(store)
        self.assertEqual((record["profile"], record["credential"]),
                         ("cn-cli", self.entries["cn-cli"]["account_key"]))
        entry = next(a for a in record["attempts"] if a["stage"] == "server_tool")
        # The server_tool entry names the account that actually served the search.
        self.assertEqual((entry["profile"], entry["credential"], entry["outcome"]),
                         ("cn-work", self.entries["cn-work"]["account_key"], "success"))

    # -- Failover retries ----------------------------------------------------

    def test_failover_retry_does_not_carry_the_failed_attempts_tool_turns(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.configure()
                self.chat_requests.clear()
                self.tool_requests.clear()
                # Attempt one searches, then its continuation fails; attempt two starts over.
                self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), status,
                                    tool_call_round("WebSearch", {"query": QUERY}), final_round()]
                with patch.dict(converter.CONFIG, {"failover_max": 1}):
                    response = self.client.post("/v1/messages", json=self.search_payload())
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(self.chat_requests), 4)
                messages = [json.loads(request.content)["messages"] for request in self.chat_requests]
                roles = [message["role"] for message in messages[0]]
                self.assertEqual(roles[-1], "user")
                self.assertEqual([message["role"] for message in messages[1]],
                                 [*roles, "assistant", "tool"])
                self.assertEqual(messages[2], messages[0])
                self.assertEqual(messages[3], messages[1])
                self.assertNotEqual(self.chat_requests[2].headers["x-user-id"],
                                    self.chat_requests[0].headers["x-user-id"])
                # Only the successful attempt's server tool blocks reach the client.
                self.assertEqual([block["type"] for block in response.json()["content"]],
                                 ["server_tool_use", "web_search_tool_result", "text"])

    # -- Request size guard ------------------------------------------------

    def guard_calls(self):
        """Patch the size guard; each call records whether it ran on the event loop thread."""
        real = converter._guard_request_size
        calls = []

        def recording(body):
            try:
                asyncio.get_running_loop()
                calls.append("loop")
            except RuntimeError:
                calls.append("worker")
            return real(body)
        self.enterContext(patch.object(converter, "_guard_request_size", side_effect=recording))
        return calls

    def test_request_without_server_tools_adds_no_guard_pass_in_the_fetch(self):
        for endpoint in ("messages", "responses", "chat/completions"):
            with self.subTest(endpoint=endpoint):
                calls = self.guard_calls()
                response = self.client.post("/v1/" + endpoint, json=self.payload(endpoint=endpoint))
                self.assertEqual(response.status_code, 200, response.text)
                # Only the preparation pass, which already runs in the threadpool.
                self.assertEqual(calls, ["worker"])

    def test_only_continuation_rounds_are_measured_again_and_off_the_event_loop(self):
        calls = self.guard_calls()
        self.chat_rounds = [tool_call_round("WebSearch", {"query": QUERY}), final_round()]
        response = self.client.post("/v1/messages", json=self.search_payload())
        self.assertEqual(response.status_code, 200, response.text)
        # The preparation pass plus the single continuation round.
        self.assertEqual(calls, ["worker", "worker"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
