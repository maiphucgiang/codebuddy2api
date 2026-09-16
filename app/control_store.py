"""Versioned control metadata, deliberately separate from credentials and audit."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .settings import validate_settings
from .audit_store import _secure_path, safe_label


class ConflictError(ValueError):
    """The caller's revision no longer matches the persisted revision."""


def _identifier(value, label):
    if safe_label(value) is None or value in (".", ".."):
        raise ValueError(f"{label} 无效")
    return value


def buddy_claim_reserved(record):
    """Only pre-claim reservations may expire; a pending send can outlive its process."""
    return bool(record and (record["claimed"] or record["stage"] not in {"reserved", "agree", "buddy_agree"}))


def validate_model(source, rule, models=None, known_models=(), *, legacy_scopes=False):
    _identifier(source, "模型规则 ID")
    if not isinstance(rule, dict) or set(rule) - {"public_id", "upstream_id", "custom", "enabled", "keep_original", "region", "profile", "credential_ids"}:
        raise ValueError("模型规则字段无效")
    clean = {"public_id": source, "upstream_id": source, "custom": False,
             "enabled": True, "keep_original": False,
             "region": None, "profile": None, "credential_ids": [], **rule}
    _identifier(clean["upstream_id"], "上游模型 ID")
    if type(clean["custom"]) is not bool or (clean["custom"] and clean["keep_original"]):
        raise ValueError("自建模型不能公开内部规则标识")
    _identifier(clean["public_id"], "公开模型 ID")
    if clean["custom"] and clean["public_id"] == source:
        raise ValueError("自建模型必须使用独立的对外 ID")
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
    if ids and (clean["region"] or clean["profile"]) and not legacy_scopes:
        raise ValueError("指定账号与区域/产品范围只能选择一种")
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
            self._db.execute("CREATE TABLE IF NOT EXISTS buddy_bootstrap ("
                             "account_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, attempted_at REAL NOT NULL, "
                             "retry_at REAL NOT NULL, stage TEXT NOT NULL, outcome TEXT NOT NULL, "
                             "consent_source TEXT NOT NULL, agreement_revision TEXT NOT NULL, "
                             "agreed INTEGER NOT NULL DEFAULT 0, claimed INTEGER NOT NULL DEFAULT 0)")
            self._db.execute("CREATE TABLE IF NOT EXISTS buddy_consents ("
                             "account_key TEXT PRIMARY KEY, agreement_revision TEXT NOT NULL, accepted_at REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS buddy_tasks ("
                             "account_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, "
                             "accept_started INTEGER NOT NULL DEFAULT 0, chat_started INTEGER NOT NULL DEFAULT 0, "
                             "completed INTEGER NOT NULL DEFAULT 0, model TEXT, chat_state TEXT NOT NULL DEFAULT 'pending', "
                             "total_tokens INTEGER, updated_at REAL NOT NULL)")
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
        data["settings"] = validate_settings(data["settings"], legacy=True)
        if not isinstance(data["models"], dict) or not isinstance(data["credentials"], dict):
            raise ValueError("管理数据库策略无效")
        for source, rule in data["models"].items():
            validate_model(source, rule, data["models"], legacy_scopes=True)  # Preserve legacy scope intersections.
        for identity, metadata in data["credentials"].items():
            _identifier(identity, "账号指纹")
            if (not isinstance(metadata, dict) or set(metadata) - {"enabled", "label", "auto_checkin", "auto_travel"}
                    or type(metadata.get("enabled")) is not bool
                    or any(key in metadata and type(metadata[key]) is not bool for key in ("auto_checkin", "auto_travel"))):
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

    def delete_model(self, source, revision):
        if type(revision) is not int:
            raise ValueError("revision 必须为整数")
        def change(state):
            if not state["models"].get(source, {}).get("custom"):
                raise ValueError("只能删除自建模型，目录模型请停用")
            del state["models"][source]
        return self._update(revision, change)


    def set_credential(self, account_key, enabled):
        _identifier(account_key, "账号指纹")
        if type(enabled) is not bool:
            raise ValueError("enabled 必须为布尔值")
        return self._update(None, lambda state: state["credentials"].setdefault(account_key, {}).update(enabled=enabled))

    def set_auto_checkin(self, account_key, enabled):
        _identifier(account_key, "账号指纹")
        if type(enabled) is not bool:
            raise ValueError("auto_checkin 必须为布尔值")
        return self._update(None, lambda state: state["credentials"].setdefault(
            account_key, {"enabled": True}).update(auto_checkin=enabled))

    def set_auto_travel(self, account_key, enabled):
        _identifier(account_key, "账号指纹")
        if type(enabled) is not bool:
            raise ValueError("auto_travel 必须为布尔值")
        return self._update(None, lambda state: state["credentials"].setdefault(
            account_key, {"enabled": True}).update(auto_travel=enabled))

    def has_buddy_consent(self, identity, revision):
        _identifier(identity, "账号指纹")
        with self._lock:
            return self._db.execute("SELECT 1 FROM buddy_consents WHERE account_key=? AND agreement_revision=?",
                                    (identity, revision)).fetchone() is not None

    def save_buddy_consent(self, identity, revision):
        _identifier(identity, "账号指纹")
        _identifier(revision, "协议版本")
        with self._lock:
            self._db.execute("INSERT INTO buddy_consents VALUES(?,?,?) ON CONFLICT(account_key) DO UPDATE SET "
                             "agreement_revision=excluded.agreement_revision, accepted_at=excluded.accepted_at",
                             (identity, revision, time.time()))


    def buddy_record(self, identity):
        _identifier(identity, "账号指纹")
        with self._lock:
            cursor = self._db.execute("SELECT * FROM buddy_bootstrap WHERE account_key=?", (identity,))
            row = cursor.fetchone()
            return dict(zip((column[0] for column in cursor.description), row)) if row else None

    def reserve_buddy(self, identity, source, revision, *, retry_seconds, now=None):
        """Reserve first-claim writes across processes without changing configuration revisions."""
        _identifier(identity, "账号指纹")
        _identifier(revision, "协议版本")
        if source not in {"manual", "environment"}:
            raise ValueError("确认来源无效")
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                previous = self.buddy_record(identity)
                if previous and (buddy_claim_reserved(previous) or previous["retry_at"] > now):
                    self._db.execute("COMMIT")
                    return None
                attempt = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO buddy_bootstrap VALUES(?,?,?,?,?,?,?,?,0,0) "
                    "ON CONFLICT(account_key) DO UPDATE SET attempt_id=excluded.attempt_id, "
                    "attempted_at=excluded.attempted_at, retry_at=excluded.retry_at, stage=excluded.stage, "
                    "outcome=excluded.outcome, consent_source=excluded.consent_source, "
                    "agreement_revision=excluded.agreement_revision, agreed=0, claimed=0",
                    (identity, attempt, now, now + retry_seconds, "reserved", "pending", source, revision))
                self._db.execute("COMMIT")
                return attempt
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def buddy_checkpoint(self, identity, attempt, stage, outcome, *, agreed=False, claimed=False):
        _identifier(stage, "首领阶段")
        if outcome not in {"pending", "uncertain", "success"}:
            raise ValueError("首领结果无效")
        with self._lock:
            updated = self._db.execute(
                "UPDATE buddy_bootstrap SET stage=?, outcome=?, agreed=MAX(agreed,?), claimed=MAX(claimed,?) "
                "WHERE account_key=? AND attempt_id=?",
                (stage, outcome, int(agreed), int(claimed), identity, attempt))
            if updated.rowcount != 1:
                raise ValueError("首领预留已变化")

    def buddy_task_record(self, identity):
        _identifier(identity, "账号指纹")
        with self._lock:
            cursor = self._db.execute("SELECT * FROM buddy_tasks WHERE account_key=?", (identity,))
            row = cursor.fetchone()
            return dict(zip((column[0] for column in cursor.description), row)) if row else None

    def reserve_buddy_task(self, identity, operation, *, model=None):
        """Reserve at most one acceptance and one billable conversation per account across restarts."""
        _identifier(identity, "账号指纹")
        if operation not in {"accept", "chat"}:
            raise ValueError("新手任务操作无效")
        if operation == "chat":
            _identifier(model, "模型")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("INSERT OR IGNORE INTO buddy_tasks "
                                 "(account_key,conversation_id,request_id,updated_at) VALUES(?,?,?,?)",
                                 (identity, str(uuid.uuid4()), uuid.uuid4().hex, time.time()))
                previous = self.buddy_task_record(identity)
                if previous[operation + "_started"] or previous["completed"] or (operation == "accept" and previous["chat_started"]):
                    self._db.execute("COMMIT")
                    return None
                if operation == "accept":
                    self._db.execute("UPDATE buddy_tasks SET accept_started=1,updated_at=? WHERE account_key=?",
                                     (time.time(), identity))
                else:
                    self._db.execute("UPDATE buddy_tasks SET chat_started=1,model=?,updated_at=? WHERE account_key=?",
                                     (model, time.time(), identity))
                record = self.buddy_task_record(identity)
                self._db.execute("COMMIT")
                return record
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def buddy_task_checkpoint(self, identity, *, completed=False, chat_state=None, total_tokens=None):
        _identifier(identity, "账号指纹")
        if chat_state not in {None, "success", "uncertain"}:
            raise ValueError("新手对话结果无效")
        if total_tokens is not None and (type(total_tokens) is not int or not 0 <= total_tokens <= 10**9):
            raise ValueError("新手对话用量无效")
        with self._lock:
            self._db.execute("UPDATE buddy_tasks SET completed=MAX(completed,?), "
                             "chat_state=COALESCE(?,chat_state),total_tokens=COALESCE(?,total_tokens),updated_at=? "
                             "WHERE account_key=?",
                             (int(completed), chat_state, total_tokens, time.time(), identity))


    def close(self):
        with self._lock:
            self._db.close()
