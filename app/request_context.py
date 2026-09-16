"""Separate local request identity, optional session hints and individual upstream attempts."""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import secrets
import threading
import uuid


PATHS = {"/v1/chat/completions": "chat", "/v1/responses": "responses", "/v1/messages": "messages"}
_SCOPE_KEY = "codebuddy.request_context"
_current = ContextVar("request_context", default=None)


class SessionIdentifierError(ValueError):
    """Reject ambiguous or unsafe session hints without echoing their contents."""


def _identifier(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 512 or not value.isprintable():
        raise SessionIdentifierError("session ID must be a printable string of at most 512 UTF-8 bytes")
    try:
        if len(value.encode("utf-8")) > 512:
            raise SessionIdentifierError("session ID exceeds 512 UTF-8 bytes")
    except UnicodeError:
        raise SessionIdentifierError("session ID contains invalid Unicode") from None
    return value.strip() or None


def _digest(value):
    digest = hashlib.sha256()
    for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value):
        for offset in range(0, len(part), 65536):
            digest.update(part[offset:offset + 65536].encode("utf-8"))
    return digest.hexdigest()


def _content_parts(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text", "output_text"):
            if block.get("text"):
                parts.append({"type": "text", "text": block["text"]})
        else:
            parts.append({key: value for key, value in block.items() if key != "cache_control"})
    return parts


@dataclass(frozen=True)
class Attempt:
    id: str
    index: int
    span: str


class RequestContext:
    def __init__(self, protocol, mode="legacy", headers=()):
        self.request_id = uuid.uuid4().hex
        self.protocol = protocol
        self.mode = mode
        self._session_headers = tuple(value.decode("latin-1") for key, value in headers
                                      if key.lower() == b"x-codebuddy-session-id") if self.scoped else ()
        self.session_key = "scoped:v1:temporary:" + self.request_id
        self.session_source = "temporary" if self.scoped else "legacy"
        self._bound = False
        self._root_span = secrets.token_hex(8)
        self._lock = threading.Lock()
        self._attempt_index = 0
        self.attempt = None

    @property
    def scoped(self):
        return self.mode == "scoped"

    def bind_session(self, payload, messages):
        """Fingerprint adapted messages before projection or desensitization, once per request."""
        if not self.scoped or self._bound:
            return
        hints = [("explicit_header", value) for value in self._session_headers]
        metadata = payload.get("metadata")
        for label, source in (("explicit_metadata", metadata), ("explicit_body", payload)):
            if isinstance(source, dict):
                hints.extend((label, source[key]) for key in ("conversation_id", "conversationId") if key in source)
        clean = [(label, _identifier(value)) for label, value in hints]
        clean = [(label, value) for label, value in clean if value is not None]
        if len({value for _, value in clean}) > 1:
            raise SessionIdentifierError("conflicting session IDs")
        if clean:
            self.session_key = "scoped:v1:" + _digest([self.protocol, "explicit", clean[0][1]])
            self.session_source = clean[0][0]
        elif isinstance(messages, list):
            instructions = [_content_parts(m.get("content")) for m in messages
                            if isinstance(m, dict) and m.get("role") in ("system", "developer")]
            first = next((m for m in messages if isinstance(m, dict) and m.get("role") == "user"), {})
            content = _content_parts(first.get("content"))
            if content:
                self.session_key = "scoped:v1:" + _digest([self.protocol, "content", instructions, content])
                self.session_source = "fingerprint"
        self._session_headers = ()
        self._bound = True

    def conversation_id(self, profile, account):
        return str(uuid.UUID(hex=_digest([profile, account, self.session_key])[:32]))

    def start_attempt(self):
        with self._lock:
            self._attempt_index += 1
            self.attempt = Attempt(uuid.uuid4().hex, self._attempt_index, secrets.token_hex(8))
            return self.attempt

    def attempt_headers(self, headers, attempt):
        if not self.scoped:
            return dict(headers)
        root, span = self.request_id, attempt.span
        return {**headers, "X-Request-ID": attempt.id, "X-Conversation-Message-ID": attempt.id,
                "X-Conversation-Request-ID": root, "X-Root-Request-ID": root, "X-Trace-ID": root,
                "traceparent": f"00-{root}-{span}-01", "b3": f"{root}-{span}-1-{self._root_span}",
                "X-B3-TraceId": root, "X-B3-SpanId": span, "X-B3-ParentSpanId": self._root_span,
                "X-B3-Sampled": "1"}

    def attempt_metadata(self):
        data = {"request_id": self.request_id, "session_source": self.session_source}
        if self.attempt is not None:
            data.update(attempt_id=self.attempt.id, attempt_index=self.attempt.index)
        return data


def current_context():
    return _current.get()


def ensure_context(scope, config=None):
    """Share one identity through middleware regardless of audit availability or ordering."""
    if _SCOPE_KEY not in scope:
        values = config() if callable(config) else config
        mode = values.get("request_context_mode", "legacy") if isinstance(values, dict) else "legacy"
        scope[_SCOPE_KEY] = RequestContext(PATHS[scope["path"]], mode, scope.get("headers", ()))
    return scope[_SCOPE_KEY]


async def with_request_context(app, scope, receive, send, config=None):
    if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") not in PATHS:
        return await app(scope, receive, send)
    context = ensure_context(scope, config)
    if current_context() is context:
        return await app(scope, receive, send)
    token = _current.set(context)
    async def identified_send(message):
        if message["type"] == "http.response.start":
            headers = [(key, value) for key, value in message.get("headers", ()) if key.lower() != b"x-request-id"]
            message = {**message, "headers": [*headers, (b"x-request-id", context.request_id.encode("ascii"))]}
        await send(message)
    try:
        await app(scope, receive, identified_send)
    finally:
        _current.reset(token)


class RequestContextMiddleware:
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        await with_request_context(self.app, scope, receive, send, self.config)
