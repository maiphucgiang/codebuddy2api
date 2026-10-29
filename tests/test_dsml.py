#!/usr/bin/env python3
"""Test DSML native tool-call markup repair: state machine semantics and gateway wiring."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import json
import unittest

import httpx

from app import dsml
from tests import test_region_routing as fixtures

ARGS = '{"cmd":"ls -la"}'


def variant_block(sep, name):
    return (f"<{sep}DSML{sep} {name}>"
            f'<{sep}DSML{sep} invoke name="exec_command">'
            f'<{sep}DSML{sep} parameter name="cmd" string="true">ls -la</{sep}DSML{sep} parameter>'
            f"</{sep}DSML{sep} invoke>"
            f"</{sep}DSML{sep} {name}>")
DSML_BLOCK = (
    "<｜｜DSML｜｜ calls>\n"
    '<｜｜DSML｜｜ invoke name="exec_command">\n'
    '<｜｜DSML｜｜ parameter name="cmd" string="true">ls -la</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>")
NAMED = {"exec_command"}


def renamed_block(name):
    return DSML_BLOCK.replace('name="exec_command"', f'name="{name}"')


class MarkupRepairTests(unittest.TestCase):
    def feed_all(self, pieces, allowed=NAMED):
        repair = dsml.MarkupRepair(allowed)
        emits, calls = [], []
        for piece in pieces:
            emit, converted = repair.feed(piece)
            emits.append(emit)
            calls.extend(converted)
        return "".join(emits), calls, repair

    def test_whole_block_converts(self):
        emit, calls, repair = self.feed_all([DSML_BLOCK])
        self.assertEqual(emit, "")
        self.assertEqual(calls, [{"name": "exec_command", "arguments": ARGS}])
        self.assertEqual((repair.converted, repair.seen, repair.rejected), (1, 1, 0))
        self.assertEqual(repair.flush(), "")

    def test_split_at_every_boundary_converts(self):
        for cut in range(1, len(DSML_BLOCK)):
            with self.subTest(cut=cut):
                emit, calls, repair = self.feed_all([DSML_BLOCK[:cut], DSML_BLOCK[cut:]])
                self.assertEqual(emit, "")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["arguments"], ARGS)
                self.assertEqual(repair.flush(), "")

    def test_impure_block_is_returned_byte_exact(self):
        opener, closer = dsml.VARIANTS[0]["open"], dsml.VARIANTS[0]["close"]
        variants = (
            opener + "正文夹带\n" + DSML_BLOCK[len(opener):-len(closer)] + closer,
            DSML_BLOCK[:-len(closer)] + "结尾夹带" + closer,
            opener + "只有正文，没有调用" + closer,
        )
        for marked in variants:
            with self.subTest(marked=marked[:24]):
                emit, calls, repair = self.feed_all([marked])
                self.assertEqual(emit, marked)
                self.assertEqual(calls, [])
                self.assertEqual((repair.seen, repair.rejected), (1, 1))

    def test_strict_mode_rejects_unknown_names(self):
        emit, calls, repair = self.feed_all([renamed_block("shell_exec")])
        self.assertEqual(emit, renamed_block("shell_exec"))
        self.assertEqual(calls, [])
        self.assertEqual(repair.rejected, 1)

    def test_weak_mode_accepts_identifier_names_only(self):
        emit, calls, _ = self.feed_all([renamed_block("browser.navigate")], allowed=frozenset())
        self.assertEqual(emit, "")
        self.assertEqual(calls[0]["name"], "browser.navigate")
        emit, calls, repair = self.feed_all(
            [DSML_BLOCK.replace('name="exec_command"', 'name="<tool_name>"')], allowed=frozenset())
        self.assertEqual(calls, [])
        self.assertEqual(repair.rejected, 1)

    def test_ascii_and_single_bar_variants_convert(self):
        for sep, name in (("|", "function_calls"), ("｜", "calls")):
            with self.subTest(sep=sep, name=name):
                emit, calls, repair = self.feed_all([variant_block(sep, name)])
                self.assertEqual(emit, "")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["arguments"], ARGS)
                self.assertEqual(repair.converted, 1)

    def test_mismatched_separators_never_convert(self):
        marked = ("<｜｜DSML｜｜ calls>"
                  '<|DSML| invoke name="exec_command">'
                  '<|DSML| parameter name="cmd" string="true">ls -la</|DSML| parameter>'
                  "</|DSML| invoke>"
                  "</|DSML| function_calls>")
        emit, calls, repair = self.feed_all([marked, "tail"])
        self.assertEqual(emit, "")                # 闭标签对不上：块永不闭合
        self.assertEqual(calls, [])
        self.assertEqual(repair.flush(), marked + "tail")   # 逐字节交还

    def test_oversized_unclosed_block_passes_through(self):
        repair = dsml.MarkupRepair(NAMED)
        opener, closer = dsml.VARIANTS[0]["open"], dsml.VARIANTS[0]["close"]
        payload = "x" * (dsml.MAX_BLOCK_BYTES + 1)
        emit, calls = repair.feed(opener + payload)
        self.assertEqual(calls, [])
        self.assertTrue(emit.startswith(opener))
        self.assertIn(payload, emit)
        emit, calls = repair.feed(closer + "tail")
        self.assertEqual(emit, closer + "tail")
        self.assertEqual(calls, [])

    def test_unclosed_block_is_flushed_verbatim(self):
        repair = dsml.MarkupRepair(NAMED)
        opener = dsml.VARIANTS[0]["open"]
        invoke = '<｜｜DSML｜｜ invoke name="exec_command">'
        emit, calls = repair.feed(opener + invoke)
        self.assertEqual(emit, "")
        self.assertEqual(calls, [])
        self.assertEqual(repair.flush(), opener + invoke)

    def test_parameter_type_variants(self):
        block = ('<｜｜DSML｜｜ calls>'
                 '<｜｜DSML｜｜ invoke name="exec_command">'
                 '<｜｜DSML｜｜ parameter name="cmd" string="true">ls -la</｜｜DSML｜｜ parameter>'
                 '<｜｜DSML｜｜ parameter name="timeout">30</｜｜DSML｜｜ parameter>'
                 '<｜｜DSML｜｜ parameter name="opts">{"a": 1}</｜｜DSML｜｜ parameter>'
                 '<｜｜DSML｜｜ parameter name="broken">[1,2</｜｜DSML｜｜ parameter>'
                 "</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>")
        _, calls, _ = self.feed_all([block])
        self.assertEqual(calls, [{"name": "exec_command", "arguments":
                                  json.dumps({"cmd": "ls -la", "timeout": 30, "opts": {"a": 1},
                                              "broken": "[1,2"},
                                             ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"))}])


class AllowlistTests(unittest.TestCase):
    def test_collects_all_sources(self):
        body = {"tools": [{"type": "function", "function": {"name": "a"}},
                          {"name": "b"}],
                "functions": [{"name": "c"}],
                "messages": [
                    {"role": "assistant", "tool_calls": [
                        {"function": {"name": "d", "arguments": "{}"}},
                        {"function": {"name": "e", "arguments": "{}"}}]},
                    {"role": "assistant", "function_call": {"name": "f", "arguments": "{}"}},
                    {"role": "user", "content": "hi"}]}
        self.assertEqual(dsml.tool_name_allowlist(body), frozenset("abcdef"))

    def test_junk_is_tolerated(self):
        self.assertEqual(dsml.tool_name_allowlist({"tools": "x", "messages": [1, {"tool_calls": "y"}]}),
                         frozenset())
        self.assertEqual(dsml.tool_name_allowlist(None), frozenset())


class AggregatedRepairTests(unittest.TestCase):
    def base(self, **message):
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok", **message},
                             "finish_reason": "stop"}]}

    def test_rewrites_choice(self):
        result = self.base(content=DSML_BLOCK)
        converted, seen, rejected = dsml.repair_aggregated(result, NAMED)
        self.assertEqual((converted, seen, rejected), (1, 1, 0))
        choice = result["choices"][0]
        self.assertIsNone(choice["message"]["content"])
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["arguments"], ARGS)
        self.assertEqual(choice["finish_reason"], "tool_calls")

    def test_never_overwrites_structured_calls(self):
        result = self.base(content=DSML_BLOCK,
                           tool_calls=[{"id": "call_real", "type": "function",
                                        "function": {"name": "real", "arguments": "{}"}}])
        self.assertEqual(dsml.repair_aggregated(result, NAMED), (0, 0, 0))
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len(result["choices"][0]["message"]["tool_calls"]), 1)

    def test_length_truncation_is_not_repaired(self):
        result = self.base(content=DSML_BLOCK)
        result["choices"][0]["finish_reason"] = "length"
        self.assertEqual(dsml.repair_aggregated(result, NAMED), (0, 0, 0))
        self.assertEqual(result["choices"][0]["message"]["content"], DSML_BLOCK)

    def test_plain_content_untouched(self):
        result = self.base(content="没有任何标记")
        self.assertEqual(dsml.repair_aggregated(result, NAMED), (0, 0, 0))
        self.assertEqual(result["choices"][0]["message"]["content"], "没有任何标记")


def chunk(delta=None, finish=None, identifier="upstream-1"):
    data = {"id": identifier, "model": "m", "choices": [{"index": 0, "delta": delta or {}}]}
    if finish:
        data["choices"][0]["finish_reason"] = finish
        data["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    return "data: " + json.dumps(data, ensure_ascii=False)


def sse_lines_for_block(parts):
    lines = [chunk({"role": "assistant", "content": ""})]
    lines.extend(chunk({"content": part}) for part in parts)
    lines.append(chunk({}, finish="stop"))
    lines.append("data: [DONE]")
    return lines


class SSERepairerTests(unittest.TestCase):
    def repair_stream(self, lines, allowed=NAMED):
        repairer = dsml.SSERepairer(allowed)
        out = []
        for line in lines:
            out.extend(repairer.feed(line))
        return out, repairer

    def test_stream_conversion_and_finish_rewrite(self):
        parts = [DSML_BLOCK[:40], DSML_BLOCK[40:90], DSML_BLOCK[90:]]
        out, repairer = self.repair_stream(sse_lines_for_block(parts))
        self.assertEqual(out[-1], "data: [DONE]")
        self.assertEqual(repairer.converted, 1)
        tool_lines = [line for line in out if '"delta": {"tool_calls"' in line]
        self.assertEqual(len(tool_lines), 1)
        payload = json.loads(tool_lines[0][5:])
        call = payload["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "exec_command")
        self.assertEqual(call["function"]["arguments"], ARGS)
        self.assertGreaterEqual(call["index"], 0)
        finish = json.loads(out[-2][5:])["choices"][0]["finish_reason"]
        self.assertEqual(finish, "tool_calls")
        joined = "".join(out)
        self.assertNotIn("DSML", joined)
        # 内容与调用两种增量的总量守恒：被转换的正文不出现在输出里。
        for line in out:
            self.assertNotIn("invoke name=", line)

    def test_prefilter_leaves_clean_lines_byte_identical(self):
        line = chunk({"content": "ordinary text without any marker"})
        out, repairer = self.repair_stream([line])
        self.assertEqual(out, [line])
        self.assertEqual((repairer.converted, repairer.seen), (0, 0))

    def test_flush_runs_before_done(self):
        opener = DSML_BLOCK[:10]   # 截断的起始标记：必须留到 DONE 之前交还
        out, repairer = self.repair_stream([chunk({"content": opener}), "data: [DONE]"])
        self.assertEqual(out[-1], "data: [DONE]")
        self.assertIn(opener, out[-2])
        self.assertEqual(repairer.seen, 0)

    def test_structured_calls_are_flagged(self):
        line = chunk({"tool_calls": [{"index": 0, "id": "call_x", "type": "function",
                                      "function": {"name": "exec_command", "arguments": "{}"}}]})
        out, repairer = self.repair_stream([line])
        self.assertEqual(out, [line])
        self.assertTrue(repairer.saw_structured)

    def test_opensource_variant_is_observed_not_repaired(self):
        text = ('<tool_calls:opensource><tool_call:opensource>exec_command'
                "<arg_key:opensource>cmd</arg_key:opensource>ls")
        out, repairer = self.repair_stream([chunk({"content": text}), chunk({}, finish="stop"),
                                            "data: [DONE]"])
        self.assertTrue(repairer.saw_opensource)
        self.assertEqual((repairer.converted, repairer.seen), (0, 0))
        self.assertIn("tool_calls:opensource", "".join(out))   # 原样透出，不吞字节


class DSHGatewayTests(fixtures.RegionRoutingTests):
    """Feed DSML-markup SSE through the mocked upstream and assert repaired client output."""

    def setUp(self):
        super().setUp()
        self.upstream_parts = []

    def handle_upstream(self, request):
        if self.upstream_parts:
            body = fixtures.request_json(request)
            self.assertNotIn("tools", body, "泄漏场景的前提就是本请求未声明 tools")
            lines = sse_lines_for_block(self.upstream_parts)
            payload = ("".join(line + "\n\n" for line in lines)).encode()
            return httpx.Response(200, content=payload, headers={"Content-Type": "text/event-stream"})
        return super().handle_upstream(request)

    def post_markup(self, *, stream):
        self.upstream_parts = [DSML_BLOCK[:60], DSML_BLOCK[60:]]
        self.allowed_profiles = {"u1"}
        return self.client.post("/v1/chat/completions",
                                json=self.payload(stream=stream, selected_model="shared-model"))

    def test_nonstream_response_is_repaired(self):
        response = self.post_markup(stream=False)
        self.assertEqual(response.status_code, 200, response.text)
        message = response.json()["choices"][0]["message"]
        self.assertIsNone(message["content"])
        self.assertEqual(len(message["tool_calls"]), 1)
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "exec_command")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], ARGS)
        self.assertEqual(response.json()["choices"][0]["finish_reason"], "tool_calls")

    def test_stream_response_is_repaired(self):
        response = self.post_markup(stream=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("DSML", response.text)
        self.assertIn('"tool_calls"', response.text)
        self.assertIn('"finish_reason": "tool_calls"', response.text)
        self.assertIn("[DONE]", response.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
