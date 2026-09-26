"""Translate requests and SSE responses between Anthropic Messages and OpenAI Chat."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.reasoning import extract_reasoning_text, map_reasoning_controls
from app.upstream_io import (StreamOutputBudget, merge_tool_call_delta, new_tool_state,
                             seal_tool_identities, tool_identity_complete)

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

    map_reasoning_controls(body, chat, protocol="messages")
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
    if role != "assistant" and any(isinstance(block, dict) and block.get("type") in
                                  ("thinking", "redacted_thinking") for block in blocks):
        raise ValueError("thinking requires an assistant message")

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
        thoughts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            thought = extract_reasoning_text(block)
            if thought is not None:
                thoughts.append(thought)
            elif block.get("type") == "tool_use":
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
        if thoughts:
            msg_out["reasoning_content"] = "".join(thoughts)
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

    def __init__(self, model: str = "unknown", *, realtime: bool = False,
                 budget: StreamOutputBudget | None = None, tool_states: dict | None = None,
                 declared_names=None):
        self.msg_id = _rand_id("msg_")
        self.model = model
        self._realtime = bool(realtime)
        self._budget = budget if budget is not None else StreamOutputBudget(0)
        self._tool_states = tool_states
        self._local_tool_states: dict[int, dict] = {}
        self._declared_names = frozenset(
            value for value in (declared_names or ()) if isinstance(value, str) and value)
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
        self._active_wire_block: int | None = None
        self._deferred_blocks: dict[int, dict] = {}

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
        if self._realtime:
            if (self._tool_uses and self._finish_reason not in
                    ("tool_calls", "length", "content_filter", "content-filter", "refusal")
                    and not self._content_filter):
                raise ValueError("tool calls require a tool_calls finish reason")
            seal_tool_identities(self._tool_states if self._tool_states is not None
                                 else self._local_tool_states, self._declared_names)
            self._flush_ready_tools(events)

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
                tc["terminal_closed"] = True

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

    def mark_content_filter(self) -> None:
        """Keep a detector-confirmed refusal from becoming a tool-choice error."""
        self._content_filter = True

    def set_validated_tools(self, tool_calls) -> None:
        """Accept terminal metadata already validated by the shared Chat accumulator."""
        if not self._realtime:
            return
        expected = [self._tool_uses[index] for index in sorted(self._tool_uses)]
        if len(expected) != len(tool_calls or []):
            raise ValueError("validated tool calls do not match stream items")
        for slot, call in zip(expected, tool_calls or []):
            function = call.get("function") or {}
            state = slot.get("state")
            values = ((slot.get("id"), call.get("id")),
                      (slot.get("name"), function.get("name")))
            if state is not None:
                values += ((state.get("id"), call.get("id")),
                           (state.get("name"), function.get("name")),
                           (state.get("arguments"), function.get("arguments")))
            if any(left != right for left, right in values):
                raise ValueError("validated tool metadata does not match stream items")
            slot["validated"] = True

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
            finish = choice.get("finish_reason") or None
            if self._finish_reason is not None:
                if (any(delta.get(key) for key in ("content", "reasoning_content", "refusal"))
                        or bool(delta.get("tool_calls")) or bool(delta.get("function_call"))):
                    raise ValueError("output after finish_reason")
                if finish is not None and finish != self._finish_reason:
                    raise ValueError("changed finish_reason")

            # Emit reasoning before text.
            thinking = delta.get("reasoning_content")
            if thinking:
                self._budget.charge_text(thinking)
                self._thinking_content += thinking
                if not self._thinking_block_open:
                    if self._text_block_open:
                        events.append(self._evt("content_block_stop", {
                            "index": self._text_block_idx}))
                        self._text_block_open = False
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
                self._budget.charge_text(content)
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
                    block_idx = None if self._realtime else self._next_block_idx
                    if not self._realtime:
                        self._next_block_idx += 1
                    self._tool_uses[idx] = {
                        "id": "", "name": "", "args": "", "block_idx": block_idx,
                        "open": False, "emitted_args_length": 0,
                    }
                slot = self._tool_uses[idx]
                if self._realtime:
                    state = self._sync_tool_state(idx, tc)
                    slot["state"] = state
                else:
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    slot["_pending_args"] = fn.get("arguments") or ""

                if self._realtime:
                    events.extend(self._flush_tool_slot(idx, slot))
                else:
                    if not slot["open"]:
                        self._close_content_blocks(events)
                        events.append(self._evt("content_block_start", {
                            "index": slot["block_idx"],
                            "content_block": {"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": {}},
                        }))
                        slot["open"] = True
                    arguments = self._new_tool_arguments(slot)
                    if arguments:
                        events.append(self._evt("content_block_delta", {
                            "index": slot["block_idx"],
                            "delta": {"type": "input_json_delta", "partial_json": arguments},
                        }))

            if finish:
                self._finish_reason = finish
                if self._realtime:
                    seal_tool_identities(self._tool_states if self._tool_states is not None
                                         else self._local_tool_states, self._declared_names)
                    self._flush_ready_tools(events)

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
                        tc["terminal_closed"] = True

        return "".join(events)

    def _close_content_blocks(self, events: list[str]) -> None:
        if self._thinking_block_open:
            events.append(self._evt("content_block_stop", {"index": self._thinking_block_idx}))
            self._thinking_block_open = False
        if self._realtime and self._text_block_open:
            events.append(self._evt("content_block_stop", {"index": self._text_block_idx}))
            self._text_block_open = False

    def _sync_tool_state(self, index: int, tool: dict) -> dict:
        state = ((self._tool_states or {}).get(index)
                 if self._tool_states is not None else self._local_tool_states.get(index))
        if state is None:
            if self._tool_states is not None:
                raise ValueError("tool state missing from realtime accumulator")
            state = new_tool_state()
            self._local_tool_states[index] = state
        if self._tool_states is None:
            merge_tool_call_delta(state, tool, declared_names=self._declared_names,
                                  charge=self._budget.charge_text)
        else:
            state["identity_complete"] = tool_identity_complete(
                state, self._declared_names, terminal=bool(state.get("_terminal")))
        slot = self._tool_uses[index]
        slot["id"] = state.get("id") or ""
        slot["name"] = state.get("name") or ""
        return state

    def _flush_tool_slot(self, index: int, slot: dict) -> list[str]:
        """Open one ready tool and flush its buffered arguments exactly once."""
        if not self._realtime or slot.get("terminal_closed"):
            return []
        state = slot.get("state")
        if state is None or not state.get("identity_complete"):
            return []
        events: list[str] = []
        if not slot.get("open"):
            if slot.get("block_idx") is None:
                slot["block_idx"] = self._next_block_idx
                self._next_block_idx += 1
            self._close_content_blocks(events)
            events.append(self._evt("content_block_start", {
                "index": slot["block_idx"],
                "content_block": {"type": "tool_use", "id": state["id"],
                                  "name": state["name"], "input": {}},
            }))
            slot["open"] = True
            state["identity_emitted"] = True
        arguments = self._new_tool_arguments(slot)
        if arguments:
            events.append(self._evt("content_block_delta", {
                "index": slot["block_idx"],
                "delta": {"type": "input_json_delta", "partial_json": arguments},
            }))
        return events

    def _flush_ready_tools(self, events: list[str]) -> None:
        for index in sorted(self._tool_uses):
            events.extend(self._flush_tool_slot(index, self._tool_uses[index]))

    def _tool_arguments(self, slot: dict) -> str:
        if self._realtime and slot.get("state") is not None:
            return slot["state"].get("arguments") or ""
        return slot["args"]

    def _new_tool_arguments(self, slot: dict) -> str:
        if not self._realtime:
            piece = slot.get("_pending_args", "")
            slot["args"] += piece
            return piece
        if not slot.get("open"):
            return ""
        arguments = self._tool_arguments(slot)
        emitted = slot.get("emitted_args_length", 0)
        if len(arguments) < emitted or not arguments.startswith(slot.get("_emitted_prefix", "")):
            raise ValueError("non-append-only tool arguments")
        piece = arguments[emitted:]
        slot["emitted_args_length"] = len(arguments)
        slot["_emitted_prefix"] = arguments
        return piece

    def _evt(self, event_type: str, data: dict) -> str:
        """Format an Anthropic SSE event with its event name."""
        payload = {"type": event_type, **data}
        wire = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        if self._realtime:
            if event_type.startswith("content_block_"):
                return self._serialize_block_event(event_type, data["index"], wire)
            if event_type in ("message_delta", "message_stop") and (
                    self._active_wire_block is not None or self._deferred_blocks):
                raise ValueError("message ended before content blocks closed")
        return wire

    def _serialize_block_event(self, kind: str, index: int, wire: str) -> str:
        """Keep one downstream block open; later blocks wait within the shared budget."""
        if kind == "content_block_start":
            if index == self._active_wire_block or index in self._deferred_blocks:
                raise ValueError("duplicate content block start")
            if self._active_wire_block is None:
                self._active_wire_block = index
                return wire
            self._budget.charge_text(wire)
            self._deferred_blocks[index] = {"events": [wire], "closed": False}
            return ""

        if index == self._active_wire_block:
            if kind != "content_block_stop":
                return wire
            self._active_wire_block = None
            ready = [wire]
            while self._deferred_blocks:
                next_index = next(iter(self._deferred_blocks))
                block = self._deferred_blocks.pop(next_index)
                ready.extend(block["events"])
                if not block["closed"]:
                    self._active_wire_block = next_index
                    break
            return "".join(ready)

        block = self._deferred_blocks.get(index)
        if block is None or block["closed"]:
            raise ValueError("content event outside its block lifecycle")
        self._budget.charge_text(wire)
        block["events"].append(wire)
        if kind == "content_block_stop":
            block["closed"] = True
        return ""

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
                block["input"] = json.loads(self._tool_arguments(tc))
            except (json.JSONDecodeError, ValueError):
                block["input"] = self._tool_arguments(tc)
            blocks.append(block)

        return blocks
