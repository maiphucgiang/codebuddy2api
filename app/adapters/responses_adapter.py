"""Translate requests and SSE responses between OpenAI Responses and Chat Completions."""

from __future__ import annotations

import json
import os
import time
from typing import Any

# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _rand_id(prefix: str = "resp_") -> str:
    return prefix + os.urandom(12).hex()

# ---------------------------------------------------------------------------
# Responses to Chat requests
# ---------------------------------------------------------------------------

def _text_format_to_response_format(fmt) -> dict | None:
    """Map Responses text.format to equivalent Chat formats, rejecting unsupported variants."""
    if fmt is None:
        return None
    if not isinstance(fmt, dict):
        raise ValueError("text.format must be an object")
    kind = fmt.get("type")
    if kind in (None, "text"):
        return None
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        schema = fmt.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("text.format json_schema requires a schema object")
        js: dict[str, Any] = {"name": fmt.get("name") or "response", "schema": schema}
        if "strict" in fmt:
            js["strict"] = bool(fmt["strict"])
        if isinstance(fmt.get("description"), str):
            js["description"] = fmt["description"]
        return {"type": "json_schema", "json_schema": js}
    raise ValueError(f"unsupported text.format type: {kind}")


def responses_request_to_chat(body: dict) -> dict:
    """Convert Responses input, instructions and tools to a Chat request."""
    messages: list[dict] = []

    # instructions → system message
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # input → messages
    inp = body.get("input", [])
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        messages.extend(_convert_input_items(inp))

    # Build the Chat request body.
    chat: dict[str, Any] = {"messages": messages, "stream": True}

    # model
    if "model" in body:
        chat["model"] = body["model"]

    # Normalize function tool definitions.
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_tools_for_chat(tools)
    if "tool_choice" in body:
        chat["tool_choice"] = body["tool_choice"]

    # Forward supported parameters.
    for key in ("temperature", "top_p", "stop", "seed",
                "presence_penalty", "frequency_penalty",
                "response_format", "reasoning_effort", "parallel_tool_calls", "prompt_cache_key"):
        if key in body:
            chat[key] = body[key]

    # Explicit top-level values override equivalent nested fields.
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and "reasoning_effort" not in chat:
        effort = reasoning.get("effort")
        if isinstance(effort, str) and effort.strip():
            chat["reasoning_effort"] = effort
    text = body.get("text")
    if isinstance(text, dict) and "response_format" not in chat:
        mapped = _text_format_to_response_format(text.get("format"))
        if mapped is not None:
            chat["response_format"] = mapped
    # max_output_tokens → max_tokens
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    elif "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]

    return chat


def _convert_input_items(items: list) -> list[dict]:
    """Convert input items and merge adjacent assistant messages with tool calls."""
    messages: list[dict] = []
    # Buffer adjacent assistant text and function calls.
    pending_assistant_content: str | list[dict] | None = None
    pending_tool_calls: list[dict] = []

    def _flush_assistant():
        nonlocal pending_assistant_content, pending_tool_calls
        if pending_assistant_content is not None or pending_tool_calls:
            msg: dict[str, Any] = {"role": "assistant",
                                   "content": pending_assistant_content or ""}
            if pending_tool_calls:
                msg["tool_calls"] = pending_tool_calls[:]
            messages.append(msg)
            pending_assistant_content = None
            pending_tool_calls.clear()

    for item in items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        role = item.get("role", "")

        # Untyped role messages
        if item_type is None and role in ("user", "system", "developer"):
            _flush_assistant()
            mapped_role = "system" if role == "developer" else role
            content = _extract_content(item.get("content", ""))
            messages.append({"role": mapped_role, "content": content})
            continue

        # Typed message items
        if item_type == "message" and role in ("user", "system", "developer"):
            _flush_assistant()
            mapped_role = "system" if role == "developer" else role
            content = _extract_content(item.get("content", ""))
            messages.append({"role": mapped_role, "content": content})
            continue

        # Assistant output from history
        if item_type == "message" and role == "assistant":
            _flush_assistant()
            content_parts = item.get("content", [])
            text = _extract_output_text(content_parts) if isinstance(content_parts, list) else str(content_parts)
            pending_assistant_content = text
            continue

        # Untyped assistant messages
        if item_type is None and role == "assistant":
            _flush_assistant()
            content = _extract_content(item.get("content", ""))
            pending_assistant_content = content
            continue

        # Merge calls into the preceding assistant message.
        if item_type == "function_call":
            if pending_assistant_content is None:
                pending_assistant_content = ""
            pending_tool_calls.append({
                "id": item.get("call_id", item.get("id", _rand_id("call_"))),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            })
            continue

        # Map function results to tool messages.
        if item_type == "function_call_output":
            _flush_assistant()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": (_extract_content(item["output"])
                            if isinstance(item.get("output"), list) else item.get("output", "")),
            })
            continue

        # Retain compatible message content from unknown item types.
        if role:
            _flush_assistant()
            content = _extract_content(item.get("content", ""))
            messages.append({"role": role, "content": content})

    _flush_assistant()
    return messages


def _extract_content(content) -> str | list[dict]:
    """Convert protocol blocks without stringifying image content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        has_image = False
        for p in content:
            if isinstance(p, dict):
                kind = p.get("type")
                if kind in ("input_text", "text", "output_text"):
                    parts.append({"type": "text", "text": p.get("text", "")})
                elif kind == "input_image":
                    if p.get("file_id"):
                        raise ValueError("Responses input_image file_id is not supported; provide image_url instead")
                    url = p.get("image_url")
                    if not isinstance(url, str) or not url:
                        raise ValueError("Responses input_image requires a non-empty image_url")
                    image_url = {"url": url}
                    if "detail" in p:
                        image_url["detail"] = p["detail"]
                    parts.append({"type": "image_url", "image_url": image_url})
                    has_image = True
                elif kind == "image_url":
                    parts.append(p)
                    has_image = True
            elif isinstance(p, str):
                parts.append({"type": "text", "text": p})
        if has_image:
            return parts
        return "".join(part["text"] for part in parts) or str(content)
    return str(content)


def _extract_output_text(content_parts: list) -> str | list[dict]:
    """Preserve historical images and extract output_text for text-only messages."""
    if any(isinstance(part, dict) and part.get("type") in ("input_image", "image_url")
           for part in content_parts):
        return _extract_content(content_parts)
    texts = []
    for part in content_parts:
        if isinstance(part, dict) and part.get("type") == "output_text":
            texts.append(part.get("text", ""))
    return "".join(texts)


def _convert_tools_for_chat(tools: list) -> list:
    """Convert Responses tool definitions to Chat function objects."""
    result = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") != "function":
            continue
        # Already in Chat format.
        if "function" in t:
            result.append(t)
            continue
        # Nest the flat Responses function fields.
        fn: dict[str, Any] = {"name": t.get("name", "")}
        if "description" in t:
            fn["description"] = t["description"]
        if "parameters" in t:
            fn["parameters"] = t["parameters"]
        if "strict" in t:
            fn["strict"] = t["strict"]
        result.append({"type": "function", "function": fn})
    return result


# ---------------------------------------------------------------------------
# Chat SSE to Responses events
# ---------------------------------------------------------------------------

class ResponsesStreamConverter:
    """Convert Chat SSE increments into Responses events."""

    def __init__(self, model: str = "unknown", parallel_tool_calls: bool = True):
        self.resp_id = _rand_id("resp_")
        self.msg_id = _rand_id("msg_")
        self.model = model
        self._parallel_tool_calls = bool(parallel_tool_calls)
        self.created_at = int(time.time())

        # Stream state
        self._emitted_created = False
        self._emitted_msg_item = False
        self._emitted_content_part = False

        # Collected content
        self._content = ""
        # Reasoning items precede message items.
        self._reasoning = ""
        self._reasoning_item_id = _rand_id("rs_")
        self._emitted_reasoning_item = False
        self._tool_calls: dict[int, dict] = {}  # index → {id, name, args, fc_id, output_idx, emitted}
        self._finish_reason: str | None = None
        self._usage: dict | None = None
        self._seq = 0  # Monotonic emitted-event sequence
    # Public methods

    def feed_line(self, line: str) -> str:
        """Convert one SSE line into Responses event text."""
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
        """Close output items and emit the terminal response status."""
        status, reason = self._final_status()
        events: list[str] = []

        # Close reasoning items.
        if self._emitted_reasoning_item:
            events.append(self._evt("response.reasoning_summary_text.done", {
                "output_index": 0, "summary_index": 0, "text": self._reasoning,
                "item_id": self._reasoning_item_id
            }))
            events.append(self._evt("response.output_item.done", {
                "output_index": 0, "item": self._reasoning_item(status)
            }))

        # Close text content.
        if self._emitted_content_part:
            events.append(self._evt("response.output_text.done", {
                "output_index": self._msg_idx(), "content_index": 0, "text": self._content,
                "item_id": self.msg_id
            }))
            events.append(self._evt("response.content_part.done", {
                "output_index": self._msg_idx(), "content_index": 0,
                "part": {"type": "output_text", "text": self._content, "annotations": []},
                "item_id": self.msg_id
            }))

        if self._emitted_msg_item:
            events.append(self._evt("response.output_item.done", {
                "output_index": self._msg_idx(),
                "item": self._msg_item(status)
            }))

        # Close function calls.
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            if tc.get("emitted"):
                oi = tc["output_idx"]
                events.append(self._evt("response.function_call_arguments.done", {
                    "output_index": oi, "arguments": tc["args"], "item_id": tc["fc_id"]
                }))
                events.append(self._evt("response.output_item.done", {
                    "output_index": oi, "item": self._fc_item(tc, status)
                }))

        # Truncated or filtered responses must not report completion.
        events.append(self._evt(f"response.{status}", {
            "response": self._response_obj(status, incomplete_reason=reason)
        }))
        return "".join(events)

    def get_nonstream_response(self) -> dict:
        """Return the complete non-streaming Response object."""
        status, reason = self._final_status()
        return self._response_obj(status, incomplete_reason=reason)

    def _final_status(self) -> tuple[str, str | None]:
        """Map finish reasons to response status without hiding truncation or filtering."""
        fr = self._finish_reason
        if fr in (None, "stop", "tool_calls"):
            return "completed", None
        if fr == "length":
            return "incomplete", "max_output_tokens"
        if fr in ("content_filter", "content-filter", "refusal"):
            return "incomplete", "content_filter"
        return "incomplete", None
    # Internal helpers

    def _process_chunk(self, chunk: dict) -> str:
        events: list[str] = []

        # Model identity
        if chunk.get("model"):
            self.model = chunk["model"]

        # Emit created and in_progress once.
        if not self._emitted_created:
            resp = self._response_obj("in_progress")
            events.append(self._evt("response.created", {"response": resp}))
            events.append(self._evt("response.in_progress", {"response": resp}))
            self._emitted_created = True

        # usage
        if chunk.get("usage"):
            self._usage = chunk["usage"]

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")

            # Emit reasoning before message content.
            reasoning = delta.get("reasoning_content")
            if reasoning:
                if not self._emitted_reasoning_item:
                    events.append(self._evt("response.output_item.added", {
                        "output_index": 0,
                        "item": {"type": "reasoning", "id": self._reasoning_item_id,
                                 "summary": [], "status": "in_progress"}
                    }))
                    self._emitted_reasoning_item = True
                self._reasoning += reasoning
                events.append(self._evt("response.reasoning_summary_text.delta", {
                    "output_index": 0, "summary_index": 0, "delta": reasoning,
                    "item_id": self._reasoning_item_id
                }))

            # Preserve refusal content as valid output_text.
            content = (delta.get("content") or "") + (delta.get("refusal") or "")
            if content:
                if not self._emitted_msg_item:
                    events.append(self._evt("response.output_item.added", {
                        "output_index": self._msg_idx(),
                        "item": self._msg_item("in_progress", empty=True)
                    }))
                    self._emitted_msg_item = True

                if not self._emitted_content_part:
                    events.append(self._evt("response.content_part.added", {
                        "output_index": self._msg_idx(), "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                        "item_id": self.msg_id
                    }))
                    self._emitted_content_part = True

                self._content += content
                events.append(self._evt("response.output_text.delta", {
                    "output_index": self._msg_idx(), "content_index": 0, "delta": content,
                    "item_id": self.msg_id
                }))

            # ---- tool_calls delta ----
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in self._tool_calls:
                    # Place function calls after reasoning and message items.
                    base = (1 if self._emitted_reasoning_item else 0) + \
                           (1 if (self._emitted_msg_item or self._content) else 0)
                    oi = base + len(self._tool_calls)
                    self._tool_calls[idx] = {
                        "id": tc.get("id", ""),
                        "name": "",
                        "args": "",
                        "fc_id": _rand_id("fc_"),
                        "output_idx": oi,
                        "emitted": False,
                    }
                slot = self._tool_calls[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]

                if not slot["emitted"]:
                    # Ensure the message item exists even when empty.
                    if not self._emitted_msg_item and (self._content or not self._tool_calls):
                        pass
                    events.append(self._evt("response.output_item.added", {
                        "output_index": slot["output_idx"],
                        "item": self._fc_item(slot, "in_progress")
                    }))
                    slot["emitted"] = True

                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    events.append(self._evt("response.function_call_arguments.delta", {
                        "output_index": slot["output_idx"],
                        "delta": fn["arguments"], "item_id": slot["fc_id"]
                    }))

            if finish:
                self._finish_reason = finish

        return "".join(events)

    def _evt(self, event_type: str, data: dict) -> str:
        """Format SSE events with monotonically increasing sequence numbers."""
        self._seq += 1
        payload = {"type": event_type, **data, "sequence_number": self._seq}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _msg_idx(self) -> int:
        """Place the message at index one when reasoning occupies index zero."""
        return 1 if self._emitted_reasoning_item else 0

    def _reasoning_item(self, status: str) -> dict:
        """Build a reasoning item with its text in the first summary block."""
        return {"type": "reasoning", "id": self._reasoning_item_id, "status": status,
                "summary": [{"type": "summary_text", "text": self._reasoning}]}

    def _msg_item(self, status: str = "in_progress", empty: bool = False) -> dict:
        content = [] if empty else [
            {"type": "output_text", "text": self._content, "annotations": []}
        ]
        return {
            "type": "message",
            "id": self.msg_id,
            "status": status,
            "role": "assistant",
            "content": content,
        }

    def _fc_item(self, tc: dict, status: str) -> dict:
        return {
            "type": "function_call",
            "id": tc["fc_id"],
            "call_id": tc["id"],
            "name": tc["name"],
            "arguments": tc["args"],
            "status": status,
        }

    def _response_obj(self, status: str, incomplete_reason: str | None = None) -> dict:
        output = []
        if self._emitted_reasoning_item:
            output.append(self._reasoning_item(status))
        if self._emitted_msg_item or self._content:
            output.append(self._msg_item(status))
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            if tc.get("emitted"):
                output.append(self._fc_item(tc, status))

        usage = None
        if self._usage:
            u = self._usage
            reasoning_tokens = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
            # Omit unknown cache details instead of reporting a fabricated zero.
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens",
                                                                   u.get("cache_read_input_tokens"))
            usage = {
                "input_tokens": u.get("prompt_tokens", u.get("input_tokens", 0)),
                "output_tokens": u.get("completion_tokens", u.get("output_tokens", 0)),
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                "total_tokens": u.get("total_tokens", 0),
            }
            if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
                usage["input_tokens_details"] = {"cached_tokens": cached}

        obj = {
            "id": self.resp_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": output,
            "parallel_tool_calls": self._parallel_tool_calls,
            "usage": usage,
        }
        if status == "incomplete":
            # Preserve a null incomplete reason when the upstream supplies none.
            obj["incomplete_details"] = {"reason": incomplete_reason}
        return obj
