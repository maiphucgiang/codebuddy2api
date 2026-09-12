"""Bounded, metadata-only SQLite audit storage (no import-time I/O).

Methods are synchronous: ASGI callers must offload them. Lock and SQLite busy
waits are capped at 250ms; SQL has a cooperative 1s progress deadline (not a hard
wall-clock guarantee for filesystem I/O). Failures are observable, not retried.
Ingest deduplication and aggregates intentionally outlive all detail eviction.
Detail accounting is transactional; indexed cleanup commits bounded batches.
Large budget/retention reductions converge on subsequent writes or detail reads
(including storage()), reported as pending_cleanup until complete. This is a
logical detail budget, not a bound on aggregate, dedup, or physical file size.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid
from typing import Any

SCHEMA_VERSION = 1
_CLEANUP_BATCH = 128
_CLEANUP_SECONDS = 0.05
METRICS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
           "reasoning_tokens", "total_tokens", "credit")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@-]{1,160}$")
_SECRET = re.compile(r"(?i)(bearer|sk-|access[_-]?token|refresh[_-]?token|api[_-]?key|eyJ|://)")


def safe_label(value: Any, limit: int = 160) -> str | None:
    """Accept identifiers, never free-form diagnostics, headers or bodies."""
    if not isinstance(value, str) or len(value) > limit:
        return None
    if not _IDENTIFIER.fullmatch(value) or _SECRET.search(value):
        return None
    return value


def number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and 0 <= value <= 1e18 else None
    except OverflowError:
        return None


def safe_attempt(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("stage", "code", "error_code", "model", "upstream_model", "profile",
                "credential", "usage_source", "outcome"):
        clean = safe_label(value.get(key))
        if clean is not None:
            result[key] = clean
    for key in ("status_code", "duration_ms", "attempt", "retry_after"):
        clean = number(value.get(key))
        if clean is not None:
            result[key] = clean
    return result


def _secure_path(path: str | os.PathLike) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    for ancestor in (*reversed(path.parents), path):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            if ancestor != path:
                ancestor.mkdir(mode=0o700)
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("audit path must not contain symlinks")
        if ancestor != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("audit parent is not a directory")
    os.chmod(path.parent, 0o700)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("audit files must be regular, unlinked files")
        if candidate != path:
            os.chmod(candidate, 0o600)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("invalid audit database file")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(path, 0o600)
    finally:
        os.close(fd)
    return path


class AuditStore:
    def __init__(self, path, max_bytes=256 * 1024 * 1024, retention_days=30,
                 preview_limit=8192):
        self._validate(max_bytes, retention_days, preview_limit)
        self.max_bytes, self.retention_days, self.preview_limit = max_bytes, retention_days, preview_limit
        self._lock = threading.Lock()
        self._health_lock = threading.Lock()
        self.failure_count = self.dropped_records = 0
        self.last_error = None
        self._closed = False
        self._epoch = 0
        self.path = _secure_path(path)
        self._db = sqlite3.connect(str(self.path), timeout=0.25, check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout=250")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("BEGIN IMMEDIATE")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            tables = self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version not in (0, SCHEMA_VERSION) or (version == 0 and tables):
                raise ValueError("unsupported audit schema")
            self._db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL, detail_generation INTEGER NOT NULL, cleared_at REAL NOT NULL)")
            self._db.execute("INSERT OR IGNORE INTO state VALUES(1,0,0,0)")
            self._db.execute("CREATE TABLE IF NOT EXISTS ingest (id TEXT PRIMARY KEY, kind TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, started_at REAL NOT NULL, model TEXT, profile TEXT, credential TEXT, outcome TEXT, status_code INTEGER, payload TEXT NOT NULL, logical_bytes INTEGER NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS requests_time ON requests(started_at,id)")
            self._db.execute("CREATE TABLE IF NOT EXISTS attempts (request_id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(request_id,ordinal))")
            self._db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, started_at REAL NOT NULL, kind TEXT NOT NULL, action TEXT, payload TEXT NOT NULL, logical_bytes INTEGER NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS events_time ON events(started_at,id)")
            self._init_accounting()
            for table in ("stats_hourly", "stats_daily", "stats_totals"):
                self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} (bucket INTEGER NOT NULL, dimension TEXT NOT NULL, dimension_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(bucket,dimension,dimension_key))")
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._epoch = self._db.execute("SELECT epoch FROM state").fetchone()[0]
            self._db.execute("COMMIT")
        except Exception:
            self._db.close()
            raise

    def _init_accounting(self):
        # Additive v1 migration: scan legacy details only once, under the same
        # write transaction that installs triggers. Reopening never recounts.
        self._db.execute("CREATE TABLE IF NOT EXISTS detail_accounting (id INTEGER PRIMARY KEY CHECK(id=1), logical_bytes INTEGER NOT NULL, request_count INTEGER NOT NULL, event_count INTEGER NOT NULL, ingest_count INTEGER NOT NULL, cleanup_target INTEGER)")
        if not self._db.execute("SELECT 1 FROM detail_accounting WHERE id=1").fetchone():
            self._db.execute("INSERT INTO detail_accounting SELECT 1,COALESCE((SELECT SUM(logical_bytes) FROM requests),0)+COALESCE((SELECT SUM(logical_bytes) FROM events),0),(SELECT COUNT(*) FROM requests),(SELECT COUNT(*) FROM events),(SELECT COUNT(*) FROM ingest),NULL")
        for table, counter in (("requests", "request_count"), ("events", "event_count")):
            self._db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_{table}_insert AFTER INSERT ON {table} BEGIN UPDATE detail_accounting SET logical_bytes=logical_bytes+NEW.logical_bytes,{counter}={counter}+1 WHERE id=1; END")
            cascade = "DELETE FROM attempts WHERE request_id=OLD.id;" if table == "requests" else ""
            self._db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_{table}_delete AFTER DELETE ON {table} BEGIN UPDATE detail_accounting SET logical_bytes=logical_bytes-OLD.logical_bytes,{counter}={counter}-1 WHERE id=1; {cascade} END")
            self._db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_{table}_update AFTER UPDATE OF logical_bytes ON {table} BEGIN UPDATE detail_accounting SET logical_bytes=logical_bytes+NEW.logical_bytes-OLD.logical_bytes WHERE id=1; END")
        for operation, delta in (("INSERT", "+1"), ("DELETE", "-1")):
            self._db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_ingest_{operation.lower()} AFTER {operation} ON ingest BEGIN UPDATE detail_accounting SET ingest_count=ingest_count{delta} WHERE id=1; END")

    @staticmethod
    def _validate(max_bytes, retention_days, preview_limit):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 <= max_bytes <= 2**40:
            raise ValueError("invalid max_bytes")
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 1 <= retention_days <= 36500:
            raise ValueError("invalid retention_days")
        if isinstance(preview_limit, bool) or not isinstance(preview_limit, int) or not 0 <= preview_limit <= 65536:
            raise ValueError("invalid preview_limit")

    def _fault(self, exc, dropped=False):
        with self._health_lock:
            self.failure_count += 1
            self.dropped_records += int(dropped)
            # Exception messages may include SQL or user data; retain only type.
            self.last_error = type(exc).__name__

    def note_failure(self, code="ObservationError"):
        self._fault(RuntimeError(), dropped=True)

    def _run(self, callback, fallback=None, write=False, dropped=False, on_commit=None):
        if not self._lock.acquire(timeout=0.25):
            self._fault(TimeoutError(), dropped=dropped)
            return fallback
        try:
            if self._closed:
                raise RuntimeError("closed")
            deadline = time.monotonic() + 1
            self._sql_deadline = deadline
            self._db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            if write:
                self._db.execute("BEGIN IMMEDIATE")
            result = callback()
            if write:
                self._db.execute("COMMIT")
            if on_commit is not None:
                on_commit(result)
            return result
        except Exception as exc:
            if not self._closed:
                self._db.set_progress_handler(None, 0)
                try:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            self._fault(exc, dropped=dropped)
            return fallback
        finally:
            if not self._closed:
                self._db.set_progress_handler(None, 0)
            self._lock.release()

    @property
    def epoch(self):
        return self.ticket()["epoch"]

    def ticket(self):
        def fetch():
            row = self._db.execute("SELECT epoch,detail_generation FROM state").fetchone()
            self._epoch = row["epoch"]
            return dict(row)
        return self._run(fetch, {"epoch": self._epoch, "detail_generation": -1})

    @staticmethod
    def _empty_stats():
        return {"requests": 0, "success": 0, "error": 0, "cancelled": 0,
                **{key: None for key in METRICS},
                **{key + "_known": 0 for key in METRICS},
                "duration_ms_sum": 0, "duration_ms_known": 0,
                "first_token_ms_sum": 0, "first_token_ms_known": 0}

    @staticmethod
    def _merge(target, source):
        for key, value in source.items():
            if isinstance(value, (int, float)):
                target[key] = (target.get(key) or 0) + value
        return target

    def _aggregate(self, record):
        increment = self._empty_stats()
        increment["requests"] = 1
        increment[record["outcome"]] = 1
        for key in METRICS:
            increment[key] = record[key]
            increment[key + "_known"] = int(record[key] is not None)
        for key in ("duration_ms", "first_token_ms"):
            increment[key + "_sum"] = record[key] or 0
            increment[key + "_known"] = int(record[key] is not None)
        dimensions = [("global", "")]
        dimensions += [(key, record.get("public_model" if key == "model" else key) or "")
                       for key in ("model", "profile", "credential")]
        for table, seconds in (("stats_hourly", 3600), ("stats_daily", 86400), ("stats_totals", 0)):
            bucket = int(record["started_at"] // seconds) * seconds if seconds else 0
            for dimension, key in dimensions:
                old = self._db.execute(f"SELECT payload FROM {table} WHERE bucket=? AND dimension=? AND dimension_key=?", (bucket, dimension, key)).fetchone()
                stats = self._merge(json.loads(old[0]) if old else self._empty_stats(), increment)
                self._db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?,?,?)", (bucket, dimension, key, json.dumps(stats)))

    def _sanitize_record(self, source):
        result = {key: safe_label(source.get(key)) for key in
                  ("upstream_model", "profile", "credential", "protocol", "error_code", "usage_source")}
        result["public_model"] = safe_label(source.get("public_model", source.get("model")))
        result["model"] = result["public_model"]
        result["id"] = safe_label(source.get("id", source.get("event_id"))) or uuid.uuid4().hex
        result["event_id"] = result["id"]
        result["epoch"] = source.get("epoch", self._epoch)
        result["started_at"] = number(source.get("started_at"))
        if result["started_at"] is None:
            result["started_at"] = time.time()
        result["outcome"] = source.get("outcome") if source.get("outcome") in ("success", "error", "cancelled") else "error"
        result["streaming"] = source.get("streaming") is True
        for key in (*METRICS, "duration_ms", "first_token_ms", "status_code"):
            result[key] = number(source.get(key))
        if result["cache_read_tokens"] is None:
            result["cache_read_tokens"] = number(source.get("cache", source.get("cached_tokens")))
        if result["reasoning_tokens"] is None:
            result["reasoning_tokens"] = number(source.get("reasoning"))
        sources = source.get("usage_sources")
        result["usage_sources"] = {key: safe_label(sources.get(key)) for key in METRICS
                                   if safe_label(sources.get(key)) is not None} if isinstance(sources, dict) else {}
        result["attempts"] = []
        used = 0
        attempts = source.get("attempts", [])
        if isinstance(attempts, (tuple, list)):
            for item in attempts[:32]:
                attempt = safe_attempt(item)
                used += len(json.dumps(attempt).encode())
                if used > self.preview_limit:
                    break
                if attempt:
                    result["attempts"].append(attempt)
        return result

    def record_request(self, record):
        def commit():
            data = self._sanitize_record(record)
            state = self._db.execute("SELECT * FROM state").fetchone()
            if data["epoch"] != state["epoch"]:
                return {"ok": True, "recorded": False, "reason": "stale_epoch"}
            if not self._db.execute("INSERT OR IGNORE INTO ingest VALUES(?, 'request')", (data["id"],)).rowcount:
                return {"ok": True, "recorded": False, "reason": "duplicate"}
            self._aggregate(data)
            generation = record.get("detail_generation")
            keep = (data["started_at"] > state["cleared_at"] and
                    (generation is None or generation == state["detail_generation"]))
            if keep:
                payload = json.dumps(data, separators=(",", ":"))
                self._db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)", (
                    data["id"], data["started_at"], data["model"], data["profile"], data["credential"],
                    data["outcome"], data["status_code"], payload,
                    len(payload.encode()) + sum(len(json.dumps(a).encode()) + 32 for a in data["attempts"]) + 256))
                for i, attempt in enumerate(data["attempts"]):
                    self._db.execute("INSERT INTO attempts VALUES(?,?,?)", (data["id"], i, json.dumps(attempt)))
            self._prune()
            return {"ok": True, "recorded": True, "details": bool(keep and self._db.execute("SELECT 1 FROM requests WHERE id=?", (data["id"],)).fetchone())}
        return self._run(commit, {"ok": False, "recorded": False, "reason": "storage_failure"}, write=True, dropped=True)

    def _oldest(self, cutoff=None):
        # Each branch walks its time index, never sorts/scans the full union.
        rows = []
        for table in ("requests", "events"):
            where, params = (" WHERE started_at<?", (cutoff,)) if cutoff is not None else ("", ())
            rows.extend((row[0], row[1], table, row[2]) for row in self._db.execute(
                f"SELECT started_at,id,logical_bytes FROM {table}{where} ORDER BY started_at,id LIMIT ?",
                (*params, _CLEANUP_BATCH)))
        return sorted(rows)[:_CLEANUP_BATCH]

    def _cleanup_deadline(self):
        # Reserve time for the rest of the transaction/commit. The SQL progress
        # handler remains the final 1s guard, including a single slow statement.
        return min(time.monotonic() + _CLEANUP_SECONDS, self._sql_deadline - 0.1)

    def _expire(self, retention_days=None, deadline=None):
        cutoff = time.time() - (self.retention_days if retention_days is None else retention_days) * 86400
        deadline = self._cleanup_deadline() if deadline is None else deadline
        for _, record_id, table, _ in self._oldest(cutoff):
            if time.monotonic() >= deadline:
                break
            self._db.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))

    def _cleanup_pending(self, budget=None, retention_days=None):
        budget = self.max_bytes if budget is None else budget
        cutoff = time.time() - (self.retention_days if retention_days is None else retention_days) * 86400
        accounting = self._db.execute("SELECT logical_bytes,cleanup_target FROM detail_accounting WHERE id=1").fetchone()
        return (accounting[0] > budget or accounting[1] is not None or any(
            self._db.execute(f"SELECT 1 FROM {table} WHERE started_at<? LIMIT 1", (cutoff,)).fetchone()
            for table in ("requests", "events")))

    def _prune(self, max_bytes=None, retention_days=None):
        deadline = self._cleanup_deadline()
        self._expire(retention_days, deadline)
        budget = self.max_bytes if max_bytes is None else max_bytes
        used, target = self._db.execute("SELECT logical_bytes,cleanup_target FROM detail_accounting WHERE id=1").fetchone()
        if max_bytes is not None:
            target = None  # A new budget supersedes any prior headroom target.
        if used > budget and target is None:
            target = budget * 9 // 10
        if target is not None:
            target = min(target, budget)
            for _, record_id, table, cost in self._oldest():
                # Headroom is best effort: don't discard the last fitting large
                # detail just to reach 90%. Mandatory eviction still fits budget.
                if used <= target or (used <= budget and used - cost < target):
                    target = None
                    break
                if time.monotonic() >= deadline:
                    break
                self._db.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))
                used -= cost
            if used <= (target if target is not None else budget):
                target = None
        self._db.execute("UPDATE detail_accounting SET cleanup_target=? WHERE id=1", (target,))

    def event(self, kind, action, details=None):
        if kind not in ("runtime", "admin"):
            raise ValueError("invalid event kind")
        started_at = time.time()
        def commit():
            source = details if isinstance(details, dict) else {}
            state = self._db.execute("SELECT * FROM state").fetchone()
            if source.get("epoch", state["epoch"]) != state["epoch"]:
                return {"ok": True, "recorded": False, "reason": "stale_epoch"}
            event_id = safe_label(source.get("event_id", source.get("id"))) or uuid.uuid4().hex
            if not self._db.execute("INSERT OR IGNORE INTO ingest VALUES(?,?)", (event_id, kind)).rowcount:
                return {"ok": True, "recorded": False, "reason": "duplicate"}
            if (started_at <= state["cleared_at"] or
                    source.get("detail_generation", state["detail_generation"]) != state["detail_generation"]):
                return {"ok": True, "recorded": False, "reason": "details_cleared"}
            data = {"id": event_id, "kind": kind, "action": safe_label(action),
                    "started_at": started_at, "details": safe_attempt(source)}
            payload = json.dumps(data)
            self._db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (event_id, data["started_at"], kind, data["action"], payload, len(payload.encode()) + 128))
            self._prune()
            return {"ok": True, "recorded": True, "id": event_id}
        return self._run(commit, {"ok": False, "recorded": False}, write=True, dropped=True)

    def list_records(self, kind="request", limit=50, cursor=None, **filters):
        if kind not in ("request", "runtime", "admin"):
            raise ValueError("invalid record kind")
        limit = max(1, min(int(limit), 200))
        # Expiry cleanup is incremental, but expired details are never visible.
        clauses, params = ["started_at>=?"], [None]
        table = "requests" if kind == "request" else "events"
        if kind != "request":
            clauses.append("kind=?")
            params.append(kind)
        if cursor:
            try:
                stamp, record_id = json.loads(cursor)
                if number(stamp) is None or not safe_label(record_id):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("invalid cursor") from None
            clauses.append("(started_at<? OR (started_at=? AND id<?))")
            params.extend((stamp, stamp, record_id))
        if kind == "request":
            for key in ("model", "profile", "credential", "outcome", "status_code"):
                value = filters.get(key)
                if value is not None and value != "":
                    clauses.append(f"{key}=?")
                    params.append(value)
            status = filters.get("status")
            if status is not None and status != "":
                column = "outcome" if status in ("success", "error", "cancelled") else "status_code"
                clauses.append(f"{column}=?")
                params.append(status)
        for key, op in (("since", ">="), ("until", "<=")):
            if filters.get(key) is not None:
                clauses.append(f"started_at{op}?")
                params.append(float(filters[key]))
        if filters.get("search"):
            # Literal identifier search, not full-body diagnostics.
            term = str(filters["search"])[:160].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            columns = ("id", "model", "profile", "credential") if kind == "request" else ("id", "action")
            clauses.append("(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in columns) + ")")
            params.extend(["%" + term + "%"] * len(columns))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        def fetch():
            self._prune()
            params[0] = time.time() - self.retention_days * 86400
            rows = self._db.execute(f"SELECT payload,started_at,id FROM {table}{where} ORDER BY started_at DESC,id DESC LIMIT ?", (*params, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            return {"items": [json.loads(row["payload"]) for row in rows],
                    "next_cursor": json.dumps([rows[-1]["started_at"], rows[-1]["id"]]) if more else None,
                    "has_more": more}
        return self._run(fetch, {"items": [], "next_cursor": None, "has_more": False, "degraded": True}, write=True)

    def get_request(self, id):
        def fetch():
            self._prune()
            row = self._db.execute("SELECT payload FROM requests WHERE id=? AND started_at>=?", (id, time.time() - self.retention_days * 86400)).fetchone()
            return json.loads(row[0]) if row else None
        return self._run(fetch, write=True)

    def dashboard(self, days=30):
        days = int(days)
        if not 1 <= days <= 36500:
            raise ValueError("invalid days")
        now = time.time()
        start = int(now // 86400) * 86400 - (days - 1) * 86400
        def fetch():
            summary = self._empty_stats()
            series, models, profiles = [], {}, {}
            rows = self._db.execute("SELECT * FROM stats_daily WHERE bucket>=? AND bucket<=? ORDER BY bucket", (start, now)).fetchall()
            for row in rows:
                stats = json.loads(row["payload"])
                if row["dimension"] == "global":
                    self._merge(summary, stats)
                    series.append({"bucket": row["bucket"], "date": time.strftime("%Y-%m-%d", time.gmtime(row["bucket"])), **stats})
                elif row["dimension"] in ("model", "profile"):
                    target = models if row["dimension"] == "model" else profiles
                    self._merge(target.setdefault(row["dimension_key"], self._empty_stats()), stats)
            summary["success_rate"] = summary["success"] / summary["requests"] if summary["requests"] else None
            return {"summary": summary, "series": series,
                    "models": [{"model": key, **value} for key, value in models.items()],
                    "profiles": [{"profile": key, **value} for key, value in profiles.items()],
                    "generated_at": now, "range": {"days": days, "start": start, "end": now, "timezone": "UTC"}}
        return self._run(fetch, {"summary": self._empty_stats(), "series": [], "models": [], "profiles": [], "generated_at": now, "range": {"days": days, "start": start, "end": now}, "degraded": True})

    def storage(self):
        def fetch():
            self._prune()
            self._epoch = self._db.execute("SELECT epoch FROM state").fetchone()[0]
            return {**dict(self._db.execute("SELECT logical_bytes,request_count,event_count,ingest_count FROM detail_accounting WHERE id=1").fetchone()),
                    "pending_cleanup": self._cleanup_pending()}
        result = self._run(fetch, {"logical_bytes": None, "pending_cleanup": None}, write=True)
        sizes = {}
        for key, suffix in (("db_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm")):
            try:
                sizes[key] = Path(str(self.path) + suffix).lstat().st_size
            except FileNotFoundError:
                sizes[key] = 0
            except OSError as exc:
                self._fault(exc)
                sizes[key] = None
        return {**result, **sizes, "max_bytes": self.max_bytes, "retention_days": self.retention_days,
                "preview_limit": self.preview_limit, "schema_version": SCHEMA_VERSION, "epoch": self._epoch,
                "degraded": bool(self.failure_count), "failure_count": self.failure_count,
                "dropped_records": self.dropped_records, "last_error": self.last_error,
                "closed": self._closed, "budget_scope": "details_logical_bytes",
                "fault_counter_scope": "process_lifetime", "automatic_vacuum": False,
                "lock_timeout_ms": 250, "sql_deadline_ms": 1000}

    def clear(self, scope="details"):
        if scope not in ("details", "all"):
            raise ValueError("invalid clear scope")
        def commit():
            for table in ("requests", "attempts", "events"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.execute("UPDATE detail_accounting SET cleanup_target=NULL WHERE id=1")
            self._db.execute("UPDATE state SET detail_generation=detail_generation+1,cleared_at=? WHERE id=1", (time.time(),))
            if scope == "all":
                for table in ("stats_hourly", "stats_daily", "stats_totals", "ingest"):
                    self._db.execute(f"DELETE FROM {table}")
                self._db.execute("UPDATE state SET epoch=epoch+1 WHERE id=1")
            epoch = self._db.execute("SELECT epoch FROM state").fetchone()[0]
            return {"ok": True, "scope": scope, "epoch": epoch, "aggregates_preserved": scope == "details"}
        return self._run(commit, {"ok": False, "scope": scope}, write=True,
                         on_commit=lambda result: setattr(self, "_epoch", result["epoch"]))

    def configure(self, max_bytes=None, retention_days=None, preview_limit=None):
        values = (self.max_bytes if max_bytes is None else max_bytes,
                  self.retention_days if retention_days is None else retention_days,
                  self.preview_limit if preview_limit is None else preview_limit)
        self._validate(*values)
        def commit():
            # Resolve unspecified settings under the lock, not from a stale
            # snapshot taken before another configure() completed.
            current = (self.max_bytes if max_bytes is None else max_bytes,
                       self.retention_days if retention_days is None else retention_days,
                       self.preview_limit if preview_limit is None else preview_limit)
            self._prune(max_bytes=current[0], retention_days=current[1])
            return {"ok": True, "max_bytes": current[0], "retention_days": current[1], "preview_limit": current[2],
                    "pending_cleanup": self._cleanup_pending(current[0], current[1])}
        def publish(result):
            self.max_bytes, self.retention_days, self.preview_limit = (
                result["max_bytes"], result["retention_days"], result["preview_limit"])
        return self._run(commit, {"ok": False}, write=True, on_commit=publish)

    def close(self):
        if self._closed:
            return
        def finish():
            self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._db.close()
            self._closed = True
        self._run(finish)
