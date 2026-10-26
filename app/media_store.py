"""Local media assets and video task metadata in one sqlite-backed store.

Files live under ``media/images|videos/<shard>/<id>.<ext>`` and are committed
atomically; metadata stays out of ControlStore snapshots and backups. Writing
first prunes expired objects and then evicts the oldest until the total fits.
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
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

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


def security_headers(meta: dict) -> dict:
    """Serving headers shared by the public and admin read routes."""
    return {"ETag": f'"{meta["sha256"]}"', "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff"}


def _row_to_asset(row) -> dict:
    keys = ("id", "kind", "mime", "size", "sha256", "model", "created", "expires")
    return dict(zip(keys, row))


def _row_to_task(row) -> dict:
    keys = ("task_id", "account_key", "profile", "model", "created", "updated",
            "status", "media_id", "error", "credit")
    return dict(zip(keys, row))


class MediaStore:
    """Own media/<kind>/<shard>/ files plus the assets and tasks tables."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.directory = self.root / "media"
        self.path = self.root / DB_NAME
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS assets(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, model TEXT, created REAL NOT NULL, expires REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS assets_created ON assets(created);
            CREATE TABLE IF NOT EXISTS tasks(
                task_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, profile TEXT NOT NULL,
                model TEXT, created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
                media_id TEXT, error TEXT, credit REAL);
            CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created);
        """)
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

    def save_asset(self, kind: str, data: bytes, *, model=None, ttl_hours: float,
                   max_total_bytes: int) -> dict:
        """Sniff, prune and evict, then commit one asset atomically."""
        if kind not in KIND_MIMES or not isinstance(data, bytes) or not data:
            raise ValueError("媒体类型或内容无效")
        identified = sniff(data)
        if identified is None or identified[1] not in KIND_MIMES[kind]:
            raise ValueError("文件头不在允许的媒体类型白名单内")
        ext, mime = identified
        with self._lock:
            now = time.time()
            self.cleanup(ttl_hours=ttl_hours, max_total_bytes=max(0, max_total_bytes - len(data)))
            for _ in range(4):  # A collision keeps the existing object untouched.
                asset_id = self._new_id(kind)
                target = self._asset_path(asset_id, ext)
                if not target.exists():
                    break
            else:
                raise RuntimeError("无法分配新的媒体 ID")
            target.parent.mkdir(parents=True, exist_ok=True)
            pending = target.with_name(".incoming-" + target.name)
            pending.write_bytes(data)
            try:
                os.replace(pending, target)
            except OSError:
                pending.unlink(missing_ok=True)
                raise
            try:
                self._db.execute(
                    "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)",
                    (asset_id, kind, mime, len(data), hashlib.sha256(data).hexdigest(),
                     model, now, now + ttl_hours * 3600))
                self._db.commit()
            except sqlite3.Error:
                target.unlink(missing_ok=True)
                raise
            row = self._db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            return _row_to_asset(row)

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

    def delete_asset(self, asset_id: str) -> bool:
        """Remove one file and row; bound tasks keep their status with media_id cleared."""
        with self._lock:
            row = self._db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                return False
            meta = _row_to_asset(row)
            self._db.execute("UPDATE tasks SET media_id=NULL WHERE media_id=?", (asset_id,))
            self._db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            self._db.commit()
            file = self.asset_file(meta)
            if file is not None:
                file.unlink(missing_ok=True)
            return True

    def list_assets(self, kind=None, cursor=None, limit=DEFAULT_PAGE_SIZE) -> dict:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        clauses, params = [], []
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM assets{where} ORDER BY created DESC, id DESC LIMIT ?",
                (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [_row_to_asset(row) for row in rows], "has_more": more,
                "next_cursor": json.dumps([rows[-1][6], rows[-1][0]]) if more and rows else None}

    def usage(self) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, COUNT(*), COALESCE(SUM(size),0) FROM assets GROUP BY kind").fetchall()
        by_kind = {name: {"count": 0, "bytes": 0} for name in KIND_MIMES}
        total = 0
        for kind, count, size in rows:
            by_kind[kind] = {"count": count, "bytes": size}
            total += size
        return {"total_bytes": total, "images": by_kind["image"], "videos": by_kind["video"]}

    def cleanup(self, *, ttl_hours: float, max_total_bytes: int) -> dict:
        """Drop expired assets and task rows, then evict the oldest to fit the quota."""
        removed = {"expired": 0, "evicted": 0}
        now = time.time()
        with self._lock:
            expired = self._db.execute(
                "SELECT * FROM assets WHERE expires<=?", (now,)).fetchall()
            for row in expired:
                self.delete_asset(_row_to_asset(row)["id"])
            removed["expired"] = len(expired)
            self._db.execute("DELETE FROM tasks WHERE created<=?", (now - ttl_hours * 3600,))
            self._db.commit()
            while self._total_bytes() > max_total_bytes:
                row = self._db.execute(
                    "SELECT * FROM assets ORDER BY created ASC, id ASC LIMIT 1").fetchone()
                if row is None:
                    break
                self.delete_asset(_row_to_asset(row)["id"])
                removed["evicted"] += 1
            self._db.commit()
        return removed

    # -- video tasks --------------------------------------------------------------------

    def create_task(self, task_id: str, account_key: str, profile: str, model) -> None:
        with self._lock:
            now = time.time()
            self._db.execute("INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (task_id, account_key, profile, model, now, now, "queued", None, None, None))
            self._db.commit()

    def get_task(self, task_id: str):
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return _row_to_task(row) if row else None

    def update_task(self, task_id: str, *, status=None, media_id=None, error=None, credit=None) -> None:
        with self._lock:
            current = self.get_task(task_id)
            if current is None:
                return
            self._db.execute(
                "UPDATE tasks SET updated=?, status=?, media_id=?, error=?, credit=? WHERE task_id=?",
                (time.time(),
                 status if status is not None else current["status"],
                 media_id if media_id is not None else current["media_id"],
                 error if error is not None else current["error"],
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
