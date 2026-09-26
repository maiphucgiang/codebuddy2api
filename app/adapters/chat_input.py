"""Normalize recognizable Anthropic history blocks embedded in Chat requests."""
import json

from fastapi import HTTPException

from app.reasoning import ReasoningInputError, extract_reasoning_text


_ANTHROPIC_BLOCKS = ("tool_use", "tool_result", "thinking", "redacted_thinking", "image")


def _invalid(param, message):
    return HTTPException(status_code=400, detail={"error": {
        "type": "invalid_request_error", "code": "invalid_chat_content", "param": param, "message": message,
    }})


def _nonempty_string(value, param):
    if not isinstance(value, str) or not value.strip():
        raise _invalid(param, "Expected a non-empty string")
    return value


def _content_part(block, param):
    if not isinstance(block, dict):
        raise _invalid(param, "Content blocks must be objects")
    kind = block.get("type")
    if kind == "text":
        if not isinstance(block.get("text"), str):
            raise _invalid(param + ".text", "Text content must be a string")
        return block
    if kind == "image_url":
        image = block.get("image_url")
        if not isinstance(image, dict):
            raise _invalid(param + ".image_url", "image_url must be an object")
        _nonempty_string(image.get("url"), param + ".image_url.url")
        return block
    if kind == "image":
        source = block.get("source")
        if not isinstance(source, dict):
            raise _invalid(param + ".source", "Image source must be an object")
        if source.get("type") == "url":
            url = _nonempty_string(source.get("url"), param + ".source.url")
        elif source.get("type") == "base64":
            media = source.get("media_type")
            if not isinstance(media, str) or not media.startswith("image/"):
                raise _invalid(param + ".source.media_type", "Base64 images require an image media type")
            data = _nonempty_string(source.get("data"), param + ".source.data")
            url = f"data:{media};base64,{data}"
        else:
            raise _invalid(param + ".source", "Only URL and base64 image sources can be converted to Chat")
        return {"type": "image_url", "image_url": {"url": url}}
    raise _invalid(param, "Unsupported block in mixed Chat/Anthropic content")


def _tool_call(block, param):
    identifier = _nonempty_string(block.get("id"), param + ".id")
    name = _nonempty_string(block.get("name"), param + ".name")
    arguments = block.get("input")
    if not isinstance(arguments, dict):
        raise _invalid(param + ".input", "tool_use input must be a JSON object")
    try:
        arguments = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise _invalid(param + ".input", "tool_use input must be a valid JSON object") from None
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def _tool_result(block, param):
    identifier = _nonempty_string(block.get("tool_use_id"), param + ".tool_use_id")
    if "is_error" in block and not isinstance(block["is_error"], bool):
        raise _invalid(param + ".is_error", "is_error must be a boolean")
    content = block.get("content", "")
    if isinstance(content, list):
        parts = [_content_part(part, f"{param}.content[{index}]") for index, part in enumerate(content)]
        content = (parts if any(part["type"] == "image_url" for part in parts)
                   else "".join(part["text"] for part in parts))
    elif not isinstance(content, str):
        raise _invalid(param + ".content", "Tool results must contain a string or text/image blocks")
    if block.get("is_error"):
        content = ([{"type": "text", "text": "[tool execution failed]"}, *content]
                   if isinstance(content, list) else "[tool execution failed]\n" + content)
    return {"role": "tool", "tool_call_id": identifier, "content": content}


def _convert_message(message, index, pending):
    content = message.get("content")
    if not isinstance(content, list) or not any(
            isinstance(block, dict) and block.get("type") in _ANTHROPIC_BLOCKS for block in content):
        return [message]
    role = message.get("role")
    param = f"messages[{index}]"
    parts, calls, results, thoughts = [], [], [], []
    call_ids, result_ids = set(), set()
    for offset, block in enumerate(content):
        location = f"{param}.content[{offset}]"
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "tool_use":
            if role != "assistant":
                raise _invalid(location, "tool_use requires an assistant message")
            if message.get("tool_calls") not in (None, []) or message.get("function_call") is not None:
                raise _invalid(location, "tool_use conflicts with existing Chat tool calls")
            call = _tool_call(block, location)
            if call["id"] in call_ids:
                raise _invalid(location + ".id", "Duplicate tool_use ID in one assistant message")
            call_ids.add(call["id"])
            calls.append(call)
        elif kind == "tool_result":
            if role != "user":
                raise _invalid(location, "tool_result requires a user message")
            if message.keys() - {"role", "content"}:
                raise _invalid(param, "Message-level attributes cannot be assigned safely when splitting tool_result content")
            if parts:
                raise _invalid(location, "Tool results must precede ordinary user content")
            result = _tool_result(block, location)
            identifier = result["tool_call_id"]
            if pending.get(identifier) != 1 or identifier in result_ids:
                raise _invalid(location + ".tool_use_id", "tool_result must match one preceding, unanswered tool call")
            result_ids.add(identifier)
            results.append(result)
        elif kind in ("thinking", "redacted_thinking"):
            if role != "assistant":
                raise _invalid(location, "thinking requires an assistant message")
            if message.get("reasoning_content") not in (None, ""):
                raise _invalid(location, "thinking conflicts with existing reasoning_content")
            try:
                thoughts.append(extract_reasoning_text(block))
            except ReasoningInputError as error:
                raise _invalid(location + ("." + error.field if error.field else ""), str(error)) from None
        else:
            parts.append(_content_part(block, location))
    if results:
        # Keep results adjacent to the assistant calls, before any follow-up user text/images.
        if parts and result_ids != pending.keys():
            raise _invalid(param, "All pending tool results must precede follow-up user content")
        return [*results, *([{**message, "content": parts}] if parts else [])]
    out = {**message, "content": parts if parts else None}
    if calls:
        out["tool_calls"] = calls
    if thoughts:
        out["reasoning_content"] = "".join(thoughts)
    return [out]


def normalize_chat_messages(messages, *, message_indices=None):
    """Convert recognized blocks without mutating input; retain caller indices in errors."""
    result, pending = [], {}
    for index, message in enumerate(messages):
        original_index = index if message_indices is None else message_indices[index]
        converted = _convert_message(message, original_index, pending)
        result.extend(converted)
        for item in converted:
            if item.get("role") == "tool":
                identifier = item.get("tool_call_id")
                if isinstance(identifier, str):
                    pending.pop(identifier, None)
                continue
            pending = {}
            if item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list):
                for call in item["tool_calls"]:
                    identifier = call.get("id") if isinstance(call, dict) else None
                    if isinstance(identifier, str):
                        pending[identifier] = pending.get(identifier, 0) + 1
    return result
