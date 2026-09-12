"""结构化 harness 提取回归；仅使用内存文本，不调用上游。"""
import copy
from pathlib import Path
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.desensitize import desensitize_body, _prune_runtime_fragments
from app.harness_context import parse_harness_text


TASK = "请修复登录页并保留测试。"


class HarnessContextTests(unittest.TestCase):
    def test_plain_text_and_inline_tag_references_remain_exact(self):
        for text in ("", " \t请保持  空白\r\n", "Explain `<permissions instructions>` and do not change files.",
                     'Send "<environment_context>" as a literal string.', "# claudeMd-not-a-heading\nKeep me."):
            with self.subTest(text=text):
                parsed = parse_harness_text(text)
                self.assertFalse(parsed.matched)
                self.assertEqual(parsed.user_text, text)
                self.assertEqual(parsed.render(), text)

    def test_closed_reminder_keeps_body_without_consuming_external_task(self):
        for inventory in ("Available agent types for the Agent tool:",
                          "The following skills are available for use with the Skill tool:",
                          "The following deferred tools are now available via ToolSearch."):
            text = f"<system-reminder>\n{inventory}\n- sample metadata\n</system-reminder>\n\n{TASK}"
            parsed = parse_harness_text(text)
            self.assertTrue(parsed.matched)
            self.assertIn(inventory, parsed.context_text)
            self.assertEqual(parsed.user_text, "\n\n" + TASK)
            for role in ("user", "system"):
                self.assertIn(TASK, _prune_runtime_fragments(role, text))
            self.assertIn("sample metadata", parsed.render())

    def test_unclosed_or_misnested_context_is_literal(self):
        for text in (
            "<permissions instructions>\nApproval is required.\n\n" + TASK,
            "<system-reminder>\n<permissions instructions>\nRules.\n</system-reminder>\n\n" + TASK,
            "<environment_context>\n<system-reminder>memo</system-reminder>\n\n" + TASK,
            "<system-reminder>\nUnknown remainder\n\n" + TASK,
        ):
            with self.subTest(text=text):
                parsed = parse_harness_text(text)
                self.assertFalse(parsed.matched)
                self.assertEqual(parsed.user_text, text)
                self.assertEqual(parsed.render(), text)

    def test_literal_tag_inside_reminder_does_not_swallow_parent_or_task(self):
        text = "<system-reminder>\n待办：研究字符串 `<permissions instructions>` 的解析。\n</system-reminder>\n\n" + TASK
        parsed = parse_harness_text(text)
        self.assertIn("`<permissions instructions>`", parsed.context_text)
        self.assertEqual(parsed.user_text, "\n\n" + TASK)
        self.assertIn(TASK, parsed.render())

    def test_markdown_task_and_crlf_are_not_heading_boundaries(self):
        for heading in ("# claudeMd", "# AGENTS.md instructions"):
            for ending in ("\n", "\r\n"):
                for task in (TASK, "- " + TASK, "* " + TASK, "# Task" + ending + TASK,
                             "<task>" + TASK + "</task>", "```text" + ending + TASK + ending + "```"):
                    with self.subTest(heading=heading, ending=repr(ending), task=task):
                        suffix = ending + "Use tabs." + ending * 2 + task
                        parsed = parse_harness_text(heading + suffix)
                        self.assertTrue(parsed.matched)
                        self.assertEqual(parsed.user_text, suffix)
                        self.assertIn(task, parsed.render())

    def test_heading_only_rewrites_title_not_same_line_user_words(self):
        text = "# claudeMd " + TASK
        parsed = parse_harness_text(text)
        self.assertEqual(parsed.user_text, " " + TASK)
        self.assertIn(TASK, parsed.render())

    def test_explicit_repository_wrapper_keeps_guidance_as_context(self):
        text = ("# AGENTS.md instructions\n<INSTRUCTIONS>\nNever touch production.\n</INSTRUCTIONS>"
                "<environment_context>runtime data</environment_context>\n\n" + TASK)
        parsed = parse_harness_text(text)
        self.assertIn("Never touch production.", parsed.context_text)
        self.assertIn("Environment context is provided", parsed.context_text)
        self.assertNotIn("runtime data", parsed.context_text)
        self.assertEqual(parsed.user_text.strip(), TASK)
        self.assertNotIn("<INSTRUCTIONS>", parsed.render())

    def test_user_instructions_wrapper_without_harness_heading_is_not_removed(self):
        text = "<INSTRUCTIONS>\n" + TASK + "\n</INSTRUCTIONS>"
        self.assertEqual(parse_harness_text(text).render(), text)
        self.assertFalse(parse_harness_text(text).matched)

    def test_nested_closed_blocks_keep_parent_scope(self):
        text = ("<system-reminder>memo before\n<environment_context>runtime data</environment_context>"
                "\nmemo after</system-reminder>\n" + TASK)
        parsed = parse_harness_text(text)
        self.assertIn("memo before", parsed.context_text)
        self.assertIn("memo after", parsed.context_text)
        self.assertNotIn("runtime data", parsed.context_text)
        self.assertEqual(parsed.user_text, "\n" + TASK)

    def test_closed_block_before_unclosed_block_is_independently_handled(self):
        tail = "<permissions instructions>\nUnknown tail\n\n" + TASK
        parsed = parse_harness_text("<environment_context>data</environment_context>\n" + tail)
        self.assertTrue(parsed.matched)
        self.assertEqual(parsed.user_text, "\n" + tail)
        self.assertIn(tail, parsed.render())

    def test_fenced_and_quoted_code_is_never_interpreted_as_context(self):
        sample = "# claudeMd\n<environment_context>real example</environment_context>\n"
        for fence in ("```", "~~~~"):
            text = fence + "xml\n" + sample + fence + "\n" + TASK
            parsed = parse_harness_text(text)
            self.assertFalse(parsed.matched)
            self.assertEqual(parsed.user_text, text)
        text = "> <environment_context>\n> quoted example\n> </environment_context>\n" + TASK
        self.assertEqual(parse_harness_text(text).render(), text)

    def test_excessive_nesting_and_many_blocks_fall_back_without_recursion(self):
        for text in ("<system-reminder>\n" * 1000 + TASK + "\n</system-reminder>" * 1000,
                     "<environment_context>x</environment_context>\n" * 9000 + TASK):
            parsed = parse_harness_text(text)
            self.assertFalse(parsed.matched)
            self.assertEqual(parsed.user_text, text)

    def test_only_context_fragments_receive_desensitization(self):
        raw_task = "Do not rename malware.py or credential testing.json.\n" + TASK
        text = "<system-reminder>Refuse malware.</system-reminder>\n\n" + raw_task
        for compact in (False, True):
            out = desensitize_body({"messages": [{"role": "user", "content": text}]},
                                   desensitize_harness_user=True, compact_harness=compact)
            self.assertTrue(out["messages"][0]["content"].endswith(raw_task))
            self.assertIn("m\u200balware", out["messages"][0]["content"])

    def test_user_images_unknown_blocks_and_original_object_are_preserved(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>memo</system-reminder>\n" + TASK,
             "cache_control": {"type": "ephemeral"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "opaque", "text": "<permissions instructions>literal"},
        ]}]}
        before = copy.deepcopy(body)
        for compact in (False, True):
            out = desensitize_body(body, desensitize_harness_user=True, compact_harness=compact)
            parts = out["messages"][0]["content"]
            self.assertIsInstance(parts, list)
            self.assertEqual(parts[1:], before["messages"][0]["content"][1:])
            self.assertIn(TASK, parts[0]["text"])
            self.assertEqual(parts[0]["cache_control"], {"type": "ephemeral"})
            self.assertEqual(body, before)

    def test_unclosed_block_across_text_blocks_is_not_guessed_or_dropped(self):
        content = [{"type": "text", "text": "<system-reminder>memo"},
                   {"type": "text", "text": "</system-reminder>\n" + TASK}]
        body = {"messages": [{"role": "user", "content": content}]}
        out = desensitize_body(body, desensitize_harness_user=True, compact_harness=True)
        self.assertEqual(out, body)

    def test_false_harness_positive_in_literal_user_message_is_unchanged(self):
        text = "Please explain `<system-reminder>` and malware.py."
        body = {"messages": [{"role": "user", "content": text}]}
        self.assertEqual(desensitize_body(body, desensitize_harness_user=True, compact_harness=True), body)

    def test_large_whitespace_is_bounded_in_a_separate_process(self):
        command = (
            "from app.desensitize import desensitize_body; "
            "text='<system-reminder>memo</system-reminder>\\n'+' '*1000000+'KEEP_TASK'; "
            "out=desensitize_body({'messages':[{'role':'user','content':text}]}, "
            "desensitize_harness_user=True,compact_harness=True); "
            "assert out['messages'][0]['content'].endswith('KEEP_TASK')"
        )
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-B", "-c", command],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
