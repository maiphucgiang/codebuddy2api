#!/usr/bin/env python3
"""Test Responses request and response adaptation."""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

from app.adapters.responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
)
from app.desensitize import desensitize_body
from app.adapters.responses_projection import project_responses_chat_body


def test_simple_text_request():
    """Convert plain text input into Chat messages."""
    req = {
        "model": "glm-5.2",
        "input": "Hello, how are you?",
        "instructions": "You are a helpful assistant.",
        "stream": True,
    }
    chat = responses_request_to_chat(req)
    assert chat["messages"][0] == {"role": "system", "content": "You are a helpful assistant."}
    assert chat["messages"][1] == {"role": "user", "content": "Hello, how are you?"}
    assert chat["model"] == "glm-5.2"
    print("✅ test_simple_text_request")


def test_array_input_request():
    """Convert mixed message, function-call and function-output items."""
    req = {
        "model": "glm-5.2",
        "input": [
            {"role": "user", "content": "Fix the bug"},
            {"type": "message", "id": "msg_1", "role": "assistant",
             "content": [{"type": "output_text", "text": "I'll check the file."}]},
            {"type": "function_call", "id": "fc_1", "call_id": "call_123",
             "name": "shell", "arguments": '{"cmd":"cat main.py"}'},
            {"type": "function_call_output", "call_id": "call_123",
             "output": "print('hello')"},
            {"role": "user", "content": "Now fix it"},
        ],
        "instructions": "You are a coding assistant.",
    }
    chat = responses_request_to_chat(req)
    msgs = chat["messages"]

    assert msgs[0] == {"role": "system", "content": "You are a coding assistant."}
    assert msgs[1] == {"role": "user", "content": "Fix the bug"}
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "I'll check the file."
    assert len(msgs[2]["tool_calls"]) == 1
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "shell"
    assert msgs[3]["role"] == "tool"
    assert msgs[3]["tool_call_id"] == "call_123"
    assert msgs[4] == {"role": "user", "content": "Now fix it"}
    print("✅ test_array_input_request")


def test_tools_conversion():
    """Convert flat Responses tools to nested Chat definitions."""
    req = {
        "model": "glm-5.2",
        "input": "test",
        "tools": [
            {"type": "function", "name": "shell",
             "description": "Run a shell command",
             "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
        ],
    }
    chat = responses_request_to_chat(req)
    tool = chat["tools"][0]
    assert tool["type"] == "function"
    assert "function" in tool
    assert tool["function"]["name"] == "shell"
    print("✅ test_tools_conversion")


def test_max_output_tokens():
    """Map max_output_tokens to max_tokens."""
    req = {"model": "glm-5.2", "input": "test", "max_output_tokens": 4096}
    chat = responses_request_to_chat(req)
    assert chat["max_tokens"] == 4096
    print("✅ test_max_output_tokens")


def test_developer_role():
    """Normalize developer roles to system roles."""
    req = {"model": "glm-5.2", "input": [
        {"role": "developer", "content": "Be concise."},
        {"role": "user", "content": "Hi"},
    ]}
    chat = responses_request_to_chat(req)
    assert chat["messages"][0] == {"role": "system", "content": "Be concise."}
    assert chat["messages"][1] == {"role": "user", "content": "Hi"}
    print("✅ test_developer_role")


def test_typed_developer_message_request():
    """Normalize developer roles inside typed messages."""
    req = {
        "model": "glm-5.2",
        "input": [
            {"type": "message", "role": "developer", "content": "Be concise."},
            {"type": "message", "role": "user", "content": "Hi"},
        ],
    }
    chat = responses_request_to_chat(req)
    assert chat["messages"][0] == {"role": "system", "content": "Be concise."}
    assert chat["messages"][1] == {"role": "user", "content": "Hi"}
    print("✅ test_typed_developer_message_request")


def test_desensitize_harness_user_and_tools():
    """Compact trusted harness input and tool metadata while preserving real user text."""
    body = {
        "messages": [
            {"role": "system", "content": "Refuse exploit development."},
            {"role": "user", "content": "# AGENTS.md instructions\n<environment_context> sandbox escalation</environment_context>"},
            {"role": "user", "content": "please explain dos attacks"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "exec_command", "description": "Run dangerous exploit development checks."}}
        ],
    }
    out = desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
    )
    assert "​" in out["messages"][0]["content"]
    assert "Repository instructions and durable user context are provided." in out["messages"][1]["content"]
    assert "Environment context is provided by the harness." in out["messages"][1]["content"]
    assert "​" not in out["messages"][2]["content"]
    assert out["messages"][2]["content"] == "please explain dos attacks"
    assert "​" in out["tools"][0]["function"]["description"]
    print("✅ test_desensitize_harness_user_and_tools")


def test_compact_harness_messages_and_strip_tool_metadata():
    """Compact long Codex templates without replacing the user's actual request."""
    body = {
        "messages": [
            {"role": "system", "content": "You are a coding agent running in the Codex CLI. # How you work\nUse sandbox and escalation."},
            {"role": "system", "content": "<permissions instructions>\nFilesystem sandboxing defines which files can be read or written.</permissions instructions>"},
            {"role": "user", "content": "# AGENTS.md instructions\n<environment_context> sandbox escalation</environment_context>"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "exec_command", "description": "Run dangerous exploit development checks.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string", "description": "Shell command to execute."}}}}}
        ],
    }
    out = desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=True,
        strip_tool_metadata=True,
    )
    assert len(out["messages"][0]["content"]) < 220
    assert "Codex CLI" in out["messages"][0]["content"]
    assert "sandboxing defines" not in out["messages"][1]["content"]
    assert "Repository instructions and durable user context are provided." in out["messages"][2]["content"]
    assert "Environment context is provided by the harness." in out["messages"][2]["content"]
    assert "description" not in out["tools"][0]["function"]
    assert "description" not in out["tools"][0]["function"]["parameters"]["properties"]["cmd"]
    print("✅ test_compact_harness_messages_and_strip_tool_metadata")


def test_no_compact_still_prunes_codex_runtime_metadata():
    """Remove trusted runtime metadata even when full conversation text is preserved."""
    body = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a coding agent running in the Codex CLI.\n\n"
                    "# How you work\nUse sandbox and escalation carefully.\n\n"
                    "<permissions instructions>\nFilesystem sandboxing defines which files can be read or written.\n"
                    "## How to request escalation\n...\n</permissions instructions>\n\n"
                    "The following deferred tools are now available via ToolSearch.\n..."
                ),
            },
            {
                "role": "user",
                "content": (
                    "# AGENTS.md instructions\n<INSTRUCTIONS>\nproject guidance\n</INSTRUCTIONS>"
                    "<environment_context>\nvery long runtime context\n</environment_context>\n"
                    "<skills_instructions>\nvery long skills metadata\n</skills_instructions>\n"
                    "test"
                ),
            },
            {"role": "user", "content": "test"},
        ],
    }
    out = desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        compact_harness=False,
    )
    system_text = out["messages"][0]["content"]
    harness_text = out["messages"][1]["content"]
    assert "You are a coding agent running in the Codex CLI" in system_text
    assert "## Planning" not in system_text
    assert "## Task execution" not in system_text
    assert "### Final answer structure and style guidelines" not in system_text
    assert "# How you work" in system_text
    assert "Filesystem sandboxing defines" not in system_text
    # Without a closed wrapper the deferred-tool paragraph is real text, not metadata.
    assert "The following deferred tools are now available via ToolSearch.\n..." in system_text
    assert "Runtime permissions apply" in system_text
    assert "Runtime tool, agent, sk" not in system_text
    assert "very long runtime context" not in harness_text
    assert "very long skills metadata" not in harness_text
    assert "# AGENTS.md instructions" not in harness_text
    assert "Repository instructions and durable user context are provided." in harness_text
    assert "Environment context is provided by the harness." in harness_text
    # Remove inserted separators to isolate harness compaction from term adaptation.
    assert "Runtime skill metadata is available" in harness_text.replace("​", "")
    assert harness_text.strip().replace("​", "").endswith("test")
    assert out["messages"][2]["content"] == "test"
    print("✅ test_no_compact_still_prunes_codex_runtime_metadata")


def test_responses_projection_compacts_codex_harness_and_tools():
    """Project Codex requests into short system context and minimal tool schemas."""
    body = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a coding agent running in the Codex CLI.\n"
                    "# AGENTS.md spec\nVery long harness instructions."
                ),
            },
            {
                "role": "system",
                "content": "Additional repo rule: always run tests after editing.",
            },
            {
                "role": "user",
                "content": "# AGENTS.md instructions\n<environment_context>\nlong context\n</environment_context>",
            },
            {"role": "user", "content": "实现该方案"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "description": "Run a command with a long dangerous description",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cmd": {"type": "string", "description": "Shell command to execute."},
                            "yield_time_ms": {"type": "number", "description": "Wait time"},
                        },
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                    "strict": False,
                },
            }
        ],
    }
    out, stats = project_responses_chat_body(body)
    assert stats["mode"] == "aggressive"
    assert out["messages"][0]["role"] == "system"
    assert "OpenAI-compatible CLI" in out["messages"][0]["content"]
    assert all("# AGENTS.md instructions" not in msg.get("content", "") for msg in out["messages"])
    assert any("Additional repo rule" in msg.get("content", "") for msg in out["messages"])
    assert out["messages"][-1] == {"role": "user", "content": "实现该方案"}
    tool = out["tools"][0]["function"]
    assert tool["name"] == "exec_command"
    assert "description" not in tool
    assert "description" not in tool["parameters"]["properties"]["cmd"]
    assert stats["projected_tool_chars"] < stats["original_tool_chars"]
    print("✅ test_responses_projection_compacts_codex_harness_and_tools")


def test_responses_projection_preserves_recent_tool_chain_and_summarizes_history():
    """Summarize older turns while retaining the recent tool chain."""
    big_output = "Chunk ID: a1\nWall time: 0.0\nProcess exited with code 0\nOutput:\n" + "\n".join(
        f"line {i}" for i in range(40)
    )
    body = {
        "messages": [
            {"role": "system", "content": "You are a coding agent running in the Codex CLI."},
            {"role": "user", "content": "# AGENTS.md instructions\n<environment_context>ctx</environment_context>"},
            {"role": "user", "content": "先看 README"},
            {
                "role": "assistant",
                "content": "I will inspect the repository.",
                "tool_calls": [
                    {
                        "id": "call_old",
                        "type": "function",
                        "function": {"name": "exec_command", "arguments": "{\"cmd\":\"ls -la\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_old", "content": "Output:\nREADME.md\nsrc\n"},
            {"role": "assistant", "content": "README is present."},
            {"role": "user", "content": "现在修复 converter 的 responses 链路"},
            {
                "role": "assistant",
                "content": "I will patch the proxy and then run tests.",
                "tool_calls": [
                    {
                        "id": "call_recent",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "sed -n '1,200p' converter.py", "yield_time_ms": 1000}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_recent", "content": big_output},
            {"role": "assistant", "content": "I found the endpoint and will implement projection now."},
            {"role": "user", "content": "继续，别依赖 fallback retry"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cmd": {"type": "string"},
                            "yield_time_ms": {"type": "number"},
                        },
                        "required": ["cmd"],
                    },
                },
            }
        ],
    }
    out, stats = project_responses_chat_body(body)
    system_messages = [m["content"] for m in out["messages"] if m["role"] == "system"]
    assert system_messages[0].startswith("You are a coding assistant serving an OpenAI-compatible CLI.")
    assert any("Earlier conversation summary" in text for text in system_messages)
    assert any("先看 README" in text for text in system_messages)

    recent_assistant = next(
        msg for msg in out["messages"]
        if msg.get("role") == "assistant" and any(tc.get("id") == "call_recent" for tc in msg.get("tool_calls", []))
    )
    recent_tool = next(msg for msg in out["messages"] if msg.get("role") == "tool" and msg.get("tool_call_id") == "call_recent")
    assert recent_assistant["tool_calls"][0]["function"]["name"] == "exec_command"
    assert "Process exited with code 0" in recent_tool["content"]
    assert "line 39" in recent_tool["content"]
    assert len(recent_tool["content"]) < len(big_output)
    assert out["messages"][-1] == {"role": "user", "content": "继续，别依赖 fallback retry"}
    assert stats["summarized_history_messages"] >= 1
    print("✅ test_responses_projection_preserves_recent_tool_chain_and_summarizes_history")


def test_responses_projection_shrinks_large_tool_arguments():
    """Compact oversized tool arguments into structured JSON summaries."""
    long_cmd = "echo " + ("x" * 1600)
    body = {
        "messages": [
            {"role": "system", "content": "You are a coding agent running in the Codex CLI."},
            {"role": "user", "content": "执行一个很长的命令"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_long",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": long_cmd, "yield_time_ms": 1000, "workdir": "/tmp"}),
                        },
                    }
                ],
            },
        ],
        "tools": [],
    }
    out, _ = project_responses_chat_body(body)
    args = out["messages"][-1]["tool_calls"][0]["function"]["arguments"]
    parsed = json.loads(args)
    assert parsed["cmd"].startswith("echo ")
    assert "truncated" in parsed["cmd"]
    print("✅ test_responses_projection_shrinks_large_tool_arguments")


def _agentic_tool():
    return [{
        "type": "function",
        "function": {
            "name": "exec_command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        },
    }]


def test_responses_projection_keeps_user_text_sharing_harness_message():
    """Preserve user and reminder text embedded in messages containing harness context."""
    body = {
        "model": "auto",
        "tools": _agentic_tool(),
        "messages": [
            {"role": "system", "content": "You are a coding agent running in the Codex CLI. # How you work"},
            {"role": "user", "content": "# AGENTS.md instructions\n<INSTRUCTIONS>\nUse tabs\n</INSTRUCTIONS>"},
            {"role": "user", "content": "<system-reminder>\n剩余任务：改看板\n</system-reminder>\n\n继续之前的前端改造工程，把登录页也改了"},
            {"role": "assistant", "content": "好的，我来改登录页"},
            {"role": "user", "content": "另外把看板的按钮也加上"},
        ],
    }
    out, stats = project_responses_chat_body(body)
    assert stats["mode"] == "aggressive"
    blob = "\n".join(str(m.get("content", "")) for m in out["messages"])
    assert "继续之前的前端改造工程" in blob, "与 harness 同条的用户原话被丢弃"
    assert "剩余任务：改看板" in blob, "system-reminder 正文被丢弃"
    assert "另外把看板的按钮也加上" in blob
    assert "# AGENTS.md instructions" not in blob, "纯 harness 载荷应以摘要出现"
    assert stats["anchor_user_preserved"] or "继续之前的前端改造工程" in blob
    print("✅ test_responses_projection_keeps_user_text_sharing_harness_message")


def test_responses_projection_keeps_last_user_when_it_carries_harness():
    """Preserve real text when the final user message also contains harness context."""
    body = {
        "model": "auto",
        "tools": _agentic_tool(),
        "messages": [
            {"role": "system", "content": "You are a coding agent running in the Codex CLI."},
            {"role": "assistant", "content": "上一轮的答复"},
            {"role": "user", "content": "<system-reminder>\n剩余任务：改看板\n</system-reminder>\n\n继续之前的前端改造工程，把登录页也改了"},
        ],
    }
    out, stats = project_responses_chat_body(body)
    blob = "\n".join(str(m.get("content", "")) for m in out["messages"])
    assert "继续之前的前端改造工程" in blob, "最后一轮用户真话丢失"
    assert "剩余任务：改看板" in blob, "reminder 正文丢失"
    print("✅ test_responses_projection_keeps_last_user_when_it_carries_harness")



def test_stream_converter_text():
    """Convert Chat text SSE into Responses events."""
    conv = ResponsesStreamConverter(model="glm-5.2")

    # Synthetic Chat SSE chunks
    chunks = [
        'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
        'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
        'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}',
        'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}',
        'data: [DONE]',
    ]

    all_events = []
    for line in chunks:
        result = conv.feed_line(line)
        if result:
            for evt_line in result.strip().split("\n\n"):
                if evt_line.startswith("data: "):
                    all_events.append(json.loads(evt_line[6:]))

    finish = conv.finish()
    for evt_line in finish.strip().split("\n\n"):
        if evt_line.startswith("data: "):
            all_events.append(json.loads(evt_line[6:]))

    types = [e["type"] for e in all_events]
    assert "response.created" in types
    assert "response.in_progress" in types
    assert "response.output_item.added" in types
    assert "response.content_part.added" in types
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert "response.content_part.done" in types
    assert "response.output_item.done" in types
    assert "response.completed" in types

    text_done = [e for e in all_events if e["type"] == "response.output_text.done"][0]
    assert text_done["text"] == "Hello world"

    completed = [e for e in all_events if e["type"] == "response.completed"][0]
    resp = completed["response"]
    assert resp["status"] == "completed"
    assert resp["output"][0]["type"] == "message"
    assert resp["output"][0]["content"][0]["text"] == "Hello world"
    assert resp["usage"]["input_tokens"] == 10

    print("✅ test_stream_converter_text")


def test_stream_converter_function_call():
    """Convert Chat tool-call SSE into Responses function-call events."""
    conv = ResponsesStreamConverter(model="glm-5.2")

    chunks = [
        'data: {"id":"chatcmpl-2","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_abc","type":"function","function":{"name":"shell","arguments":""}}]},"finish_reason":null}]}',
        'data: {"id":"chatcmpl-2","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"cmd"}}]},"finish_reason":null}]}',
        'data: {"id":"chatcmpl-2","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\": \\"ls\\"}"}}]},"finish_reason":null}]}',
        'data: {"id":"chatcmpl-2","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        'data: [DONE]',
    ]

    all_events = []
    for line in chunks:
        result = conv.feed_line(line)
        if result:
            for evt_line in result.strip().split("\n\n"):
                if evt_line.startswith("data: "):
                    all_events.append(json.loads(evt_line[6:]))

    finish = conv.finish()
    for evt_line in finish.strip().split("\n\n"):
        if evt_line.startswith("data: "):
            all_events.append(json.loads(evt_line[6:]))

    types = [e["type"] for e in all_events]
    assert "response.output_item.added" in types
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    assert "response.completed" in types

    args_done = [e for e in all_events if e["type"] == "response.function_call_arguments.done"][0]
    assert args_done["arguments"] == '{"cmd": "ls"}'

    print("✅ test_stream_converter_function_call")


def test_nonstream_response():
    """Build a non-streaming Response object."""
    conv = ResponsesStreamConverter(model="glm-5.2")
    conv.feed_line('data: {"id":"c1","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}')
    conv.feed_line('data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}')

    resp = conv.get_nonstream_response()
    assert resp["object"] == "response"
    assert resp["status"] == "completed"
    assert resp["output"][0]["type"] == "message"
    assert resp["output"][0]["content"][0]["text"] == "Hi"
    assert resp["usage"]["input_tokens"] == 5

    print("✅ test_nonstream_response")


def test_finish_reason_maps_to_terminal_status():
    """Report truncated or filtered output as incomplete in streaming and aggregated responses."""
    conv = ResponsesStreamConverter(model="glm-5.2")
    conv.feed_line('data: {"id":"c2","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}')
    conv.feed_line('data: {"id":"c2","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}')
    tail = conv.finish()
    assert '"type": "response.incomplete"' in tail and '"type": "response.completed"' not in tail
    resp = conv.get_nonstream_response()
    assert resp["status"] == "incomplete"
    assert resp["incomplete_details"] == {"reason": "max_output_tokens"}
    assert resp["output"][0]["status"] == "incomplete"

    conv = ResponsesStreamConverter(model="glm-5.2")
    conv.feed_line('data: {"id":"c3","choices":[{"index":0,"delta":{},"finish_reason":"content_filter"}]}')
    resp = conv.get_nonstream_response()
    assert resp["status"] == "incomplete" and resp["incomplete_details"]["reason"] == "content_filter"

    conv = ResponsesStreamConverter(model="glm-5.2")
    conv.feed_line('data: {"id":"c4","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}')
    conv.feed_line('data: {"id":"c4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    assert '"type": "response.completed"' in conv.finish()
    assert "incomplete_details" not in conv.get_nonstream_response()

    print("✅ test_finish_reason_maps_to_terminal_status")


def test_stream_events_carry_sequence_and_item_ids():
    """Emit monotonic sequence numbers and item IDs on every corresponding delta."""
    conv = ResponsesStreamConverter(model="glm-5.2")
    chunks = [
        'data: {"id":"s1","choices":[{"index":0,"delta":{"reasoning_content":"想"},"finish_reason":null}]}',
        'data: {"id":"s1","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}',
        'data: {"id":"s1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"shell","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"id":"s1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
    ]
    raw = "".join(conv.feed_line(line) for line in chunks) + conv.finish()
    evts = [json.loads(part[6:]) for part in raw.strip().split("\n\n") if part.startswith("data: ")]
    seqs = [e["sequence_number"] for e in evts]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
    msg_ids = {e.get("item_id") for e in evts if e["type"].startswith(("response.output_text.", "response.content_part."))}
    assert msg_ids == {conv.msg_id}, msg_ids
    rs = [e for e in evts if e["type"].startswith("response.reasoning_summary_text.")]
    assert rs and all(e["item_id"] == conv._reasoning_item_id for e in rs)
    fc = [e for e in evts if e["type"].startswith("response.function_call_arguments.")]
    fc_ids = {e.get("item_id") for e in fc}
    assert len(fc_ids) == 1 and None not in fc_ids and next(iter(fc_ids)).startswith("fc_")

    print("✅ test_stream_events_carry_sequence_and_item_ids")


def test_usage_maps_cached_tokens_and_omits_when_unknown():
    """Preserve known cache counters and omit details when upstream counters are absent."""
    conv = ResponsesStreamConverter(model="m")
    conv.feed_line('data: {"id":"u1","choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}')
    conv.feed_line('data: {"id":"u1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":1,"total_tokens":10,"prompt_tokens_details":{"cached_tokens":7}}}')
    assert conv.get_nonstream_response()["usage"]["input_tokens_details"] == {"cached_tokens": 7}

    conv = ResponsesStreamConverter(model="m")
    conv.feed_line('data: {"id":"u2","choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}')
    conv.feed_line('data: {"id":"u2","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":1,"total_tokens":10,"cache_read_input_tokens":3}}')
    assert conv.get_nonstream_response()["usage"]["input_tokens_details"] == {"cached_tokens": 3}

    conv = ResponsesStreamConverter(model="m")
    conv.feed_line('data: {"id":"u3","choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}')
    conv.feed_line('data: {"id":"u3","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":1,"total_tokens":10}}')
    assert "input_tokens_details" not in conv.get_nonstream_response()["usage"]

    print("✅ test_usage_maps_cached_tokens_and_omits_when_unknown")


def test_reasoning_effort_and_text_format_are_mapped():
    """Map reasoning and text format options with explicit top-level precedence."""
    chat = responses_request_to_chat({"input": "hi", "reasoning": {"effort": "high"},
                                      "text": {"format": {"type": "json_object"}}})
    assert chat["reasoning_effort"] == "high"
    assert chat["response_format"] == {"type": "json_object"}

    chat = responses_request_to_chat({"input": "hi", "reasoning_effort": "low",
                                      "reasoning": {"effort": "high"}})
    assert chat["reasoning_effort"] == "low"  # Explicit top-level fields take precedence.

    chat = responses_request_to_chat({"input": "hi", "text": {"format": {
        "type": "json_schema", "name": "answer", "strict": True,
        "schema": {"type": "object", "properties": {"a": {"type": "integer"}}}}}})
    fmt = chat["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "answer" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["properties"]["a"]["type"] == "integer"

    chat = responses_request_to_chat({"input": "hi", "text": {"format": {"type": "text"}}})
    assert "response_format" not in chat

    try:
        responses_request_to_chat({"input": "hi", "text": {"format": {"type": "xml"}}})
        raise AssertionError("unsupported text.format must raise")
    except ValueError:
        pass

    print("✅ test_reasoning_effort_and_text_format_are_mapped")

def test_parallel_tool_calls_roundtrip():
    """Preserve the requested parallel_tool_calls setting in upstream and response objects."""
    chat = responses_request_to_chat({"input": "hi", "parallel_tool_calls": False})
    assert chat["parallel_tool_calls"] is False
    conv = ResponsesStreamConverter(model="m", parallel_tool_calls=False)
    conv.feed_line('data: {"id":"p1","choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}')
    conv.feed_line('data: {"id":"p1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    assert conv.get_nonstream_response()["parallel_tool_calls"] is False
    conv = ResponsesStreamConverter(model="m")
    conv.feed_line('data: {"id":"p2","choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}')
    conv.feed_line('data: {"id":"p2","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    assert conv.get_nonstream_response()["parallel_tool_calls"] is True
    print("✅ test_parallel_tool_calls_roundtrip")


if __name__ == "__main__":
    test_simple_text_request()
    test_array_input_request()
    test_tools_conversion()
    test_max_output_tokens()
    test_developer_role()
    test_typed_developer_message_request()
    test_desensitize_harness_user_and_tools()
    test_compact_harness_messages_and_strip_tool_metadata()
    test_no_compact_still_prunes_codex_runtime_metadata()
    test_responses_projection_compacts_codex_harness_and_tools()
    test_responses_projection_preserves_recent_tool_chain_and_summarizes_history()
    test_responses_projection_shrinks_large_tool_arguments()
    test_responses_projection_keeps_user_text_sharing_harness_message()
    test_responses_projection_keeps_last_user_when_it_carries_harness()
    test_stream_converter_text()
    test_stream_converter_function_call()
    test_nonstream_response()
    test_finish_reason_maps_to_terminal_status()
    test_stream_events_carry_sequence_and_item_ids()
    test_usage_maps_cached_tokens_and_omits_when_unknown()
    test_reasoning_effort_and_text_format_are_mapped()
    test_parallel_tool_calls_roundtrip()
    print(f"\n🎉 All {22} tests passed!")
