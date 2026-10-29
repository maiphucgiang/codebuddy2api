"""Private SQLite state and one-time imports of legacy runtime snapshots."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import warnings

MAX_BYTES = 16 * 1024 * 1024
LEGACY_FILES = {
    "sessions": "admin-sessions.json",
    "cooldowns": "credential-cooldowns.json",
    "model_blocks": "model-site-blocks.json",
    "credits": "credits-ledger.json",
    "trial": "trial-ledger.json",
    "catalog": "model-catalog.json",
    "catalog_probe": "model-catalog-probe.json",
    "usage": "usage-snapshots.json",
}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate state field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("non-finite state value")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)

def load_document(path, store, name, limit=MAX_BYTES):
    """Read SQLite at runtime; retain bounded legacy readers for import validation."""
    if store is not None:
        return store.get(name)
    try:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.read(limit + 1)
        return strict_json(raw) if len(raw) <= limit else None
    except (OSError, ValueError, RecursionError):
        return None



def _check_document(name, value):
    if name not in LEGACY_FILES or not isinstance(value, dict):
        raise ValueError("Invalid runtime state")
    # Never promote an accidentally supplied credential file into SQLite.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(str(key).lower().replace("_", "") in {"accesstoken", "refreshtoken", "idtoken"} for key in item):
                raise ValueError("Credential material is not runtime state")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    field, versions = {
        "sessions": ("sessions", (1,)), "cooldowns": ("accounts", (1,)),
        "model_blocks": ("blocks", (1,)), "credits": ("creds", (1,)),
        "trial": ("accounts", (1,)), "catalog": ("groups", (1, 2)),
        "catalog_probe": ("profiles", (1,)),
        "usage": ("accounts", (1,)),
    }[name]
    if type(value.get("version")) is not int or value["version"] not in versions or not isinstance(value.get(field), dict):
        raise ValueError("Unsupported runtime state schema")
    if name != "sessions" and set(value) != {"version", field}:
        raise ValueError("Unexpected runtime state fields")
    if name == "sessions":
        from .admin_auth import _TOKEN, _FINGERPRINT, MAX_PERSISTED_SESSIONS
        if (set(value) != {"version", "fingerprint", "sessions"}
                or not isinstance(value.get("fingerprint"), str) or not _FINGERPRINT.fullmatch(value["fingerprint"])
                or len(value[field]) > MAX_PERSISTED_SESSIONS):
            raise ValueError("Invalid session snapshot")
        for sid, row in value[field].items():
            if (not isinstance(sid, str) or not _TOKEN.fullmatch(sid) or not isinstance(row, dict)
                    or set(row) not in ({"csrf_token", "expires"}, {"csrf_token", "expires", "remember"})
                    or not isinstance(row.get("csrf_token"), str) or not _TOKEN.fullmatch(row["csrf_token"])
                    or type(row.get("expires")) not in (int, float)
                    or not math.isfinite(row["expires"]) or not 0 < row["expires"] < 1e11
                    or "remember" in row and type(row["remember"]) is not bool):
                raise ValueError("Invalid persisted session")
    elif name == "trial":
        from .trial_rewards import _key, _timestamp, _safe_result, _result, _RECORD_FIELDS, _RESULT_FIELDS, _MAX_ACCOUNTS
        if len(value[field]) > _MAX_ACCOUNTS:
            raise ValueError("Too many trial accounts")
        for key, row in value[field].items():
            _key(key)
            if not isinstance(row, dict) or set(row) != _RECORD_FIELDS:
                raise ValueError("Invalid trial history")
            _timestamp(row["attempted_at"])
            if row["finished_at"] is not None:
                _timestamp(row["finished_at"])
                if row["finished_at"] < row["attempted_at"]:
                    raise ValueError("Invalid trial deadline")
            result = {key: row[key] for key in _RESULT_FIELDS}
            if (type(result["ok"]) is not bool or type(result["already"]) is not bool
                    or (result["code"] is not None and type(result["code"]) is not int)
                    or (result["status"] is not None and type(result["status"]) is not int)
                    or result != _safe_result(result)
                    or (row["finished_at"] is None and result != _result())):
                raise ValueError("Invalid trial result")
    elif name == "usage":
        from .usage_snapshots import UsageSnapshots, _FIELDS, MAX_ACCOUNTS
        if len(value[field]) > MAX_ACCOUNTS:
            raise ValueError("Too many usage snapshots")
        for path, row in value[field].items():
            if (not isinstance(path, str) or not path or len(path) > 4096 or not isinstance(row, dict)
                    or set(row) != _FIELDS or UsageSnapshots._valid_row(row) is None):
                raise ValueError("Invalid usage snapshot")
    elif name == "credits":
        for row in value[field].values():
            if (not isinstance(row, dict) or set(row) - {"identity", "checkin", "credits", "error", "travel"}
                    or not isinstance(row.get("checkin", {}), dict) or not isinstance(row.get("credits", {}), dict)):
                raise ValueError("Invalid credit history")
    elif name == "cooldowns":
        from .credential_cooldowns import _FIELDS, _valid_identity, _valid_token, _number, MAX_ACCOUNTS, MAX_MODELS
        if len(value[field]) > MAX_ACCOUNTS:
            raise ValueError("Too many cooldown accounts")
        for identity, row in value[field].items():
            if (not _valid_identity(identity) or not isinstance(row, dict) or set(row) != _FIELDS
                    or not _valid_token(row["profile"]) or not isinstance(row["models"], dict)
                    or len(row["models"]) > MAX_MODELS
                    or _number(row["fail_until"]) is None or _number(row["failed_at"]) is None
                    or any(not _valid_token(model) or _number(until) is None for model, until in row["models"].items())):
                raise ValueError("Invalid cooldown snapshot")
    elif name == "model_blocks":
        for models in value[field].values():
            if not isinstance(models, dict):
                raise ValueError("Invalid model blocks")
            for row in models.values():
                if (not isinstance(row, dict) or type(row.get("until")) not in (int, float)
                        or not math.isfinite(row["until"]) or type(row.get("hits", 1)) is not int):
                    raise ValueError("Invalid model backoff")
    elif name == "catalog_probe":
        from .catalog_probe import valid_row
        if len(value[field]) > 8 or not all(valid_row(row) for row in value[field].values()):
            raise ValueError("Invalid catalog probe state")
    elif name == "catalog":
        for row in value[field].values():
            if (not isinstance(row, dict) or not isinstance(row.get("models"), list)
                    or not all(isinstance(model, dict) for model in row["models"])
                    or type(row.get("fetched_at")) not in (int, float)
                    or not math.isfinite(row["fetched_at"])):
                raise ValueError("Invalid model catalog")


class StateStore:
    """Share the control connection and lock without exposing secrets in public snapshots."""

    def __init__(self, control):
        self.control = control
        self.path = control.path
        self._db = control._db
        self._lock = control._lock

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _get(self, name):
        if name not in LEGACY_FILES:
            raise ValueError("Unknown state namespace")
        row = self._db.execute("SELECT payload FROM runtime_state WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        value = strict_json(row[0])
        _check_document(name, value)
        return value

    def get(self, name):
        with self._lock:
            return self._get(name)

    def _put(self, name, value):
        content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        _check_document(name, value)
        if len(content.encode()) > MAX_BYTES:
            raise ValueError("Runtime state exceeds size limit")
        self._db.execute("INSERT INTO runtime_state(name,payload) VALUES(?,?) "
                         "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (name, content))

    def put(self, name, value):
        with self.transaction():
            self._put(name, value)

    def put_cache(self, name, value):
        """Cache failures stay visible without replacing the upstream inference outcome."""
        if name not in ("catalog", "catalog_probe", "model_blocks"):
            raise ValueError("Not a rebuildable cache")
        try:
            self.put(name, value)
            return True
        except (OSError, ValueError, sqlite3.Error) as error:
            warnings.warn(f"SQLite {name} cache write failed ({type(error).__name__}); keeping memory state",
                          RuntimeWarning, stacklevel=2)
            return False


    def delete(self, name):
        if name not in LEGACY_FILES:
            raise ValueError("Unknown state namespace")
        with self.transaction():
            self._db.execute("DELETE FROM runtime_state WHERE name=?", (name,))


    def migrate(self, root):
        """Import each legacy source once, including absence, before any maintenance starts."""
        root = Path(root)
        with self.transaction():
            for name, filename in LEGACY_FILES.items():
                if self._db.execute("SELECT 1 FROM state_imports WHERE name=?", (name,)).fetchone():
                    continue
                path = root / filename
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    raw = None
                else:
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > MAX_BYTES:
                        raise ValueError(f"Cannot migrate {filename}: invalid file")
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)):
                            raise ValueError(f"Cannot migrate {filename}: changed file")
                        raw = stream.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError(f"Cannot migrate {filename}: size limit")
                if raw is not None:
                    try:
                        value = strict_json(raw)
                        _check_document(name, value)
                        if self._get(name) is None:
                            self._put(name, value)
                    except (ValueError, TypeError, KeyError, RecursionError):
                        raise ValueError(f"Cannot migrate {filename}: invalid state; restore a valid backup") from None
                self._db.execute("INSERT INTO state_imports(name,imported,migrated_at) VALUES(?,?,?)",
                                 (name, int(raw is not None), time.time()))

    def default_key(self, *, create=False):
        with self.transaction():
            row = self._db.execute("SELECT value,announced FROM gateway_secrets WHERE name='default_api_key'").fetchone()
            if row is not None:
                if not re.fullmatch(r"cb-[0-9a-f]{32}", row[0]) or row[1] not in (0, 1):
                    raise ValueError("Invalid saved API key; restore the control database")
                return row[0], not bool(row[1])
            if not create:
                return None, False
            key = "cb-" + secrets.token_hex(16)
            self._db.execute("INSERT INTO gateway_secrets(name,value,announced) VALUES('default_api_key',?,0)", (key,))
            return key, True

    @contextmanager
    def claim_announcement(self, key):
        """Serialize disclosure and roll back the marker if terminal output or commit fails."""
        with self.transaction():
            claimed = self._db.execute("UPDATE gateway_secrets SET announced=1 WHERE name='default_api_key' "
                                       "AND value=? AND announced=0", (key,)).rowcount == 1
            yield claimed
