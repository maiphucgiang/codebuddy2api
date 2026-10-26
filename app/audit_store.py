"""Store metadata-only SQLite audits with bounded waits; detail eviction preserves aggregates.
ASGI callers must offload synchronous methods; capacity limits cover logical detail storage only.
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
from urllib.parse import quote

SCHEMA_VERSION = 4
_CLEANUP_BATCH = 128
_CLEANUP_SECONDS = 0.05
METRICS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
           "reasoning_tokens", "total_tokens", "credit")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@-]{1,160}$")
_SECRET = re.compile(r"(?i)(bearer|sk-|cb-|access[_-]?token|refresh[_-]?token|api[_-]?key|eyJ|://)")


def safe_label(value: Any, limit: int = 160) -> str | None:
    """Accept identifiers, never free-form diagnostics, headers or bodies."""
    if not isinstance(value, str) or len(value) > limit:
        return None
    if not _IDENTIFIER.fullmatch(value) or _SECRET.search(value):
        return None
    return value



def safe_agent(value: Any) -> str | None:
    """Single-line printable User-Agent snippet; never header-shaped secrets."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:200]
    if not cleaned or not all(0x20 <= ord(char) <= 0x7E for char in cleaned):
        return None
    return None if _SECRET.search(cleaned) else cleaned


def safe_diagnostic(value: Any, limit: int = 256) -> str | None:
    """Short printable upstream diagnostic text; collapsed to one line and secret-checked."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:limit]
    if not cleaned or not cleaned.isprintable() or _SECRET.search(cleaned):
        return None
    return cleaned


_UPSTREAM_BODY_LIMIT = 256 * 1024
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}")


def safe_body(value: Any, limit: int = _UPSTREAM_BODY_LIMIT) -> str | None:
    """Bounded upstream error JSON; redacted upstream, re-checked here for token leakage."""
    if not isinstance(value, str) or not value:
        return None
    if len(value) > limit:
        value = value[:limit] + "...[truncated]"
    if _JWT.search(value):
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
                "credential", "usage_source", "outcome", "consent_source", "agreement_revision", "conversation_id", "request_id",
                "attempt_id", "upstream_request_id", "session_source", "upstream_code"):
        clean = safe_label(value.get(key))
        if clean is not None:
            result[key] = clean
    for key in ("upstream_msg",):
        clean = safe_diagnostic(value.get(key))
        if clean is not None:
            result[key] = clean
    for key in ("upstream_body",):
        clean = safe_body(value.get(key))
        if clean is not None:
            result[key] = clean
    for key in ("status_code", "duration_ms", "attempt", "retry_after", "max_attempts", "total_tokens",
                "attempt_index", "dropped", "merged_runs", "merged_messages"):
        clean = number(value.get(key))
        if clean is not None:
            result[key] = clean
    return result


CREDIT_CHANGES = ("income", "consume", "expire", "adjust")


def safe_credit_change(value: Any) -> dict:
    """Signed credit deltas plus the upstream package label; never account names or tokens."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("credential", "package_code"):
        clean = safe_label(value.get(key))
        if clean is not None:
            result[key] = clean
    source = value.get("source")
    # Package labels are short upstream display text, often Chinese, so they cannot be identifiers.
    if (isinstance(source, str) and 0 < len(source) <= 60 and source.isprintable()
            and not _SECRET.search(source)):
        result["source"] = source
    delta = value.get("delta")
    if not isinstance(delta, bool) and isinstance(delta, (int, float)) and math.isfinite(delta) \
            and abs(delta) <= 1e12:
        result["delta"] = delta
    for key in ("balance", "expires_at"):
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
    # 一次瞬时写失败只让降级标记保持一天；终身计数仍如实上报，不随时间消失。
    DEGRADED_WINDOW_SECONDS = 24 * 3600
    TRANSIENT_WINDOW_SECONDS = 3600

    def __init__(self, path, max_bytes=256 * 1024 * 1024, retention_days=30,
                 preview_limit=1536 * 1024):
        self._validate(max_bytes, retention_days, preview_limit)
        self.max_bytes, self.retention_days, self.preview_limit = max_bytes, retention_days, preview_limit
        self._lock = threading.Lock()
        self._archive_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self.failure_count = self.dropped_records = self.transient_count = 0
        self.failure_events: list[float] = []
        self.transient_events: list[float] = []
        self.last_failure_at: float | None = None
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
            if version not in (0, 1, 2, 3, SCHEMA_VERSION) or (version == 0 and tables):
                raise ValueError("unsupported audit schema")
            self._db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL, detail_generation INTEGER NOT NULL, cleared_at REAL NOT NULL)")
            self._db.execute("INSERT OR IGNORE INTO state VALUES(1,0,0,0)")
            self._db.execute("CREATE TABLE IF NOT EXISTS ingest (id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, started_at REAL NOT NULL, model TEXT, profile TEXT, credential TEXT, outcome TEXT, status_code INTEGER, payload TEXT NOT NULL, logical_bytes INTEGER NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS requests_time ON requests(started_at,id)")
            self._db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, started_at REAL NOT NULL, kind TEXT NOT NULL, action TEXT, payload TEXT NOT NULL, logical_bytes INTEGER NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS events_time ON events(started_at,id)")
            self._init_accounting()
            self._migrate_attempts()
            for table in ("stats_hourly", "stats_daily", "stats_totals"):
                self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} (bucket INTEGER NOT NULL, dimension TEXT NOT NULL, dimension_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(bucket,dimension,dimension_key))")
            self._migrate_ingest_time()
            self._migrate_client_ip()
            self._migrate_duration()
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._epoch = self._db.execute("SELECT epoch FROM state").fetchone()[0]
            self._db.execute("COMMIT")
        except Exception:
            self._db.close()
            raise

    def _migrate_ingest_time(self):
        # Backfill dedup timestamps so retention can expire them alongside request details.
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(ingest)").fetchall()]
        if "created_at" not in columns:
            self._db.execute("ALTER TABLE ingest ADD COLUMN created_at REAL")
            self._db.execute("""UPDATE ingest SET created_at=COALESCE(
                (SELECT started_at FROM requests WHERE requests.id=ingest.id),
                (SELECT started_at FROM events WHERE events.id=ingest.id), ?)""", (time.time(),))
        self._db.execute("CREATE INDEX IF NOT EXISTS ingest_time ON ingest(created_at)")

    def _migrate_client_ip(self):
        # Additive v3 migration: record the audit-visible client address per request.
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(requests)").fetchall()]
        if "client_ip" not in columns:
            self._db.execute("ALTER TABLE requests ADD COLUMN client_ip TEXT")
        # Exact-match client filters must not scan the retained time range.
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS requests_client ON requests(client_ip, started_at)")

    def _migrate_duration(self):
        # Additive v4 migration: a covered (started_at, duration_ms) index keeps
        # the dashboard P95 scan off per-row json_extract payload parsing.
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(requests)").fetchall()]
        if "duration_ms" not in columns:
            self._db.execute("ALTER TABLE requests ADD COLUMN duration_ms REAL")
            self._db.execute("UPDATE requests SET duration_ms=json_extract(payload,'$.duration_ms')")
        self._db.execute("CREATE INDEX IF NOT EXISTS requests_duration ON requests(started_at,duration_ms)")

    def _migrate_attempts(self):
        # Attempts live only in the request payload; the per-row copy was never read.
        if not self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'").fetchone():
            return
        # Recount without the duplicated attempts; audit_requests_update keeps detail_accounting in step.
        self._db.execute("UPDATE requests SET logical_bytes=length(CAST(payload AS BLOB))+256")
        # The old delete trigger cascades into the table dropped below.
        self._db.execute("DROP TRIGGER IF EXISTS audit_requests_delete")
        self._init_accounting()
        self._db.execute("DROP TABLE attempts")

    def _open_read(self):
        """Fresh read-only WAL snapshot per call; None falls back to the write path."""
        try:
            conn = sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True, timeout=5,
                                   check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            return conn
        except sqlite3.Error:
            return None

    def _init_accounting(self):
        # Additive v1 migration: scan legacy details only once, under the same
        # write transaction that installs triggers. Reopening never recounts.
        self._db.execute("CREATE TABLE IF NOT EXISTS detail_accounting (id INTEGER PRIMARY KEY CHECK(id=1), logical_bytes INTEGER NOT NULL, request_count INTEGER NOT NULL, event_count INTEGER NOT NULL, ingest_count INTEGER NOT NULL, cleanup_target INTEGER)")
        if not self._db.execute("SELECT 1 FROM detail_accounting WHERE id=1").fetchone():
            self._db.execute("INSERT INTO detail_accounting SELECT 1,COALESCE((SELECT SUM(logical_bytes) FROM requests),0)+COALESCE((SELECT SUM(logical_bytes) FROM events),0),(SELECT COUNT(*) FROM requests),(SELECT COUNT(*) FROM events),(SELECT COUNT(*) FROM ingest),NULL")
        for table, counter in (("requests", "request_count"), ("events", "event_count")):
            self._db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_{table}_insert AFTER INSERT ON {table} BEGIN UPDATE detail_accounting SET logical_bytes=logical_bytes+NEW.logical_bytes,{counter}={counter}+1 WHERE id=1; END")
            self._db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_{table}_delete AFTER DELETE ON {table} BEGIN UPDATE detail_accounting SET logical_bytes=logical_bytes-OLD.logical_bytes,{counter}={counter}-1 WHERE id=1; END")
            self._db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_{table}_update AFTER UPDATE OF logical_bytes ON {table} BEGIN UPDATE detail_accounting SET logical_bytes=logical_bytes+NEW.logical_bytes-OLD.logical_bytes WHERE id=1; END")
        for operation, delta in (("INSERT", "+1"), ("DELETE", "-1")):
            self._db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_ingest_{operation.lower()} AFTER {operation} ON ingest BEGIN UPDATE detail_accounting SET ingest_count=ingest_count{delta} WHERE id=1; END")

    @staticmethod
    def _validate(max_bytes, retention_days, preview_limit):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 <= max_bytes <= 2**40:
            raise ValueError("invalid max_bytes")
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 1 <= retention_days <= 36500:
            raise ValueError("invalid retention_days")
        if isinstance(preview_limit, bool) or not isinstance(preview_limit, int) or not 0 <= preview_limit <= 4 * 1024 * 1024:
            raise ValueError("invalid preview_limit")

    def _fault(self, exc, dropped=False):
        # Lock contention is expected under load and never latches the degraded
        # banner; only real storage errors do. Lifetime counters stay honest.
        with self._health_lock:
            self.failure_count += 1
            self.dropped_records += int(dropped)
            self.last_failure_at = time.time()
            if isinstance(exc, TimeoutError):
                self.transient_count += 1
                self.transient_events.append(time.monotonic())
                return
            # Exception messages may include SQL or user data; retain only type.
            self.failure_events.append(time.monotonic())
            self.last_error = type(exc).__name__

    def _recent_failures(self):
        # 只统计降级窗口内的失败，瞬时抖动过后横幅自动消失；计数不丢历史。
        with self._health_lock:
            deadline = time.monotonic() - self.DEGRADED_WINDOW_SECONDS
            while self.failure_events and self.failure_events[0] <= deadline:
                self.failure_events.pop(0)
            return len(self.failure_events)

    def _recent_transients(self):
        # Contention is reported for a shorter window; it is visibility, not alarm.
        with self._health_lock:
            deadline = time.monotonic() - self.TRANSIENT_WINDOW_SECONDS
            while self.transient_events and self.transient_events[0] <= deadline:
                self.transient_events.pop(0)
            return len(self.transient_events)

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
            result = callback(self._db)
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

    def _read_run(self, callback, fallback=None):
        """Each read takes a fresh read-only WAL snapshot: dashboards and log
        listings never queue behind write transactions or each other."""
        conn = self._open_read()
        if conn is None:
            return self._run(callback, fallback)
        try:
            if self._closed:
                raise RuntimeError("closed")
            deadline = time.monotonic() + 5
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            return callback(conn)
        except Exception as exc:
            self._fault(exc)
            return fallback
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    @property
    def epoch(self):
        return self.ticket()["epoch"]

    def ticket(self):
        def fetch(db):
            row = db.execute("SELECT epoch,detail_generation FROM state").fetchone()
            self._epoch = row["epoch"]
            return dict(row)
        return self._read_run(fetch, {"epoch": self._epoch, "detail_generation": -1})

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

    def _aggregate(self, db, record):
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
        # Unknown model/profile/credential values get no bucket of their own: the global
        # row still counts the request, and an empty key would rank as "未识别模型".
        dimensions += [(key, value)
                       for key in ("model", "profile", "credential")
                       if (value := record.get("public_model" if key == "model" else key))]
        for table, seconds in (("stats_hourly", 3600), ("stats_daily", 86400), ("stats_totals", 0)):
            bucket = int(record["started_at"] // seconds) * seconds if seconds else 0
            for dimension, key in dimensions:
                old = db.execute(f"SELECT payload FROM {table} WHERE bucket=? AND dimension=? AND dimension_key=?", (bucket, dimension, key)).fetchone()
                stats = self._merge(json.loads(old[0]) if old else self._empty_stats(), increment)
                db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?,?,?)", (bucket, dimension, key, json.dumps(stats)))

    def _sanitize_record(self, source):
        result = {key: safe_label(source.get(key)) for key in
                  ("upstream_model", "profile", "credential", "protocol", "error_code", "usage_source")}
        result["client_ip"] = safe_label(source.get("client_ip"))
        result["user_agent"] = safe_agent(source.get("user_agent"))
        result["stream_mode"] = (source.get("stream_mode")
                                 if source.get("stream_mode") in ("compatible", "realtime") else None)
        projection_mode = source.get("responses_projection_mode")
        if projection_mode in ("balanced", "passthrough"):
            result["responses_projection_mode"] = projection_mode
            result["responses_projection_max_bytes"] = number(source.get("responses_projection_max_bytes"))
            result["responses_truncated_items"] = number(source.get("responses_truncated_items"))
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
        def commit(db):
            data = self._sanitize_record(record)
            state = db.execute("SELECT * FROM state").fetchone()
            if data["epoch"] != state["epoch"]:
                return {"ok": True, "recorded": False, "reason": "stale_epoch"}
            if not db.execute("INSERT OR IGNORE INTO ingest VALUES(?, 'request', ?)", (data["id"], data["started_at"])).rowcount:
                return {"ok": True, "recorded": False, "reason": "duplicate"}
            self._aggregate(db, data)
            generation = record.get("detail_generation")
            keep = (data["started_at"] > state["cleared_at"] and
                    (generation is None or generation == state["detail_generation"]))
            if keep:
                payload = json.dumps(data, separators=(",", ":"))
                db.execute(
                    "INSERT INTO requests(id,started_at,model,profile,credential,outcome,status_code,payload,logical_bytes,client_ip,duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (data["id"], data["started_at"], data["model"], data["profile"], data["credential"],
                     data["outcome"], data["status_code"], payload,
                     len(payload.encode()) + 256, data["client_ip"], data["duration_ms"]))
            self._prune()
            return {"ok": True, "recorded": True, "details": bool(keep and db.execute("SELECT 1 FROM requests WHERE id=?", (data["id"],)).fetchone())}
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
        # Expire dedup records with request details to bound retention growth.
        for row in self._db.execute("SELECT id FROM ingest WHERE created_at<? ORDER BY created_at LIMIT ?",
                                    (cutoff, _CLEANUP_BATCH)):
            if time.monotonic() >= deadline:
                break
            self._db.execute("DELETE FROM ingest WHERE id=?", (row[0],))

    def _cleanup_pending(self, budget=None, retention_days=None, db=None):
        budget = self.max_bytes if budget is None else budget
        cutoff = time.time() - (self.retention_days if retention_days is None else retention_days) * 86400
        db = self._db if db is None else db
        accounting = db.execute("SELECT logical_bytes,cleanup_target FROM detail_accounting WHERE id=1").fetchone()
        ingest_expired = db.execute(
            "SELECT 1 FROM ingest WHERE created_at<? LIMIT 1", (cutoff,)).fetchone()
        return (accounting[0] > budget or accounting[1] is not None or ingest_expired or any(
            db.execute(f"SELECT 1 FROM {table} WHERE started_at<? LIMIT 1", (cutoff,)).fetchone()
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
        if kind not in ("runtime", "admin", "credit"):
            raise ValueError("invalid event kind")
        started_at = time.time()
        def commit(db):
            source = details if isinstance(details, dict) else {}
            state = db.execute("SELECT * FROM state").fetchone()
            if source.get("epoch", state["epoch"]) != state["epoch"]:
                return {"ok": True, "recorded": False, "reason": "stale_epoch"}
            event_id = safe_label(source.get("event_id", source.get("id"))) or uuid.uuid4().hex
            if not db.execute("INSERT OR IGNORE INTO ingest VALUES(?,?,?)", (event_id, kind, started_at)).rowcount:
                return {"ok": True, "recorded": False, "reason": "duplicate"}
            if (started_at <= state["cleared_at"] or
                    source.get("detail_generation", state["detail_generation"]) != state["detail_generation"]):
                return {"ok": True, "recorded": False, "reason": "details_cleared"}
            data = {"id": event_id, "kind": kind, "action": safe_label(action),
                    "started_at": started_at,
                    "details": safe_credit_change(source) if kind == "credit" else safe_attempt(source)}
            payload = json.dumps(data)
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (event_id, data["started_at"], kind, data["action"], payload, len(payload.encode()) + 128))
            self._prune()
            return {"ok": True, "recorded": True, "id": event_id}
        return self._run(commit, {"ok": False, "recorded": False}, write=True, dropped=True)

    def list_records(self, kind="request", limit=50, cursor=None, **filters):
        if kind not in ("request", "runtime", "admin", "credit"):
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
            for key in ("model", "profile", "credential", "outcome", "status_code", "client_ip"):
                value = filters.get(key)
                if value is not None and value != "":
                    clauses.append(f"{key}=?")
                    params.append(value)
            status = filters.get("status")
            if status is not None and status != "":
                column = "outcome" if status in ("success", "error", "cancelled") else "status_code"
                clauses.append(f"{column}=?")
                params.append(status)
        if kind == "credit":
            if filters.get("credential"):
                clauses.append("json_extract(payload,'$.details.credential')=?")
                params.append(filters["credential"])
            change = filters.get("type")
            if change:
                if change not in CREDIT_CHANGES:
                    raise ValueError("invalid credit change type")
                clauses.append("action=?")
                params.append("credit." + change)
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
        def fetch(db):
            params[0] = time.time() - self.retention_days * 86400
            rows = db.execute(f"SELECT payload,started_at,id FROM {table}{where} ORDER BY started_at DESC,id DESC LIMIT ?", (*params, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            return {"items": [json.loads(row["payload"]) for row in rows],
                    "next_cursor": json.dumps([rows[-1]["started_at"], rows[-1]["id"]]) if more else None,
                    "has_more": more}
        return self._read_run(fetch, {"items": [], "next_cursor": None, "has_more": False, "degraded": True})

    def get_request(self, id):
        def fetch(db):
            row = db.execute("SELECT payload FROM requests WHERE id=? AND started_at>=?", (id, time.time() - self.retention_days * 86400)).fetchone()
            return json.loads(row[0]) if row else None
        return self._read_run(fetch)

    def dashboard(self, days=30, granularity="auto"):
        days = int(days)
        if not 1 <= days <= 36500 or granularity not in ("auto", "hour", "day"):
            raise ValueError("invalid dashboard range or granularity")
        grain = ("hour" if days == 1 else "day") if granularity == "auto" else granularity
        if grain == "hour" and days > 90:
            raise ValueError("hourly range exceeds 90 days")
        now = time.time()
        start = int(now // 86400) * 86400 - (days - 1) * 86400
        period = {"days": days, "start": start, "end": now, "timezone": "UTC", "granularity": grain}
        def fetch(db):
            summary = self._empty_stats()
            series, models, profiles = [], {}, {}
            rows = db.execute("SELECT * FROM stats_daily WHERE bucket>=? AND bucket<=? ORDER BY bucket", (start, now)).fetchall()
            for row in rows:
                stats = json.loads(row["payload"])
                if row["dimension"] == "global":
                    self._merge(summary, stats)
                    if grain == "day":
                        series.append({"bucket": row["bucket"], **stats})
                elif row["dimension"] in ("model", "profile"):
                    # Legacy rows recorded an empty key for unidentified models; skip
                    # them instead of surfacing an anonymous bucket in the ranking.
                    if not row["dimension_key"]:
                        continue
                    target = models if row["dimension"] == "model" else profiles
                    self._merge(target.setdefault(row["dimension_key"], self._empty_stats()), stats)
            if grain == "hour":
                rows = db.execute("SELECT bucket,payload FROM stats_hourly WHERE dimension='global' AND bucket>=? AND bucket<=? ORDER BY bucket", (start, now)).fetchall()
                series = [{"bucket": row["bucket"], **json.loads(row["payload"])} for row in rows]
            partial = grain == "hour" and sum(row["requests"] for row in series) != summary["requests"]
            step = 3600 if grain == "hour" else 86400
            if days <= 90 and not partial:
                recorded = {row["bucket"]: row for row in series}
                series = [recorded.get(bucket, {"bucket": bucket, **self._empty_stats()})
                          for bucket in range(start, int(now // step) * step + 1, step)]
            for row in series:
                row["date"] = time.strftime("%Y-%m-%d %H:00" if grain == "hour" else "%Y-%m-%d", time.gmtime(row["bucket"]))
            summary["success_rate"] = summary["success"] / summary["requests"] if summary["requests"] else None
            previous = self._empty_stats()
            for row in db.execute(
                    "SELECT payload FROM stats_daily WHERE dimension='global' AND bucket>=? AND bucket<?",
                    (start - days * 86400, start)).fetchall():
                self._merge(previous, json.loads(row["payload"]))
            previous["success_rate"] = previous["success"] / previous["requests"] if previous["requests"] else None
            known = db.execute(
                "SELECT COUNT(*) FROM requests WHERE started_at>=? AND started_at<=? AND duration_ms IS NOT NULL",
                (start, now)).fetchone()[0]
            # Detail rows can be pruned, archived, or cleared independently of daily
            # aggregates; only report P95 when the detail sample covers the period.
            summary["p95_duration_ms"] = (
                db.execute(
                    "SELECT duration_ms FROM requests WHERE started_at>=? AND started_at<=? AND duration_ms IS NOT NULL"
                    " ORDER BY duration_ms LIMIT 1 OFFSET ?",
                    (start, now, max(0, math.ceil(known * 0.95) - 1))).fetchone()[0]
                if known and known >= summary["duration_ms_known"]
                else None)
            return {"summary": summary, "previous": previous, "series": series,
                    "models": [{"model": key, **value} for key, value in models.items()],
                    "profiles": [{"profile": key, **value} for key, value in profiles.items()],
                    "generated_at": now, "range": {**period, "partial": partial}}
        return self._read_run(fetch, {"summary": {**self._empty_stats(), "p95_duration_ms": None},
                                 "previous": None, "series": [], "models": [], "profiles": [],
                                 "generated_at": now, "range": period, "degraded": True})

    def credential_stats(self):
        def fetch(db):
            out = {}
            for row in db.execute(
                    "SELECT dimension_key,payload FROM stats_totals WHERE dimension='credential' AND dimension_key!=''").fetchall():
                stats = json.loads(row["payload"])
                reqs = stats.get("requests") or 0
                dur_known = stats.get("duration_ms_known") or 0
                out[row["dimension_key"]] = {
                    "requests": reqs,
                    "success": stats.get("success") or 0,
                    "error": stats.get("error") or 0,
                    "success_rate": (stats.get("success") or 0) / reqs if reqs else None,
                    "input_tokens": stats.get("input_tokens"),
                    "output_tokens": stats.get("output_tokens"),
                    "cache_read_tokens": stats.get("cache_read_tokens"),
                    "total_tokens": stats.get("total_tokens"),
                    "credit": stats.get("credit"),
                    "avg_duration_ms": round((stats.get("duration_ms_sum") or 0) / dur_known, 1) if dur_known else None,
                }
            for row in db.execute(
                    "SELECT credential,MAX(started_at) AS last_at,model,outcome,status_code "
                    "FROM requests WHERE credential IS NOT NULL AND credential!='' GROUP BY credential").fetchall():
                entry = out.setdefault(row["credential"], {
                    "requests": 0, "success": 0, "error": 0, "success_rate": None,
                    "input_tokens": None, "output_tokens": None, "cache_read_tokens": None,
                    "total_tokens": None, "credit": None, "avg_duration_ms": None,
                })
                entry["last_request_at"] = row["last_at"]
                entry["last_model"] = row["model"]
                entry["last_outcome"] = row["outcome"]
                entry["last_status_code"] = row["status_code"]
            return out
        return self._read_run(fetch, {})

    def credit_samples(self, since):
        """Per-request upstream charges with their token counts, oldest first.

        Only requests whose upstream usage reported a credit figure are returned; the
        model is the upstream one, since billing follows what was actually sent.
        """
        def fetch(db):
            rows = db.execute(
                "SELECT started_at, profile, COALESCE(json_extract(payload,'$.upstream_model'), model) AS model, "
                "json_extract(payload,'$.credit') AS credit, "
                "COALESCE(json_extract(payload,'$.input_tokens'),0) AS input_tokens, "
                "COALESCE(json_extract(payload,'$.cache_read_tokens'),0) AS cache_read_tokens, "
                "COALESCE(json_extract(payload,'$.output_tokens'),0) AS output_tokens "
                "FROM requests WHERE started_at>=? AND json_extract(payload,'$.credit') IS NOT NULL "
                "AND profile IS NOT NULL "
                # Probes are logged too, but their calibrated samples come from the probe table.
                "AND COALESCE(json_extract(payload,'$.protocol'),'')!='rate_probe' "
                "ORDER BY started_at", (since,)).fetchall()
            return [dict(row) for row in rows if row["model"]]
        return self._read_run(fetch, [])

    def storage(self):
        # Opening the storage/dashboard page keeps incremental cleanup moving
        # with the same bounded write slice; heavy scans stay read-only.
        self._run(lambda db: self._prune(), write=True)

        def fetch(db):
            self._epoch = db.execute("SELECT epoch FROM state").fetchone()[0]
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            freelist = db.execute("PRAGMA freelist_count").fetchone()[0]
            return {**dict(db.execute("SELECT logical_bytes,request_count,event_count,ingest_count FROM detail_accounting WHERE id=1").fetchone()),
                    "pending_cleanup": self._cleanup_pending(db=db),
                    "page_count": db.execute("PRAGMA page_count").fetchone()[0],
                    "freelist_pages": freelist, "reclaimable_bytes": freelist * page_size}
        result = self._read_run(fetch, {"logical_bytes": None, "pending_cleanup": None,
                                        "reclaimable_bytes": None})
        sizes = {}
        for key, suffix in (("db_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm")):
            try:
                sizes[key] = Path(str(self.path) + suffix).lstat().st_size
            except FileNotFoundError:
                sizes[key] = 0
            except OSError as exc:
                self._fault(exc)
                sizes[key] = None
        recent = self._recent_failures()
        return {**result, **sizes, "max_bytes": self.max_bytes, "retention_days": self.retention_days,
                "preview_limit": self.preview_limit, "schema_version": SCHEMA_VERSION, "epoch": self._epoch,
                "degraded": bool(recent), "recent_failures": recent, "failure_count": self.failure_count,
                "transient_contention": self._recent_transients(), "transient_count": self.transient_count,
                "dropped_records": self.dropped_records, "last_error": self.last_error,
                "last_failure_at": self.last_failure_at,
                "closed": self._closed, "budget_scope": "details_logical_bytes",
                "fault_counter_scope": "process_lifetime", "automatic_vacuum": False,
                "lock_timeout_ms": 250, "sql_deadline_ms": 1000}

    def clear(self, scope="details"):
        if scope not in ("details", "all"):
            raise ValueError("invalid clear scope")
        def commit(db):
            for table in ("requests", "events"):
                db.execute(f"DELETE FROM {table}")
            db.execute("UPDATE detail_accounting SET cleanup_target=NULL WHERE id=1")
            db.execute("UPDATE state SET detail_generation=detail_generation+1,cleared_at=? WHERE id=1", (time.time(),))
            if scope == "all":
                for table in ("stats_hourly", "stats_daily", "stats_totals", "ingest"):
                    db.execute(f"DELETE FROM {table}")
                db.execute("UPDATE state SET epoch=epoch+1 WHERE id=1")
            epoch = db.execute("SELECT epoch FROM state").fetchone()[0]
            return {"ok": True, "scope": scope, "epoch": epoch, "aggregates_preserved": scope == "details"}
        return self._run(commit, {"ok": False, "scope": scope}, write=True,
                         on_commit=lambda result: setattr(self, "_epoch", result["epoch"]))

    def archive(self, days):
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650:
            raise ValueError("invalid archive window")
        cutoff = time.time() - days * 86400
        if not self._archive_lock.acquire(timeout=0.25):
            self._fault(TimeoutError())
            return {"ok": False}
        def step(callback, write=False):
            if not self._lock.acquire(timeout=0.25):
                raise TimeoutError()
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
                return result
            except Exception:
                if not self._closed:
                    try:
                        if self._db.in_transaction:
                            self._db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                if not self._closed:
                    self._db.set_progress_handler(None, 0)
                self._lock.release()
        target = None
        try:
            has_old = step(lambda: (
                self._db.execute("SELECT 1 FROM requests WHERE started_at<? LIMIT 1", (cutoff,)).fetchone()
                or self._db.execute("SELECT 1 FROM events WHERE started_at<? LIMIT 1", (cutoff,)).fetchone()))
            if not has_old:
                return {"ok": True, "archived_requests": 0, "archived_events": 0, "file": None}
            directory = Path(str(self.path)).parent / "archives"
            directory.mkdir(mode=0o700, exist_ok=True)
            name = f"audit-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(cutoff))}-{uuid.uuid4().hex[:8]}.jsonl"
            target = directory / name
            tmp = target.with_suffix(".tmp")
            request_ids: list[str] = []
            event_ids: list[str] = []
            try:
                with open(tmp, "x", encoding="utf-8") as handle:
                    # Short per-batch lock holds let live record_request/event calls
                    # interleave within their 250ms lock budget while file I/O runs unlocked.
                    last = (0.0, "")
                    while True:
                        rows = step(lambda last=last: self._db.execute(
                            "SELECT id,started_at,model,profile,credential,outcome,status_code,payload,client_ip "
                            "FROM requests WHERE started_at<? AND (started_at,id)>(?,?) "
                            "ORDER BY started_at,id LIMIT 2000",
                            (cutoff, *last)).fetchall())
                        if not rows:
                            break
                        for row in rows:
                            handle.write(json.dumps(
                                {"kind": "request", "id": row[0], "started_at": row[1], "model": row[2],
                                 "profile": row[3], "credential": row[4], "outcome": row[5],
                                 "status_code": row[6], "payload": json.loads(row[7]), "client_ip": row[8]},
                                ensure_ascii=False) + "\n")
                            request_ids.append(row[0])
                        last = (rows[-1][1], rows[-1][0])
                    last = (0.0, "")
                    while True:
                        rows = step(lambda last=last: self._db.execute(
                            "SELECT id,started_at,kind,action,payload FROM events "
                            "WHERE started_at<? AND (started_at,id)>(?,?) ORDER BY started_at,id LIMIT 2000",
                            (cutoff, *last)).fetchall())
                        if not rows:
                            break
                        for row in rows:
                            handle.write(json.dumps(
                                {"kind": "event", "id": row[0], "started_at": row[1], "event_kind": row[2],
                                 "action": row[3], "payload": json.loads(row[4])},
                                ensure_ascii=False) + "\n")
                            event_ids.append(row[0])
                        last = (rows[-1][1], rows[-1][0])
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, target)
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            # Delete only the exact exported detail rows in short slices; ingest dedup
            # keys stay until _expire() prunes them at retention_days.
            for sql, ids in (("DELETE FROM requests WHERE id=?", request_ids),
                             ("DELETE FROM events WHERE id=?", event_ids)):
                for i in range(0, len(ids), 500):
                    params = [(item,) for item in ids[i:i + 500]]
                    step(lambda sql=sql, params=params: self._db.executemany(sql, params), write=True)
            return {"ok": True, "archived_requests": len(request_ids),
                    "archived_events": len(event_ids), "file": name}
        except Exception as exc:
            if target is not None:
                try:
                    target.unlink()
                except OSError:
                    pass
            self._fault(exc)
            return {"ok": False}
        finally:
            self._archive_lock.release()

    def configure(self, max_bytes=None, retention_days=None, preview_limit=None):
        values = (self.max_bytes if max_bytes is None else max_bytes,
                  self.retention_days if retention_days is None else retention_days,
                  self.preview_limit if preview_limit is None else preview_limit)
        self._validate(*values)
        def commit(db):
            # Resolve unspecified settings under the lock, not from a stale
            # snapshot taken before another configure() completed.
            current = (self.max_bytes if max_bytes is None else max_bytes,
                       self.retention_days if retention_days is None else retention_days,
                       self.preview_limit if preview_limit is None else preview_limit)
            self._prune(max_bytes=current[0], retention_days=current[1])
            return {"ok": True, "max_bytes": current[0], "retention_days": current[1], "preview_limit": current[2],
                    "pending_cleanup": self._cleanup_pending(current[0], current[1], db=db)}
        def publish(result):
            self.max_bytes, self.retention_days, self.preview_limit = (
                result["max_bytes"], result["retention_days"], result["preview_limit"])
        return self._run(commit, {"ok": False}, write=True, on_commit=publish)

    def close(self):
        if self._closed:
            return
        def finish(db):
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            db.close()
            self._closed = True
        self._run(finish)
