"""Single-key, management-only sessions and ASGI protection for legacy routes."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

COOKIE_NAME = "cb_admin_session"
SESSION_TTL = 12 * 3600


def error_response(status, message):
    return JSONResponse({"error": {"message": message, "type": "conflict_error" if status == 409 else "admin_error"}}, status_code=status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def same_origin(request):
    origin = request.headers.get("origin")
    if not origin:
        # Browsers often omit Origin on same-origin GET; CSRF is still required for OAuth polling.
        return request.method in ("GET", "HEAD") and request.headers.get("sec-fetch-site") == "same-origin"
    try:
        supplied = urlsplit(origin)
        target = urlsplit(str(request.url))
        return (supplied.scheme in ("http", "https") and not supplied.username and not supplied.password
                and not supplied.path and not supplied.query and not supplied.fragment
                and (supplied.scheme, supplied.hostname, supplied.port or (443 if supplied.scheme == "https" else 80))
                == (target.scheme, target.hostname, target.port or (443 if target.scheme == "https" else 80)))
    except ValueError:
        return False


class AdminAuth:
    def __init__(self, config, *, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = OrderedDict()
        self.failures = OrderedDict()
        self._fingerprint = None

    def _key(self):
        key = self.config.get("api_key") or ""
        if not isinstance(key, str):
            key = ""
        fingerprint = hashlib.sha256(key.encode()).digest()
        if fingerprint != self._fingerprint:
            self.sessions.clear()
            self._fingerprint = fingerprint
        return key

    def enabled(self):
        with self.lock:
            return bool(self._key())

    def check_key(self, candidate):
        with self.lock:
            key = self._key()
            return bool(key) and isinstance(candidate, str) and hmac.compare_digest(candidate.encode(), key.encode())

    def header_identity(self, request):
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if self.check_key(bearer) or self.check_key(request.headers.get("x-api-key")):
            with self.lock:
                return "key:" + self._fingerprint.hex()
        return None

    def session(self, request):
        sid = request.cookies.get(COOKIE_NAME, "")
        with self.lock:
            self._key()
            item = self.sessions.get(sid)
            if item and item["expires"] > self.clock():
                return sid, dict(item)
            self.sessions.pop(sid, None)
        return None, None

    def login(self, request, key):
        address = request.client.host if request.client else "unknown"
        now = self.clock()
        with self.lock:
            self._key()
            for peer, (started, _) in list(self.failures.items()):
                if now - started >= 60:
                    del self.failures[peer]
            started, count = self.failures.get(address, (now, 0))
            if count >= 10:
                return None, 429
            if not self.check_key(key):
                self.failures[address] = (started, count + 1)
                self.failures.move_to_end(address)
                while len(self.failures) > 1024:
                    self.failures.popitem(last=False)
                return None, 401
            self.failures.pop(address, None)
            old = request.cookies.get(COOKIE_NAME)
            self.sessions.pop(old, None)
            sid = secrets.token_urlsafe(32)
            item = {"csrf_token": secrets.token_urlsafe(32), "expires": now + SESSION_TTL}
            self.sessions[sid] = item
            while len(self.sessions) > 256:
                self.sessions.popitem(last=False)
            return (sid, dict(item)), 200

    def logout(self, request):
        with self.lock:
            self.sessions.pop(request.cookies.get(COOKIE_NAME), None)


class AdminMiddleware:
    def __init__(self, app, auth, dispatch=None):
        self.app, self.auth, self.dispatch = app, auth, dispatch

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin/"):
            return await self.app(scope, receive, send)
        request = Request(scope, receive)

        async def no_cache(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in (b"cache-control", b"pragma", b"expires")]
                message = {**message, "headers": headers + [(b"cache-control", b"no-store"), (b"pragma", b"no-cache"), (b"expires", b"0")]}
            await send(message)

        if not self.auth.enabled():
            return await error_response(503, "未配置 API key，管理接口已锁定")(scope, receive, no_cache)
        path, method = scope["path"], scope["method"]
        public_session = path == "/admin/session" and method in ("POST", "GET")
        identity = self.auth.header_identity(request)
        sid, session = self.auth.session(request)
        cookie = not identity and session is not None
        if not public_session and not identity and not cookie:
            return await error_response(401, "管理认证无效或会话已过期")(scope, receive, no_cache)
        if cookie and not public_session and (method not in ("GET", "HEAD", "OPTIONS") or path == "/admin/oauth/poll"):
            supplied = request.headers.get("x-csrf-token", "")
            if not same_origin(request) or not hmac.compare_digest(supplied.encode(), session["csrf_token"].encode()):
                return await error_response(403, "Origin 或 CSRF 校验失败")(scope, receive, no_cache)
        scope.setdefault("state", {}).update(admin_identity=identity or sid, admin_cookie=cookie,
                                              admin_session=session)
        if cookie:
            # Only this /admin ASGI scope receives the header, never the client or inference scope.
            with self.auth.lock:
                key = self.auth._key()
                if sid not in self.auth.sessions:
                    return await error_response(401, "管理会话已失效")(scope, receive, no_cache)
            scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k.lower() not in (b"authorization", b"x-api-key")]
                     + [(b"authorization", ("Bearer " + key).encode())]}
        if self.dispatch and not public_session:
            response = await self.dispatch(Request(scope, receive))
            if response is not None:
                return await response(scope, receive, no_cache)
        return await self.app(scope, receive, no_cache)
