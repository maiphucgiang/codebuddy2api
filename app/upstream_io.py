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


class UpstreamHTTPError(UpstreamResponseError):
    """上游**用 HTTP 状态码**给出的答复（429 / 401 / 503 …）。

    与聚合器从 200 响应体里合成的 502（空流、坏 SSE、已开流后断连）区分开：只有前者的请求
    确定没被上游收下处理，换一个账号重放不会重复计费；后者上游已经回了 200，可能已经计费。
    """


class ChatSSEAccumulator:
    """聚合 Chat SSE，拒绝错误事件、空输出和无结束标记的残流。"""

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
        """聚合收集总字节预算：超限即失败，不把无界输出缓存在内存里。"""
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


ERROR_BODY_LIMIT = 4 * 1024 * 1024  # 错误响应读取上限：错误页不应撑爆内存


async def read_bounded_error(response, limit: int = ERROR_BODY_LIMIT) -> bytes:
    """错误体有界读取：超限即截断，不再整段 aread。"""
    if limit <= 0:
        return b""
    buf = bytearray()
    async for chunk in response.aiter_bytes():
        buf.extend(chunk[:limit - len(buf)])
        if len(buf) >= limit:
            break
    return bytes(buf)


# 写下第一个请求体字节之前就失败：上游手里没有任何正文，重放零风险。
BODY_NOT_ACCEPTED = (httpx.ConnectError, httpx.ConnectTimeout)
# 写请求体超时：只证明「声明的正文没写完」，证不了上游没收到或没处理已经收到的那部分。
# 半截正文会怎样是上游的行为，从这一侧观察不到，因此默认不重放（见 `retry_write_timeout`）。
WRITE_TIMEOUT = (httpx.WriteTimeout,)


@asynccontextmanager
async def open_backend_stream(url, headers, body, *, read_timeout=300, on_retry=None,
                              retry_write_timeout=False):
    """只重试一次「上游确定没收下请求体」的失败（建连失败 / 建连超时），其余交给调用方按协议返回。

    重试每次新建 `AsyncClient`，即重建 TCP+TLS，通常能换到另一个边缘节点。

    `retry_write_timeout=True` 把 60s 写超时也算进重放集。跨境长会话最容易撞的正是写超时
    而不是建连失败，实测某部署的传输失败 100% 是它；但写超时能证明的只有「正文没写完」，
    上游是否已按半截正文动过账，这一侧看不到，所以留给运维显式决定。

    响应已经开始之后（`opened` 置位）绝不重放 POST。
    """
    retryable = BODY_NOT_ACCEPTED + (WRITE_TIMEOUT if retry_write_timeout else ())
    timeout = httpx.Timeout(read_timeout, connect=15, write=60, pool=15)
    for attempt in range(2):
        opened = False
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    opened = True
                    yield response
                    return
        except retryable as error:
            if opened or attempt == 1:
                raise
            if on_retry is not None:
                on_retry(error)
            await asyncio.sleep(0.25)
