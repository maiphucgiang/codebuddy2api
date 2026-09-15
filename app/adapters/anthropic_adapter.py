"""Translate requests and SSE responses between Anthropic Messages and OpenAI Chat."""

from __future__ import annotations

import json
import os
import time
from typing import Any

# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _rand_id(prefix: str = "") -> str:
    return prefix + os.urandom(12).hex()


def map_usage_to_anthropic(u: dict) -> dict:
    """Map Chat usage to Anthropic counters, subtracting cache reads from input tokens."""
    cached = (u.get("cache_read_input_tokens")
              or (u.get("prompt_tokens_details") or {}).get("cached_tokens")
              or 0)
    return {
        "input_tokens": max(u.get("prompt_tokens", 0) - cached, 0),
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens") or 0,
        "cache_read_input_tokens": cached,
        "output_tokens": u.get("completion_tokens", 0),
    }


# ---------------------------------------------------------------------------
# Anthropic to Chat requests
# ---------------------------------------------------------------------------

def anthropic_request_to_chat(body: dict) -> dict:
    """Convert Anthropic instructions, message blocks and tools to a Chat request."""
    messages: list[dict] = []

    # Place system instructions first.
    system = body.get("system")
    if system:
        sys_content = _extract_system_text(system)
        if sys_content:
            messages.append({"role": "system", "content": sys_content})

    # Convert message content.
    for m in body.get("messages", []):
        if not isinstance(m, dict):
            continue
        messages.extend(_convert_anthropic_message(m))

    chat: dict[str, Any] = {"messages": messages, "stream": True}

    # Preserve the requested model.
    if "model" in body:
        chat["model"] = body["model"]

    # max_tokens
    if "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]

    # tools
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_anthropic_tools(tools)

    if "tool_choice" in body:
        tc = body["tool_choice"]
        if isinstance(tc, dict):
            kind = tc.get("type")
            if kind == "tool":
                chat["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
            elif kind in ("auto", "none", "any"):
                chat["tool_choice"] = "required" if kind == "any" else kind
            else:
                raise ValueError("unsupported tool_choice type")
            # Preserve the caller's restriction on parallel tool calls.
            if isinstance(tc.get("disable_parallel_tool_use"), bool):
                chat["parallel_tool_calls"] = not tc["disable_parallel_tool_use"]
        elif isinstance(tc, str):
            chat["tool_choice"] = tc if tc in ("none", "auto", "required") else {"type": "function", "function": {"name": tc}}
    # Forward supported parameters.
    for key in ("temperature", "top_p", "stop", "top_k"):
        if key in body:
            chat[key] = body[key]
    # Explicit stop takes precedence over Anthropic stop_sequences.
    stop_sequences = body.get("stop_sequences")
    if stop_sequences is not None and "stop" not in chat:
        if not isinstance(stop_sequences, list) or not all(isinstance(s, str) for s in stop_sequences):
            raise ValueError("stop_sequences must be an array of strings")
        chat["stop"] = stop_sequences

    return chat


def _extract_system_text(system) -> str:
    """Extract plain system text from a string or text-block array."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _convert_anthropic_message(msg: dict) -> list[dict]:
    """Convert one Anthropic message into one or more Chat messages."""
    role = msg.get("role", "")
    content = msg.get("content")

    # Plain text content
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    # Empty content
    if not isinstance(content, list) or not content:
        return []

    # Structured content blocks
    blocks = content

    # User messages may contain tool results.
    if role == "user":
        result: list[dict] = []
        user_blocks: list[dict] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt in ("text", "image"):
                user_blocks.append(block)
            elif bt == "tool_result":
                # Emit each tool result as a separate tool message.
                tc_id = block.get("tool_use_id", "")
                output = block.get("content", "")
                if isinstance(output, list):
                    output = _convert_content_blocks(output)
                if block.get("is_error") is True:
                    # Encode tool failure as text while preserving image blocks.
                    if isinstance(output, list):
                        output = [{"type": "text", "text": "[tool execution failed]"}] + output
                    else:
                        output = "[tool execution failed]\n" + (output or "")
                result.append({"role": "tool", "tool_call_id": tc_id, "content": output})
        if user_blocks:
            # Tool results must immediately follow their assistant tool calls.
            result.append({"role": "user", "content": _convert_content_blocks(user_blocks)})
        return result

    # Assistant content
    if role == "assistant":
        content_out = _convert_content_blocks(blocks)
        tool_calls: list[dict] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "tool_use":
                tc = {
                    "id": block.get("id", _rand_id("call_")),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
                tool_calls.append(tc)
        msg_out: dict[str, Any] = {"role": "assistant"}
        has_text = any(isinstance(block, dict) and block.get("type") == "text" for block in blocks)
        msg_out["content"] = content_out if content_out or has_text else None
        if tool_calls:
            msg_out["tool_calls"] = tool_calls
        return [msg_out]

    content_out = _convert_content_blocks(blocks)
    return [{"role": role, "content": content_out}] if content_out else []


def _convert_content_blocks(blocks: list) -> str | list[dict]:
    """Preserve image/text order and use strings for text-only content."""
    parts = []
    has_image = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image":
            source = block.get("source")
            if not isinstance(source, dict):
                raise ValueError("Anthropic image requires a URL or base64 source")
            if source.get("type") == "url":
                url = source.get("url")
                if not isinstance(url, str) or not url:
                    raise ValueError("Anthropic image URL source requires a non-empty url")
            elif source.get("type") == "base64":
                media_type = source.get("media_type")
                data = source.get("data")
                if (not isinstance(media_type, str) or not media_type.startswith("image/")
                        or not isinstance(data, str) or not data):
                    raise ValueError("Anthropic base64 image requires image media_type and data")
                url = f"data:{media_type};base64,{data}"
            else:
                raise ValueError("Unsupported Anthropic image source; only URL and base64 are supported")
            parts.append({"type": "image_url", "image_url": {"url": url}})
            has_image = True
    return parts if has_image else "".join(part["text"] for part in parts)


def _convert_anthropic_tools(tools: list) -> list:
    """Convert Anthropic tool definitions to Chat function objects."""
    result = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Already in Chat format.
        if "function" in t:
            result.append(t)
            continue
        fn: dict[str, Any] = {"name": t.get("name", "")}
        if "description" in t:
            fn["description"] = t["description"]
        if "input_schema" in t:
            fn["parameters"] = t["input_schema"]
        result.append({"type": "function", "function": fn})
    return result


# ---------------------------------------------------------------------------
# Chat SSE to Anthropic Messages events
# ---------------------------------------------------------------------------

class AnthropicStreamConverter:
    """Convert Chat SSE increments to Anthropic Messages events."""

    def __init__(self, model: str = "unknown"):
        self.msg_id = _rand_id("msg_")
        self.model = model
        self.created_at = int(time.time())

        # Stream state
        self._emitted_start = False

        # Text block state
        self._text_content = ""
        self._text_block_open = False
        self._text_block_idx = 0

        # Map reasoning_content to thinking blocks.
        self._thinking_content = ""
        self._thinking_block_open = False
        self._thinking_block_idx = 0

        # Tool blocks indexed by upstream call position.
        self._tool_uses: dict[int, dict] = {}
        self._next_block_idx = 0

        # Completion metadata
        self._finish_reason: str | None = None
        self._usage: dict | None = None
        self._content_filter: bool = False

    # Public methods

    def feed_line(self, line: str) -> str:
        """Convert one SSE line into Anthropic event text."""
        line = line.strip()
        if not line or not line.startswith("data:"):
            return ""
        data = line[5:].strip()
        if data == "[DONE]":
            return ""
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return ""
        return self._process_chunk(chunk)

    def finish(self) -> str:
        """Emit final events and close the message."""
        events: list[str] = []

        # Close thinking blocks.
        if self._thinking_block_open:
            events.append(self._evt(
                "content_block_stop", {"index": self._thinking_block_idx}
            ))
            self._thinking_block_open = False

        # Close text blocks.
        if self._text_block_open:
            events.append(self._evt(
                "content_block_stop", {"index": self._text_block_idx}
            ))
            self._text_block_open = False

        # Close tool blocks.
        for tc in self._tool_uses.values():
            if tc.get("open"):
                events.append(self._evt(
                    "content_block_stop", {"index": tc["block_idx"]}
                ))
                tc["open"] = False

        # Map the finish reason.
        sr = self._finish_reason or "stop"
        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_map.get(sr, "end_turn")

        # message_delta
        delta: dict[str, str | None] = {"stop_reason": stop_reason, "stop_sequence": None}
        usage = None
        if self._usage:
            usage = map_usage_to_anthropic(self._usage)
        events.append(self._evt("message_delta", {"delta": delta, "usage": usage}))

        # message_stop
        events.append(self._evt("message_stop", {}))

        return "".join(events)

    def get_nonstream_response(self) -> dict:
        """Return the complete non-streaming Message response."""
        content = self._build_content_blocks()
        sr = self._finish_reason or "stop"
        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_map.get(sr, "end_turn")

        resp: dict[str, Any] = {
            "id": self.msg_id,
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": self.model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
        }
        if self._usage:
            resp["usage"] = map_usage_to_anthropic(self._usage)
        return resp

    # Internal helpers

    def _process_chunk(self, chunk: dict) -> str:
        events: list[str] = []

        if chunk.get("model"):
            self.model = chunk["model"]

        # Emit message_start once.
        if not self._emitted_start:
            events.append(self._evt("message_start", {
                "message": {
                    "id": self.msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            }))
            self._emitted_start = True

        if chunk.get("usage"):
            self._usage = chunk["usage"]

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")

            # Emit reasoning before text.
            thinking = delta.get("reasoning_content")
            if thinking:
                self._thinking_content += thinking
                if not self._thinking_block_open:
                    self._thinking_block_idx = self._next_block_idx
                    self._next_block_idx += 1
                    events.append(self._evt("content_block_start", {
                        "index": self._thinking_block_idx,
                        "content_block": {"type": "thinking", "thinking": ""},
                    }))
                    self._thinking_block_open = True
                events.append(self._evt("content_block_delta", {
                    "index": self._thinking_block_idx,
                    "delta": {"type": "thinking_delta", "thinking": thinking},
                }))

            # Anthropic has no refusal block; preserve refusal text as ordinary content.
            content = (delta.get("content") or "") + (delta.get("refusal") or "")
            if content:
                # Close thinking before text begins.
                if self._thinking_block_open:
                    events.append(self._evt("content_block_stop", {
                        "index": self._thinking_block_idx
                    }))
                    self._thinking_block_open = False
                self._text_content += content
                if not self._text_block_open:
                    self._text_block_idx = self._next_block_idx
                    self._next_block_idx += 1
                    events.append(self._evt("content_block_start", {
                        "index": self._text_block_idx,
                        "content_block": {"type": "text", "text": ""},
                    }))
                    self._text_block_open = True
                events.append(self._evt("content_block_delta", {
                    "index": self._text_block_idx,
                    "delta": {"type": "text_delta", "text": content},
                }))

            # tool_calls delta
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in self._tool_uses:
                    block_idx = self._next_block_idx
                    self._next_block_idx += 1
                    self._tool_uses[idx] = {
                        "id": tc.get("id", ""),
                        "name": "",
                        "args": "",
                        "block_idx": block_idx,
                        "open": False,
                    }
                slot = self._tool_uses[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]

                if not slot["open"]:
                    # Close thinking before a tool block begins.
                    if self._thinking_block_open:
                        events.append(self._evt("content_block_stop", {
                            "index": self._thinking_block_idx
                        }))
                        self._thinking_block_open = False
                    events.append(self._evt("content_block_start", {
                        "index": slot["block_idx"],
                        "content_block": {"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": {}},
                    }))
                    slot["open"] = True

                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    events.append(self._evt("content_block_delta", {
                        "index": slot["block_idx"],
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                    }))

            if finish:
                self._finish_reason = finish

                # Close open blocks when the upstream finishes.
                if self._thinking_block_open:
                    events.append(self._evt("content_block_stop", {
                        "index": self._thinking_block_idx
                    }))
                    self._thinking_block_open = False

                if self._text_block_open:
                    events.append(self._evt("content_block_stop", {
                        "index": self._text_block_idx
                    }))
                    self._text_block_open = False

                for tc in self._tool_uses.values():
                    if tc.get("open"):
                        events.append(self._evt("content_block_stop", {
                            "index": tc["block_idx"]
                        }))
                        tc["open"] = False

        return "".join(events)

    def _evt(self, event_type: str, data: dict) -> str:
        """Format an Anthropic SSE event with its event name."""
        payload = {"type": event_type, **data}
        return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _build_content_blocks(self) -> list[dict]:
        """Build content blocks for a non-streaming response."""
        blocks: list[dict] = []

        # Thinking must precede text.
        if self._thinking_content:
            blocks.append({"type": "thinking", "thinking": self._thinking_content})

        # text block
        if self._text_content or self._text_block_open:
            blocks.append({"type": "text", "text": self._text_content})

        # tool_use blocks
        for _, tc in sorted(self._tool_uses.items()):
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": {},
            }
            # Parse tool arguments as a JSON object.
            try:
                block["input"] = json.loads(tc["args"])
            except (json.JSONDecodeError, ValueError):
                block["input"] = tc["args"]
            blocks.append(block)

        return blocks
