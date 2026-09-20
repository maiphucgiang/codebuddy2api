"""Single-key, management-only sessions and ASGI protection for legacy routes."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

COOKIE_NAME = "cb_admin_session"
SESSION_TTL = 12 * 3600


class SessionStoreError(RuntimeError):
    """A superseded session snapshot could not be durably revoked.

    Startup must abort on this: the surviving snapshot could otherwise be adopted by a
    later start under the superseded key, resurrecting sessions that were meant to die.
    """


# Optional persisted session table so a restart does not force another login.
# Revocation is enforced by clearing this file: the stored fingerprint only tells us
# which key epoch the snapshot belongs to, it is not an integrity MAC over the sessions.
SESSION_FILE_VERSION = 1
MAX_PERSISTED_SESSIONS = 256
_SESSION_FILE_BYTES = 256 * 1024
_SESSION_KEY_LABEL = b"codebuddy2api-admin-session-key-v1"
# SIDs and CSRF tokens are token_urlsafe(32); bound them so a crafted file cannot
# install arbitrarily large values into memory.
_MAX_TOKEN_CHARS = 128
_TOKEN = re.compile(r"\A[A-Za-z0-9_-]{16,%d}\Z" % _MAX_TOKEN_CHARS)
_FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")


def error_response(status, message):
    return JSONResponse({"error": {"message": message, "type": "conflict_error" if status == 409 else "admin_error"}}, status_code=status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _session_path(value):
    """Resolve the optional session file; a missing or unusable path keeps sessions in memory."""
    if not value:
        return None
    try:
        path = Path(os.path.abspath(os.fspath(value)))
    except (TypeError, ValueError):
        return None
    if not path.name or path.name in (".", ".."):
        return None
    return path


def _strict_json(content):
    """Parse JSON rejecting duplicate keys and non-finite constants."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON field")
            value[key] = item
        return value

    def invalid_constant(_):
        raise ValueError("Invalid JSON constant")

    return json.loads(content, object_pairs_hook=pairs, parse_constant=invalid_constant)


def origin_allowlist(value):
    """Parse a normalized origin list into comparable (scheme, host, port) triples."""
    triples = set()
    for entry in str(value or "").split(","):
        try:
            parts = urlsplit(entry.strip())
            port = parts.port
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and parts.hostname:
            triples.add((parts.scheme, parts.hostname, port if port is not None else (443 if parts.scheme == "https" else 80)))
    return frozenset(triples)


def same_origin(request, allowed=()):
    origin = request.headers.get("origin")
    reference = origin
    if origin is None:
        if request.method not in ("GET", "HEAD"):
            return False
        site = request.headers.get("sec-fetch-site")
        if site is not None:
            return site == "same-origin"
        # Plain-HTTP browsers may omit Fetch Metadata; OAuth polling still requires CSRF.
        reference = request.headers.get("referer")
        if not reference:
            return False
    try:
        supplied = urlsplit(reference)
        target = urlsplit(str(request.url))
        supplied_port = supplied.port if supplied.port is not None else (443 if supplied.scheme == "https" else 80)
        target_port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
        clean = (supplied.scheme in ("http", "https") and supplied.username is None and supplied.password is None
                 and not supplied.fragment and (origin is None or (not supplied.path and not supplied.query)))
        if clean and (supplied.scheme, supplied.hostname, supplied_port) == (target.scheme, target.hostname, target_port):
            return True
        # Explicitly trusted origins survive proxies that rewrite the forwarded Host/scheme.
        return (origin is not None and clean and not supplied.path and not supplied.query
                and (supplied.scheme, supplied.hostname, supplied_port) in allowed)
    except ValueError:
        return False


class AdminAuth:
    def __init__(self, config, *, clock=time.monotonic, wall_clock=time.time):
        self.config = config
        self.clock = clock          # Monotonic: login throttling only.
        self.wall_clock = wall_clock  # Wall clock: session expiry, so it survives a restart.
        self.lock = threading.RLock()
        self.sessions = OrderedDict()
        self.failures = OrderedDict()
        self._configured_key = None
        self._identity = None
        self._path = _session_path(config.get("session_path"))
        self._storage_error = None  # Set when the snapshot could not be written or cleared.

    @staticmethod
    def _fingerprint(key):
        """Keyed fingerprint naming the key epoch a snapshot belongs to.

        It identifies the epoch; revocation itself is performed by clearing the file.
        """
        return hmac.new(key.encode(), _SESSION_KEY_LABEL, hashlib.sha256).hexdigest()

    @staticmethod
    def _token(value):
        """Accept only bounded url-safe tokens, rejecting bools and non-strings."""
        return value if isinstance(value, str) and _TOKEN.match(value) else None

    @staticmethod
    def _deadline(value):
        """Accept only finite, in-range numeric deadlines; bool is not a deadline."""
        if type(value) not in (int, float):
            return None
        try:
            number = float(value)
        except OverflowError:
            # An integer too large for float() is not a deadline.
            return None
        return number if math.isfinite(number) and 0 < number < 1e11 else None

    def _storage_failed(self, error):
        """Record why the snapshot could not be made durable, so callers can report it."""
        self._storage_error = type(error).__name__

    def _storage_ok(self):
        self._storage_error = None

    def storage(self):
        """Whether the snapshot's durable state is known-good, mirroring AuditStore.storage().

        Reports the outcome of the last write or clear: a successful clear leaves no
        snapshot, so the on-disk state is consistent again and the flag goes back to False.
        """
        return {"path": str(self._path) if self._path is not None else None,
                "degraded": self._storage_error is not None,
                "last_error": self._storage_error}

    def _revoke(self):
        """Revoke the persisted snapshot; returns False when it could not be cleared."""
        if self._path is None:
            return True
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            return True
        except OSError as error:
            self._storage_failed(error)
            return False
        self._storage_ok()
        return True

    def _is_direct_file(self, metadata):
        """True when the opened file is the path itself rather than a symlink target."""
        try:
            link = os.lstat(self._path)
        except OSError:
            return False
        if stat.S_ISLNK(link.st_mode):
            return False
        # Identity is the fallback where lstat cannot report a link; both values are
        # zero on filesystems that do not expose inodes, which leaves the check above.
        return (link.st_dev, link.st_ino) == (metadata.st_dev, metadata.st_ino)

    def _restore(self, key):
        """Load persisted sessions for the current key epoch.

        Returns False when a snapshot exists that cannot be trusted or validated,
        so the caller revokes it instead of leaving it available to a later start.
        """
        if self._path is None:
            return True
        try:
            fd = os.open(self._path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return True
        except OSError:
            # Unreadable or not a plain readable file: treat as an unusable snapshot.
            return False
        try:
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _SESSION_FILE_BYTES:
                    return False
                # O_NOFOLLOW is absent on Windows, where os.open follows a symlink. Compare
                # the opened file with the path itself, so a link is rejected, not followed.
                if not self._is_direct_file(metadata):
                    return False
                raw = stream.read(_SESSION_FILE_BYTES + 1)
        except OSError:
            return False
        if len(raw) > _SESSION_FILE_BYTES:
            return False
        try:
            document = _strict_json(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            # RecursionError: size-bounded but deeply nested JSON still exhausts the parser.
            return False
        if (not isinstance(document, dict) or set(document) != {"version", "fingerprint", "sessions"}
                or type(document["version"]) is not int or document["version"] != SESSION_FILE_VERSION):
            return False
        stored = document["fingerprint"]
        if not isinstance(stored, str) or not _FINGERPRINT.match(stored):
            return False
        # A snapshot for another key epoch (including a disabled key) is never adopted.
        if not key or not hmac.compare_digest(stored, self._fingerprint(key)):
            return False
        entries = document["sessions"]
        if not isinstance(entries, dict) or len(entries) > MAX_PERSISTED_SESSIONS:
            return False
        now = self.wall_clock()
        restored = OrderedDict()
        for sid, item in entries.items():
            if not isinstance(item, dict) or set(item) != {"csrf_token", "expires"}:
                return False
            token = self._token(sid)
            csrf_token = self._token(item["csrf_token"])
            expires = self._deadline(item["expires"])
            if token is None or csrf_token is None or expires is None:
                return False
            if expires > now:
                restored[token] = {"csrf_token": csrf_token, "expires": expires}
        self.sessions.update(restored)
        if len(restored) != len(entries):
            # Expired records were dropped: rewrite the snapshot, so a wall clock that
            # later moves backward cannot restore them from the file we just read.
            # A failure here is recorded as degraded state by _persist(); the entries we
            # already adopted stay valid, so this is not a reason to reject the snapshot.
            self._persist()
        return True

    def _persist(self):
        """Atomically rewrite the session table; returns False when it was not durable."""
        if self._path is None:
            return True
        if not self._configured_key:
            # A disabled key revokes everywhere; drop the snapshot instead of writing one.
            return self._revoke()
        document = {"version": SESSION_FILE_VERSION, "fingerprint": self._fingerprint(self._configured_key),
                    "sessions": {sid: {"csrf_token": item["csrf_token"], "expires": item["expires"]}
                                 for sid, item in self.sessions.items()}}
        try:
            content = json.dumps(document, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return False
        temporary = None
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".admin-sessions-", suffix=".tmp", dir=self._path.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
            temporary = None
            self._storage_ok()
        except (OSError, ValueError) as error:
            # Fall back to removing the stale snapshot so revoked sessions cannot return.
            self._storage_failed(error)
            return self._revoke()
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return True

    def _key(self):
        key = self.config.get("api_key") or ""
        if not isinstance(key, str):
            key = ""
        if self._configured_key is None or not hmac.compare_digest(key.encode(), self._configured_key.encode()):
            previous, self._configured_key = self._configured_key, key
            restoring = previous is None
            self.sessions.clear()
            # Restoring a previous key must not restore that epoch's OAuth owner.
            self._identity = secrets.token_urlsafe(32)
            if restoring:
                # Adopt only a snapshot that matches the current epoch; anything else is
                # revoked, so switching back to an old key cannot resurrect its sessions.
                durable = self._restore(key) or self._revoke()
            else:
                # A rotated or cleared key revokes every session, on disk as well.
                durable = self._persist()
            if not durable:
                # Leave the epoch unactivated, so the failure is retried instead of being
                # recorded as done. `_configured_key` is what _persist() fingerprints, so
                # it has to be set for the attempt above and rolled back here.
                self._configured_key = previous
                raise SessionStoreError(
                    f"无法持久撤销上一 epoch 的会话快照（{self._storage_error or 'unknown'}）："
                    f"{self._path}；请修复管理目录权限后重启，否则旧会话可能被复活")
        return key

    def csrf_enabled(self):
        """Only an explicitly disabled startup option skips browser-origin protection."""
        return self.config.get("admin_csrf", True) is not False

    def allowed_origins(self):
        """Extra trusted browser origins from hot configuration."""
        return origin_allowlist(self.config.get("admin_allowed_origins"))

    def reconcile(self):
        """Resolve the key epoch at startup rather than on the first `/admin` request.

        `_key()` is otherwise lazy, so a process that only serves inference traffic never
        reaches it and would leave the previous epoch's snapshot on disk. Restarting back
        to the original key would then adopt that snapshot and resurrect a cookie which the
        rotation in between was supposed to revoke.

        Raises SessionStoreError when that revocation cannot be made durable; the caller
        must abort startup rather than activate the new epoch.
        """
        with self.lock:
            self._key()

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
        with self.lock:
            if self.check_key(bearer) or self.check_key(request.headers.get("x-api-key")):
                return "key:" + self._identity
        return None

    def session(self, request):
        sid = request.cookies.get(COOKIE_NAME, "")
        with self.lock:
            self._key()
            item = self.sessions.get(sid)
            if item and item["expires"] > self.wall_clock():
                return sid, dict(item)
            if self.sessions.pop(sid, None) is not None:
                self._persist()
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
            item = {"csrf_token": secrets.token_urlsafe(32), "expires": self.wall_clock() + SESSION_TTL}
            self.sessions[sid] = item
            while len(self.sessions) > MAX_PERSISTED_SESSIONS:
                self.sessions.popitem(last=False)
            self._persist()
            return (sid, dict(item)), 200

    def logout(self, request):
        """Revoke the cookie's session; returns False when the snapshot could not be updated.

        The entry is kept when the snapshot cannot be rewritten, so a retry is still
        authenticated and can finish the revocation instead of being acknowledged early.
        """
        sid = request.cookies.get(COOKIE_NAME)
        with self.lock:
            item = self.sessions.pop(sid, None)
            if item is None:
                return True
            if self._persist():
                return True
            self.sessions[sid] = item
            return False


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

        try:
            enabled = self.auth.enabled()
        except SessionStoreError:
            # The key epoch changed but its superseded snapshot survives. Deny rather than
            # continue: startup refuses this case, so it is only reachable mid-process.
            return await error_response(
                503, "会话快照无法持久撤销，管理接口已锁定；请检查管理目录权限后重启")(scope, receive, no_cache)
        if not enabled:
            return await error_response(503, "未配置 API key，管理接口已锁定")(scope, receive, no_cache)
        path, method = scope["path"], scope["method"]
        public_session = path == "/admin/session" and method in ("POST", "GET")
        identity = self.auth.header_identity(request)
        sid, session = self.auth.session(request)
        cookie = not identity and session is not None
        if not public_session and not identity and not cookie:
            return await error_response(401, "管理认证无效或会话已过期")(scope, receive, no_cache)
        if (self.auth.csrf_enabled() and cookie and not public_session
                and (method not in ("GET", "HEAD", "OPTIONS") or path == "/admin/oauth/poll")):
            supplied = request.headers.get("x-csrf-token", "")
            if not same_origin(request, self.auth.allowed_origins()) or not hmac.compare_digest(supplied.encode(), session["csrf_token"].encode()):
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
