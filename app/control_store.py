"""Versioned control metadata, deliberately separate from credentials and audit."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import threading

from .settings import validate_settings
from .audit_store import _secure_path, safe_label


class ConflictError(ValueError):
    """The caller's revision no longer matches the persisted revision."""


def _identifier(value, label):
    if safe_label(value) is None or value in (".", ".."):
        raise ValueError(f"{label} 无效")
    return value


def validate_model(source, rule, models=None, known_models=()):
    _identifier(source, "上游模型 ID")
    if not isinstance(rule, dict) or set(rule) - {"public_id", "enabled", "keep_original", "region", "profile", "credential_ids"}:
        raise ValueError("模型规则字段无效")
    clean = {"public_id": source, "enabled": True, "keep_original": False,
             "region": None, "profile": None, "credential_ids": [], **rule}
    _identifier(clean["public_id"], "公开模型 ID")
    if type(clean["enabled"]) is not bool or type(clean["keep_original"]) is not bool:
        raise ValueError("enabled/keep_original 必须为布尔值")
    if clean["region"] not in (None, "cn", "intl") or clean["profile"] not in (None, "cn-cli", "cn-work", "intl-cli", "intl-work"):
        raise ValueError("区域或产品无效")
    if clean["region"] and clean["profile"] and not clean["profile"].startswith(clean["region"] + "-"):
        raise ValueError("区域与产品冲突")
    ids = clean["credential_ids"]
    if not isinstance(ids, list) or len(ids) > 1000:
        raise ValueError("credential_ids 必须是账号指纹列表")
    for identity in ids:
        _identifier(identity, "账号指纹")
    if len(set(ids)) != len(ids):
        raise ValueError("账号指纹重复")
    all_rules = {**(models or {}), source: clean}
    real_ids = set(known_models) | set(all_rules) | {"auto"}
    aliases = {}
    for upstream, current in all_rules.items():
        public = current["public_id"]
        if public != upstream and public in real_ids:
            raise ValueError("公开 ID 与真实模型冲突或构成别名链")
        if public in aliases and aliases[public] != upstream:
            raise ValueError("公开模型 ID 重复")
        aliases[public] = upstream
    return copy.deepcopy(clean)


class ControlStore:
    SCHEMA_VERSION = 1

    def __init__(self, path):
        path = Path(path)
        existed = path.exists()
        path = _secure_path(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=1)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("BEGIN IMMEDIATE")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            tables = self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and not tables and not existed:
                self._db.execute("CREATE TABLE control (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL)")
                self._db.execute("INSERT INTO control VALUES (1,0,?)", (json.dumps({"settings": {}, "models": {}, "credentials": {}}),))
                self._db.execute("PRAGMA user_version=1")
            elif version != self.SCHEMA_VERSION:
                raise ValueError("管理数据库 schema 不受支持或已有库为空，未执行初始化")
            self._snapshot = self._load()
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            raise

    def _load(self):
        row = self._db.execute("SELECT revision,payload FROM control WHERE id=1").fetchone()
        if row is None:
            raise ValueError("管理数据库状态缺失")
        data = json.loads(row[1])
        if not isinstance(data, dict) or set(data) != {"settings", "models", "credentials"}:
            raise ValueError("管理数据库状态无效")
        validate_settings(data["settings"])
        if not isinstance(data["models"], dict) or not isinstance(data["credentials"], dict):
            raise ValueError("管理数据库策略无效")
        for source, rule in data["models"].items():
            validate_model(source, rule, data["models"])
        for identity, metadata in data["credentials"].items():
            _identifier(identity, "账号指纹")
            if not isinstance(metadata, dict) or set(metadata) - {"enabled", "label"} or type(metadata.get("enabled")) is not bool:
                raise ValueError("管理数据库凭证元数据无效")
        return {"revision": row[0], **data}

    def snapshot(self):
        """Return a detached snapshot; callers cannot mutate the published state."""
        # Published dictionaries are never mutated; SQLite writer locks cannot stall routing.
        return copy.deepcopy(self._snapshot)

    def _update(self, revision, change):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                state = self._load()
                if revision is not None and (type(revision) is not int or revision != state["revision"]):
                    raise ConflictError("配置已更新，请刷新后重试")
                change(state)
                state["revision"] += 1
                payload = {key: state[key] for key in ("settings", "models", "credentials")}
                self._db.execute("UPDATE control SET revision=?,payload=? WHERE id=1", (state["revision"], json.dumps(payload, ensure_ascii=False, allow_nan=False)))
                self._db.execute("COMMIT")
                self._snapshot = state
                return copy.deepcopy(state)
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def update_settings(self, values, revision):
        if type(revision) is not int:
            raise ValueError("revision 必须为整数")
        clean = validate_settings(values)
        return self._update(revision, lambda state: state["settings"].update(clean))

    def update_model(self, source, rule, revision, known_models=()):
        if type(revision) is not int:
            raise ValueError("revision 必须为整数")
        def change(state):
            state["models"][source] = validate_model(source, rule, state["models"], known_models)
        return self._update(revision, change)

    def set_credential(self, account_key, enabled):
        _identifier(account_key, "账号指纹")
        if type(enabled) is not bool:
            raise ValueError("enabled 必须为布尔值")
        return self._update(None, lambda state: state["credentials"].setdefault(account_key, {}).update(enabled=enabled))

    def close(self):
        with self._lock:
            self._db.close()
