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

def test_function_call_arguments_must_be_string():
    """Reject non-standard object arguments before Chat adaptation."""
    try:
        responses_request_to_chat({"model": "auto", "input": [
            {"type": "function_call", "name": "tool", "arguments": {"x": 1}},
        ]})
    except ValueError as error:
        assert "JSON string" in str(error)
    else:
        raise AssertionError("non-string function call arguments were accepted")


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


def test_responses_projection_balanced_preserves_real_text_tools_and_structure():
    """Balanced mode replaces recognized harness blocks without changing tools or real text."""
    tool = {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "Run a command",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string", "description": "Command"}},
                "required": ["cmd"],
                "additionalProperties": False,
                "x-vendor-detail": {"deep": {"schema": "kept"}},
            },
            "strict": False,
        },
    }
    body = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": "Repository policy: run tests."},
            {"role": "user", "content": "# AGENTS.md instructions\n<environment_context>\nvolatile context\n</environment_context>"},
            {"role": "user", "content": "实现该方案"},
        ],
        "tools": [tool],
    }
    before = json.loads(json.dumps(body, ensure_ascii=False))
    out, stats = project_responses_chat_body(body)
    assert body == before
    assert out["tools"] == body["tools"]
    assert out["messages"][0] == body["messages"][0]
    assert out["messages"][1]["content"] != body["messages"][1]["content"]
    assert "# AGENTS.md instructions" not in out["messages"][1]["content"]
    assert "Environment context is provided by the harness." in out["messages"][1]["content"]
    assert out["messages"][2] == body["messages"][2]
    assert stats["mode"] == "balanced"
    assert stats["original_tools"] == stats["projected_tools"] == 1
    assert stats["original_tool_chars"] == stats["projected_tool_chars"]
    assert stats["harness_messages_projected"] == 1
    print("✅ test_responses_projection_balanced_preserves_real_text_tools_and_structure")


def test_responses_projection_preserves_history_and_tool_chain():
    """Balanced mode does not summarize history or drop tool-call relationships."""
    body = {
        "messages": [
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "old answer", "tool_calls": [{
                "id": "call_old", "type": "function",
                "function": {"name": "exec_command", "arguments": '{"cmd":"ls"}'},
            }]},
            {"role": "tool", "tool_call_id": "call_old", "content": "old output"},
            {"role": "user", "content": "new task"},
        ],
        "tools": [{"type": "function", "function": {"name": "exec_command", "parameters": {"type": "object"}}}],
    }
    before = json.loads(json.dumps(body))
    out, stats = project_responses_chat_body(body, max_item_bytes=40000)
    assert body == before
    assert out["messages"] == body["messages"]
    assert stats["original_messages"] == stats["projected_messages"] == 4
    assert "anchor_user_preserved" not in stats
    print("✅ test_responses_projection_preserves_history_and_tool_chain")


def test_responses_projection_truncates_generated_content_and_json_arguments():
    """Oversized assistant, tool output, and JSON values retain UTF-8 head and tail."""
    long_text = "HEAD\n" + ("中" * 180) + "\nTAIL"
    long_output = "OUTPUT\n" + ("输出" * 180) + "\nEND"
    arguments = json.dumps({"cmd": "echo " + ("x" * 500), "workdir": "/tmp"})
    apply_patch = json.dumps({"patch": "*** Begin Patch\n" + ("+" * 500) + "*** End Patch"})
    body = {"messages": [
        {"role": "assistant", "content": long_text, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "exec_command", "arguments": arguments},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": long_output},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_patch", "type": "function",
            "function": {"name": "apply_patch", "arguments": apply_patch},
        }]},
    ]}
    before = json.loads(json.dumps(body, ensure_ascii=False))
    out, stats = project_responses_chat_body(body, max_item_bytes=256)
    assert body == before
    assistant = out["messages"][0]["content"]
    assert assistant.startswith("HEAD")
    assert assistant.endswith("TAIL")
    assert "middle omitted" in assistant
    assert "original bytes:" in assistant
    assert "estimated tokens:" in assistant
    assert "total lines:" in assistant
    tool_output = out["messages"][1]["content"]
    assert tool_output.startswith("OUTPUT")
    assert tool_output.endswith("END")
    args_wire = out["messages"][0]["tool_calls"][0]["function"]["arguments"]
    args = json.loads(args_wire)
    assert len(args_wire.encode("utf-8")) <= 256
    assert "middle omitted" in args["_truncated"]["warning"]
    assert '"cmd"' in args["head"] and "echo" in args["head"]
    assert "workdir" in args["tail"] and args["tail"].endswith("}")
    patch_wire = out["messages"][2]["tool_calls"][0]["function"]["arguments"]
    patch_args = json.loads(patch_wire)
    assert len(patch_wire.encode("utf-8")) <= 256
    assert '"patch"' in patch_args["head"] and "*** Begin Patch" in patch_args["head"]
    assert patch_args["tail"].endswith('*** End Patch"}')
    assert stats["truncated_items"] == 4
    assert stats["truncated_original_bytes"] > stats["truncated_projected_bytes"]
    print("✅ test_responses_projection_truncates_generated_content_and_json_arguments")


def test_responses_projection_passthrough_and_zero_limit_are_lossless():
    """Passthrough and max_item_bytes=0 preserve the request payload."""
    body = {"messages": [
        {"role": "user", "content": "# AGENTS.md instructions\n<environment_context>volatile</environment_context>\nreal task"},
        {"role": "assistant", "content": "assistant " + "x" * 500},
        {"role": "tool", "tool_call_id": "c", "content": "output " + "y" * 500},
    ], "tools": [{"type": "function", "function": {"name": "tool", "parameters": {"type": "object", "x": 1}}}]}
    before = json.loads(json.dumps(body, ensure_ascii=False))
    for kwargs in ({"mode": "passthrough"}, {"max_item_bytes": 0}):
        out, stats = project_responses_chat_body(body, **kwargs)
        if "mode" in kwargs:
            assert out == before
        else:
            assert out["messages"][1:] == before["messages"][1:]
            assert out["tools"] == before["tools"]
        assert body == before
        assert stats["mode"] == ("passthrough" if "mode" in kwargs else "balanced")
        assert stats["truncated_items"] == 0
    print("✅ test_responses_projection_passthrough_and_zero_limit_are_lossless")


def test_responses_projection_rejects_invalid_mode_and_limit():
    """Projection validates its public mode and byte-limit arguments."""
    for mode in ("aggressive", "conservative", "", None, True):
        try:
            project_responses_chat_body({"messages": []}, mode=mode)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid mode accepted: {mode!r}")
    for limit in (-1, 1, 128, 255, True, 1.5, "256", None, [], {}):
        try:
            project_responses_chat_body({"messages": []}, max_item_bytes=limit)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid max_item_bytes accepted: {limit!r}")
    assert project_responses_chat_body({"messages": []}, mode="balanced", max_item_bytes=256)[1]["max_item_bytes"] == 256
    print("✅ test_responses_projection_rejects_invalid_mode_and_limit")


def test_responses_projection_stats_have_official_shape():
    """Stats expose current projection counters without legacy mode fields."""
    body = {"messages": [{"role": "user", "content": "hello"}], "tools": [{"type": "function", "function": {"name": "t"}}]}
    _, stats = project_responses_chat_body(body, mode="balanced", max_item_bytes=0)
    assert stats == {
        "mode": "balanced", "max_item_bytes": 0, "original_messages": 1,
        "projected_messages": 1, "original_message_chars": stats["original_message_chars"],
        "projected_message_chars": stats["projected_message_chars"], "original_tools": 1,
        "projected_tools": 1, "original_tool_chars": stats["original_tool_chars"],
        "projected_tool_chars": stats["projected_tool_chars"], "harness_messages_projected": 0,
        "truncated_items": 0, "truncated_original_bytes": 0, "truncated_projected_bytes": 0,
    }
    print("✅ test_responses_projection_stats_have_official_shape")


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


def test_tool_registry_and_identity_mapping():
    """Verify ToolRegistry handles plain tools, namespaced tools, collision avoidance, and reversible lookup."""
    from app.adapters.responses_adapter import ToolRegistry

    registry = ToolRegistry()
    # 1. Plain tool: must keep original name
    name1 = registry.register(None, "collaboration__spawn_agent")
    assert name1 == "collaboration__spawn_agent"
    assert registry.get_identity("collaboration__spawn_agent") == (None, "collaboration__spawn_agent")

    # 2. Namespaced tool colliding with existing plain tool: must disambiguate safely
    name2 = registry.register("collaboration", "spawn_agent")
    assert name2 == "collaboration__spawn_agent_1"
    assert registry.get_identity("collaboration__spawn_agent_1") == ("collaboration", "spawn_agent")
    # Plain tool lookup still returns original identity
    assert registry.get_identity("collaboration__spawn_agent") == (None, "collaboration__spawn_agent")

    # 3. Tool with __ in name inside namespace
    name3 = registry.register("custom_ns", "exec__command")
    assert name3 == "custom_ns__exec__command"
    assert registry.get_identity("custom_ns__exec__command") == ("custom_ns", "exec__command")

    # 4. Tools with same name in different namespaces
    name_ns1 = registry.register("ns1", "search")
    name_ns2 = registry.register("ns2", "search")
    assert name_ns1 == "ns1__search"
    assert name_ns2 == "ns2__search"
    assert registry.get_identity("ns1__search") == ("ns1", "search")
    assert registry.get_identity("ns2__search") == ("ns2", "search")

    # 5. Serialization and round-trip
    dumped = registry.to_dict()
    restored = ToolRegistry.from_dict(dumped)
    assert restored.get_identity("custom_ns__exec__command") == ("custom_ns", "exec__command")
    assert restored.get_identity("collaboration__spawn_agent") == (None, "collaboration__spawn_agent")
    assert restored.get_identity("collaboration__spawn_agent_1") == ("collaboration", "spawn_agent")
    print("✅ test_tool_registry_and_identity_mapping")


def test_responses_multiagent_request_conversion_and_schema_sanitization():
    """Verify responses_request_to_chat converts tools, strips encrypted markers, and maps tool_choice and history."""
    req = {
        "model": "gpt-4o",
        "tools": [
            {
                "type": "function",
                "name": "collaboration__spawn_agent",
                "description": "Standard unnamespaced tool",
                "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
            },
            {
                "type": "namespace",
                "name": "collaboration",
                "tools": [
                    {
                        "type": "function",
                        "name": "spawn_agent",
                        "description": "Spawn an agent",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {"type": "string", "encrypted": True},
                                "task_name": {"type": "string"},
                                "meta": {
                                    "type": "object",
                                    "properties": {
                                        "secret": {"type": "string", "encrypted": True},
                                        "public": {"type": "string"},
                                    },
                                },
                            },
                        },
                    }
                ],
            },
            {
                "type": "namespace",
                "name": "ns1",
                "tools": [{"type": "function", "name": "search", "parameters": {"type": "object"}}],
            },
            {
                "type": "namespace",
                "name": "ns2",
                "tools": [{"type": "function", "name": "search", "parameters": {"type": "object"}}],
            },
            {
                "type": "namespace",
                "name": "custom_ns",
                "tools": [{"type": "function", "name": "exec__command", "parameters": {"type": "object"}}],
            },
        ],
        "tool_choice": {"type": "function", "name": "spawn_agent", "namespace": "collaboration"},
        "input": [
            {
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "extra",
                        "tools": [{"type": "function", "name": "ping", "parameters": {"type": "object"}}],
                    }
                ],
            },
            {
                "type": "agent_message",
                "author": "/root",
                "recipient": "/root/worker",
                "content": [
                    {"type": "input_text", "text": "Task header\n"},
                    {"type": "encrypted_content", "encrypted_content": "Sensitive payload text"},
                ],
            },
            {
                "type": "function_call",
                "call_id": "call_hist_1",
                "name": "spawn_agent",
                "namespace": "collaboration",
                "arguments": '{"task_name": "t1"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_hist_1",
                "output": "ok",
            },
        ],
    }

    chat = responses_request_to_chat(req)
    chat_tools = chat.get("tools", [])
    tool_names = [t["function"]["name"] for t in chat_tools]

    # 1. Verify plain tool preserved original name
    assert "collaboration__spawn_agent" in tool_names
    # 2. Verify namespaced tool disambiguated
    assert "collaboration__spawn_agent_1" in tool_names
    # 3. Verify same name under different namespaces preserved
    assert "ns1__search" in tool_names
    assert "ns2__search" in tool_names
    # 4. Verify tool with __ in name inside namespace
    assert "custom_ns__exec__command" in tool_names
    # 5. Verify additional_tools registered
    assert "extra__ping" in tool_names

    # 6. Verify parameter schema cleaning (no 'encrypted' key anywhere)
    collab_tool = next(t for t in chat_tools if t["function"]["name"] == "collaboration__spawn_agent_1")
    props = collab_tool["function"]["parameters"]["properties"]
    assert "encrypted" not in props["message"]
    assert "encrypted" not in props["meta"]["properties"]["secret"]

    # 7. Verify tool_choice mapped
    assert chat.get("tool_choice") == {"type": "function", "function": {"name": "collaboration__spawn_agent_1"}}

    # 8. Verify messages conversion
    messages = chat.get("messages", [])
    # User message from agent_message with encrypted_content
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "Task header\n" in user_msg["content"]
    assert "Sensitive payload text" in user_msg["content"]

    # Assistant message from function_call
    asst_msg = next(m for m in messages if m["role"] == "assistant")
    assert asst_msg["tool_calls"][0]["function"]["name"] == "collaboration__spawn_agent_1"

    # 9. Verify stream converter output reconstruction
    conv = ResponsesStreamConverter(model="test", tool_registry=chat.get("_tool_registry"))

    # Namespaced tool call
    slot_collab = {"id": "c1", "name": "collaboration__spawn_agent_1", "args": "{}", "fc_id": "fc_1", "output_idx": 0, "emitted": False, "emitted_args_length": 0}
    item_collab = conv._fc_item(slot_collab, "completed")
    assert item_collab["name"] == "spawn_agent"
    assert item_collab["namespace"] == "collaboration"

    # Plain tool call
    slot_plain = {"id": "c2", "name": "collaboration__spawn_agent", "args": "{}", "fc_id": "fc_2", "output_idx": 1, "emitted": False, "emitted_args_length": 0}
    item_plain = conv._fc_item(slot_plain, "completed")
    assert item_plain["name"] == "collaboration__spawn_agent"
    assert "namespace" not in item_plain

    # Custom namespace with __ in name
    slot_custom = {"id": "c3", "name": "custom_ns__exec__command", "args": "{}", "fc_id": "fc_3", "output_idx": 2, "emitted": False, "emitted_args_length": 0}
    item_custom = conv._fc_item(slot_custom, "completed")
    assert item_custom["name"] == "exec__command"
    assert item_custom["namespace"] == "custom_ns"

    print("✅ test_responses_multiagent_request_conversion_and_schema_sanitization")


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
    test_responses_projection_balanced_preserves_real_text_tools_and_structure()
    test_responses_projection_preserves_history_and_tool_chain()
    test_responses_projection_truncates_generated_content_and_json_arguments()
    test_responses_projection_passthrough_and_zero_limit_are_lossless()
    test_responses_projection_rejects_invalid_mode_and_limit()
    test_responses_projection_stats_have_official_shape()
    test_stream_converter_text()
    test_stream_converter_function_call()
    test_nonstream_response()
    test_finish_reason_maps_to_terminal_status()
    test_stream_events_carry_sequence_and_item_ids()
    test_usage_maps_cached_tokens_and_omits_when_unknown()
    test_reasoning_effort_and_text_format_are_mapped()
    test_parallel_tool_calls_roundtrip()
    test_tool_registry_and_identity_mapping()
    test_responses_multiagent_request_conversion_and_schema_sanitization()
    print(f"\n🎉 All {22} tests passed!")
