"""Translate requests and SSE responses between OpenAI Responses and Chat Completions."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.upstream_io import (StreamOutputBudget, merge_tool_call_delta, new_tool_state,
                             seal_tool_identities, tool_identity_complete)

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
    tools = list(body.get("tools") or [])
    inp = body.get("input", [])
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, dict) and item.get("type") == "additional_tools" and isinstance(item.get("tools"), list):
                tools.extend(item["tools"])

    ns_map = _build_tool_namespace_map(tools)

    if tools:
        chat["tools"] = _convert_tools_for_chat(tools)
    if ns_map:
        chat["_tool_namespaces"] = ns_map
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

        # Ignore additional_tools in message sequence
        if item_type == "additional_tools":
            continue

        # Agent message from multi-agent collaboration
        if item_type == "agent_message":
            _flush_assistant()
            content = _extract_content(item.get("content", ""))
            messages.append({"role": "user", "content": content})
            continue

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
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                raise ValueError("function_call.arguments must be a JSON string")
            if pending_assistant_content is None:
                pending_assistant_content = ""
            pending_tool_calls.append({
                "id": item.get("call_id", item.get("id", _rand_id("call_"))),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": arguments,
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
                elif kind == "encrypted_content":
                    text = p.get("encrypted_content") or p.get("text", "")
                    if isinstance(text, str) and text:
                        parts.append({"type": "text", "text": text})
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


def _build_tool_namespace_map(tools: list, current_ns: str = "") -> dict[str, str]:
    """Map tool names to their declaring namespace for Responses event reconstruction."""
    ns_map: dict[str, str] = {}
    for t in tools:
        if not isinstance(t, dict):
            continue
        t_type = t.get("type")
        if t_type == "namespace" and isinstance(t.get("tools"), list):
            ns_name = t.get("name", "")
            full_ns = f"{current_ns}.{ns_name}" if current_ns else ns_name
            ns_map.update(_build_tool_namespace_map(t["tools"], full_ns))
        elif t_type == "function":
            name = t.get("name") or (t.get("function", {}).get("name") if isinstance(t.get("function"), dict) else "")
            if name and current_ns:
                ns_map[name] = current_ns
    return ns_map


def _convert_tools_for_chat(tools: list) -> list:
    """Convert Responses tool definitions to Chat function objects."""
    result = []
    seen_names = set()
    for t in tools:
        if not isinstance(t, dict):
            continue
        tool_type = t.get("type")
        if tool_type == "namespace" and isinstance(t.get("tools"), list):
            for sub_tool in _convert_tools_for_chat(t["tools"]):
                fn_name = sub_tool.get("function", {}).get("name")
                if fn_name and fn_name not in seen_names:
                    seen_names.add(fn_name)
                    result.append(sub_tool)
            continue
        if tool_type != "function":
            continue
        # Already in Chat format.
        if "function" in t:
            tool_obj = json.loads(json.dumps(t))
            fn_dict = tool_obj.get("function")
            if isinstance(fn_dict, dict) and "parameters" in fn_dict:
                props = (fn_dict.get("parameters") or {}).get("properties")
                if isinstance(props, dict):
                    for p_name, p_val in props.items():
                        if isinstance(p_val, dict) and "encrypted" in p_val:
                            p_val.pop("encrypted", None)
            fn_name = fn_dict.get("name") if isinstance(fn_dict, dict) else None
            if fn_name and fn_name not in seen_names:
                seen_names.add(fn_name)
                result.append(tool_obj)
            elif not fn_name:
                result.append(tool_obj)
            continue
        # Nest the flat Responses function fields.
        name = t.get("name", "")
        if name and name in seen_names:
            continue
        if name:
            seen_names.add(name)
        fn: dict[str, Any] = {"name": name}
        if "description" in t:
            fn["description"] = t["description"]
        if "parameters" in t:
            params = t["parameters"]
            if isinstance(params, dict):
                params = json.loads(json.dumps(params))
                props = params.get("properties")
                if isinstance(props, dict):
                    for p_name, p_val in props.items():
                        if isinstance(p_val, dict) and "encrypted" in p_val:
                            p_val.pop("encrypted", None)
            fn["parameters"] = params
        if "strict" in t:
            fn["strict"] = t["strict"]
        result.append({"type": "function", "function": fn})
    return result


# ---------------------------------------------------------------------------
# Chat SSE to Responses events
# ---------------------------------------------------------------------------

class ResponsesStreamConverter:
    """Convert Chat SSE increments into Responses events."""

    def __init__(self, model: str = "unknown", parallel_tool_calls: bool = True, *,
                 realtime: bool = False, budget: StreamOutputBudget | None = None,
                 tool_states: dict | None = None, declared_names=None,
                 tool_namespaces: dict[str, str] | None = None):
        self.resp_id = _rand_id("resp_")
        self.msg_id = _rand_id("msg_")
        self.model = model
        self._parallel_tool_calls = bool(parallel_tool_calls)
        self._realtime = bool(realtime)
        self._budget = budget if budget is not None else StreamOutputBudget(0)
        self._tool_states = tool_states
        self._local_tool_states: dict[int, dict] = {}
        self._tool_namespaces = dict(tool_namespaces or {})
        self._declared_names = frozenset(
            value for value in (declared_names or ()) if isinstance(value, str) and value)
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
        self._reasoning_output_idx: int | None = None
        self._message_output_idx: int | None = None
        self._tool_calls: dict[int, dict] = {}  # index → stable output item state
        self._output_order: list[tuple[str, int | None]] = []
        self._next_output_idx = 0
        self._finish_reason: str | None = None
        self._usage: dict | None = None
        self._content_filter = False
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
        if self._realtime:
            if (self._tool_calls and self._finish_reason not in
                    ("tool_calls", "length", "content_filter", "content-filter", "refusal")
                    and not self._content_filter):
                raise ValueError("tool calls require a tool_calls finish reason")
            seal_tool_identities(self._tool_states if self._tool_states is not None
                                 else self._local_tool_states, self._declared_names)
            pending = self._flush_ready_tools()
        else:
            pending = []
        status, reason = self._final_status()
        if self._realtime:
            return "".join(pending) + self._finish_realtime(status)
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

    def mark_content_filter(self) -> None:
        """Keep a detector-confirmed refusal from becoming a successful terminal."""
        self._content_filter = True

    def set_validated_tools(self, tool_calls) -> None:
        """Accept terminal tool metadata already validated by the shared Chat accumulator."""
        if not self._realtime:
            return
        expected = [self._tool_calls[index] for index in sorted(self._tool_calls)]
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

    def _finish_realtime(self, status: str) -> str:
        events: list[str] = []
        for kind, index in self._output_order:
            if kind == "reasoning" and self._emitted_reasoning_item:
                output_index = self._reasoning_output_idx
                events.append(self._evt("response.reasoning_summary_text.done", {
                    "output_index": output_index, "summary_index": 0, "text": self._reasoning,
                    "item_id": self._reasoning_item_id}))
                events.append(self._evt("response.output_item.done", {
                    "output_index": output_index, "item": self._reasoning_item(status)}))
            elif kind == "message" and self._emitted_msg_item:
                output_index = self._message_output_idx
                events.append(self._evt("response.output_text.done", {
                    "output_index": output_index, "content_index": 0, "text": self._content,
                    "item_id": self.msg_id}))
                events.append(self._evt("response.content_part.done", {
                    "output_index": output_index, "content_index": 0,
                    "part": {"type": "output_text", "text": self._content, "annotations": []},
                    "item_id": self.msg_id}))
                events.append(self._evt("response.output_item.done", {
                    "output_index": output_index, "item": self._msg_item(status)}))
            elif kind == "tool":
                slot = self._tool_calls[index]
                if slot.get("emitted"):
                    output_index = slot["output_idx"]
                    events.append(self._evt("response.function_call_arguments.done", {
                        "output_index": output_index, "arguments": self._tool_arguments(slot),
                        "item_id": slot["fc_id"]}))
                    events.append(self._evt("response.output_item.done", {
                        "output_index": output_index, "item": self._fc_item(slot, status)}))
        events.append(self._evt(f"response.{status}", {
            "response": self._response_obj(status, incomplete_reason=self._final_status()[1])}))
        return "".join(events)

    def get_nonstream_response(self) -> dict:
        """Return the complete non-streaming Response object."""
        status, reason = self._final_status()
        return self._response_obj(status, incomplete_reason=reason)

    def _final_status(self) -> tuple[str, str | None]:
        """Map finish reasons to response status without hiding truncation or filtering."""
        fr = self._finish_reason
        if self._content_filter and fr in (None, "stop", "tool_calls"):
            return "incomplete", "content_filter"
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
            finish = choice.get("finish_reason") or None
            if self._finish_reason is not None:
                if (any(delta.get(key) for key in ("content", "reasoning_content", "refusal"))
                        or bool(delta.get("tool_calls")) or bool(delta.get("function_call"))):
                    raise ValueError("output after finish_reason")
                if finish is not None and finish != self._finish_reason:
                    raise ValueError("changed finish_reason")

            # Emit reasoning before message content.
            reasoning = delta.get("reasoning_content")
            if reasoning:
                if not self._emitted_reasoning_item:
                    self._reasoning_output_idx = self._claim_output("reasoning")
                    events.append(self._evt("response.output_item.added", {
                        "output_index": self._reasoning_output_idx,
                        "item": {"type": "reasoning", "id": self._reasoning_item_id,
                                 "summary": [], "status": "in_progress"}
                    }))
                    self._emitted_reasoning_item = True
                self._budget.charge_text(reasoning)
                self._reasoning += reasoning
                events.append(self._evt("response.reasoning_summary_text.delta", {
                    "output_index": self._reasoning_output_idx, "summary_index": 0, "delta": reasoning,
                    "item_id": self._reasoning_item_id
                }))

            # Preserve refusal content as valid output_text.
            content = (delta.get("content") or "") + (delta.get("refusal") or "")
            if content:
                if not self._emitted_msg_item:
                    self._message_output_idx = self._claim_output("message")
                    events.append(self._evt("response.output_item.added", {
                        "output_index": self._message_output_idx,
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

                self._budget.charge_text(content)
                self._content += content
                events.append(self._evt("response.output_text.delta", {
                    "output_index": self._msg_idx(), "content_index": 0, "delta": content,
                    "item_id": self.msg_id
                }))

            # ---- tool_calls delta ----
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in self._tool_calls:
                    output_idx = None if self._realtime else self._claim_output("tool", idx)
                    self._tool_calls[idx] = {
                        "id": tc.get("id", ""), "name": "", "args": "",
                        "fc_id": _rand_id("fc_"), "output_idx": output_idx,
                        "emitted": False, "emitted_args_length": 0,
                    }
                slot = self._tool_calls[idx]
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
                    if not slot["emitted"]:
                        events.append(self._evt("response.output_item.added", {
                            "output_index": slot["output_idx"],
                            "item": self._fc_item(slot, "in_progress")
                        }))
                        slot["emitted"] = True
                    arguments = self._new_tool_arguments(slot)
                    if arguments:
                        events.append(self._evt("response.function_call_arguments.delta", {
                            "output_index": slot["output_idx"],
                            "delta": arguments, "item_id": slot["fc_id"]
                        }))

            if finish:
                self._finish_reason = finish
                if self._realtime:
                    seal_tool_identities(self._tool_states if self._tool_states is not None
                                         else self._local_tool_states, self._declared_names)
                    for pending_idx in sorted(self._tool_calls):
                        events.extend(self._flush_tool_slot(pending_idx, self._tool_calls[pending_idx]))

        return "".join(events)

    def _evt(self, event_type: str, data: dict) -> str:
        """Format SSE events with monotonically increasing sequence numbers."""
        self._seq += 1
        payload = {"type": event_type, **data, "sequence_number": self._seq}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _claim_output(self, kind: str, index: int | None = None) -> int:
        if self._realtime:
            output_index = self._next_output_idx
            self._next_output_idx += 1
            self._output_order.append((kind, index))
            return output_index
        if kind == "reasoning":
            return 0
        if kind == "message":
            return 1 if self._emitted_reasoning_item else 0
        return ((1 if self._emitted_reasoning_item else 0)
                + (1 if self._emitted_msg_item else 0) + len(self._tool_calls))

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
        slot = self._tool_calls[index]
        slot["id"] = state.get("id") or ""
        slot["name"] = state.get("name") or ""
        return state

    def _flush_tool_slot(self, index: int, slot: dict) -> list[str]:
        """Start one ready tool and flush its buffered arguments exactly once."""
        if not self._realtime:
            return []
        state = slot.get("state")
        if state is None or not state.get("identity_complete"):
            return []
        if not slot.get("emitted"):
            if slot.get("output_idx") is None:
                slot["output_idx"] = self._claim_output("tool", index)
            events = [self._evt("response.output_item.added", {
                "output_index": slot["output_idx"],
                "item": self._fc_item(slot, "in_progress", include_arguments=False)
            })]
            slot["emitted"] = True
            state["identity_emitted"] = True
        else:
            events = []
        arguments = self._new_tool_arguments(slot)
        if arguments:
            events.append(self._evt("response.function_call_arguments.delta", {
                "output_index": slot["output_idx"],
                "delta": arguments, "item_id": slot["fc_id"]
            }))
        return events

    def _flush_ready_tools(self) -> list[str]:
        events: list[str] = []
        for index in sorted(self._tool_calls):
            events.extend(self._flush_tool_slot(index, self._tool_calls[index]))
        return events

    def _tool_arguments(self, slot: dict) -> str:
        if self._realtime and slot.get("state") is not None:
            return slot["state"].get("arguments") or ""
        return slot["args"]

    def _new_tool_arguments(self, slot: dict) -> str:
        if not self._realtime:
            piece = slot.get("_pending_args", "")
            slot["args"] += piece
            return piece
        if not slot.get("emitted"):
            return ""
        arguments = self._tool_arguments(slot)
        emitted = slot.get("emitted_args_length", 0)
        if len(arguments) < emitted or not arguments.startswith(slot.get("_emitted_prefix", "")):
            raise ValueError("non-append-only tool arguments")
        piece = arguments[emitted:]
        slot["emitted_args_length"] = len(arguments)
        slot["_emitted_prefix"] = arguments
        return piece

    def _msg_idx(self) -> int:
        """Return the stable message index in realtime mode or the canonical placement."""
        return (self._message_output_idx if self._realtime else
                1 if self._emitted_reasoning_item else 0)

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

    def _fc_item(self, tc: dict, status: str, *, include_arguments: bool = True) -> dict:
        raw_name = tc["name"] or (tc.get("state", {}).get("name") or "")
        name = raw_name
        ns = tc.get("namespace")
        if not ns:
            if "." in raw_name:
                ns_prefix, base_name = raw_name.split(".", 1)
                if ns_prefix in ("collaboration", "collaboration-optimize") or (self._tool_namespaces and base_name in self._tool_namespaces):
                    ns = ns_prefix
                    name = base_name
            elif "__" in raw_name:
                ns_prefix, base_name = raw_name.split("__", 1)
                if ns_prefix in ("collaboration", "collaboration-optimize") or (self._tool_namespaces and base_name in self._tool_namespaces):
                    ns = ns_prefix
                    name = base_name
            elif self._tool_namespaces and raw_name in self._tool_namespaces:
                ns = self._tool_namespaces[raw_name]

        item: dict[str, Any] = {
            "type": "function_call",
            "id": tc["fc_id"],
            "call_id": tc["id"] or (tc.get("state", {}).get("id") or ""),
            "name": name,
            "arguments": self._tool_arguments(tc) if include_arguments else "",
            "status": status,
        }
        if ns:
            item["namespace"] = ns
        return item

    def _response_obj(self, status: str, incomplete_reason: str | None = None) -> dict:
        output = []
        if self._realtime:
            for kind, index in self._output_order:
                if kind == "reasoning" and self._emitted_reasoning_item:
                    output.append(self._reasoning_item(status))
                elif kind == "message" and self._emitted_msg_item:
                    output.append(self._msg_item(status))
                elif kind == "tool" and self._tool_calls[index].get("emitted"):
                    output.append(self._fc_item(self._tool_calls[index], status))
        else:
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
