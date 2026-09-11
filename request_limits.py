"""Request-wide image limits without decoding images or inspecting arbitrary JSON."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


_IMAGE_TYPES = ("image_url", "input_image", "image")
_IMAGE_PLACEHOLDER = "[Image omitted by request image limit.]"


class ImageLimitError(ValueError):
    """The request contains more protocol image blocks than permitted."""

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(f"Request contains {count} images; maximum allowed is {limit}.")


def _map_content(content: Any, visit: Callable[[dict], bool], *, text_type: str) -> Any:
    """Visit content arrays only; tool arguments, schemas and text are opaque."""
    if not isinstance(content, list):
        return content
    mapped = []
    changed = False
    for block in content:
        replacement = block
        if isinstance(block, dict):
            kind = block.get("type")
            if kind in _IMAGE_TYPES:
                if not visit(block):
                    changed = True
                    continue
            elif kind == "tool_result":
                nested = block.get("content")
                updated = _map_content(nested, visit, text_type="text")
                if updated is not nested:
                    replacement = {**block, "content": updated}
        mapped.append(replacement)
        changed = changed or replacement is not block
    if not changed:
        return content
    # Keep image-only messages/tool results valid without removing their envelope.
    return mapped or [{"type": text_type, "text": _IMAGE_PLACEHOLDER}]


def _map_items(items: Any, visit: Callable[[dict], bool], *, field: str) -> Any:
    if not isinstance(items, list):
        return items
    mapped = []
    changed = False
    for item in items:
        replacement = item
        if isinstance(item, dict):
            kind = item.get("type")
            key = None
            if field == "input" and kind == "function_call_output":
                key = "output"
            elif kind in (None, "message") and "role" in item:
                key = "content"
            if key is not None:
                content = item.get(key)
                text_type = "input_text" if field == "input" else "text"
                if field == "input" and item.get("role") == "assistant":
                    text_type = "output_text"
                updated = _map_content(content, visit, text_type=text_type)
                if updated is not content:
                    replacement = {**item, key: updated}
        mapped.append(replacement)
        changed = changed or replacement is not item
    return mapped if changed else items


def apply_image_policy(
    payload: dict,
    *,
    field: str = "messages",
    max_images: int = 16,
    policy: str = "truncate",
) -> tuple[dict, dict]:
    """Keep the newest N protocol image blocks across the complete request.

    ``field='messages'`` handles Chat/Anthropic history; ``field='input'`` handles
    Responses history, including function_call_output.output content arrays.
    Repeated URLs count separately. Zero permits no images. ``error`` raises
    ImageLimitError before any modification. Invalid limits/policies raise
    ValueError. Truncation copies only changed containers; untouched subtrees
    remain shared with the original payload. Strings and arbitrary JSON are not
    searched, and image data/URLs are never decoded or fetched.
    """
    if isinstance(max_images, bool) or not isinstance(max_images, int) or max_images < 0:
        raise ValueError("max_images must be a non-negative integer")
    if policy not in ("truncate", "error"):
        raise ValueError("policy must be 'truncate' or 'error'")

    count = 0

    def count_image(block: dict) -> bool:
        nonlocal count
        count += 1
        return True

    items = payload.get(field)
    _map_items(items, count_image, field=field)
    dropped = max(0, count - max_images)
    if dropped and policy == "error":
        raise ImageLimitError(count, max_images)
    stats = {"count": count, "retained": count - dropped, "dropped": dropped}
    if not dropped:
        return payload, stats

    remaining = dropped

    def retain_image(block: dict) -> bool:
        nonlocal remaining
        if remaining:
            remaining -= 1
            return False
        return True

    updated = _map_items(items, retain_image, field=field)
    return {**payload, field: updated}, stats
