"""Track one-time international WorkBuddy trial claims with account-scoped daily failure backoff."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

import httpx

from .client_profiles import identity_headers
from .credential_io import credential_file_lock
from .site_routing import PROFILE_ENDPOINTS, profile_for_headers

RETRY_INTERVAL = 24 * 60 * 60
REQUEST_TIMEOUT = 12.0
MAX_RESPONSE_BYTES = 64 * 1024
RESPONSE_DEADLINE = 30.0
_MAX_BYTES = 1024 * 1024
_MAX_ACCOUNTS = 2048  # Reject new entries rather than evict permanent claim records.
_RESULT_FIELDS = {"ok", "already", "code", "status"}
_RECORD_FIELDS = _RESULT_FIELDS | {"attempted_at", "finished_at"}


def _result(code=None, status=None, *, ok=False, already=False, error=None):
    result = {"ok": ok, "already": already, "code": code, "status": status}
    if error:
        result["error"] = error
    return result


def _integer(value, lower, upper):
    return value if type(value) is int and lower <= value <= upper else None


def _strict_json(content):
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


def _trial_headers(headers):
    copied = dict(headers)
    profile = profile_for_headers(copied)
    if profile != "intl-work":
        raise ValueError("Trial requires intl-work profile")
    present = {name.lower() for name in copied}
    for name, value in identity_headers(profile).items():
        if name.lower() not in present:
            copied[name] = value
    return copied


def claim_trial(headers: dict) -> dict:
    """Send one same-origin POST without redirects, retries or raw response disclosure."""
    headers = _trial_headers(headers)
    status = None
    started = time.monotonic()
    request_headers = httpx.Headers(headers)
    request_headers["Accept-Encoding"] = "identity"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            with client.stream("POST", PROFILE_ENDPOINTS["intl-work"] + "/billing/ide/trial",
                               headers=request_headers, json={}) as response:
                status = response.status_code
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return _result(status=status, error="invalid_response")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > RESPONSE_DEADLINE:
                        return _result(status=status, error="timeout")
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        return _result(status=status, error="response_too_large")
                    content.extend(chunk)
    except httpx.TimeoutException:
        return _result(status=status, error="timeout")
    except httpx.HTTPError:
        return _result(status=status, error="network_error")
    try:
        envelope = _strict_json(content)
    except (ValueError, UnicodeError, RecursionError):
        return _result(status=status, error="invalid_response")
    if not isinstance(envelope, dict):
        return _result(status=status, error="invalid_response")
    code = _integer(envelope.get("code"), -(2**31), 2**31 - 1)
    result = _result(code, status)
    accepted = 200 <= status < 300 or (status in (400, 409) and code == 14051)
    if not accepted or code not in (0, 14051):
        return result
    data = envelope.get("data")
    if data is not None and not isinstance(data, dict):
        return result
    # A zero code cannot override an explicit failure or malformed success flag.
    for layer in (envelope, data or {}):
        for flag in ("success", "ok"):
            if flag in layer and (type(layer[flag]) is not bool or (code == 0 and not layer[flag])):
                return result
    return _result(code, status, ok=code == 0, already=code == 14051)


def _key(key):
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise ValueError("Trial key must be a SHA-256 account fingerprint")
    return key


def _timestamp(value):
    if type(value) not in (int, float) or not 0 <= value <= 1e12 or not math.isfinite(value):
        raise ValueError("Invalid trial timestamp")
    return value


def _safe_result(result):
    if not isinstance(result, dict):
        raise ValueError("Invalid trial result")
    code = _integer(result.get("code"), -(2**31), 2**31 - 1)
    status = _integer(result.get("status"), 100, 599)
    http_ok = status is not None and 200 <= status < 300
    ok = http_ok and code == 0 and result.get("ok") is True and result.get("already") is False
    already = (http_ok or status in (400, 409)) and code == 14051 and result.get("already") is True and result.get("ok") is False
    return _result(code, status, ok=ok, already=already)


def _empty_record():
    return {**_result(), "attempted_at": None, "finished_at": None}


class TrialSaveError(OSError):
    """The upstream completed but its safe result could not be persisted."""
    def __init__(self, result):
        super().__init__("Trial result persistence failed")
        self.result = _safe_result(result)


class TrialLedger:
    """Persist a bounded JSON ledger under one cross-process read-modify-write lock."""

    def __init__(self, path):
        path = Path(path)
        if not path.name or path.name in (".", ".."):
            raise ValueError("Trial ledger requires a file path")
        # Resolve the parent without following a symlink at the final filename.
        self.path = path.parent.resolve() / path.name
        # Use an .info lock name independently of the ledger filename suffix.
        self._lock_name = "trial-" + hashlib.sha256(self.path.name.encode()).hexdigest() + ".info"

    def _lock(self):
        return credential_file_lock(self.path.parent, self._lock_name)

    def _load(self):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_BYTES:
                raise ValueError("Invalid trial ledger file")
            content = stream.read(_MAX_BYTES + 1)
        if len(content) > _MAX_BYTES:
            raise ValueError("Trial ledger exceeds size limit")
        try:
            document = _strict_json(content)
            if (not isinstance(document, dict) or set(document) != {"version", "accounts"}
                    or type(document["version"]) is not int or document["version"] != 1):
                raise ValueError
            accounts = document["accounts"]
            if not isinstance(accounts, dict) or len(accounts) > _MAX_ACCOUNTS:
                raise ValueError
            for key, record in accounts.items():
                _key(key)
                if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
                    raise ValueError
                _timestamp(record["attempted_at"])
                if record["finished_at"] is not None:
                    _timestamp(record["finished_at"])
                    if record["finished_at"] < record["attempted_at"]:
                        raise ValueError
                fields = {name: record[name] for name in _RESULT_FIELDS}
                if (type(fields["ok"]) is not bool or type(fields["already"]) is not bool
                        or (fields["code"] is not None and type(fields["code"]) is not int)
                        or (fields["status"] is not None and type(fields["status"]) is not int)
                        or fields != _safe_result(fields)
                        or (record["finished_at"] is None and fields != _result())):
                    raise ValueError
            return accounts
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid trial ledger contents") from None

    def _save(self, accounts):
        content = json.dumps({"version": 1, "accounts": accounts},
                             separators=(",", ":"), allow_nan=False).encode()
        if len(content) > _MAX_BYTES:
            raise ValueError("Trial ledger exceeds size limit")
        fd, temporary = tempfile.mkstemp(prefix=".trial-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), 0o600)
                else:
                    os.chmod(temporary, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            # Sync the directory entry before allowing a claim POST.
            if os.name != "nt":
                directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def begin(self, key, now=None) -> bool:
        """Authorize sending only after durable reservation; propagate storage and lock failures."""
        key = _key(key)
        with self._lock():
            current = _timestamp(time.time() if now is None else now)
            accounts = self._load()
            previous = accounts.get(key)
            if previous is not None:
                if (previous["ok"] or previous["already"]
                        or current - previous["attempted_at"] < RETRY_INTERVAL):
                    return False
            elif len(accounts) >= _MAX_ACCOUNTS:
                raise ValueError("Trial ledger account limit reached")
            accounts[key] = {**_empty_record(), "attempted_at": current}
            self._save(accounts)
            return True

    def finish(self, key, result, now=None) -> None:
        """Require a reservation, preserve permanent outcomes and ignore unapproved response fields."""
        key, result = _key(key), _safe_result(result)
        with self._lock():
            current = _timestamp(time.time() if now is None else now)
            accounts = self._load()
            previous = accounts.get(key)
            if previous is None:
                raise ValueError("Trial finish requires a persisted attempt")
            if previous["ok"] or previous["already"]:
                return
            accounts[key] = {**result, "attempted_at": previous["attempted_at"],
                             "finished_at": max(current, previous["attempted_at"])}
            self._save(accounts)

    def snapshot(self) -> dict:
        """Read one atomically published snapshot without creating or waiting on lock files."""
        return self._load()


    def summary(self, key) -> dict:
        """Return a safe independent snapshot without identities, paths, headers or raw responses."""
        key = _key(key)
        with self._lock():
            return dict(self._load().get(key, _empty_record()))


def attempt_trial(ledger: TrialLedger, key: str, headers: dict, *, can_claim=lambda: True) -> dict:
    """Reserve before the one manual POST; never replay an unconfirmed claim."""
    headers = _trial_headers(headers)
    if not can_claim():
        return _result(error="changed")
    if not ledger.begin(key):
        return _result()
    result = claim_trial(headers) if can_claim() else _result(error="changed")
    try:
        ledger.finish(key, result)
    except Exception:
        raise TrialSaveError(result) from None
    return result
