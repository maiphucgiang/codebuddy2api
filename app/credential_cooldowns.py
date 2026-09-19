#!/usr/bin/env python3
"""Persist credential circuit-breaker and per-model 429 cooldowns across restarts.

Cooldowns are advisory: they only delay the next attempt against a backend that just
refused, so losing one costs a single wasted request. This store is therefore tolerant —
unusable storage degrades to the previous in-memory behaviour and never disables an
account. The properties that must hold on every path are:

* a restored deadline can never sit further out than its ceiling, so a stale or crafted
  file cannot park an account permanently;
* what the writer produces is always something the reader accepts, because a file the
  reader rejects discards *every* cooldown it held, not just the offending row.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import time

VERSION = 1
MAX_ACCOUNTS = 256          # Bounded table; matches the credential pool's practical size.
MAX_MODELS = 64             # Bounded per-account model rows.
MAX_BYTES = 256 * 1024      # Bounded read *and* write; the writer never exceeds this.
# A credential breaker is short-lived (CRED_COOLDOWN is 300s), so it gets a much tighter
# ceiling than a per-model quota cooldown, which may legitimately run to MODEL_COOLDOWN_MAX.
AUTH_CEILING_S = 3600
MODEL_CEILING_S = 24 * 3600

_IDENTITY = re.compile(r"[0-9a-f]{64}")
# Model and profile identifiers may contain slashes (vendor-qualified names), so allow the
# punctuation seen in routing rather than assuming a bare token.
_TOKEN = re.compile(r"[A-Za-z0-9_.:/\-]{1,128}")
_REASON_CHARS = 256
_FIELDS = {"profile", "fail_until", "reason", "failed_at", "models"}


def _text(value, limit=_REASON_CHARS):
    """Accept a bounded printable string; never raise on hostile input."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:limit]


def _number(value):
    """Accept only a finite number; bool is not a number, and a huge int must not raise."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _deadline(value, now, ceiling):
    """Accept only a finite deadline still in the future and within the ceiling."""
    number = _number(value)
    if number is None or number <= now or number > now + ceiling:
        return None
    return number


def _valid_identity(value):
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


def _valid_token(value):
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def _strict_json(raw: bytes):
    """Parse JSON while rejecting duplicate keys and Infinity/NaN."""
    def no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError("duplicate key")
            seen[key] = value
        return seen

    def no_constants(name):
        raise ValueError(name)

    return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=no_constants)


class CredentialCooldowns:
    """Thread-safe, optionally persisted cooldown table keyed by validated account identity."""

    def __init__(self, path=None, *, auth_ceiling_s: float = AUTH_CEILING_S,
                 model_ceiling_s: float = MODEL_CEILING_S):
        self.path = str(path) if path else None
        self.auth_ceiling_s = _bounded_ceiling(auth_ceiling_s, AUTH_CEILING_S)
        self.model_ceiling_s = _bounded_ceiling(model_ceiling_s, MODEL_CEILING_S)
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        # Last write failure, surfaced for diagnostics instead of failing silently.
        self.last_error: str | None = None
        # Set when the in-memory table changed but the write did not land. A later clear must
        # retry the write instead of reporting "nothing to do" while stale rows sit on disk.
        self._dirty = False
        if self.path:
            self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        """Adopt only a fully valid snapshot; anything else leaves the table empty."""
        try:
            # Reject a symlink or FIFO *before* opening, so a device cannot block the read.
            # This is best effort on Windows and is backed up by the fstat check below.
            if not stat.S_ISREG(os.lstat(self.path).st_mode):
                return
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return
        try:
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return
                raw = stream.read(MAX_BYTES + 1)
        except OSError:
            return
        if len(raw) > MAX_BYTES:
            return
        try:
            document = _strict_json(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return
        if (not isinstance(document, dict) or set(document) != {"version", "accounts"}
                or type(document["version"]) is not int or document["version"] != VERSION):
            return
        accounts = document["accounts"]
        if not isinstance(accounts, dict) or len(accounts) > MAX_ACCOUNTS:
            return
        now = time.time()
        restored: dict[str, dict] = {}
        for identity, row in accounts.items():
            if not _valid_identity(identity) or not isinstance(row, dict) or set(row) != _FIELDS:
                return
            profile, models = row["profile"], row["models"]
            if not _valid_token(profile) or not isinstance(models, dict) or len(models) > MAX_MODELS:
                return
            fail_until = _deadline(row["fail_until"], now, self.auth_ceiling_s)
            kept = {}
            for model, until in models.items():
                if not _valid_token(model):
                    return
                deadline = _deadline(until, now, self.model_ceiling_s)
                if deadline is not None:
                    kept[model] = deadline
            if fail_until is None and not kept:
                continue
            restored[identity] = {"profile": profile, "fail_until": fail_until or 0.0,
                                  "reason": _text(row["reason"]),
                                  "failed_at": _number(row["failed_at"]) or 0.0, "models": kept}
        with self._lock:
            self._data = restored

    def _serialize_locked(self):
        """Build the exact payload to write, shedding rows until it fits MAX_BYTES.

        Returns the payload plus whether rows had to be shed, because a caller that asked to
        record a cooldown must not be told its write was durable when that row was the one
        given up to satisfy the byte bound.
        """
        accounts = dict(self._data)
        shed = False
        while True:
            try:
                content = json.dumps({"version": VERSION, "accounts": accounts},
                                     ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                return None, shed
            if len(content) <= MAX_BYTES or not accounts:
                return content, shed
            # Shed the accounts nearest expiry first, in one proportional step.
            ordered = sorted(accounts, key=lambda key: max(
                [float(accounts[key].get("fail_until") or 0.0)] + list(accounts[key]["models"].values())))
            keep = max(0, int(len(ordered) * MAX_BYTES / len(content) * 0.9))
            for key in ordered[:max(1, len(ordered) - keep)]:
                accounts.pop(key, None)
                shed = True

    def _save_locked(self) -> bool:
        """Atomically rewrite the table; returns False when the update was not fully durable.

        The payload is written even when rows had to be shed, so the file stays bounded and
        readable; the False return tells the caller its update may not have survived.
        """
        if not self.path:
            return True
        content, shed = self._serialize_locked()
        if content is None:
            self.last_error = "serialize"
            self._dirty = True
            return False
        temporary = None
        try:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".credential-cooldowns-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass    # chmod does not establish owner-only ACLs on Windows.
            os.replace(temporary, self.path)
        except OSError as error:
            self.last_error = type(error).__name__
            self._dirty = True
            return False
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self.last_error = "capacity" if shed else None
        self._dirty = shed          # The write landed, but rows were dropped to fit.
        return not shed

    # -- table helpers -----------------------------------------------------

    @staticmethod
    def _blank(profile):
        return {"profile": profile, "fail_until": 0.0, "reason": "", "failed_at": 0.0, "models": {}}

    def _lookup_locked(self, identity, profile):
        """Read-only lookup; a row owned by another product is not visible."""
        if not _valid_identity(identity):
            return None
        row = self._data.get(identity)
        if row is None or (profile is not None and row.get("profile") != profile):
            return None
        return row

    def _ensure_locked(self, identity, profile):
        """Write-side lookup that adopts a new product identity for a reused path.

        Rejects values the reader would refuse, because writing one unreadable row would
        make the reader discard the whole file on the next start.
        """
        if not _valid_identity(identity) or not _valid_token(profile):
            return None
        row = self._data.get(identity)
        if row is None:
            if len(self._data) >= MAX_ACCOUNTS:
                self._drop_least_useful_locked()
            row = self._data[identity] = self._blank(profile)
        elif row.get("profile") != profile:
            # A path reused by another product must not inherit the old account's cooldowns.
            row = self._data[identity] = self._blank(profile)
        return row

    def _drop_least_useful_locked(self):
        """Evict the account whose furthest deadline is nearest, to stay within capacity."""
        if not self._data:
            return
        oldest = min(self._data, key=lambda key: max(
            [float(self._data[key].get("fail_until") or 0.0)] + list(self._data[key]["models"].values())))
        self._data.pop(oldest, None)

    def _retry_locked(self) -> bool:
        """Re-attempt a write that previously failed, so stale disk rows are not left behind."""
        if not self._dirty:
            return True
        return self._save_locked()

    def _drop_locked(self, identity, row):
        if not row.get("models") and not row.get("fail_until"):
            self._data.pop(identity, None)

    # -- writes ------------------------------------------------------------

    def note_credential(self, identity, profile, until, reason="", now=None) -> bool:
        """Record a credential-wide circuit-breaker deadline; returns whether it is durable."""
        deadline = _number(until)
        stamp = _number(time.time() if now is None else now)
        if deadline is None or stamp is None:
            return False        # Reject before mutating anything.
        with self._lock:
            self._expire_locked(stamp)   # Capacity must reflect live rows only.
            row = self._ensure_locked(identity, profile)
            if row is None:
                return False
            # Never shorten a live deadline, and never extend past the ceiling.
            row["fail_until"] = max(float(row.get("fail_until") or 0.0),
                                    min(deadline, stamp + self.auth_ceiling_s))
            row["reason"] = _text(reason)
            row["failed_at"] = stamp
            return self._save_locked()

    def note_model(self, identity, profile, model, until, now=None) -> bool:
        """Record a per-model 429 deadline; returns whether it is durable."""
        deadline = _number(until)
        stamp = _number(time.time() if now is None else now)
        if not _valid_token(model) or deadline is None or stamp is None:
            return False        # Reject before mutating anything.
        with self._lock:
            self._expire_locked(stamp)   # Capacity must reflect live rows only.
            row = self._ensure_locked(identity, profile)
            if row is None:
                return False
            models = row["models"]
            if model not in models and len(models) >= MAX_MODELS:
                # Evict the model cooldown nearest expiry rather than refusing the new one.
                models.pop(min(models, key=lambda name: models[name]), None)
            models[model] = max(float(models.get(model) or 0.0),
                                min(deadline, stamp + self.model_ceiling_s))
            return self._save_locked()

    def _expire_locked(self, now):
        """Drop expired deadlines so capacity reflects live rows only."""
        for identity in list(self._data):
            row = self._data[identity]
            if row.get("fail_until") and float(row["fail_until"]) <= now:
                row["fail_until"] = 0.0
            row["models"] = {m: u for m, u in row["models"].items() if u > now}
            self._drop_locked(identity, row)

    def clear_credential(self, identity, profile=None) -> dict:
        """Drop a circuit breaker, reporting whether anything changed and whether it is durable.

        "Nothing to change" and "the write failed" are different outcomes: only the second
        leaves stale data on disk that a later restart would restore.
        """
        with self._lock:
            row = self._lookup_locked(identity, profile)
            if row is None:
                # Nothing to change here, but a previous failed write may still be on disk.
                return {"changed": False, "durable": self._retry_locked()}
            row["fail_until"] = 0.0
            row["reason"] = ""
            self._drop_locked(identity, row)
            return {"changed": True, "durable": self._save_locked()}

    def clear_model(self, identity, profile, model) -> dict:
        """Drop one model's cooldown, reporting whether anything changed and whether it is durable."""
        with self._lock:
            row = self._lookup_locked(identity, profile)
            if row is None or model not in row["models"]:
                return {"changed": False, "durable": self._retry_locked()}
            row["models"].pop(model, None)
            self._drop_locked(identity, row)
            return {"changed": True, "durable": self._save_locked()}

    def forget(self, identity) -> dict:
        """Remove an account entirely; used when its credential is deleted or replaced."""
        with self._lock:
            if self._data.pop(identity, None) is None:
                return {"changed": False, "durable": self._retry_locked()}
            return {"changed": True, "durable": self._save_locked()}

    def prune(self, now=None) -> dict:
        """Drop expired deadlines so the table cannot accumulate dead rows."""
        now = time.time() if now is None else now
        with self._lock:
            changed = False
            for identity in list(self._data):
                row = self._data[identity]
                if row.get("fail_until") and float(row["fail_until"]) <= now:
                    row["fail_until"] = 0.0
                    changed = True
                kept = {m: u for m, u in row["models"].items() if u > now}
                if kept != row["models"]:
                    row["models"] = kept
                    changed = True
                before = len(self._data)
                self._drop_locked(identity, row)
                changed = changed or len(self._data) != before
            return {"changed": changed, "durable": self._save_locked() if changed else True}

    # -- reads -------------------------------------------------------------

    def credential_until(self, identity, profile=None) -> float:
        """Return a live circuit-breaker deadline, or zero."""
        now = time.time()
        with self._lock:
            row = self._lookup_locked(identity, profile)
            if row is None:
                return 0.0
            until = float(row.get("fail_until") or 0.0)
            return until if until > now else 0.0

    def model_until(self, identity, profile, model) -> float:
        """Return a live per-model cooldown deadline, or zero."""
        now = time.time()
        with self._lock:
            row = self._lookup_locked(identity, profile)
            if row is None:
                return 0.0
            until = float(row["models"].get(model) or 0.0)
            return until if until > now else 0.0

    def restore(self, identity, profile) -> dict:
        """Return the persisted state for one account as a plain dict."""
        now = time.time()
        with self._lock:
            row = self._lookup_locked(identity, profile)
            if row is None:
                return {}
            until = float(row.get("fail_until") or 0.0)
            return {"fail_until": until if until > now else 0.0,
                    "reason": row.get("reason") or "",
                    "failed_at": float(row.get("failed_at") or 0.0),
                    "models": {m: u for m, u in row["models"].items() if u > now}}

    def detail(self) -> list:
        """Return a bounded snapshot for diagnostics."""
        now = time.time()
        with self._lock:
            return [{"identity": identity, "profile": row.get("profile"),
                     "fail_until": float(row.get("fail_until") or 0.0),
                     "reason": row.get("reason") or "",
                     "models": {m: u for m, u in row["models"].items() if u > now}}
                    for identity, row in self._data.items()]


def _bounded_ceiling(value, default):
    """Keep a caller-supplied ceiling finite and within the module default."""
    number = _number(value)
    if number is None or number < 60:
        return float(default)
    return min(number, float(default))
