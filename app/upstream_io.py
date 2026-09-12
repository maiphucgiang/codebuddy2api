"""有界连接重试；请求已发送或响应已开始后不重放 POST。"""

import asyncio
from contextlib import asynccontextmanager

import json
import httpx

from app.content_filter import ContentFilterDetector


class UpstreamResponseError(Exception):
    """保留上游 HTTP 状态与错误体，由端点映射为对应协议。"""

    def __init__(self, status, raw):
        self.status = status
        self.raw = raw
        super().__init__(f"upstream HTTP {status}")


class ChatSSEAccumulator:
    """聚合 Chat SSE，拒绝错误事件、空输出和无结束标记的残流。"""

    def __init__(self, *, collect=True):
        self.collect = collect
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
            if self.collect and delta.get("content"):
                self.content.append(delta["content"])
            if self.collect and delta.get("reasoning_content"):
                self.reasoning.append(delta["reasoning_content"])
            if self.collect and delta.get("refusal"):
                self.refusal.append(delta["refusal"])
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
                    slot["arguments"] += function.get("arguments") or ""
            self.filter_detector.feed(delta, choice.get("finish_reason"))

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


@asynccontextmanager
async def open_backend_stream(url, headers, body, *, read_timeout=300, on_retry=None):
    """只重试一次建连失败，其他错误交给调用方按协议返回。"""
    timeout = httpx.Timeout(read_timeout, connect=15, write=60, pool=15)
    for attempt in range(2):
        opened = False
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    opened = True
                    yield response
                    return
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            if opened or attempt == 1:
                raise
            if on_retry is not None:
                on_retry(error)
            await asyncio.sleep(0.25)
