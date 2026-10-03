"""Bounded ACP channel for one WorkBuddy agent turn over streamable HTTP.

WorkBuddy's console web app drives an agent turn with the Agent Client Protocol
transport: one long-lived ``GET`` channel carrying server-sent events, plus one
short JSON-RPC ``POST`` per method, both tied together by the ``Acp-Connection-Id``
response header. Only enough of the protocol is implemented here to run a single
turn and report a bounded outcome -- turn results are never interpreted, because
completion is read back from the console API instead.

``http.client`` is used deliberately rather than ``httpx``: the channel has to
stay open for the whole turn while console polling proceeds on separate
connections, and its bytes are drained opportunistically so a read can never
block the caller. That is the one place this module deviates from the ``httpx``
style of the other automation modules.
"""
import json
import select
import time
from http.client import HTTPConnection, HTTPSConnection, HTTPSConnection as _HTTPS
from urllib.parse import urlsplit

from .credits import BROWSER_UA

PROTOCOL_VERSION = 1
# A turn must never depend on client-provided files, terminals or permissions:
# declaring them would make the sandbox wait for callbacks it will not get.
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False},
                       "terminal": False}
MAX_CHANNEL_BYTES = 256 * 1024
CONNECT_TIMEOUT = 10.0
POST_TIMEOUT = 30.0
DRAIN_STEP = 0.2
READ_CHUNK = 8192
_METHODS = ("initialize", "session/load", "session/prompt")


class AcpError(ValueError):
    """One bounded failure; never carries upstream text or headers."""

    def __init__(self, kind, http_status=None, code=None):
        super().__init__("ACP turn was not confirmed")
        self.diagnostics = {"error_kind": kind, "http_status": http_status, "code": code}


def _target(link):
    parts = urlsplit(link if isinstance(link, str) else "")
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.path:
        raise AcpError("protocol")
    # Keep the query string: dropping it would silently change the sandbox endpoint.
    return parts, parts.path + (("?" + parts.query) if parts.query else "")


class AcpChannel:
    """One SSE channel plus the JSON-RPC requests bound to its connection id."""

    def __init__(self, link, token):
        self.parts, self.path = _target(link)
        token = token if isinstance(token, str) else ""
        self.token = token
        self.connection_id = None
        self._connection = None
        self._response = None
        self._buffer = bytearray()
        self._line = bytearray()
        self._closed = False

    def open(self):
        self._connection = self._connect()
        try:
            self._connection.putrequest("GET", self.path)
            self._connection.putheader("Accept", "text/event-stream")
            self._connection.putheader("Accept-Encoding", "identity")
            self._connection.putheader("Authorization", "Bearer " + self.token)
            self._connection.putheader("User-Agent", BROWSER_UA)
            self._connection.endheaders()
            self._response = self._connection.getresponse()
        except (OSError, ValueError):
            self.close()
            raise AcpError("network") from None
        if self._response.status != 200:
            status = self._response.status
            self.close()
            raise AcpError("http", status)
        connection_id = self._response.getheader("Acp-Connection-Id")
        if not isinstance(connection_id, str) or not connection_id.strip():
            self.close()
            raise AcpError("protocol")
        self.connection_id = connection_id.strip()
        return self

    def post(self, method, params, request_id):
        """Send one JSON-RPC request on a fresh connection; its reply rides the channel."""
        if method not in _METHODS:
            raise AcpError("protocol")
        if self._closed or not self.connection_id:
            raise AcpError("protocol")
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": params}, allow_nan=False).encode()
        connection = self._connect()
        status = None
        try:
            connection.putrequest("POST", self.path)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Accept", "application/json, text/event-stream")
            connection.putheader("Acp-Connection-Id", self.connection_id)
            connection.putheader("Authorization", "Bearer " + self.token)
            connection.putheader("User-Agent", BROWSER_UA)
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(message_body=body)
            response = connection.getresponse()
            response.read()
            status = response.status
        except (OSError, ValueError):
            raise AcpError("network") from None
        finally:
            self._drop(connection)
        if status not in (200, 202):
            raise AcpError("http", status)

    def drain(self, seconds):
        """Read available channel bytes without blocking; return harvested usage updates."""
        updates = []
        if self._closed or self._response is None:
            return updates
        deadline = time.monotonic() + max(0.0, float(seconds))
        while len(self._buffer) <= MAX_CHANNEL_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readable, _, _ = select.select([self._response.fp], [], [],
                                               min(DRAIN_STEP, remaining))
            except (OSError, ValueError, TypeError):
                break
            if not readable:
                continue
            try:
                chunk = self._response.read1(READ_CHUNK)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            self._buffer.extend(chunk)
            updates.extend(self._events())
        return updates

    def _events(self):
        """Yield ACP ``usage_update`` notifications from the drained buffer."""
        found = []
        while True:
            break_at = self._buffer.find(b"\n")
            if break_at < 0:
                break
            line = bytes(self._buffer[:break_at])
            del self._buffer[:break_at + 1]
            line = line.rstrip(b"\r")
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                message = json.loads(payload)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(message, dict):
                continue
            update = message.get("params", {})
            if not isinstance(update, dict):
                continue
            update = update.get("update")
            if (message.get("method") == "session/update" and isinstance(update, dict)
                    and update.get("sessionUpdate") == "usage_update"):
                found.append(update)
        return found

    def close(self):
        self._closed = True
        response, connection = self._response, self._connection
        self._response = self._connection = None
        if response is not None:
            try:
                response.close()
            except (OSError, ValueError):
                pass
        if connection is not None:
            self._drop(connection)

    def _connect(self):
        host = self.parts.hostname
        if not host:
            raise AcpError("protocol")
        try:
            if self.parts.scheme == "https":
                return _HTTPS(host, self.parts.port, timeout=CONNECT_TIMEOUT)
            return HTTPConnection(host, self.parts.port, timeout=CONNECT_TIMEOUT)
        except (OSError, ValueError):
            raise AcpError("network") from None

    @staticmethod
    def _drop(connection):
        try:
            connection.close()
        except (OSError, ValueError):
            pass


def run_turn(link, token, session_id, cwd, prompt, *, on_prompt=None):
    """Open the channel and drive one turn; return it open for console polling.

    ``on_prompt`` runs immediately before the prompt is posted, so the caller can
    record the turn as committed at the last moment where that is still knowable.
    """
    channel = AcpChannel(link, token).open()
    try:
        channel.post("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                    "clientCapabilities": CLIENT_CAPABILITIES}, 1)
        channel.post("session/load", {"sessionId": session_id, "cwd": cwd,
                                      "mcpServers": []}, 2)
        if on_prompt is not None:
            on_prompt()
        channel.post("session/prompt", {"sessionId": session_id,
                                        "prompt": [{"type": "text", "text": prompt}]}, 3)
    except Exception:
        channel.close()
        raise
    return channel
