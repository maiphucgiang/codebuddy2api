"""Bound upstream retries and prohibit replay after a response has opened."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time

import json
import httpx

from app.content_filter import ContentFilterDetector


class UpstreamResponseError(Exception):
    """Preserve upstream HTTP status and error bytes for protocol-specific mapping."""

    def __init__(self, status, raw):
        self.status = status
        self.raw = raw
        super().__init__(f"upstream HTTP {status}")


class UpstreamHTTPError(UpstreamResponseError):
    """Distinguish actual upstream HTTP errors from failures synthesized while collecting a response."""

    def __init__(self, status, raw, *, retry_after=None):
        super().__init__(status, raw)
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}


MAX_RETRY_AFTER = 86400


def parse_retry_after(value, *, now=None) -> int | None:
    """Normalize bounded Retry-After seconds or HTTP dates; ignore invalid or expired values."""
    if not isinstance(value, str) or len(value) > 128 or not value.isascii() or not value.isprintable():
        return None
    value = value.strip()
    if not value:
        return None
    try:
        if value.isdecimal():
            delay = int(value)
        else:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)  # Obsolete HTTP asctime uses GMT.
            delay = deadline.timestamp() - (time.time() if now is None else now)
        return math.ceil(delay) if 0 <= delay <= MAX_RETRY_AFTER else None
    except (TypeError, ValueError, OverflowError):
        return None


class ChatSSEAccumulator:
    """Collect Chat SSE and reject error events, empty output and incomplete streams."""

    def __init__(self, *, collect=True, max_collect_bytes: int = 0):
        self.collect = collect
        self.max_collect_bytes = max(0, int(max_collect_bytes or 0))
        self.collected_bytes = 0
        self.content = []
        self.reasoning = []
        self.refusal = []
        self.tools = {}
        self.model = self.finish_reason = self.usage = None
        self.done = self.saw_choice = self.saw_output = False
        self.filter_detector = ContentFilterDetector()

    def feed_line(self, line):
        line = line.strip()
        if self.done or not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            self.done = True
            return
        try:
            chunk = json.loads(data)
        except ValueError:
            raise httpx.RemoteProtocolError("Invalid JSON in upstream SSE") from None
        if not isinstance(chunk, dict):
            raise httpx.RemoteProtocolError("Invalid upstream SSE object")
        if chunk.get("error") is not None:
            raise UpstreamResponseError(502, json.dumps(chunk).encode("utf-8"))
        try:
            self._consume_chunk(chunk)
        except (AttributeError, TypeError, ValueError):
            raise httpx.RemoteProtocolError("Invalid upstream SSE fields") from None

    def _consume_chunk(self, chunk):
        usage = chunk.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise ValueError("usage")
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens",
                        "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                    raise ValueError(key)
            for key in ("prompt_tokens_details", "completion_tokens_details"):
                details = usage.get(key)
                if details is not None:
                    if not isinstance(details, dict):
                        raise ValueError(key)
                    for count in details.values():
                        if count is not None and (type(count) is not int or count < 0):
                            raise ValueError(key)
        if chunk.get("model") is not None and not isinstance(chunk["model"], str):
            raise ValueError("model")
        if "choices" in chunk and not isinstance(chunk["choices"], list):
            raise ValueError("choices")
        self.model = chunk.get("model") or self.model
        self.usage = chunk.get("usage") or self.usage
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                raise ValueError("choice")
            if choice.get("finish_reason") is not None and not isinstance(choice["finish_reason"], str):
                raise ValueError("finish_reason")
            self.saw_choice = True
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise ValueError("delta")
            for key in ("content", "reasoning_content", "refusal"):
                if delta.get(key) is not None and not isinstance(delta[key], str):
                    raise ValueError(key)
                if delta.get(key):
                    self.saw_output = True
            if "tool_calls" in delta and not isinstance(delta["tool_calls"], list):
                raise ValueError("tool_calls")
            if self.collect:
                for key in ("content", "reasoning_content", "refusal"):
                    if delta.get(key):
                        getattr(self, key if key != "reasoning_content" else "reasoning").append(delta[key])
                        self._charge(len(delta[key].encode("utf-8")))
            for tool in delta.get("tool_calls") or []:
                if not isinstance(tool, dict):
                    raise ValueError("tool")
                index = tool.get("index", 0)
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise ValueError("tool index")
                if tool.get("id") is not None and not isinstance(tool["id"], str):
                    raise ValueError("tool id")
                slot = self.tools.setdefault(index, {"id": None, "name": None, "arguments": ""})
                slot["id"] = tool.get("id") or slot["id"]
                function = tool.get("function", {})
                if not isinstance(function, dict):
                    raise ValueError("function")
                for key in ("name", "arguments"):
                    if function.get(key) is not None and not isinstance(function[key], str):
                        raise ValueError(key)
                if tool.get("id") or function.get("name") or function.get("arguments"):
                    self.saw_output = True
                slot["name"] = function.get("name") or slot["name"]
                if self.collect:
                    piece = function.get("arguments") or ""
                    slot["arguments"] += piece
                    self._charge(len(piece.encode("utf-8")))
            self.filter_detector.feed(delta, choice.get("finish_reason"))

    def _charge(self, size: int):
        """Fail when collected bytes exceed the configured memory budget."""
        if not self.collect or not self.max_collect_bytes:
            return
        self.collected_bytes += size
        if self.collected_bytes > self.max_collect_bytes:
            raise UpstreamResponseError(502, json.dumps({"error": {
                "message": f"upstream response exceeds the {self.max_collect_bytes}-byte collection budget",
                "type": "upstream_error", "code": "response_too_large"}}).encode())

    def result(self):
        if not self.saw_choice or not (self.done or self.finish_reason):
            raise httpx.RemoteProtocolError("Upstream SSE ended without a completion marker")
        if not self.saw_output:
            if self.finish_reason in ("content_filter", "content-filter", "refusal"):
                raw = {"error": {"type": "upstream_error", "code": self.finish_reason,
                                 "message": "Upstream rejected the response without output"}}
                raise UpstreamResponseError(502, json.dumps(raw).encode("utf-8"))
            raw = {"error": {"type": "upstream_error", "code": "empty_response",
                             "message": "Upstream SSE ended without output"}}
            raise UpstreamResponseError(502, json.dumps(raw).encode("utf-8"))
        tools = [{"id": value["id"], "type": "function",
                  "function": {"name": value["name"], "arguments": value["arguments"]}}
                 for _, value in sorted(self.tools.items())] or None
        return {"content": "".join(self.content), "reasoning_content": "".join(self.reasoning) or None,
                "refusal": "".join(self.refusal) or None,
                "tool_calls": tools, "finish_reason": self.finish_reason,
                "usage": self.usage, "model": self.model}


ERROR_BODY_LIMIT = 4 * 1024 * 1024  # Bound error-body memory usage.


async def read_bounded_error(response, limit: int = ERROR_BODY_LIMIT) -> bytes:
    """Read and truncate upstream error bytes within a fixed budget."""
    if limit <= 0:
        return b""
    buf = bytearray()
    async for chunk in response.aiter_bytes():
        buf.extend(chunk[:limit - len(buf)])
        if len(buf) >= limit:
            break
    return bytes(buf)


# Connection failures occur before any request body is sent.
BODY_NOT_ACCEPTED = (httpx.ConnectError, httpx.ConnectTimeout)
# Write-timeout replay is opt-in because partial requests may already have been processed.
WRITE_TIMEOUT = (httpx.WriteTimeout,)


@asynccontextmanager
async def _attempt_client(url, timeout, clients):
    client = clients.get(url) if clients is not None else None
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=timeout) as client:
            yield client


@asynccontextmanager
async def open_backend_stream(url, headers, body, *, read_timeout=300, on_retry=None,
                              retry_write_timeout=False, clients=None, headers_for_attempt=None):
    """Retry connection failures once on a fresh client; write timeouts require explicit opt-in.
    Never replay after the upstream response opens.
    """
    retryable = BODY_NOT_ACCEPTED + (WRITE_TIMEOUT if retry_write_timeout else ())
    timeout = httpx.Timeout(read_timeout, connect=15, write=60, pool=15)
    for attempt in range(2):
        opened = False
        try:
            async with _attempt_client(url, timeout, clients if attempt == 0 else None) as client:
                attempt_headers = headers_for_attempt() if headers_for_attempt is not None else headers
                async with client.stream("POST", url, headers=attempt_headers, json=body, timeout=timeout) as response:
                    opened = True
                    yield response
                    return
        except retryable as error:
            if opened or attempt == 1:
                raise
            if on_retry is not None:
                on_retry(error)
            await asyncio.sleep(0.25)
