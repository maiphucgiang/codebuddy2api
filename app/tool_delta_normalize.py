"""Repair fragmented ``tool_calls`` deltas without holding back the stream.

Some OpenAI-compatible upstreams stream one tool call as several deltas: the first carries
``id`` and ``name``, the rest only append ``arguments`` while repeating an empty ``name``.
Forwarding those verbatim breaks strict clients, which read the name of every delta.  The
normalizer remembers each call's name and backfills it into later fragments, so the stream
stays realtime and every delta carries a usable name.
"""
from __future__ import annotations

import json

_DATA_PREFIX = "data:"


class ToolDeltaNormalizer:
    """Backfill a known tool name into fragments that repeat an empty one."""

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._names: dict[int, str] = {}

    def feed(self, line: str) -> list[str]:
        """Return the line, rewritten when a fragment needs its name restored."""
        if not self.enabled or not line.startswith(_DATA_PREFIX):
            return [line]
        payload = line[len(_DATA_PREFIX):].strip()
        if not payload or payload == "[DONE]":
            return [line]
        try:
            event = json.loads(payload)
        except ValueError:
            return [line]
        if not isinstance(event, dict):
            return [line]
        choices = event.get("choices")
        if not isinstance(choices, list):
            return [line]
        changed = False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            tools = delta.get("tool_calls")
            if not isinstance(tools, list):
                continue
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                index = tool.get("index")
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    index = 0
                function = tool.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if isinstance(name, str) and name:
                    self._names[index] = name
                    continue
                known = self._names.get(index)
                if not known:
                    continue
                function["name"] = known
                changed = True
        if not changed:
            return [line]
        return ["data: " + json.dumps(event, ensure_ascii=False)]

    def flush(self) -> list[str]:
        """Nothing is buffered, so there is never a tail to emit."""
        return []


# Backwards-compatible alias for callers that imported the earlier name.
ToolCallCoalescer = ToolDeltaNormalizer
