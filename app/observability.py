"""Metadata-only observation for the three public inference POST endpoints.

Request bodies and headers are never inspected. Response parsing retains two
16 KiB buffers at most, discards oversized SSE lines/JSON, and never changes the ASGI wire. Route
hooks should provide model/account identifiers (never a credential file/token).
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import time
import uuid
from typing import Any

from app.audit_store import AuditStore, METRICS, number, safe_attempt, safe_label

_PATHS = {"/v1/chat/completions": "chat", "/v1/responses": "responses", "/v1/messages": "messages"}
_PARSE_LIMIT = 16384
_current: ContextVar[_Observation | None] = ContextVar("audit_observation", default=None)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def normalize_usage(usage):
    """Keep provider counters independent; cache/reasoning are not extra total.

No total is fabricated: Anthropic input/cache semantics differ from OpenAI.
A known zero is retained. `usage_source` identifies observation provenance, not
an inferred billing rate; credit is never derived from token counts.
"""
    usage = _mapping(usage)
    result = {}
    aliases = {"input_tokens": ("input_tokens", "prompt_tokens"),
               "output_tokens": ("output_tokens", "completion_tokens"),
               "cache_read_tokens": ("cache_read_tokens", "cache_read_input_tokens", "cached_tokens", "cache"),
               "cache_creation_tokens": ("cache_creation_tokens", "cache_creation_input_tokens"),
               "reasoning_tokens": ("reasoning_tokens", "reasoning"),
               "total_tokens": ("total_tokens",), "credit": ("credit",)}
    for key, names in aliases.items():
        for name in names:
            value = number(usage.get(name))
            if value is not None:
                result[key] = value
                break
    for key, parents, child in (
        ("cache_read_tokens", ("input_tokens_details", "prompt_tokens_details"), "cached_tokens"),
        ("reasoning_tokens", ("output_tokens_details", "completion_tokens_details"), "reasoning_tokens"),
    ):
        if key not in result:
            for parent in parents:
                value = number(_mapping(usage.get(parent)).get(child))
                if value is not None:
                    result[key] = value
                    break
    return result


@dataclass
class _Observation:
    record: dict
    monotonic_start: float
    attempts: list = field(default_factory=list)
    failed: bool = False
    terminal: bool = False
    body_finished: bool = False
    status: int | None = None
    streaming: bool = False
    first_token_ms: float | None = None
    usage_priority: dict = field(default_factory=dict)
    usage_sources: dict = field(default_factory=dict)
    parser_dropped: int = 0

    def fail(self, code):
        self.failed = True
        self.record["error_code"] = safe_label(code, 80) or "upstream_error"

    def usage(self, value, source, priority):
        fields = normalize_usage(value)
        for key, counter in fields.items():
            if priority >= self.usage_priority.get(key, -1):
                self.record[key] = counter
                self.usage_priority[key] = priority
                self.usage_sources[key] = source
        if fields:
            sources = set(self.usage_sources.values())
            self.record["usage_source"] = next(iter(sources)) if len(sources) == 1 else "mixed"
            self.record["usage_sources"] = dict(self.usage_sources)

    def output(self):
        if self.streaming and self.first_token_ms is None:
            self.first_token_ms = (time.monotonic() - self.monotonic_start) * 1000

    def payload(self, value, source):
        value = _mapping(value)
        kind = value.get("type")
        error = value.get("error")
        if error is not None or kind in ("error", "response.failed", "response.incomplete"):
            error = _mapping(error)
            self.fail(error.get("code") or error.get("type") or kind or "upstream_error")
        response = _mapping(value.get("response"))
        if response.get("status") in ("failed", "incomplete", "cancelled"):
            self.fail("response_" + response["status"])
        if response.get("error"):
            self.fail(_mapping(response["error"]).get("code") or "upstream_error")
        for item in (value, response, _mapping(value.get("message"))):
            if "usage" in item:
                self.usage(item["usage"], source, 1)
            model = safe_label(item.get("model"))
            if model and not self.record.get("public_model"):
                self.record["public_model"] = model
        if kind in ("message_stop", "response.completed"):
            self.terminal = True
        if kind in ("response.output_text.delta", "response.reasoning_text.delta", "response.reasoning_summary_text.delta", "response.function_call_arguments.delta") and value.get("delta"):
            self.output()
        if kind == "content_block_delta":
            delta = _mapping(value.get("delta"))
            if any(delta.get(key) for key in ("text", "thinking", "partial_json")):
                self.output()
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices[:16]:
                choice = _mapping(choice)
                if choice.get("finish_reason") is not None:
                    self.terminal = True
                delta = _mapping(choice.get("delta"))
                meaningful = any(delta.get(key) for key in ("content", "reasoning_content", "reasoning"))
                calls = delta.get("tool_calls")
                functions = [_mapping(delta.get("function_call"))]
                if isinstance(calls, list):
                    functions += [_mapping(_mapping(call).get("function")) for call in calls[:16]]
                if meaningful or any(call.get("name") or call.get("arguments") for call in functions):
                    self.output()


def observe_route(public_model, upstream_model, profile, credential):
    observation = _current.get()
    if observation is not None:
        for key, value in (("public_model", public_model), ("upstream_model", upstream_model),
                           ("profile", profile), ("credential", credential)):
            clean = safe_label(value)
            if clean is not None:
                observation.record[key] = clean


def observe_usage(usage):
    observation = _current.get()
    if observation is not None:
        observation.usage(usage, "upstream_hook", 2)


def observe_attempt(stage, **safe_metadata):
    observation = _current.get()
    if observation is not None and len(observation.attempts) < 32:
        observation.attempts.append(safe_attempt({**safe_metadata, "stage": stage}))


def observe_failure(code):
    observation = _current.get()
    if observation is not None:
        observation.fail(code)


class _Parser:
    def __init__(self, observation):
        self.observation = observation
        self.buffer = bytearray()
        self.event_data = bytearray()
        self.discard_line = False
        self.discard_event = False
        self.json_oversized = False

    def _decode(self, raw, source):
        if raw.strip() == b"[DONE]":
            self.observation.terminal = True
            return
        try:
            self.observation.payload(json.loads(raw), source)
        except (ValueError, UnicodeError, RecursionError):
            # Malformed payload is not copied into an error record.
            self.observation.parser_dropped += 1

    def _line(self, line):
        line = line.rstrip(b"\r")
        if not line:
            if self.event_data and not self.discard_event:
                self._decode(self.event_data, "wire_sse")
            self.event_data.clear()
            self.discard_event = False
        elif line.startswith(b"event:"):
            if line[6:].strip() in (b"error", b"response.failed", b"response.incomplete"):
                self.observation.fail("sse_error")
        elif line.startswith(b"data:") and not self.discard_event:
            data = line[5:].lstrip(b" ")
            if len(self.event_data) + len(data) + 1 > _PARSE_LIMIT:
                self.event_data.clear()
                self.discard_event = True
                self.observation.parser_dropped += 1
            else:
                if self.event_data:
                    self.event_data.extend(b"\n")
                self.event_data.extend(data)

    def feed(self, body, final=False):
        if not self.observation.streaming:
            if not self.json_oversized:
                if len(self.buffer) + len(body) > _PARSE_LIMIT:
                    self.buffer.clear()
                    self.json_oversized = True
                    self.observation.parser_dropped += 1
                else:
                    self.buffer.extend(body)
            if final and self.buffer:
                self._decode(self.buffer, "wire_json")
                self.buffer.clear()
            return
        # Scan without split(): a huge incoming chunk cannot create a huge list
        # or retained copy. Only a bounded partial line/event survives a send.
        position = 0
        while position < len(body):
            end = body.find(b"\n", position)
            stop = len(body) if end == -1 else end
            if not self.discard_line:
                if len(self.buffer) + stop - position > _PARSE_LIMIT:
                    self.buffer.clear()
                    self.discard_line = self.discard_event = True
                    self.event_data.clear()
                    self.observation.parser_dropped += 1
                else:
                    self.buffer.extend(body[position:stop])
            if end == -1:
                break
            if not self.discard_line:
                self._line(self.buffer)
            self.buffer.clear()
            self.discard_line = False
            position = end + 1
        if final:
            if self.buffer and not self.discard_line:
                self._line(self.buffer)
            self._line(b"")
            self.buffer.clear()


class AuditMiddleware:
    def __init__(self, app, config):
        self.app = app
        if isinstance(config, AuditStore) or callable(getattr(config, "record_request", None)):
            self.store = config
        elif isinstance(config, dict):
            self.store = config.get("audit_store", config.get("store"))
        else:
            self.store = getattr(config, "audit_store", getattr(config, "store", None))
        self.observation_failures = 0
        self._pending = set()

    def _fault(self):
        self.observation_failures += 1
        if self.store is not None:
            try:
                self.store.note_failure()
            except Exception:
                pass  # Local counter still exposes stores without health support.

    async def drain(self):
        """Await shielded submissions before the owner's normal store.close()."""
        while self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            async def lifespan_receive():
                message = await receive()
                if message.get("type") == "lifespan.shutdown":
                    await self.drain()
                return message
            await self.app(scope, lifespan_receive, send)
            return
        if (self.store is None or scope.get("type") != "http" or scope.get("method") != "POST"
                or scope.get("path") not in _PATHS):
            await self.app(scope, receive, send)
            return
        started, monotonic_start = time.time(), time.monotonic()
        observation = _Observation({"id": uuid.uuid4().hex, "epoch": -1,
                                    "detail_generation": -1, "started_at": started,
                                    "protocol": _PATHS[scope["path"]],
                                    **{key: None for key in METRICS}}, monotonic_start)
        token = _current.set(observation)
        parser = _Parser(observation)
        cancelled = False

        async def observed_receive():
            message = await receive()
            if message.get("type") == "http.disconnect":
                nonlocal cancelled
                cancelled = True
            return message

        async def observed_send(message):
            # Observe only after successful delivery; never mutate the message.
            try:
                await send(message)
            except OSError:
                nonlocal cancelled
                cancelled = True
                raise
            try:
                if message.get("type") == "http.response.start":
                    observation.status = message.get("status")
                    observation.streaming = any(
                        key.lower() == b"content-type" and b"text/event-stream" in value.lower()
                        for key, value in message.get("headers", []))
                elif message.get("type") == "http.response.body":
                    final = not message.get("more_body", False)
                    parser.feed(message.get("body", b""), final)
                    if final:
                        observation.body_finished = True
            except Exception:
                self._fault()

        try:
            ticket_task = asyncio.create_task(asyncio.to_thread(self.store.ticket))
            try:
                observation.record.update(await asyncio.shield(ticket_task))
            except asyncio.CancelledError:
                # A disconnect during ticket acquisition is still one terminal
                # request, not an invisible gap before the observation context.
                try:
                    observation.record.update(await asyncio.shield(ticket_task))
                except Exception:
                    self._fault()
                raise
            except Exception:
                self._fault()
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BaseException:
            observation.fail("application_error")
            raise
        finally:
            try:
                if not observation.body_finished and not cancelled and not observation.failed:
                    observation.fail("incomplete_response")
                if observation.streaming and not observation.terminal and not cancelled and not observation.failed:
                    observation.fail("incomplete_stream")
                outcome = "cancelled" if cancelled else (
                    "error" if observation.failed or not observation.status or observation.status >= 400 else "success")
                observation.record.update(status_code=observation.status, outcome=outcome,
                                          streaming=observation.streaming,
                                          duration_ms=(time.monotonic() - monotonic_start) * 1000,
                                          first_token_ms=observation.first_token_ms,
                                          attempts=observation.attempts)
                if observation.parser_dropped:
                    observation.record["attempts"] = observation.attempts + [{"stage": "observer", "code": "bounded_parse_skipped"}]
                # Shield one bounded store operation from client cancellation.
                # No upstream replay and no promise of delivery after process exit.
                task = asyncio.create_task(asyncio.to_thread(self.store.record_request, observation.record))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # The thread continues; retain the task and consume its error.
                    task.add_done_callback(self._submission_done)
                    raise
                except Exception:
                    self._fault()
            finally:
                _current.reset(token)

    def _submission_done(self, task):
        try:
            task.result()
        except BaseException:
            self._fault()
