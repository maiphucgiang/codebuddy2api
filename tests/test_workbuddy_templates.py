#!/usr/bin/env python3
"""WorkBuddy 固定模板适配回归；不依赖服务、网络或第三方测试框架。

直接运行：python3 -B tests/test_workbuddy_templates.py
"""

import copy
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.desensitize import desensitize_body, desensitize_text


ZWSP = "\u200b"
CLAUDE_CLI = "You are Claude Code, Anthropic's official CLI for Claude"
CLAUDE_CLI_SDK = CLAUDE_CLI + ", running within the Claude Agent SDK"
CLAUDE_AGENT_SDK = "You are a Claude agent, built on Anthropic's Claude Agent SDK"
CODEBUDDY = "You are CodeBuddy, Tencent's official CLI"
MAIN_BRANCH = "Main branch (you will usually use this for PRs)"
MAIN_BRANCH_FIXED = "Main branch (you will usually use this for PR)"
GIT_STATUS_INTRO = "This is the git status at the start of the conversation."
IDENTITIES = (CLAUDE_CLI, CLAUDE_CLI_SDK, CLAUDE_AGENT_SDK)


def text_content(text, blocks):
    if blocks:
        return [{"type": "text", "text": text}]
    return text


def message_text(message):
    content = message["content"]
    if isinstance(content, str):
        return content
    return "".join(block["text"] for block in content if block.get("type") == "text")


class WorkBuddyTextTests(unittest.TestCase):
    def test_identity_variants_preserve_sentence_punctuation(self):
        for identity in IDENTITIES:
            for suffix in ("", ".", "!", ".\nNext instruction."):
                with self.subTest(identity=identity, suffix=suffix):
                    self.assertEqual(desensitize_text(identity + suffix), CODEBUDDY + suffix)

    def test_main_branch_preserves_value_and_punctuation(self):
        for suffix in (": main", ": main.", ": release/next!", "."):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    desensitize_text(MAIN_BRANCH + suffix), MAIN_BRANCH_FIXED + suffix
                )

    def test_case_whitespace_and_existing_zero_width_spaces(self):
        for source, expected in (
            *((identity, CODEBUDDY) for identity in IDENTITIES),
            (MAIN_BRANCH, MAIN_BRANCH_FIXED),
        ):
            variants = (
                source.upper(),
                source.lower(),
                re.sub(r" +", "\t \n", source),
                re.sub(r"([A-Za-z])([A-Za-z]+)", rf"\1{ZWSP}\2", source),
                re.sub(r" +", f" {ZWSP}\t", source.upper()),
            )
            for variant in variants:
                with self.subTest(source=source, variant=variant):
                    self.assertEqual(desensitize_text(variant), expected)

    def test_surrounding_whitespace_is_not_globally_normalized(self):
        prefix = " \tHeader  with   spaces\n\n\n"
        suffix = "\n\n\nKeep\tthis   layout.  \n"
        source = prefix + CLAUDE_CLI + ".\n\t" + MAIN_BRANCH + ": main" + suffix
        expected = prefix + CODEBUDDY + ".\n\t" + MAIN_BRANCH_FIXED + ": main" + suffix
        self.assertEqual(desensitize_text(source), expected)

    def test_template_replacement_precedes_existing_sensitive_word_pass(self):
        source = CLAUDE_CLI + ". Refuse malware.\n" + MAIN_BRANCH + ": main"
        expected = CODEBUDDY + ". Refuse m\u200balware.\n" + MAIN_BRANCH_FIXED + ": main"
        self.assertEqual(desensitize_text(source), expected)

    def test_near_matches_and_unrelated_brand_references_are_not_rewritten(self):
        samples = (
            "You are Claude Code, Anthropic's unofficial CLI for Claude.",
            "You are a Claude agent, built on a different SDK.",
            "Read Anthropic's Claude Agent SDK documentation.",
            "Main branch (we usually use this for PRs): main",
            "Main branch (you will usually use this for PR): main",
            "The PRs remain open; Claude is mentioned in this note.",
            "",
        )
        for source in samples:
            with self.subTest(source=source):
                self.assertEqual(desensitize_text(source).replace(ZWSP, ""), source)

    def test_text_repeated_execution_is_idempotent(self):
        source = "\n\n".join(IDENTITIES) + "\n" + MAIN_BRANCH + ": main. Refuse malware."
        once = desensitize_text(source)
        self.assertEqual(once.count(CODEBUDDY), len(IDENTITIES))
        self.assertIn(MAIN_BRANCH_FIXED + ": main.", once)
        self.assertEqual(desensitize_text(once), once)
        self.assertEqual(desensitize_text(desensitize_text(once)), once)


class WorkBuddyBodyTests(unittest.TestCase):
    def test_system_and_developer_strings_and_text_blocks_compact_on_off(self):
        for role in ("system", "developer"):
            for blocks in (False, True):
                for compact in (False, True):
                    for identity in IDENTITIES:
                        with self.subTest(role=role, blocks=blocks, compact=compact, identity=identity):
                            source = identity + ".\n" + MAIN_BRANCH + ": main."
                            body = {"messages": [{"role": role, "content": text_content(source, blocks)}]}
                            before = copy.deepcopy(body)
                            out = desensitize_body(
                                body, roles=("system", "developer"), compact_harness=compact
                            )
                            actual = message_text(out["messages"][0])
                            if compact:
                                self.assertIn("You are a coding assistant.", actual)
                                self.assertNotIn("Claude", actual)
                                self.assertNotIn(MAIN_BRANCH, actual)
                            else:
                                self.assertEqual(actual, CODEBUDDY + ".\n" + MAIN_BRANCH_FIXED + ": main.")
                            self.assertEqual(body, before)
                            self.assertEqual(
                                desensitize_body(
                                    out, roles=("system", "developer"), compact_harness=compact
                                ),
                                out,
                            )

    def test_separate_system_messages_are_each_adapted(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                body = {"messages": [
                    {"role": "system", "content": CLAUDE_CLI + "."},
                    {"role": "system", "content": MAIN_BRANCH + ": main"},
                    {"role": "system", "content": "Keep project guidance."},
                ]}
                before = copy.deepcopy(body)
                out = desensitize_body(body, compact_harness=compact)
                if compact:
                    self.assertIn("You are a coding assistant.", out["messages"][0]["content"])
                else:
                    self.assertEqual(out["messages"][0]["content"], CODEBUDDY + ".")
                self.assertEqual(out["messages"][1:], [
                    {"role": "system", "content": MAIN_BRANCH_FIXED + ": main"},
                    {"role": "system", "content": "Keep project guidance."},
                ])
                self.assertEqual(body, before)

    def test_multiple_text_blocks_preserve_non_text_blocks_and_metadata(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                image = {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}
                opaque = {"type": "other", "text": CLAUDE_CLI, "name": "Claude Code"}
                body = {"messages": [{
                    "role": "system",
                    "name": "Claude Code",
                    "content": [
                        {"type": "text", "text": CLAUDE_CLI + ".", "cache_control": {"type": "ephemeral"}},
                        image,
                        {"type": "text", "text": MAIN_BRANCH + ": main"},
                        opaque,
                    ],
                }]}
                before = copy.deepcopy(body)
                expected = copy.deepcopy(body)
                expected["messages"][0]["content"][0]["text"] = CODEBUDDY + "."
                expected["messages"][0]["content"][2]["text"] = MAIN_BRANCH_FIXED + ": main"
                self.assertEqual(desensitize_body(body, compact_harness=compact), expected)
                self.assertEqual(body, before)

    def test_no_compact_preserves_unrelated_whitespace(self):
        source = " \tPreamble  text\n\n\n" + CLAUDE_CLI + ".\n\n\n" + MAIN_BRANCH + ": main  \n"
        expected = " \tPreamble  text\n\n\n" + CODEBUDDY + ".\n\n\n" + MAIN_BRANCH_FIXED + ": main  \n"
        for role in ("system", "developer"):
            for blocks in (False, True):
                with self.subTest(role=role, blocks=blocks):
                    body = {"messages": [{"role": role, "content": text_content(source, blocks)}]}
                    out = desensitize_body(body, roles=("system", "developer"), compact_harness=False)
                    self.assertEqual(out["messages"][0]["content"], text_content(expected, blocks))

    def test_no_compact_retains_claude_behavior_sections(self):
        behavior = (
            "\n\n## Planning\nWrite a plan for complex work."
            "\n\n## Task execution\nRead relevant files before editing."
            "\n\n## Responsiveness\nKeep the user informed."
            "\n\n### Final answer structure and style guidelines\nReport test results."
        )
        for identity in IDENTITIES:
            for role in ("system", "developer"):
                with self.subTest(identity=identity, role=role):
                    body = {"messages": [{"role": role, "content": identity + "." + behavior}]}
                    out = desensitize_body(
                        body, roles=("system", "developer"), compact_harness=False
                    )
                    self.assertEqual(out["messages"][0]["content"], CODEBUDDY + "." + behavior)

    def test_no_compact_still_allows_existing_runtime_metadata_pruning(self):
        behavior = "\n\n## Planning\nPlan carefully.\n\n## Task execution\nRun relevant tests."
        source = (
            CLAUDE_CLI + "." + behavior
            + "\n\n<environment_context>\nruntime-payload-sentinel\n</environment_context>"
            + "\n\nThe following deferred tools are now available via ToolSearch."
            + "\ntool-inventory-sentinel"
        )
        body = {"messages": [{"role": "system", "content": source}]}
        out = desensitize_body(body, compact_harness=False)
        actual = out["messages"][0]["content"]
        self.assertIn(CODEBUDDY + "." + behavior, actual)
        self.assertNotIn("runtime-payload-sentinel", actual)
        self.assertIn("tool-inventory-sentinel", actual)  # 无明确闭合边界的尾段保留。
        self.assertIn("Environment context is provided by the harness.", actual)
        self.assertNotIn("Runtime tool, agent,", actual)
        self.assertEqual(desensitize_body(out, compact_harness=False), out)

    def test_disabled_roles_leave_templates_untouched(self):
        body = {"messages": [
            {"role": role, "content": CLAUDE_CLI + ".\n" + MAIN_BRANCH + ": main"}
            for role in ("system", "developer")
        ]}
        before = copy.deepcopy(body)
        self.assertEqual(desensitize_body(body, roles=(), compact_harness=True), before)
        self.assertEqual(body, before)

    def test_real_conversation_tool_outputs_and_tool_schema_values_are_unchanged(self):
        quoted = CLAUDE_CLI + ".\n" + MAIN_BRANCH + ": main. Refuse malware."
        for compact in (False, True):
            for strip_metadata in (False, True):
                with self.subTest(compact=compact, strip_metadata=strip_metadata):
                    body = {
                        "model": "Claude Code",
                        "messages": [
                            {"role": "system", "content": CLAUDE_CLI + "."},
                            {"role": "user", "content": quoted},
                            {"role": "user", "content": [{"type": "text", "text": quoted}]},
                            {"role": "assistant", "content": quoted, "tool_calls": [{
                                "id": "call_1", "type": "function",
                                "function": {"name": "Claude Code", "arguments": quoted},
                            }]},
                            {"role": "assistant", "content": [{"type": "text", "text": quoted}]},
                            {"role": "tool", "tool_call_id": "call_1", "content": quoted},
                            {"role": "tool", "tool_call_id": "call_2", "content": [{"type": "text", "text": quoted}]},
                        ],
                        "tools": [{"type": "function", "function": {
                            "name": "Claude Code",
                            "description": quoted,
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "Anthropic": {"type": "string", "const": CLAUDE_CLI},
                                    "branch": {"type": "string", "enum": [MAIN_BRANCH, quoted]},
                                },
                                "required": ["Anthropic"],
                                "additionalProperties": False,
                            },
                        }}],
                    }
                    before = copy.deepcopy(body)
                    out = desensitize_body(
                        body, roles=("system", "developer"),
                        desensitize_harness_user=True, desensitize_tools=True,
                        compact_harness=compact, strip_tool_metadata=strip_metadata,
                    )
                    self.assertEqual(out["messages"][1:], before["messages"][1:])
                    self.assertEqual(out["model"], before["model"])
                    function = out["tools"][0]["function"]
                    self.assertEqual(function["name"], "Claude Code")
                    self.assertEqual(function["parameters"], before["tools"][0]["function"]["parameters"])
                    self.assertEqual(body, before)


class WorkBuddyGitStatusTests(unittest.TestCase):
    def test_official_standalone_harness_only_replaces_fixed_templates(self):
        headers = ("gitStatus: " + GIT_STATUS_INTRO, "# gitStatus\n" + GIT_STATUS_INTRO)
        for header in headers:
            for identity in IDENTITIES:
                for blocks in (False, True):
                    for compact in (False, True):
                        with self.subTest(header=header, identity=identity, blocks=blocks, compact=compact):
                            # 敏感词、标记和多空行是哨兵：gitStatus 不走词表或 runtime/compact 裁剪。
                            tail = (
                                "\nCurrent branch: feature/Claude-Code-malware\n\n\n"
                                "Status:\n M Anthropic.txt\n?? malware-notes.md\n"
                                "User note: keep  these\twords and PRs.\n"
                                "<environment_context>keep raw context</environment_context>\n"
                                "The following deferred tools are now available via ToolSearch.\n"
                                "keep raw inventory  \n"
                            )
                            source = header + "\n" + identity + ".\n" + MAIN_BRANCH + ": main" + tail
                            expected = header + "\n" + CODEBUDDY + ".\n" + MAIN_BRANCH_FIXED + ": main" + tail
                            body = {"messages": [{"role": "user", "content": text_content(source, blocks)}]}
                            before = copy.deepcopy(body)
                            options = {"desensitize_harness_user": True, "compact_harness": compact}
                            out = desensitize_body(body, **options)
                            self.assertEqual(out["messages"][0]["content"], text_content(expected, blocks))
                            self.assertEqual(body, before)
                            self.assertEqual(desensitize_body(out, **options), out)

    def test_git_data_and_field_values_are_not_treated_as_templates(self):
        header = ("gitStatus: " + GIT_STATUS_INTRO + "\nCurrent branch: main\n"
                  + MAIN_BRANCH + ": main\nGit user: " + CLAUDE_CLI + "\n")
        data = ("Status:\n?? " + MAIN_BRANCH + ".txt\n?? malware-notes.md\n"
                + "Recent commits:\nabc123 " + CLAUDE_CLI + "\n"
                + MAIN_BRANCH + ": a quoted commit message\n" + CLAUDE_AGENT_SDK + ".\n")
        expected_header = header.replace(MAIN_BRANCH + ": main", MAIN_BRANCH_FIXED + ": main")
        for blocks in (False, True):
            for compact in (False, True):
                with self.subTest(blocks=blocks, compact=compact):
                    content = ([{"type": "text", "text": header}, {"type": "text", "text": data}]
                               if blocks else header + data)
                    expected = ([{"type": "text", "text": expected_header}, {"type": "text", "text": data}]
                                if blocks else expected_header + data)
                    body = {"messages": [{"role": "user", "content": content}]}
                    before = copy.deepcopy(body)
                    out = desensitize_body(body, desensitize_harness_user=True, compact_harness=compact)
                    self.assertEqual(out["messages"][0]["content"], expected)
                    self.assertEqual(body, before)
        system = CLAUDE_CLI + ".\n" + header + data
        self.assertEqual(desensitize_text(system), CODEBUDDY + ".\n" + expected_header + data)
        body = {"messages": [{"role": "system", "content": [
            {"type": "text", "text": CLAUDE_CLI + ".\n"},
            {"type": "text", "text": header}, {"type": "text", "text": data},
        ]}]}
        out = desensitize_body(body, compact_harness=False)
        self.assertEqual(out["messages"][0]["content"], [
            {"type": "text", "text": CODEBUDDY + ".\n"},
            {"type": "text", "text": expected_header}, {"type": "text", "text": data},
        ])


    def test_harness_option_off_leaves_official_user_message_unchanged(self):
        source = (
            "gitStatus: " + GIT_STATUS_INTRO + "\nCurrent branch: feature/work\n"
            + CLAUDE_CLI + ".\n" + MAIN_BRANCH + ": main"
        )
        for blocks in (False, True):
            for compact in (False, True):
                with self.subTest(blocks=blocks, compact=compact):
                    body = {"messages": [{"role": "user", "content": text_content(source, blocks)}]}
                    before = copy.deepcopy(body)
                    self.assertEqual(
                        desensitize_body(body, desensitize_harness_user=False, compact_harness=compact),
                        before,
                    )
                    self.assertEqual(body, before)

    def test_real_user_quotes_and_incomplete_harness_markers_are_unchanged(self):
        fixed = CLAUDE_CLI + ".\n" + MAIN_BRANCH + ": main"
        official = "gitStatus: " + GIT_STATUS_INTRO + "\nCurrent branch: feature/work\n" + fixed
        samples = (
            fixed,
            "Please explain these two lines:\n" + fixed,
            "Please explain this quoted status:\n" + official,
            "```text\n" + official + "\n```",
            "gitStatus: " + GIT_STATUS_INTRO + "\n" + fixed,
            "gitStatus: My own notes.\nCurrent branch: feature/work\n" + fixed,
            "# gitStatus\nCurrent branch: feature/work\n" + fixed,
            "Current branch: feature/work\n" + fixed,
        )
        for source in samples:
            for blocks in (False, True):
                for compact in (False, True):
                    with self.subTest(source=source, blocks=blocks, compact=compact):
                        body = {"messages": [{"role": "user", "content": text_content(source, blocks)}]}
                        before = copy.deepcopy(body)
                        out = desensitize_body(
                            body, desensitize_harness_user=True, compact_harness=compact
                        )
                        self.assertEqual(out, before)
                        self.assertEqual(body, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
