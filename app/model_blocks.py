#!/usr/bin/env python3
"""Cache confirmed unsupported backend/model pairs with bounded exponential retry backoff."""

from __future__ import annotations

import json
import os
import threading
import time

DEFAULT_TTL_S = 6 * 3600        # Initial model backoff
MAX_TTL_S = 24 * 3600           # Maximum repeated-failure backoff
RETAIN_AFTER_S = 24 * 3600      # Retain expired hits for continued backoff


class ModelBlocks:
    """Maintain thread-safe per-backend model backoff with optional persistence."""

    def __init__(self, path=None, ttl_s: float = DEFAULT_TTL_S, max_ttl_s: float = MAX_TTL_S):
        self.path = str(path) if path else None
        self.ttl_s = max(60.0, float(ttl_s or 0))
        self.max_ttl_s = max(self.ttl_s, float(max_ttl_s or 0))
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self.path:
            self._load()

    # Persistence

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        out: dict[str, dict] = {}
        for endpoint, models in (data.get("blocks") or {}).items():
            if not isinstance(models, dict):
                continue
            rows = {}
            for model, row in models.items():
                if isinstance(row, dict) and float(row.get("until") or 0) > 0:
                    rows[str(model)] = {"until": float(row["until"]), "hits": int(row.get("hits") or 1),
                                        "code": str(row.get("code") or ""),
                                        "since": float(row.get("since") or 0),
                                        "msg": str(row.get("msg") or "")[:200]}
            if rows:
                out[str(endpoint)] = rows
        with self._lock:
            self._data = out

    def _save_locked(self):
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps({"version": 1, "blocks": self._data}, ensure_ascii=False))
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError:
            pass    # Persistence failure must not interrupt inference.

    def _prune_locked(self, now: float):
        cutoff = now - RETAIN_AFTER_S
        for endpoint in list(self._data):
            rows = {m: r for m, r in self._data[endpoint].items() if float(r.get("until") or 0) > cutoff}
            if rows:
                self._data[endpoint] = rows
            else:
                self._data.pop(endpoint, None)

    # Updates

    def note(self, endpoint: str, model: str, code: str = "", msg: str = "",
             now: float | None = None) -> dict:
        """Record unsupported-model failures with hit-based exponential backoff."""
        endpoint, model = str(endpoint or ""), str(model or "")
        if not endpoint or not model:
            return {}
        now = time.time() if now is None else now
        with self._lock:
            previous = (self._data.get(endpoint) or {}).get(model) or {}
            hits = int(previous.get("hits") or 0) + 1
            ttl = min(self.ttl_s * (2 ** min(hits - 1, 6)), self.max_ttl_s)
            entry = {"until": round(now + ttl, 3), "hits": hits, "code": str(code or ""),
                     "since": float(previous.get("since") or now), "msg": str(msg or "")[:200]}
            self._data.setdefault(endpoint, {})[model] = entry
            self._prune_locked(now)
            self._save_locked()
            return dict(entry)

    def clear(self, endpoint: str, model: str, now: float | None = None) -> bool:
        """Clear backoff immediately after confirmed model availability."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._data.get(str(endpoint)) or {}
            row = rows.get(str(model))
            if not row or now >= float(row.get("until") or 0):
                return False
            rows.pop(str(model), None)
            self._save_locked()
            return True

    # Queries

    def until(self, endpoint: str, model: str, now: float | None = None) -> float:
        """Return an active retry deadline, or zero when probing is allowed."""
        now = time.time() if now is None else now
        with self._lock:
            row = (self._data.get(str(endpoint)) or {}).get(str(model))
            value = float(row.get("until") or 0) if row else 0.0
        return value if value > now else 0.0

    def blocked(self, endpoint: str, model: str, now: float | None = None) -> bool:
        return self.until(endpoint, model, now) > 0.0

    def view(self, now: float | None = None) -> dict:
        """Return active backend/model retry deadlines."""
        now = time.time() if now is None else now
        with self._lock:
            return {endpoint: {m: float(r.get("until") or 0) for m, r in rows.items()
                               if float(r.get("until") or 0) > now}
                    for endpoint, rows in self._data.items()}

    def detail(self, now: float | None = None) -> list:
        """Return backoff details ordered by retry time, including hits and upstream codes."""
        now = time.time() if now is None else now
        with self._lock:
            rows = [{"endpoint": endpoint, "model": m, **r}
                    for endpoint, models in self._data.items() for m, r in models.items()
                    if float(r.get("until") or 0) > now]
        rows.sort(key=lambda r: r["until"])
        return rows
