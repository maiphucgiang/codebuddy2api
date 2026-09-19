#!/usr/bin/env python3
"""Cache per-account usage snapshots so the dashboard is not blank after a restart.

Usage is presentation data: it feeds the billing and dashboard totals and is never
consulted when choosing a credential, so a cache miss or a corrupt file costs only a
cosmetic gap until the next refresh. That is exactly why this store must never become a
source of truth — it is rebuilt from upstream, and a cached snapshot is always treated as
stale-but-usable rather than authoritative.

The file is keyed by credential path, matching how the aggregate is assembled, but each
row also records the account identity it was fetched for. A path reused by a different
account therefore cannot inherit the previous account's usage.
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
MAX_DAYS = 31               # The upstream usage window is 30 days; allow one spare day.
MAX_MODELS = 128            # Bounded model rows per day.
MAX_BYTES = 512 * 1024      # Bounded read *and* write; the writer never exceeds this.
MAX_CREDITS = 1e9           # A single day/model credit figure is far below this.

_IDENTITY = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FIELDS = {"identity", "site", "by_day", "total_credits", "requests", "partial", "fetched_at"}


def _number(value, *, low=None, high=None):
    """Accept only a finite number inside the given range; bool is not a number."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if low is not None and number < low:
        return None
    if high is not None and number > high:
        return None
    return number


def _valid_date(value):
    """Accept a real calendar date; a shape-only match would allow 2026-02-31."""
    if _DATE.fullmatch(value) is None:
        return False
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _valid_identity(value):
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


def _valid_site(value):
    return value in ("domestic", "international")


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


class UsageSnapshots:
    """Thread-safe, optionally persisted cache of per-account usage snapshots."""

    def __init__(self, path=None):
        self.path = str(path) if path else None
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self.last_error: str | None = None
        # Set when the cache changed but the write did not land, so a later forget retries.
        self._dirty = False
        if self.path:
            self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        """Adopt only a fully valid snapshot; anything else leaves the cache empty."""
        try:
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
        restored: dict[str, dict] = {}
        for path, row in accounts.items():
            if not isinstance(path, str) or not path or len(path) > 4096:
                return
            if not isinstance(row, dict) or set(row) != _FIELDS:
                return
            clean = self._valid_row(row)
            if clean is None:
                return
            restored[path] = clean
        with self._lock:
            self._data = restored

    @staticmethod
    def _valid_row(row):
        """Return a normalized row, or None when any field is unusable.

        Malformed values are rejected rather than coerced: turning a bad credit figure into
        zero would silently display a wrong number as if it were measured.
        """
        if not _valid_identity(row["identity"]) or not _valid_site(row["site"]):
            return None
        total = _number(row["total_credits"], low=0.0, high=MAX_CREDITS)
        requests = _number(row["requests"], low=0.0, high=MAX_CREDITS)
        fetched_at = _number(row["fetched_at"], low=0.0)
        if total is None or requests is None or fetched_at is None or type(row["partial"]) is not bool:
            return None
        if requests != int(requests):        # A request count is a whole number.
            return None
        if fetched_at > time.time() + 86400:  # A snapshot cannot be fetched in the future.
            return None
        by_day = row["by_day"]
        if not isinstance(by_day, dict) or len(by_day) > MAX_DAYS:
            return None
        days = {}
        for day, models in by_day.items():
            if not isinstance(day, str) or not _valid_date(day) or not isinstance(models, dict):
                return None
            if len(models) > MAX_MODELS:
                return None
            clean_models = {}
            for model, credit in models.items():
                value = _number(credit, low=0.0, high=MAX_CREDITS)
                if not isinstance(model, str) or not model or len(model) > 128 or value is None:
                    return None
                clean_models[model] = value
            days[day] = clean_models
        return {"identity": row["identity"], "site": row["site"], "by_day": days,
                "total_credits": total, "requests": int(requests),
                "partial": row["partial"], "fetched_at": fetched_at}

    def _serialize_locked(self):
        """Build the payload, shedding the oldest snapshots until it fits MAX_BYTES.

        Returns the payload plus whether rows had to be shed, so a caller is not told its
        snapshot was cached when that row was the one given up to satisfy the byte bound.
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
            ordered = sorted(accounts, key=lambda key: accounts[key].get("fetched_at") or 0.0)
            keep = max(0, int(len(ordered) * MAX_BYTES / len(content) * 0.9))
            for key in ordered[:max(1, len(ordered) - keep)]:
                accounts.pop(key, None)
                shed = True

    def _save_locked(self) -> bool:
        """Atomically rewrite the cache; returns False when the update was not fully durable."""
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
            fd, temporary = tempfile.mkstemp(prefix=".usage-snapshots-", suffix=".tmp", dir=directory)
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
        self._dirty = shed
        return not shed

    # -- reads / writes ----------------------------------------------------

    def accounts(self) -> dict:
        """Return a copy of the cached snapshots, for merging into the live aggregate."""
        with self._lock:
            return {path: dict(row, by_day={day: dict(models) for day, models in row["by_day"].items()})
                    for path, row in self._data.items()}

    def store(self, path, identity, site, usage, *, partial=False, now=None) -> bool:
        """Cache one account's usage snapshot; returns whether it is durable.

        Malformed input is rejected rather than normalized: turning a missing or bad field
        into a plausible zero would present unmeasured data as if it had been measured.
        """
        stamp = _number(time.time() if now is None else now, low=0.0)
        if not isinstance(path, str) or not path or len(path) > 4096 or stamp is None:
            return False
        if not isinstance(usage, dict) or type(partial) is not bool:
            return False
        row = self._valid_row({"identity": identity, "site": site,
                               "by_day": usage.get("by_day"),
                               "total_credits": usage.get("total_credits"),
                               "requests": usage.get("requests"),
                               "partial": partial, "fetched_at": stamp})
        if row is None:
            return False
        with self._lock:
            if path not in self._data and len(self._data) >= MAX_ACCOUNTS:
                self._evict_locked()
            # Keep the validated copy so later caller mutation cannot corrupt the cache.
            self._data[path] = row
            return self._save_locked()

    def forget(self, path) -> dict:
        """Drop a deleted credential's snapshot, reporting change and durability separately."""
        with self._lock:
            if self._data.pop(path, None) is None:
                return {"changed": False, "durable": self._retry_locked()}
            return {"changed": True, "durable": self._save_locked()}

    def identity_matches(self, path, identity) -> bool:
        """Whether a cached row belongs to the account currently occupying this path."""
        with self._lock:
            row = self._data.get(path)
            return row is not None and row.get("identity") == identity

    def drop_mismatched(self, pool) -> bool:
        """Discard snapshots whose path is now owned by a different account."""
        # Read the pool first: taking the pool lock while holding this store's lock would
        # invert the pool -> store order used by store(), which can deadlock.
        owners = [(entry["id"], entry.get("account_key")) for entry in pool.entries()]
        with self._lock:
            dropped = False
            for path, identity in owners:
                row = self._data.get(path)
                if row is not None and identity and row.get("identity") != identity:
                    self._data.pop(path, None)
                    dropped = True
            if dropped:
                self._save_locked()
            return dropped

    def _retry_locked(self) -> bool:
        """Re-attempt a write that previously failed, so stale disk rows are not left behind."""
        return self._save_locked() if self._dirty else True

    def _evict_locked(self):
        if self._data:
            self._data.pop(min(self._data, key=lambda key: self._data[key].get("fetched_at") or 0.0), None)

    def prune(self, max_age_s: float = 7 * 24 * 3600, now=None) -> bool:
        """Drop snapshots too old to be worth showing; the next refresh replaces them."""
        now = time.time() if now is None else now
        with self._lock:
            before = len(self._data)
            self._data = {path: row for path, row in self._data.items()
                          if now - float(row.get("fetched_at") or 0.0) <= max_age_s}
            if len(self._data) != before:
                self._save_locked()
                return True
            return False

    def detail(self) -> list:
        """Return a bounded snapshot for diagnostics."""
        with self._lock:
            return [{"path": path, "identity": row["identity"], "site": row["site"],
                     "total_credits": row["total_credits"], "requests": row["requests"],
                     "partial": row["partial"], "fetched_at": row["fetched_at"]}
                    for path, row in self._data.items()]
