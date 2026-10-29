"""Inference API key storage, principals and admission checks."""
from __future__ import annotations

from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import threading
import time

from fastapi import HTTPException

from .observability import client_meta
from .request_context import PATHS
from .settings import parse_networks


_ID = re.compile(r"k_[A-Za-z0-9_-]{12}\Z")
_SECRET = re.compile(r"cb-[0-9a-f]{32}\Z")
_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,64}\Z")
_EXTERNAL_ID = "admin-external"
_ANONYMOUS_ID = "anonymous"
api_principal: ContextVar["Principal | None"] = ContextVar("api_principal", default=None)


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    type: str
    models: tuple[str, ...] | None = None
    rpm: int | None = None

    @property
    def admin(self) -> bool:
        return self.type == "admin"


class ApiKeyError(HTTPException):
    def __init__(self, status: int, message: str, code: str, *, headers=None):
        super().__init__(status_code=status, detail={"error": {
            "message": message, "type": "permission_error" if status == 403 else
            "rate_limit_error" if status == 429 else "auth_error", "code": code}},
                         headers=headers)


def _secret() -> str:
    return "cb-" + secrets.token_hex(16)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _validate_name(value: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None or not value.strip():
        raise ValueError("密钥名称必须为 1 至 64 个字符")
    return value


def _validate_id(value: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("密钥 ID 无效")
    return value


def _json_list(value, label: str, *, max_items: int, allow_none=True) -> tuple[str, ...] | None:
    if value is None and allow_none:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} 格式无效") from None
    if not isinstance(value, (list, tuple)) or len(value) > max_items or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} 格式无效")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} 不能重复")
    return tuple(value)


def _row(row) -> dict:
    keys = ("id", "name", "type", "secret", "secret_hash", "prefix", "enabled",
            "expires_at", "ip_allow", "rpm", "models", "created_at", "last_used_at", "version")
    item = dict(zip(keys, row))
    _validate_id(item["id"])
    _validate_name(item["name"])
    if item["type"] not in ("admin", "standard"):
        raise ValueError("密钥类型无效")
    if item["secret"] is not None and not _SECRET.fullmatch(item["secret"]):
        raise ValueError("密钥明文无效")
    if not isinstance(item["secret_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["secret_hash"]):
        raise ValueError("密钥 hash 无效")
    if not isinstance(item["prefix"], str) or len(item["prefix"]) != 8:
        raise ValueError("密钥前缀无效")
    if item["type"] == "admin" and item["secret"] is not None:
        raise ValueError("管理密钥不能保存明文")
    if type(item["enabled"]) is not int or item["enabled"] not in (0, 1):
        raise ValueError("密钥启用状态无效")
    if item["rpm"] is not None and (type(item["rpm"]) is not int or not 1 <= item["rpm"] <= 100000):
        raise ValueError("RPM 必须为 1 至 100000")
    item["ip_allow"] = _json_list(item["ip_allow"], "IP 白名单", max_items=64)
    item["models"] = _json_list(item["models"], "模型列表", max_items=256)
    return item


class KeyStore:
    def __init__(self, control):
        self.control = control
        self._lock = threading.RLock()
        self._index: dict[str, dict] = {}
        self._rows: dict[str, dict] = {}
        self._loaded_data_version = None
        self._last_data_version_check = 0.0
        self._rpm: dict[str, deque[float]] = {}
        self._used: dict[str, float] = {}
        self._refresh(force=True)

    def _query_rows(self) -> list[dict]:
        rows = self.control._db.execute("SELECT id,name,type,secret,secret_hash,prefix,enabled,expires_at,"
                                        "ip_allow,rpm,models,created_at,last_used_at,version FROM api_keys").fetchall()
        return [_row(row) for row in rows]

    def _refresh(self, *, force=False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_data_version_check < 1.0:
                return
            self._last_data_version_check = now
            with self.control._lock:
                data_version = self.control._db.execute("PRAGMA data_version").fetchone()[0]
                if not force and data_version == self._loaded_data_version:
                    return
                rows = self._query_rows()
            self._rows = {item["id"]: item for item in rows}
            self._index = {item["secret_hash"]: item for item in rows}
            self._loaded_data_version = data_version

    def has_any(self) -> bool:
        self._refresh()
        return bool(self._rows)

    def _write(self, sql, params=()):
        with self.control._lock:
            self.control._db.execute("BEGIN IMMEDIATE")
            try:
                result = self.control._db.execute(sql, params)
                self.control._db.execute("COMMIT")
            except BaseException:
                if self.control._db.in_transaction:
                    self.control._db.execute("ROLLBACK")
                raise
        self._refresh(force=True)
        return result

    def list(self) -> list[dict]:
        self._refresh()
        with self._lock:
            return [dict(item) for item in self._rows.values()]

    def get(self, key_id: str) -> dict | None:
        _validate_id(key_id)
        self._refresh()
        item = self._rows.get(key_id)
        return dict(item) if item else None

    def create(self, *, name: str, kind: str, expires_at=None, ip_allow=None, rpm=None, models=None,
               secret: str | None = None, key_id: str | None = None) -> tuple[dict, str]:
        _validate_name(name)
        if kind not in ("admin", "standard"):
            raise ValueError("密钥类型无效")
        if kind == "admin" and any(value is not None for value in (expires_at, ip_allow, rpm, models)):
            raise ValueError("管理密钥不接受限制字段")
        if rpm is not None and (type(rpm) is not int or not 1 <= rpm <= 100000):
            raise ValueError("RPM 必须为 1 至 100000")
        expires_at = _expires(expires_at)
        ips = tuple(parse_networks(ip_allow, limit=64, label="IP 白名单")) if ip_allow is not None else None
        model_list = _json_list(models, "模型列表", max_items=256)
        secret = _secret() if secret is None else secret
        if not _SECRET.fullmatch(secret):
            raise ValueError("密钥格式无效")
        key_id = key_id or ("k_" + secrets.token_urlsafe(9))
        _validate_id(key_id)
        now = time.time()
        values = (key_id, name, kind, secret if kind == "standard" else None, _hash(secret), secret[:8], 1,
                  float(expires_at) if expires_at is not None else None,
                  json.dumps(list(ips)) if ips is not None else None, rpm,
                  json.dumps(list(model_list)) if model_list is not None else None, now, None, 1)
        try:
            self._write("INSERT INTO api_keys(id,name,type,secret,secret_hash,prefix,enabled,expires_at,"
                        "ip_allow,rpm,models,created_at,last_used_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise ValueError("密钥名称或密钥已存在") from None
            raise
        return self.get(key_id), secret

    def update(self, key_id: str, version: int, values: dict) -> dict:
        item = self.get(key_id)
        if item is None:
            raise KeyError(key_id)
        if type(version) is not int or version != item["version"]:
            raise RuntimeError("version_conflict")
        if item["type"] == "admin" and any(field in values for field in ("expires_at", "ip_allow", "rpm", "models")):
            raise ValueError("管理密钥不接受限制字段")
        allowed = {"name", "enabled", "expires_at", "ip_allow", "rpm", "models"}
        if set(values) - allowed:
            raise ValueError("密钥字段无效")
        clean = dict(item)
        clean.update(values)
        _validate_name(clean["name"])
        if "enabled" in values and type(clean["enabled"]) is not bool:
            raise ValueError("enabled 必须为布尔值")
        clean["enabled"] = int(clean["enabled"])
        if "expires_at" in values:
            clean["expires_at"] = _expires(clean["expires_at"])
        if clean["rpm"] is not None and (type(clean["rpm"]) is not int or not 1 <= clean["rpm"] <= 100000):
            raise ValueError("RPM 必须为 1 至 100000")
        ips = tuple(parse_networks(clean["ip_allow"], limit=64, label="IP 白名单")) if clean["ip_allow"] is not None else None
        models = _json_list(clean["models"], "模型列表", max_items=256)
        next_version = version + 1
        params = (clean["name"], clean["enabled"], clean["expires_at"],
                  json.dumps(list(ips)) if ips is not None else None, clean["rpm"],
                  json.dumps(list(models)) if models is not None else None, next_version, key_id, version)
        with self.control._lock:
            self.control._db.execute("BEGIN IMMEDIATE")
            try:
                changed = self.control._db.execute(
                    "UPDATE api_keys SET name=?,enabled=?,expires_at=?,ip_allow=?,rpm=?,models=?,version=? "
                    "WHERE id=? AND version=?", params).rowcount
                if changed != 1:
                    self.control._db.execute("ROLLBACK")
                    raise RuntimeError("version_conflict")
                self.control._db.execute("COMMIT")
            except BaseException:
                if self.control._db.in_transaction:
                    self.control._db.execute("ROLLBACK")
                raise
        self._refresh(force=True)
        return self.get(key_id)

    def rotate(self, key_id: str, version: int) -> tuple[dict, str]:
        item = self.get(key_id)
        if item is None:
            raise KeyError(key_id)
        if type(version) is not int or version != item["version"]:
            raise RuntimeError("version_conflict")
        secret = _secret()
        with self.control._lock:
            self.control._db.execute("BEGIN IMMEDIATE")
            try:
                changed = self.control._db.execute(
                    "UPDATE api_keys SET secret=?,secret_hash=?,prefix=?,version=? WHERE id=? AND version=?",
                    (secret if item["type"] == "standard" else None, _hash(secret), secret[:8], version + 1, key_id, version)).rowcount
                if changed != 1:
                    self.control._db.execute("ROLLBACK")
                    raise RuntimeError("version_conflict")
                self.control._db.execute("COMMIT")
            except BaseException:
                if self.control._db.in_transaction:
                    self.control._db.execute("ROLLBACK")
                raise
        self._refresh(force=True)
        return self.get(key_id), secret

    def reveal(self, key_id: str) -> str:
        item = self.get(key_id)
        if item is None:
            raise KeyError(key_id)
        if item["type"] != "standard" or not item["secret"]:
            raise ValueError("管理密钥只在创建或轮换时显示")
        return item["secret"]

    def delete(self, key_id: str, version: int) -> None:
        item = self.get(key_id)
        if item is None:
            raise KeyError(key_id)
        if type(version) is not int or version != item["version"]:
            raise RuntimeError("version_conflict")
        with self.control._lock:
            self.control._db.execute("BEGIN IMMEDIATE")
            try:
                changed = self.control._db.execute("DELETE FROM api_keys WHERE id=? AND version=?",
                                                   (key_id, version)).rowcount
                if changed != 1:
                    self.control._db.execute("ROLLBACK")
                    raise RuntimeError("version_conflict")
                self.control._db.execute("COMMIT")
            except BaseException:
                if self.control._db.in_transaction:
                    self.control._db.execute("ROLLBACK")
                raise
        self._refresh(force=True)

    def _consume_rpm(self, item: dict) -> None:
        rpm = item["rpm"]
        if rpm is None:
            return
        now = time.monotonic()
        with self._lock:
            queue = self._rpm.setdefault(item["id"], deque())
            cutoff = now - 60.0
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if len(queue) >= rpm:
                retry = max(1, int((queue[0] + 60.0 - now) + 0.999999))
                raise ApiKeyError(429, "请求频率超过密钥限制，请稍后重试", "rpm_exceeded",
                                   headers={"Retry-After": str(retry)})
            queue.append(now)

    def authenticate(self, scope, config, *, count_rpm=True) -> Principal:
        self._refresh()
        headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                   for key, value in scope.get("headers") or []}
        authorization = headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not token:
            token = headers.get("x-api-key", "")
        external = config.get("api_key") or ""
        has_any = bool(external) or bool(self._rows)
        if not has_any:
            if config.get("allow_open_noauth"):
                return Principal(_ANONYMOUS_ID, "匿名", "admin")
            raise ApiKeyError(401, "尚未创建 API key，请登录 WebUI 创建", "api_key_required")
        if external and hmac.compare_digest(token.encode(), external.encode()):
            return Principal(_EXTERNAL_ID, "外部管理密钥", "admin")
        item = self._index.get(_hash(token)) if token else None
        if item is None:
            raise ApiKeyError(401, "API key 无效", "invalid_api_key")
        if external and item["type"] == "admin":
            raise ApiKeyError(401, "API key 无效", "invalid_api_key")
        if not item["enabled"]:
            raise ApiKeyError(401, "API key 已停用", "api_key_disabled")
        if item["expires_at"] is not None and float(item["expires_at"]) <= time.time():
            raise ApiKeyError(401, "API key 已过期", "api_key_expired")
        if item["ip_allow"]:
            ip, _ = client_meta(scope, config.get("trusted_proxies"))
            try:
                address = ipaddress.ip_address(ip)
            except (ValueError, TypeError):
                address = None
            if address is None or not any(address in ipaddress.ip_network(entry, strict=False) for entry in item["ip_allow"]):
                raise ApiKeyError(403, "客户端 IP 不在密钥白名单内", "ip_not_allowed")
        if count_rpm and scope.get("method") == "POST" and scope.get("path") in PATHS:
            self._consume_rpm(item)
        with self._lock:
            self._used[item["id"]] = time.time()
        return Principal(item["id"], item["name"], item["type"], item["models"], item["rpm"])

    def flush_used(self):
        with self._lock:
            pending, self._used = self._used, {}
        if not pending:
            return
        try:
            with self.control.state.transaction():
                self.control._db.executemany(
                    "UPDATE api_keys SET last_used_at=MAX(COALESCE(last_used_at,0),?) WHERE id=?",
                    [(stamp, key_id) for key_id, stamp in pending.items()])
            self._refresh(force=True)
        except BaseException:
            with self._lock:
                for key_id, stamp in pending.items():
                    self._used[key_id] = max(stamp, self._used.get(key_id, 0))
            raise


def _expires(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value <= time.time():
        raise ValueError("有效期必须晚于当前时间")
    return float(value)

