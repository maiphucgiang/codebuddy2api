"""Local media assets and video task metadata in one sqlite-backed store.

Files live under ``media/images|videos/<shard>/<id>.<ext>`` and are committed
atomically from ``media/.incoming``; heavy work (sniffing, hashing, writing the
pending file) stays outside the store lock. Metadata stays out of ControlStore
snapshots and backups. Retention is always derived from the current TTL setting,
so shrinking it applies to existing assets immediately.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

DB_NAME = "media.sqlite3"
SCHEMA_VERSION = 2              # v2 keeps the submitted public model name on video tasks.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
INCOMING_DIR = ".incoming"

# Fixed magic numbers, sniffed before anything is committed to disk.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)
_RIFF_WEBP = (b"RIFF", 0, b"WEBP", 8)
_FTYP = (b"ftyp", 4)

KIND_MIMES = {"image": ("image/png", "image/jpeg", "image/webp", "image/gif"),
              "video": ("video/mp4", "video/webm")}
KIND_PREFIX = {"image": "img_", "video": "vid_"}

# Distinguishes "leave error unchanged" from an explicit error=None to clear it.
UNSET = object()


def sniff(data: bytes):
    """Identify an allowed media type from its header; None means rejection."""
    for magic, ext, mime in _MAGIC:
        if data.startswith(magic):
            return ext, mime
    if data[:4] == _RIFF_WEBP[0] and data[8:12] == _RIFF_WEBP[2]:
        return "webp", "image/webp"
    if data[_FTYP[1]:_FTYP[1] + 4] == _FTYP[0]:
        return "mp4", "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm", "video/webm"
    return None


def is_expired(meta: dict, ttl_hours: float) -> bool:
    """Retention is derived from the current TTL, never stored per asset."""
    try:
        return float(meta.get("created") or 0) + float(ttl_hours) * 3600 <= time.time()
    except (TypeError, ValueError):
        return True


def security_headers(meta: dict) -> dict:
    """Serving headers shared by the public and admin read routes."""
    return {"ETag": f'"{meta["sha256"]}"', "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff"}


def _row_to_asset(row) -> dict:
    keys = ("id", "kind", "mime", "size", "sha256", "model", "created")
    return dict(zip(keys, row))


def _row_to_task(row) -> dict:
    keys = ("task_id", "account_key", "profile", "model", "created", "updated",
            "status", "media_id", "error", "credit", "public_model")
    return dict(zip(keys, row))


class MediaStore:
    """Own media/<kind>/<shard>/ files plus the assets and tasks tables."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.directory = self.root / "media"
        self.path = self.root / DB_NAME
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / INCOMING_DIR).mkdir(exist_ok=True)
        self.sweep_incoming()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        self._migrate_schema()
        self._db.commit()

    def _create_schema(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS assets(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, model TEXT, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS assets_created ON assets(created);
            CREATE TABLE IF NOT EXISTS tasks(
                task_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, profile TEXT NOT NULL,
                model TEXT, created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
                media_id TEXT, error TEXT, credit REAL, public_model TEXT);
            CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created);
        """)

    def _migrate_schema(self):
        """Apply additive task metadata and remove the obsolete asset expiry column."""
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if version < 1:
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(assets)").fetchall()}
            if "expires" in columns:
                self._db.execute("ALTER TABLE assets DROP COLUMN expires")
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(tasks)").fetchall()}
        if "public_model" not in columns:
            self._db.execute("ALTER TABLE tasks ADD COLUMN public_model TEXT")
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()
    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # -- assets -------------------------------------------------------------------------

    def _asset_path(self, asset_id: str, ext: str) -> Path:
        return self.directory / ("videos" if asset_id.startswith("vid_") else "images") \
            / asset_id[4:6] / f"{asset_id}.{ext}"

    def _new_id(self, kind: str) -> str:
        return KIND_PREFIX[kind] + secrets.token_hex(16)

    def _total_bytes(self) -> int:
        return self._db.execute("SELECT COALESCE(SUM(size),0) FROM assets").fetchone()[0]

    def _write_pending(self, ext: str, data: bytes, digest: str) -> Path:
        """Write the payload to .incoming outside the store lock (heavy work)."""
        pending = self.directory / INCOMING_DIR / f"{digest[:16]}-{secrets.token_hex(8)}.{ext}"
        pending.write_bytes(data)
        return pending

    def save_asset(self, kind: str, data: bytes, *, model=None, ttl_hours: float,
                   max_total_bytes: int) -> dict:
        """Sniff, hash and stage outside the lock, then commit atomically inside it."""
        if kind not in KIND_MIMES or not isinstance(data, bytes) or not data:
            raise ValueError("媒体类型或内容无效")
        identified = sniff(data)
        if identified is None or identified[1] not in KIND_MIMES[kind]:
            raise ValueError("文件头不在允许的媒体类型白名单内")
        ext, mime = identified
        digest = hashlib.sha256(data).hexdigest()
        pending = self._write_pending(ext, data, digest)
        try:
            with self._lock:
                stale = self._cleanup_locked(ttl_hours=ttl_hours,
                                             max_total_bytes=max(0, max_total_bytes - len(data)))
                for _ in range(4):  # A collision keeps the existing object untouched.
                    asset_id = self._new_id(kind)
                    target = self._asset_path(asset_id, ext)
                    if not target.exists():
                        break
                else:
                    raise RuntimeError("无法分配新的媒体 ID")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(pending, target)
                try:
                    self._db.execute(
                        "INSERT INTO assets VALUES(?,?,?,?,?,?,?)",
                        (asset_id, kind, mime, len(data), digest, model, time.time()))
                    self._db.commit()
                except sqlite3.Error:
                    target.unlink(missing_ok=True)
                    raise
                row = self._db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
                meta = _row_to_asset(row)
            self._unlink(stale)
            return meta
        finally:
            pending.unlink(missing_ok=True)

    def get_asset(self, asset_id: str):
        with self._lock:
            row = self._db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            return _row_to_asset(row) if row else None

    def asset_file(self, meta: dict) -> Path | None:
        """Return the committed file for a metadata row, or None when it is gone."""
        ext = (meta.get("mime") or "").split("/")[-1]
        ext = {"jpeg": "jpg"}.get(ext, ext)
        path = self._asset_path(meta["id"], ext)
        return path if path.is_file() else None

    def _delete_rows(self, asset_ids) -> list:
        """Delete metadata inside the lock and return the files to unlink outside."""
        paths = []
        for asset_id in asset_ids:
            row = self._db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                continue
            meta = _row_to_asset(row)
            self._db.execute("UPDATE tasks SET media_id=NULL WHERE media_id=?", (asset_id,))
            self._db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            file = self.asset_file(meta)
            if file is not None:
                paths.append(file)
        self._db.commit()
        return paths

    def delete_asset(self, asset_id: str) -> bool:
        """Remove one file and row; bound tasks keep their status with media_id cleared."""
        with self._lock:
            exists = self._db.execute(
                "SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone() is not None
            paths = self._delete_rows([asset_id]) if exists else []
        self._unlink(paths)
        return exists

    @staticmethod
    def _unlink(paths) -> None:
        """Best-effort: this runs after the metadata commit and must not fail a committed change."""
        for file in paths:
            try:
                file.unlink(missing_ok=True)
            except OSError:
                pass

    def sweep_incoming(self, max_age_seconds: float = 3600) -> None:
        """Drop staged payloads that a crashed save left behind; fresh ones may still be in flight."""
        cutoff = time.time() - max_age_seconds
        try:
            staged = list((self.directory / INCOMING_DIR).iterdir())
        except OSError:
            return
        for file in staged:
            try:
                if file.stat().st_mtime < cutoff:
                    file.unlink(missing_ok=True)
            except OSError:
                pass

    def list_assets(self, kind=None, cursor=None, limit=DEFAULT_PAGE_SIZE, *,
                    ttl_hours: float) -> dict:
        """List live assets newest first; rows past the current TTL stay hidden until swept."""
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        clauses, params = ["created>?"], [time.time() - float(ttl_hours) * 3600]
        if kind in KIND_MIMES:
            clauses.append("kind=?")
            params.append(kind)
        if cursor:
            try:
                stamp, row_id = json.loads(cursor)
                if not isinstance(stamp, (int, float)) or not isinstance(row_id, str):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("invalid cursor") from None
            clauses.append("(created<? OR (created=? AND id<?))")
            params.extend((stamp, stamp, row_id))
        where = " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM assets{where} ORDER BY created DESC, id DESC LIMIT ?",
                (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [_row_to_asset(row) for row in rows], "has_more": more,
                "next_cursor": json.dumps([rows[-1][6], rows[-1][0]]) if more and rows else None}

    def usage(self, *, ttl_hours: float) -> dict:
        """Count live assets only, matching what reads still serve."""
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, COUNT(*), COALESCE(SUM(size),0) FROM assets WHERE created>? "
                "GROUP BY kind", (time.time() - float(ttl_hours) * 3600,)).fetchall()
        by_kind = {name: {"count": 0, "bytes": 0} for name in KIND_MIMES}
        total = 0
        for kind, count, size in rows:
            by_kind[kind] = {"count": count, "bytes": size}
            total += size
        return {"total_bytes": total, "images": by_kind["image"], "videos": by_kind["video"]}

    def _cleanup_locked(self, *, ttl_hours: float, max_total_bytes: int) -> list:
        """Drop expired and evicted rows under the lock; files are unlinked by the caller."""
        now = time.time()
        expired = [row[0] for row in self._db.execute(
            "SELECT id FROM assets WHERE created<=?", (now - float(ttl_hours) * 3600,)).fetchall()]
        paths = self._delete_rows(expired)
        self._db.execute("DELETE FROM tasks WHERE created<=?", (now - float(ttl_hours) * 3600,))
        while self._total_bytes() > max_total_bytes:
            row = self._db.execute(
                "SELECT id FROM assets ORDER BY created ASC, id ASC LIMIT 1").fetchone()
            if row is None:
                break
            paths.extend(self._delete_rows([row[0]]))
        self._db.commit()
        return paths

    def cleanup(self, *, ttl_hours: float, max_total_bytes: int) -> dict:
        """Drop expired assets and task rows, then evict the oldest to fit the quota."""
        with self._lock:
            before = self._db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            paths = self._cleanup_locked(ttl_hours=ttl_hours, max_total_bytes=max_total_bytes)
            after = self._db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        self._unlink(paths)
        self.sweep_incoming()
        return {"expired_or_evicted": before - after, "removed_files": len(paths)}

    # -- video tasks --------------------------------------------------------------------

    def create_task(self, task_id: str, account_key: str, profile: str, model,
                    public_model=None) -> dict | None:
        with self._lock:
            now = time.time()
            self._db.execute(
                "INSERT OR IGNORE INTO tasks(task_id,account_key,profile,model,created,updated,"
                "status,media_id,error,credit,public_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, account_key, profile, model, now, now, "queued", None, None, None,
                 public_model or model))
            self._db.commit()
            return self.get_task(task_id)
    def get_task(self, task_id: str):
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return _row_to_task(row) if row else None

    def update_task(self, task_id: str, *, status=None, media_id=None, error=UNSET,
                    credit=None) -> None:
        """Update selected fields; pass error=None explicitly to clear a stored failure."""
        with self._lock:
            current = self.get_task(task_id)
            if current is None:
                return
            self._db.execute(
                "UPDATE tasks SET updated=?, status=?, media_id=?, error=?, credit=? WHERE task_id=?",
                (time.time(),
                 status if status is not None else current["status"],
                 media_id if media_id is not None else current["media_id"],
                 current["error"] if error is UNSET else error,
                 credit if credit is not None else current["credit"],
                 task_id))
            self._db.commit()

    def list_tasks(self, status=None, cursor=None, limit=DEFAULT_PAGE_SIZE) -> dict:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        clauses, params = [], []
        if status:
            if status == "active":
                clauses.append("status IN ('queued','in_progress')")
            elif status in ("completed", "failed"):
                clauses.append("status=?")
                params.append(status)
            else:
                raise ValueError("invalid task status")
        if cursor:
            try:
                stamp, row_id = json.loads(cursor)
                if not isinstance(stamp, (int, float)) or not isinstance(row_id, str):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("invalid cursor") from None
            clauses.append("(created<? OR (created=? AND task_id<?))")
            params.extend((stamp, stamp, row_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM tasks{where} ORDER BY created DESC, task_id DESC LIMIT ?",
                (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [_row_to_task(row) for row in rows], "has_more": more,
                "next_cursor": json.dumps([rows[-1][4], rows[-1][0]]) if more and rows else None}

    def task_counts(self) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
        counts = dict(rows)
        return {"active": counts.get("queued", 0) + counts.get("in_progress", 0),
                "failed": counts.get("failed", 0)}
