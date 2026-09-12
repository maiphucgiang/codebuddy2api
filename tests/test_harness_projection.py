"""Responses harness regressions; endpoint requests use only a mock upstream.

Run: python -B tests/test_harness_projection.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import converter
from app import upstream_io
from app.adapters.responses_projection import (
    BASE_SYSTEM_PROMPT,
    HISTORY_PREFIX,
    MAX_USER_CONTEXT_CHARS,
    project_responses_chat_body,
)
from app.desensitize import desensitize_body
from app.harness_context import parse_harness_text


TOOLS = [{"type": "function", "function": {
    "name": "exec_command", "parameters": {"type": "object"},
}}]
SYSTEM = {"role": "system", "content": "You are a coding agent running in the Codex CLI."}
ENVIRONMENT = "<environment_context>platform: synthetic</environment_context>"
TASK = "Inspect the implementation. " + "Repository context. " * 200 + (
    "\nMUST_KEEP_LATEST_USER_CONSTRAINT: do not change data."
)
BULLET_TASK = "- MUST_KEEP_USER_BULLET_TASK: inspect only\n- Do not modify files."
CUSTOM_POLICY = "MUST_KEEP_CUSTOM_SYSTEM_POLICY: never modify production resources."


def tool_rounds(count=10, start=0):
    messages = []
    for index in range(start, start + count):
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call-{index}", "type": "function", "function": {
                    "name": "exec_command", "arguments": "{}",
                },
            }]},
            {"role": "tool", "tool_call_id": f"call-{index}",
             "content": f"Synthetic inspection step {index} completed."},
        ])
    return messages


def differential_cases():
    """The three review failures, with full-text assertions rather than markers only."""
    yield "context_replaces_anchor", [
        SYSTEM, {"role": "user", "content": TASK}, *tool_rounds(),
        {"role": "user", "content": ENVIRONMENT},
    ], "user", TASK
    yield "harness_displaces_system", [
        {"role": "system", "content": SYSTEM["content"] + "\n" + "General runtime guidance. " * 100},
        {"role": "system", "content": CUSTOM_POLICY},
        {"role": "user", "content": "Inspect the repository."},
    ], "system", CUSTOM_POLICY
    yield "markdown_task_disappears", [SYSTEM, {"role": "user", "content": (
        "# AGENTS.md instructions\n<INSTRUCTIONS>Use tabs.</INSTRUCTIONS>\n\n" + BULLET_TASK
    )}], "user", BULLET_TASK


def body_for(messages):
    return {"model": "auto", "tools": deepcopy(TOOLS), "messages": deepcopy(messages)}


def text_of(message):
    content = message.get("content", "")
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content or ""


def responses_items(messages):
    """Use real Responses function items so endpoint probes retain the tool chain."""
    items = []
    for message in messages:
        if message["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message["content"]})
        elif message["role"] == "assistant":
            items.append({"type": "message", "role": "assistant", "content": message.get("content", "")})
            for call in message.get("tool_calls", []):
                items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
        else:
            items.append(deepcopy(message))
    return items


class ProjectionTests(unittest.TestCase):
    def assert_preserved(self, body, role, expected):
        self.assertTrue(any(expected in text_of(message) for message in body["messages"]
                            if message["role"] == role), (role, expected[-100:], body))

    def assert_wire_fields(self, body):
        self.assertEqual(set(body), {"model", "tools", "messages"})
        for message in body["messages"]:
            allowed = {"role", "content", "tool_calls"} if message["role"] == "assistant" else (
                {"role", "content", "tool_call_id"} if message["role"] == "tool" else {"role", "content"}
            )
            self.assertFalse(set(message) - allowed, message)

    def test_three_differential_failures_and_second_desensitization(self):
        for name, messages, role, expected in differential_cases():
            with self.subTest(case=name):
                body = body_for(messages)
                before = deepcopy(body)
                out, stats = project_responses_chat_body(body)
                self.assertEqual(body, before)
                self.assertEqual(stats["mode"], "aggressive")
                self.assert_preserved(out, role, expected)
                self.assert_wire_fields(out)
                if name == "context_replaces_anchor":
                    self.assertTrue(stats["anchor_user_preserved"])
                    self.assertIn({"role": "user", "content": TASK}, out["messages"])
                    self.assertTrue(any(text_of(m).startswith(HISTORY_PREFIX) for m in out["messages"]))
                    calls = {call["id"] for m in out["messages"] for call in m.get("tool_calls", [])}
                    self.assertIn("call-9", calls)
                    self.assertTrue(all(m["tool_call_id"] in calls for m in out["messages"] if m["role"] == "tool"))
                for compact in (False, True):
                    again = desensitize_body(out, roles=("system", "developer"),
                                            desensitize_harness_user=True, compact_harness=compact)
                    self.assert_preserved(again, role, expected)
                    self.assert_wire_fields(again)

    def test_all_custom_system_constraints_have_independent_budget(self):
        policies = [f"Policy {index}: " + "Keep every condition. " * 80 + f"END_POLICY_{index}"
                    for index in range(4)]
        messages = [{"role": "system", "content": "<system-reminder>" + "Runtime notes. " * 800
                     + "</system-reminder>"}]
        for policy in policies:
            messages.extend([{"role": "system", "content": ENVIRONMENT},
                             {"role": "system", "content": policy}])
        messages.append({"role": "user", "content": "Inspect only."})
        out, _ = project_responses_chat_body(body_for(messages))
        for policy in policies:
            self.assertIn({"role": "system", "content": policy}, out["messages"])
        self.assertNotIn("Additional instructions:", "\n".join(map(text_of, out["messages"])))

    def test_system_custom_text_after_long_context_is_not_truncated(self):
        text = "<system-reminder>" + "Runtime metadata. " * 500 + "</system-reminder>\n" + CUSTOM_POLICY
        out, _ = project_responses_chat_body(body_for([
            {"role": "system", "content": text}, {"role": "user", "content": "Inspect."},
        ]))
        self.assert_preserved(out, "system", CUSTOM_POLICY)

    def test_unwrapped_system_paragraph_does_not_become_a_fabricated_summary(self):
        text = (SYSTEM["content"] + "\nThe following deferred tools are now available via ToolSearch.\n"
                + CUSTOM_POLICY + "\n# Custom rules\nKeep this later system paragraph exactly.")
        out, _ = project_responses_chat_body(body_for([
            {"role": "system", "content": text}, {"role": "user", "content": "Inspect only."},
        ]))
        self.assert_preserved(out, "system", text)
        # --no-compact preserves unbounded system prose; explicit whole-harness
        # compaction remains an existing opt-in behavior tested separately.
        again = desensitize_body(out, roles=("system", "developer"),
                                desensitize_harness_user=True, compact_harness=False)
        self.assert_preserved(again, "system", text)

    def test_mixed_user_sensitive_words_are_not_reclassified_as_metadata(self):
        task = "请解释 exploit development 和 sandbox 的含义，不执行任何操作。"
        text = "# AGENTS.md instructions\n<system-reminder>Runtime sandbox rules.</system-reminder>\n" + task
        out, _ = project_responses_chat_body(body_for([{"role": "user", "content": text}]))
        for compact in (False, True):
            again = desensitize_body(out, desensitize_harness_user=True, compact_harness=compact)
            self.assert_preserved(again, "user", task)

    def test_long_reminder_before_and_after_full_user_task(self):
        reminder = "<system-reminder>" + "Older memory context. " * 800 + "</system-reminder>"
        for text in (reminder + "\n" + TASK, TASK + "\n" + reminder,
                     reminder + "\n" + TASK + "\n" + reminder):
            with self.subTest(order=text[:30]):
                out, _ = project_responses_chat_body(body_for([SYSTEM, {"role": "user", "content": text}]))
                user = next(message for message in out["messages"] if message["role"] == "user")
                self.assertIn(TASK, user["content"])
                self.assertLessEqual(len(user["content"]), len(TASK) + MAX_USER_CONTEXT_CHARS + 4)
                self.assertIn("Older memory context.", user["content"])
                again = desensitize_body(out, desensitize_harness_user=True, compact_harness=True)
                self.assert_preserved(again, "user", TASK)

    def test_short_real_task_is_not_budgeted_as_long_context(self):
        task = "只审查登录页，不修改文件。"
        text = "<system-reminder>" + "Old memory. " * 600 + "</system-reminder>\n" + task
        out, _ = project_responses_chat_body(body_for([{"role": "user", "content": text}]))
        self.assert_preserved(out, "user", task)

    def test_context_only_turns_do_not_take_latest_user_anchor(self):
        contexts = [ENVIRONMENT, "<permissions instructions>runtime rules</permissions instructions>",
                    "<skills_instructions>runtime skills</skills_instructions>",
                    "<system-reminder>old memory</system-reminder>", "# AGENTS.md instructions"]
        for context in contexts:
            with self.subTest(context=context):
                parsed = parse_harness_text(context)
                self.assertTrue(parsed.matched)
                self.assertFalse(parsed.user_text.strip())
                out, stats = project_responses_chat_body(body_for([
                    {"role": "user", "content": TASK}, *tool_rounds(), {"role": "user", "content": context},
                ]))
                self.assertTrue(stats["anchor_user_preserved"])
                self.assertIn({"role": "user", "content": TASK}, out["messages"])
                self.assert_wire_fields(out)

    def test_only_context_and_system_do_not_duplicate_fallback(self):
        for messages in ([SYSTEM], [SYSTEM, {"role": "user", "content": ENVIRONMENT}]):
            with self.subTest(messages=messages):
                out, stats = project_responses_chat_body(body_for(messages))
                self.assertFalse(stats["anchor_user_preserved"])
                self.assertEqual(sum(text_of(m) == BASE_SYSTEM_PROMPT for m in out["messages"]), 1)
                self.assertEqual(sum(text_of(m) == SYSTEM["content"] for m in out["messages"]), 1)
                self.assertEqual(len(out["messages"]), len(messages) + 1)
                self.assert_wire_fields(out)

    def test_unclosed_wrappers_and_markdown_are_real_latest_users(self):
        tasks = [
            "<environment_context>\n" + "Unclosed metadata-looking text. " * 150 + "\n" + BULLET_TASK,
            "# AGENTS.md instructions\n" + BULLET_TASK,
            "# claudeMd\n" + BULLET_TASK,
            "```xml\n<environment_context>quoted example</environment_context>\n```\n" + BULLET_TASK,
            "# AGENTS.md instructions\n\n" + "- A required detail\n" * 150 + BULLET_TASK,
        ]
        for task in tasks:
            with self.subTest(task=task[:60]):
                parsed = parse_harness_text(task)
                self.assertIn(BULLET_TASK, parsed.user_text)
                out, stats = project_responses_chat_body(body_for([
                    {"role": "user", "content": "Previous task"}, *tool_rounds(),
                    {"role": "user", "content": task}, *tool_rounds(start=10),
                ]))
                self.assertTrue(stats["anchor_user_preserved"])
                self.assert_preserved(out, "user", parsed.user_text)
                again = desensitize_body(out, desensitize_harness_user=True, compact_harness=True)
                self.assert_preserved(again, "user", parsed.user_text)

    def test_text_blocks_are_preserved_and_share_only_context_budget(self):
        blocks = [
            {"type": "text", "text": "<system-reminder>" + "Old memory. " * 600 + "</system-reminder>"},
            {"type": "text", "text": TASK},
            {"type": "text", "text": "\nFinal user condition."},
        ]
        out, _ = project_responses_chat_body(body_for([{"role": "user", "content": blocks}]))
        content = out["messages"][-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), len(blocks))
        self.assertEqual(content[1:], blocks[1:])
        self.assertLess(len(content[0]["text"]), len(blocks[0]["text"]))

    def test_images_use_conservative_path_and_keep_old_chain_and_real_text(self):
        image = {"type": "image_url", "image_url": {"url": "https://synthetic.invalid/image.png", "detail": "high"}}
        blocks = [{"type": "text", "text": "<system-reminder>" + "Old memory. " * 600 + "</system-reminder>"},
                  image, {"type": "text", "text": TASK}]
        messages = [SYSTEM, {"role": "system", "content": CUSTOM_POLICY},
                    {"role": "user", "content": blocks}, *tool_rounds(),
                    {"role": "user", "content": ENVIRONMENT}]
        body = body_for(messages)
        before = deepcopy(body)
        out, stats = project_responses_chat_body(body)
        self.assertEqual(stats["mode"], "conservative")
        self.assertEqual(len(out["messages"]), len(messages))
        self.assertEqual(out["messages"][2]["content"][1:], blocks[1:])
        self.assertEqual(out["messages"][3:-1], messages[3:-1])
        self.assert_preserved(out, "system", CUSTOM_POLICY)
        self.assert_wire_fields(out)
        again = desensitize_body(out, desensitize_harness_user=True, compact_harness=True)
        self.assertEqual(again["messages"][2]["content"][1:], blocks[1:])
        self.assertEqual(body, before)

    def test_normal_requests_preserve_user_and_custom_system_verbatim(self):
        body = {"model": "auto", "messages": [
            {"role": "system", "content": "  Custom system. " + "Rule. " * 300},
            {"role": "user", "content": "  Plain user. " + "Detail. " * 600 + "\n"},
        ]}
        out, stats = project_responses_chat_body(body)
        self.assertEqual(stats["mode"], "conservative")
        self.assertEqual(out, body)


class ResponsesEndpointTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "model_guard": False,
            "max_images": 16, "image_policy": "truncate", "max_request_bytes": 32 * 1024 * 1024,
            "log_body_limit": 0, "log_path": None, "desensitize": False, "no_compact": False,
        }))
        self.credentials = self.enterContext(patch.object(converter, "_cred_for", return_value=(None, {})))
        self.enterContext(patch.object(converter, "_log"))
        self.captured = []
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.handle)
        self.enterContext(patch.object(upstream_io.httpx, "AsyncClient",
                                      side_effect=lambda **kw: real_client(transport=transport, **kw)))
        self.client = self.enterContext(TestClient(converter.app))

    def handle(self, request):
        self.captured.append(json.loads(request.content))
        chunk = {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())

    def post(self, messages):
        return self.client.post("/v1/responses", json={
            "model": "auto", "stream": False, "input": responses_items(messages),
            "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        })

    def test_three_differential_cases_reach_mock_upstream_with_desensitization_on_and_off(self):
        for enabled in (False, True):
            converter.CONFIG["desensitize"] = enabled
            for name, messages, role, expected in differential_cases():
                with self.subTest(desensitize=enabled, case=name):
                    self.captured.clear()
                    response = self.post(messages)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(len(self.captured), 1)
                    out = self.captured[0]
                    self.assertTrue(any(expected in text_of(m) for m in out["messages"] if m["role"] == role))
                    for message in out["messages"]:
                        self.assertFalse(set(message) - {"role", "content", "tool_calls", "tool_call_id"})
                    if name == "context_replaces_anchor":
                        self.assertIn({"role": "user", "content": TASK}, out["messages"])
                        self.assertTrue(any(m.get("tool_call_id") == "call-9" for m in out["messages"]))

    def test_long_reminder_and_image_reach_mock_upstream(self):
        converter.CONFIG["desensitize"] = True
        image_url = "https://synthetic.invalid/prompt.png"
        blocks = [
            {"type": "input_text", "text": "<system-reminder>" + "Old memory. " * 600 + "</system-reminder>"},
            {"type": "input_image", "image_url": image_url},
            {"type": "input_text", "text": TASK},
        ]
        response = self.post([SYSTEM, {"role": "user", "content": blocks}, *tool_rounds()])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.captured), 1)
        users = [m for m in self.captured[0]["messages"] if m["role"] == "user"]
        self.assertEqual(users[0]["content"][1], {"type": "image_url", "image_url": {"url": image_url}})
        self.assertEqual(users[0]["content"][2], {"type": "text", "text": TASK})

    def test_existing_gateway_limit_rejects_long_task_without_upstream_call(self):
        converter.CONFIG["max_request_bytes"] = 1024
        response = self.post([{"role": "user", "content": TASK}])
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["detail"]["error"]["code"], "request_too_large")
        self.assertFalse(self.captured)
        self.credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
