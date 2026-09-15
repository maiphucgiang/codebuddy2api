#!/usr/bin/env python3
"""Bound raw inference request bytes before JSON parsing and replay accepted bodies unchanged."""

from __future__ import annotations

import json


import asyncio


_GATED_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
_BODY_PATHS = (*_GATED_PATHS, "/v1/messages/count_tokens")


class ConcurrencyLimitMiddleware:
    """Reject excess inference concurrency with HTTP 503 and retain slots through response completion."""

    def __init__(self, app, config):
        self.app = app
        self.config = config
        self._semaphore = None

    def _gate(self):
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._limit())
        return self._semaphore

    def _limit(self) -> int:
        try:
            return max(0, int(self.config.get("max_concurrent") or 0))
        except (TypeError, ValueError):
            return 0

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path", "") not in _GATED_PATHS):
            return await self.app(scope, receive, send)
        limit = self._limit()
        if limit <= 0:
            return await self.app(scope, receive, send)
        gate = self._gate()
        if gate.locked():  # Reject overload without queueing request bodies.
            error = {"message": "inference concurrency limit reached, retry later",
                     "type": "rate_limit_error", "code": "concurrency_limit"}
            payload = {"error": error}
            if scope["path"] == "/v1/messages":
                error["type"] = "api_error"
                payload["type"] = "error"
            raw = json.dumps(payload).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"retry-after", b"3"),
                                    (b"content-length", str(len(raw)).encode())]})
            await send({"type": "http.response.body", "body": raw})
            return
        await gate.acquire()
        try:
            await self.app(scope, receive, send)
        finally:
            gate.release()


class InboundBodyLimitMiddleware:
    """Buffer only inference and token-estimation POST bodies."""

    def __init__(self, app, config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path", "") not in _BODY_PATHS):
            return await self.app(scope, receive, send)
        try:
            limit = int(self.config.get("max_inbound_bytes") or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return await self.app(scope, receive, send)

        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                more = bool(message.get("more_body"))
                if len(body) > limit:
                    return await self._reject(send, scope["path"], limit)
            elif message["type"] == "http.disconnect":
                return

        buffered = bytes(body)
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()  # Body completion does not imply client disconnect.
            replayed = True
            return {"type": "http.request", "body": buffered, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send, path: str, limit: int):
        # Middleware builds protocol-shaped errors before routing.
        if path.startswith("/v1/messages"):
            payload = {"type": "error", "error": {"type": "invalid_request_error",
                                                  "message": f"request body exceeds {limit} bytes",
                                                  "code": "request_too_large"}}
        else:
            payload = {"error": {"message": f"request body exceeds {limit} bytes",
                                 "type": "invalid_request_error", "code": "request_too_large"}}
        raw = json.dumps(payload).encode("utf-8")
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(raw)).encode())]})
        await send({"type": "http.response.body", "body": raw})
