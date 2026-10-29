"""Versioned control metadata, deliberately separate from credentials and audit."""
from __future__ import annotations

import copy
import json
import math
from datetime import date
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .settings import validate_settings
from .audit_store import _secure_path, safe_label
from .growth_tasks import KNOWN_TASK_CODES as GROWTH_TASK_CODES


class ConflictError(ValueError):
    """The caller's revision no longer matches the persisted revision."""


def _identifier(value, label):
    if safe_label(value) is None or value in (".", ".."):
        raise ValueError(f"{label} 无效")
    return value


def _day(value, label="日期"):
    """Accept only a YYYY-MM-DD local calendar day; never a timestamp or free text.

    The shape check alone admits 2026-02-31 and 2026-99-99, and a day that never
    happened would become a distinct key that the once-per-day guard could be
    satisfied against instead of the real day.
    """
    if (not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-"
            or not value.replace("-", "").isdigit()):
        raise ValueError(f"{label} 无效")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} 无效") from None
    return value


def buddy_claim_reserved(record):
    """Only pre-claim reservations may expire; a pending send can outlive its process."""
    return bool(record and (record["claimed"] or record["stage"] not in {"reserved", "agree", "buddy_agree"}))


def validate_model(source, rule, models=None, known_models=(), *, legacy_scopes=False):
    _identifier(source, "模型规则 ID")
    if not isinstance(rule, dict) or set(rule) - {"public_id", "upstream_id", "routes", "custom", "enabled", "keep_original", "region", "profile", "credential_ids", "credits_override"}:
        raise ValueError("模型规则字段无效")
    clean = {"public_id": source, "upstream_id": source, "custom": False,
             "enabled": True, "keep_original": False,
             "region": None, "profile": None, "credential_ids": [], **rule}
    _identifier(clean["upstream_id"], "上游模型 ID")
    routes = clean.get("routes")
    if routes is not None:
        if not isinstance(routes, list) or not routes or len(routes) > 100:
            raise ValueError("routes 必须是 1–100 条路由列表")
        normalized_routes = []
        seen_routes = set()
        weighted = False
        for route in routes:
            if not isinstance(route, dict) or set(route) - {"upstream_id", "weight", "enabled", "source"}:
                raise ValueError("路由字段无效")
            route_id = route.get("upstream_id")
            _identifier(route_id, "路由上游模型 ID")
            if route_id in seen_routes:
                raise ValueError("路由上游模型重复")
            seen_routes.add(route_id)
            enabled = route.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError("路由 enabled 必须为布尔值")
            source_kind = route.get("source", "manual")
            if source_kind not in ("builtin", "cloud", "manual"):
                raise ValueError("路由 source 无效")
            weight = route.get("weight")
            if weight is not None:
                if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
                        or not math.isfinite(weight) or weight < 0 or weight > 1000:
                    raise ValueError("路由 weight 必须是 0–1000 的有限数字")
                weight = float(weight)
                weighted = True
            normalized_routes.append({"upstream_id": route_id, "weight": weight,
                                      "enabled": enabled, "source": source_kind})
        if weighted and not any((route["weight"] or 0) > 0 for route in normalized_routes):
            raise ValueError("加权路由至少需要一条正权重路线")
        clean["routes"] = normalized_routes
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
    # Manual rates sit on the catalog model itself; an empty mapping is not stored at all.
    override = clean.pop("credits_override", None)
    override = {} if override is None else override
    if (not isinstance(override, dict)
            or set(override) - {"cn-cli", "cn-work", "intl-cli", "intl-work"}
            or any(isinstance(rate, bool) or not isinstance(rate, (int, float))
                   or not math.isfinite(rate) or not 0 <= rate <= 1000 for rate in override.values())):
        raise ValueError("credits_override 必须是产品到 0–1000 倍率的映射")
    if override and clean["custom"]:
        raise ValueError("自建模型不能手填倍率，请在其上游模型的规则上设置")
    if override:
        clean["credits_override"] = {profile: float(rate) for profile, rate in sorted(override.items())}
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
    SCHEMA_VERSION = 3

    def __init__(self, path):
        path = Path(path)
        existed = path.exists()
        path = _secure_path(path)
        self.path = path
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
            elif version not in (1, 2, self.SCHEMA_VERSION):
                raise ValueError("管理数据库 schema 不受支持或已有库为空，未执行初始化")
            self._snapshot = self._load()
            self._db.execute("CREATE TABLE IF NOT EXISTS buddy_bootstrap ("
                             "account_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, attempted_at REAL NOT NULL, "
                             "retry_at REAL NOT NULL, stage TEXT NOT NULL, outcome TEXT NOT NULL, "
                             "consent_source TEXT NOT NULL, agreement_revision TEXT NOT NULL, "
                             "agreed INTEGER NOT NULL DEFAULT 0, claimed INTEGER NOT NULL DEFAULT 0)")
            self._db.execute("CREATE TABLE IF NOT EXISTS travel_writes ("
                             "account_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, operation TEXT NOT NULL, "
                             "phase TEXT NOT NULL, location_id INTEGER, reserved_at REAL NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0)")
            self._db.execute("CREATE TABLE IF NOT EXISTS buddy_consents ("
                             "account_key TEXT PRIMARY KEY, agreement_revision TEXT NOT NULL, accepted_at REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS buddy_tasks ("
                             "account_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, "
                             "accept_started INTEGER NOT NULL DEFAULT 0, chat_started INTEGER NOT NULL DEFAULT 0, "
                             "completed INTEGER NOT NULL DEFAULT 0, model TEXT, chat_state TEXT NOT NULL DEFAULT 'pending', "
                             "total_tokens INTEGER, updated_at REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS daily_chats ("
                             "account_key TEXT NOT NULL, day TEXT NOT NULL, attempt_id TEXT NOT NULL, "
                             "phase TEXT NOT NULL, conversation_id TEXT, sandbox_status TEXT, "
                             "usage_before REAL, usage_after REAL, acp_usage REAL, "
                             "reserved_at REAL NOT NULL, confirmed_at REAL, updated_at REAL NOT NULL, "
                             "PRIMARY KEY (account_key, day))")
            # One row per manual rate probe: the charge upstream reported for a known-size request.
            self._db.execute("CREATE TABLE IF NOT EXISTS rate_probes ("
                             "probed_at REAL NOT NULL, profile TEXT NOT NULL, model TEXT NOT NULL, "
                             "credit REAL NOT NULL, input_tokens INTEGER NOT NULL, "
                             "cache_read_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS rate_probes_time ON rate_probes(probed_at)")
            # Additive: the calibrated rate of a charged probe, NULL when it could not be derived.
            if "rate" not in [row[1] for row in self._db.execute("PRAGMA table_info(rate_probes)")]:
                self._db.execute("ALTER TABLE rate_probes ADD COLUMN rate REAL")
            self._db.execute("CREATE TABLE IF NOT EXISTS runtime_state (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS state_imports (name TEXT PRIMARY KEY, imported INTEGER NOT NULL, migrated_at REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS gateway_secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL, announced INTEGER NOT NULL DEFAULT 0 CHECK(announced IN (0,1)))")
            self._db.execute("CREATE TABLE IF NOT EXISTS api_keys ("
                             "id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
                             "type TEXT NOT NULL CHECK(type IN ('admin','standard')), "
                             "secret TEXT, secret_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL, "
                             "enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), "
                             "expires_at REAL, ip_allow TEXT, rpm INTEGER, models TEXT, "
                             "created_at REAL NOT NULL, last_used_at REAL, "
                             "version INTEGER NOT NULL DEFAULT 1)")
            self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS api_keys_single_admin "
                             "ON api_keys(type) WHERE type='admin'")
            self._validate_api_keys_schema()
            # Running-instance lease; heartbeat writes bypass the settings revision on purpose.
            self._db.execute("CREATE TABLE IF NOT EXISTS service_lease (id INTEGER PRIMARY KEY CHECK(id=1), "
                             "pid INTEGER NOT NULL, started_at REAL NOT NULL, heartbeat_at REAL NOT NULL)")
            self._db.execute("PRAGMA user_version=3")
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            raise
        from .state_store import StateStore
        self.state = StateStore(self)

    def _validate_api_keys_schema(self):
        expected = {"id", "name", "type", "secret", "secret_hash", "prefix", "enabled",
                    "expires_at", "ip_allow", "rpm", "models", "created_at", "last_used_at", "version"}
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(api_keys)")}
        if columns != expected:
            raise ValueError("管理数据库 api_keys 表结构不匹配，请从备份恢复")
        indexes = {row[1]: row for row in self._db.execute("PRAGMA index_list(api_keys)")}
        index = indexes.get("api_keys_single_admin")
        if index is None or not index[2]:
            raise ValueError("管理数据库 api_keys 索引结构不匹配，请从备份恢复")

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
            growth = metadata.get("growth_auto")
            growth = metadata.get("growth_auto")
            if (not isinstance(metadata, dict)
                    or set(metadata) - {"enabled", "label", "auto_checkin", "auto_travel",
                                        "auto_daily_chat", "auto_growth", "growth_auto",
                                        "auto_disabled", "auto_reason", "auto_body", "auto_at"}
                    or type(metadata.get("enabled")) is not bool
                    or metadata.get("auto_disabled") is not None and type(metadata["auto_disabled"]) is not bool
                    or metadata.get("auto_reason") is not None and not isinstance(metadata["auto_reason"], str)
                    or metadata.get("auto_body") is not None and not isinstance(metadata["auto_body"], str)
                    or metadata.get("auto_at") is not None and not isinstance(metadata["auto_at"], (int, float))
                    or metadata.get("auto_growth") is not None and type(metadata["auto_growth"]) is not bool
                    or any(key in metadata and type(metadata[key]) is not bool for key in
                           ("auto_checkin", "auto_travel", "auto_daily_chat"))
                    or growth is not None and (
                        not isinstance(growth, dict)
                        or set(growth) - set(GROWTH_TASK_CODES)
                        or any(type(value) is not bool for value in growth.values()))):
                raise ValueError("管理数据库凭证元数据无效")
        return {"revision": row[0], **data}

    def snapshot(self):
        """Return a detached snapshot; callers cannot mutate the published state."""
        # Published dictionaries are never mutated; SQLite writer locks cannot stall routing.
        return copy.deepcopy(self._snapshot)

    @property
    def revision(self):
        """Current revision without copying the snapshot, for cache and policy views."""
        return self._snapshot["revision"]

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
        """Manual toggle: any operator action clears the automatic-disable provenance."""
        _identifier(account_key, "账号指纹")
        if type(enabled) is not bool:
            raise ValueError("enabled 必须为布尔值")

        def change(state):
            record = state["credentials"].setdefault(account_key, {})
            record["enabled"] = enabled
            # Any manual toggle clears automatic restriction provenance. The reason/body/time
            # fields are evidence for the disabled state and must not survive an appeal release.
            for key in ("auto_disabled", "auto_reason", "auto_body", "auto_at"):
                record.pop(key, None)
        return self._update(None, change)

    def disable_credential_auto(self, account_key, reason="", body=None, at=None):
        """Record an automatic disable with its upstream evidence so it survives restarts."""
        _identifier(account_key, "账号指纹")

        def change(state):
            record = state["credentials"].setdefault(account_key, {})
            record.update(enabled=False, auto_disabled=True)
            for key, value in (("auto_reason", str(reason or "")[:256] or None),
                               ("auto_body", str(body)[:8192] if body else None),
                               ("auto_at", at)):
                if value is None:
                    record.pop(key, None)
                else:
                    record[key] = value
        return self._update(None, change)

    def unbind_credential(self, account_key):
        """Drop an account key from every rule, returning {source: old_ids} for rollback."""
        _identifier(account_key, "账号指纹")
        if not any(account_key in (rule.get("credential_ids") or [])
                   for rule in self.snapshot()["models"].values()):
            return {}  # Nothing references the identity; keep the revision stable.
        affected = {}
        def change(state):
            for source, rule in state["models"].items():
                ids = rule.get("credential_ids") or []
                if account_key in ids:
                    affected[source] = list(ids)
                    rule["credential_ids"] = [identity for identity in ids if identity != account_key]
        self._update(None, change)
        return affected

    def restore_bindings(self, account_key, affected):
        """Re-add the identity where a rule still matches its post-unbind state.

        Concurrent edits that changed a rule's binding list are preserved.
        """
        _identifier(account_key, "账号指纹")
        pending = {source: ids for source, ids in dict(affected or {}).items() if isinstance(ids, list)}
        def restorable(rule, ids):
            if rule is None:
                return False  # A rule deleted meanwhile stays deleted.
            current = rule.get("credential_ids") or []
            return current == [identity for identity in ids if identity != account_key]
        snapshot = self.snapshot()
        if not any(restorable(snapshot["models"].get(source), ids) for source, ids in pending.items()):
            return snapshot  # Nothing to restore; concurrent edits win.
        def change(state):
            for source, ids in pending.items():
                if restorable(state["models"].get(source), ids):
                    state["models"][source]["credential_ids"] = list(ids)
        return self._update(None, change)

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

    def set_auto_daily_chat(self, account_key, enabled):
        _identifier(account_key, "账号指纹")
        if type(enabled) is not bool:
            raise ValueError("auto_daily_chat 必须为布尔值")
        return self._update(None, lambda state: state["credentials"].setdefault(
            account_key, {"enabled": True}).update(auto_daily_chat=enabled))

    def set_auto_growth(self, account_key, enabled):
        _identifier(account_key, "账号指纹")
        if type(enabled) is not bool:
            raise ValueError("auto_growth 必须为布尔值")
        return self._update(None, lambda state: state["credentials"].setdefault(
            account_key, {"enabled": True}).update(auto_growth=enabled))

    def set_growth_auto(self, account_key, mapping):
        """Replace the per-task automation switches; unknown codes are rejected, not dropped."""
        _identifier(account_key, "账号指纹")
        if not isinstance(mapping, dict) or set(mapping) - set(GROWTH_TASK_CODES) \
                or any(type(value) is not bool for value in mapping.values()):
            raise ValueError("growth_auto 必须是 task_code 到布尔的映射")
        return self._update(None, lambda state: state["credentials"].setdefault(
            account_key, {"enabled": True}).update(growth_auto=mapping))

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

    def travel_write_record(self, identity):
        _identifier(identity, "账号指纹")
        with self._lock:
            cursor = self._db.execute("SELECT * FROM travel_writes WHERE account_key=?", (identity,))
            row = cursor.fetchone()
            return dict(zip((column[0] for column in cursor.description), row)) if row else None

    def reserve_travel_write(self, identity, operation, location_id=None, *, expected_attempt=None):
        """Reserve one unresolved travel write per account without expiry-based replay."""
        _identifier(identity, "账号指纹")
        if operation not in {"claim", "depart"}:
            raise ValueError("旅行操作无效")
        if operation == "depart" and (type(location_id) is not int or not 0 < location_id <= 2**31 - 1):
            raise ValueError("派遣地点无效")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                previous = self.travel_write_record(identity)
                current_attempt = previous["attempt_id"] if previous else None
                if current_attempt != expected_attempt or previous and previous["phase"] not in {"cancelled", "reconciled"}:
                    self._db.execute("COMMIT")
                    return None
                attempt = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO travel_writes VALUES(?,?,?,'reserved',?,?,0) "
                    "ON CONFLICT(account_key) DO UPDATE SET attempt_id=excluded.attempt_id,operation=excluded.operation, "
                    "phase='reserved',location_id=excluded.location_id,reserved_at=excluded.reserved_at,confirmed=0",
                    (identity, attempt, operation, location_id, time.time()))
                self._db.execute("COMMIT")
                return attempt
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def transition_travel_write(self, identity, attempt, phase):
        """Late receipts cannot reopen or replace an already reconciled reservation."""
        _identifier(identity, "账号指纹")
        expected = {"sent": ("reserved",), "confirmed": ("sent", "confirmed", "reconciled"),
                    "cancelled": ("reserved", "sent", "cancelled"),
                    "reconciled": ("reserved", "sent", "confirmed", "reconciled")}.get(phase)
        if expected is None:
            raise ValueError("旅行写入阶段无效")
        with self._lock:
            updated = self._db.execute(
                "UPDATE travel_writes SET phase=CASE WHEN phase='reconciled' AND ?='confirmed' THEN phase ELSE ? END, "
                "confirmed=MAX(confirmed,?) WHERE account_key=? AND attempt_id=? AND phase IN ("
                + ",".join("?" for _ in expected) + ")",
                (phase, phase, int(phase in {"confirmed", "reconciled"}), identity, attempt, *expected))
            if updated.rowcount != 1:
                raise ValueError("旅行写入预留已变化")


    def buddy_task_record(self, identity):
        _identifier(identity, "账号指纹")
        with self._lock:
            cursor = self._db.execute("SELECT * FROM buddy_tasks WHERE account_key=?", (identity,))
            row = cursor.fetchone()
            return dict(zip((column[0] for column in cursor.description), row)) if row else None

    def reserve_buddy_task(self, identity, operation, *, model=None):
        """Reserve one conversation with a fresh owner token for each unsent attempt."""
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
                    self._db.execute("UPDATE buddy_tasks SET chat_started=1,request_id=?,model=?,"
                                     "chat_state='pending',total_tokens=NULL,updated_at=? WHERE account_key=?",
                                     (uuid.uuid4().hex, model, time.time(), identity))
                record = self.buddy_task_record(identity)
                self._db.execute("COMMIT")
                return record
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def release_buddy_task(self, identity, request_id):
        """Release only the caller's known-unsent reservation, never a recorded outcome."""
        _identifier(identity, "账号指纹")
        _identifier(request_id, "请求 ID")
        with self._lock:
            updated = self._db.execute(
                "UPDATE buddy_tasks SET chat_started=0,model=NULL,updated_at=? "
                "WHERE account_key=? AND request_id=? AND chat_started=1 AND completed=0 "
                "AND chat_state='pending' AND total_tokens IS NULL",
                (time.time(), identity, request_id))
            return updated.rowcount == 1


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


    # -- International daily-activity turns ------------------------------------
    # One row per account per local day. A turn may consume credits on the account
    # and is never replayed automatically, so only an unsent reservation is
    # cancellable: a sent-but-unconfirmed one stays pending until the next day.

    _DAILY_CHAT_PHASES = ("reserved", "sent", "confirmed", "cancelled", "reconciled")

    _DAILY_CHAT_TRANSITIONS = {"sent": ("reserved",),
                               "confirmed": ("sent", "confirmed", "reconciled"),
                               "cancelled": ("reserved", "sent", "cancelled"),
                               "reconciled": ("reserved", "sent", "confirmed", "reconciled")}
    _DAILY_CHAT_UPDATE_SQL = {
        "sent": "UPDATE daily_chats SET phase=CASE WHEN phase='reconciled' AND ?='confirmed' THEN phase ELSE ? END, "
                "confirmed_at=CASE WHEN ? IN ('confirmed','reconciled') THEN COALESCE(confirmed_at,?) ELSE confirmed_at END, "
                "updated_at=? WHERE account_key=? AND day=? AND attempt_id=? AND phase IN (?)",
        "confirmed": "UPDATE daily_chats SET phase=CASE WHEN phase='reconciled' AND ?='confirmed' THEN phase ELSE ? END, "
                     "confirmed_at=CASE WHEN ? IN ('confirmed','reconciled') THEN COALESCE(confirmed_at,?) ELSE confirmed_at END, "
                     "updated_at=? WHERE account_key=? AND day=? AND attempt_id=? AND phase IN (?,?,?)",
        "cancelled": "UPDATE daily_chats SET phase=CASE WHEN phase='reconciled' AND ?='confirmed' THEN phase ELSE ? END, "
                     "confirmed_at=CASE WHEN ? IN ('confirmed','reconciled') THEN COALESCE(confirmed_at,?) ELSE confirmed_at END, "
                     "updated_at=? WHERE account_key=? AND day=? AND attempt_id=? AND phase IN (?,?,?)",
        "reconciled": "UPDATE daily_chats SET phase=CASE WHEN phase='reconciled' AND ?='confirmed' THEN phase ELSE ? END, "
                      "confirmed_at=CASE WHEN ? IN ('confirmed','reconciled') THEN COALESCE(confirmed_at,?) ELSE confirmed_at END, "
                      "updated_at=? WHERE account_key=? AND day=? AND attempt_id=? AND phase IN (?,?,?,?)",
    }
    def daily_chat_record(self, identity, day):
        _identifier(identity, "账号指纹")
        _day(day)
        with self._lock:
            cursor = self._db.execute("SELECT * FROM daily_chats WHERE account_key=? AND day=?",
                                      (identity, day))
            row = cursor.fetchone()
            return dict(zip((column[0] for column in cursor.description), row)) if row else None

    def reserve_daily_chat(self, identity, day):
        """Reserve the single daily turn; a completed or already-sent day is never reopened.

        A ``reserved`` row is retryable because it means nothing was written upstream
        yet (the caller only marks ``sent`` once the turn can really run), so a crash
        or a failed sandbox provisioning does not cost the day.
        """
        _identifier(identity, "账号指纹")
        _day(day)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                previous = self.daily_chat_record(identity, day)
                if previous and previous["phase"] not in {"reserved", "cancelled", "reconciled"}:
                    self._db.execute("COMMIT")
                    return None
                attempt = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO daily_chats (account_key,day,attempt_id,phase,reserved_at,updated_at) "
                    "VALUES(?,?,?,'reserved',?,?) ON CONFLICT(account_key,day) DO UPDATE SET "
                    "attempt_id=excluded.attempt_id,phase='reserved',conversation_id=NULL,"
                    "sandbox_status=NULL,usage_before=NULL,usage_after=NULL,acp_usage=NULL,"
                    "reserved_at=excluded.reserved_at,confirmed_at=NULL,updated_at=excluded.updated_at",
                    (identity, day, attempt, time.time(), time.time()))
                self._db.execute("COMMIT")
                return {"attempt_id": attempt}
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def transition_daily_chat(self, identity, day, attempt, phase):
        """Late receipts cannot reopen or replace an already reconciled reservation."""
        _identifier(identity, "账号指纹")
        _day(day)
        _identifier(attempt, "尝试 ID")
        if phase not in self._DAILY_CHAT_PHASES:
            raise ValueError("打卡阶段无效")
        expected = self._DAILY_CHAT_TRANSITIONS[phase]
        with self._lock:
            updated = self._db.execute(
                self._DAILY_CHAT_UPDATE_SQL[phase],
                (phase, phase, phase, time.time(), time.time(), identity, day, attempt, *expected))
            if updated.rowcount != 1:
                raise ValueError("打卡预留已变化")

    def daily_chat_checkpoint(self, identity, day, *, sandbox_status=None, usage_before=None,
                              usage_after=None, acp_usage=None, conversation_id=None):
        _identifier(identity, "账号指纹")
        _day(day)
        if sandbox_status is not None:
            _identifier(sandbox_status, "会话状态")
        if conversation_id is not None:
            _identifier(conversation_id, "会话 ID")
        for name, value in (("usage_before", usage_before), ("usage_after", usage_after),
                            ("acp_usage", acp_usage)):
            if value is not None and (type(value) not in (int, float) or not 0 <= value <= 10**9):
                raise ValueError(f"{name} 无效")
        with self._lock:
            self._db.execute(
                "UPDATE daily_chats SET sandbox_status=COALESCE(?,sandbox_status), "
                "conversation_id=COALESCE(?,conversation_id), usage_before=COALESCE(?,usage_before), "
                "usage_after=COALESCE(?,usage_after), acp_usage=COALESCE(?,acp_usage), updated_at=? "
                "WHERE account_key=? AND day=?",
                (sandbox_status, conversation_id, usage_before, usage_after, acp_usage,
                 time.time(), identity, day))

    def daily_chat_done(self, identity, day):
        record = self.daily_chat_record(identity, day)
        return bool(record and record["phase"] in {"confirmed", "reconciled"})

    def prune_daily_chats(self, keep_days=30):
        """Drop old rows; records are diagnostic, never a source of truth for routing."""
        keep_days = int(keep_days)
        if not 1 <= keep_days <= 3650:
            raise ValueError("保留天数无效")
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep_days * 86400))
        with self._lock:
            self._db.execute("DELETE FROM daily_chats WHERE day<?", (cutoff,))


    def record_rate_probe(self, profile, model, sample, rate=None):
        """Persist one probe outcome; only the billing figures are kept, never the request."""
        if profile not in ("cn-cli", "cn-work", "intl-cli", "intl-work"):
            raise ValueError("产品无效")
        _identifier(model, "模型 ID")
        values = [sample.get(key) for key in ("credit", "input_tokens", "cache_read_tokens", "output_tokens")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               or value < 0 for value in values):
            raise ValueError("探测结果无效")
        if rate is not None and (isinstance(rate, bool) or not isinstance(rate, (int, float))
                                 or not math.isfinite(rate) or not 0 <= rate <= 1000):
            raise ValueError("探测倍率无效")
        with self._lock:
            self._db.execute("INSERT INTO rate_probes(probed_at,profile,model,credit,input_tokens,"
                             "cache_read_tokens,output_tokens,rate) VALUES(?,?,?,?,?,?,?,?)",
                             (time.time(), profile, model, float(values[0]),
                              *(int(value) for value in values[1:]), rate))

    def rate_probes(self, since):
        """Probe rows in the sample shape the rate measurement consumes, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT probed_at,profile,model,credit,input_tokens,cache_read_tokens,output_tokens,rate "
                "FROM rate_probes WHERE probed_at>=? ORDER BY probed_at", (since,)).fetchall()
        keys = ("started_at", "profile", "model", "credit", "input_tokens", "cache_read_tokens",
                "output_tokens", "rate")
        return [dict(zip(keys, row)) for row in rows]

    def prune_rate_probes(self, keep_days=30):
        cutoff = time.time() - int(keep_days) * 86400
        with self._lock:
            self._db.execute("DELETE FROM rate_probes WHERE probed_at<?", (cutoff,))

    def claim_service_lease(self) -> None:
        """Mark this process as the running instance so offline tools can refuse to write."""
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO service_lease (id, pid, started_at, heartbeat_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET pid=excluded.pid, started_at=excluded.started_at, "
                "heartbeat_at=excluded.heartbeat_at", (os.getpid(), now, now))

    def heartbeat_service_lease(self) -> None:
        """Refresh only our own lease row; a superseded process never extends another's."""
        with self._lock:
            self._db.execute("UPDATE service_lease SET heartbeat_at=? WHERE id=1 AND pid=?",
                             (time.time(), os.getpid()))

    def release_service_lease(self) -> None:
        """Drop a cleanly stopped process's lease so an offline migration need not wait it out."""
        with self._lock:
            self._db.execute("DELETE FROM service_lease WHERE id=1 AND pid=?", (os.getpid(),))

    def service_lease(self) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT pid, started_at, heartbeat_at FROM service_lease WHERE id=1").fetchone()
        return {"pid": row[0], "started_at": row[1], "heartbeat_at": row[2]} if row else None

    def close(self):
        with self._lock:
            self._db.close()
