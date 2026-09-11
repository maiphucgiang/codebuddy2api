"""国际 WorkBuddy 一次性体验领取；账号指纹记账，失败至少退避 24 小时。"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

import httpx

from client_profiles import identity_headers
from credential_io import credential_file_lock
from site_routing import PROFILE_ENDPOINTS, profile_for_headers

RETRY_INTERVAL = 24 * 60 * 60
REQUEST_TIMEOUT = 12.0
_MAX_BYTES = 1024 * 1024
_MAX_ACCOUNTS = 2048  # 满时拒绝新增，不能逐出已经领取的永久记录。
_RESULT_FIELDS = {"ok", "already", "code", "status"}
_RECORD_FIELDS = _RESULT_FIELDS | {"attempted_at", "finished_at"}


def _result(code=None, status=None, *, ok=False, already=False):
    return {"ok": ok, "already": already, "code": code, "status": status}


def _integer(value, lower, upper):
    return value if type(value) is int and lower <= value <= upper else None


def _strict_json(content):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON field")
            value[key] = item
        return value

    def invalid_constant(_):
        raise ValueError("Invalid JSON constant")

    return json.loads(content, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _trial_headers(headers):
    copied = dict(headers)
    profile = profile_for_headers(copied)
    if profile != "intl-work":
        raise ValueError("Trial requires intl-work profile")
    present = {name.lower() for name in copied}
    for name, value in identity_headers(profile).items():
        if name.lower() not in present:
            copied[name] = value
    return copied


def claim_trial(headers: dict) -> dict:
    """仅一次同域 POST；保留传入身份头，不跟随重定向、不重试、不输出响应原文。"""
    headers = _trial_headers(headers)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            response = client.post(PROFILE_ENDPOINTS["intl-work"] + "/billing/ide/trial",
                                   headers=headers, json={})
    except httpx.HTTPError:
        return _result()
    status = response.status_code
    try:
        envelope = _strict_json(response.content)
    except (ValueError, UnicodeError, RecursionError):
        return _result(status=status)
    if not isinstance(envelope, dict):
        return _result(status=status)
    code = _integer(envelope.get("code"), -(2**31), 2**31 - 1)
    result = _result(code, status)
    accepted = 200 <= status < 300 or (status in (400, 409) and code == 14051)
    if not accepted or code not in (0, 14051):
        return result
    data = envelope.get("data")
    if data is not None and not isinstance(data, dict):
        return result
    # 不把 code=0 与显式失败（或非布尔成功标志）的矛盾响应当成成功。
    for layer in (envelope, data or {}):
        for flag in ("success", "ok"):
            if flag in layer and (type(layer[flag]) is not bool or (code == 0 and not layer[flag])):
                return result
    return _result(code, status, ok=code == 0, already=code == 14051)


def _key(key):
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise ValueError("Trial key must be a SHA-256 account fingerprint")
    return key


def _timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
        raise ValueError("Invalid trial timestamp")
    return value


def _safe_result(result):
    if not isinstance(result, dict):
        raise ValueError("Invalid trial result")
    code = _integer(result.get("code"), -(2**31), 2**31 - 1)
    status = _integer(result.get("status"), 100, 599)
    http_ok = status is not None and 200 <= status < 300
    ok = http_ok and code == 0 and result.get("ok") is True and result.get("already") is False
    already = (http_ok or status in (400, 409)) and code == 14051 and result.get("already") is True and result.get("ok") is False
    return _result(code, status, ok=ok, already=already)


def _empty_record():
    return {**_result(), "attempted_at": None, "finished_at": None}


class TrialLedger:
    """锁保护的限量 JSON 账本；读改写均在同一跨进程锁内，不缓存磁盘状态。"""

    def __init__(self, path):
        path = Path(path)
        if not path.name or path.name in (".", ".."):
            raise ValueError("Trial ledger requires a file path")
        # 只规范化父目录，不能 resolve 最终文件而跟随其符号链接。
        self.path = path.parent.resolve() / path.name
        # credential_file_lock 的 name 契约是普通 .info 文件名；不限制账本后缀。
        self._lock_name = "trial-" + hashlib.sha256(self.path.name.encode()).hexdigest() + ".info"

    def _lock(self):
        return credential_file_lock(self.path.parent, self._lock_name)

    def _load(self):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_BYTES:
                raise ValueError("Invalid trial ledger file")
            content = stream.read(_MAX_BYTES + 1)
        if len(content) > _MAX_BYTES:
            raise ValueError("Trial ledger exceeds size limit")
        try:
            document = _strict_json(content)
            if (not isinstance(document, dict) or set(document) != {"version", "accounts"}
                    or type(document["version"]) is not int or document["version"] != 1):
                raise ValueError
            accounts = document["accounts"]
            if not isinstance(accounts, dict) or len(accounts) > _MAX_ACCOUNTS:
                raise ValueError
            for key, record in accounts.items():
                _key(key)
                if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
                    raise ValueError
                _timestamp(record["attempted_at"])
                if record["finished_at"] is not None:
                    _timestamp(record["finished_at"])
                    if record["finished_at"] < record["attempted_at"]:
                        raise ValueError
                fields = {name: record[name] for name in _RESULT_FIELDS}
                if (type(fields["ok"]) is not bool or type(fields["already"]) is not bool
                        or (fields["code"] is not None and type(fields["code"]) is not int)
                        or (fields["status"] is not None and type(fields["status"]) is not int)
                        or fields != _safe_result(fields)
                        or (record["finished_at"] is None and fields != _result())):
                    raise ValueError
            return accounts
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid trial ledger contents") from None

    def _save(self, accounts):
        content = json.dumps({"version": 1, "accounts": accounts},
                             separators=(",", ":"), allow_nan=False).encode()
        if len(content) > _MAX_BYTES:
            raise ValueError("Trial ledger exceeds size limit")
        fd, temporary = tempfile.mkstemp(prefix=".trial-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), 0o600)
                else:
                    os.chmod(temporary, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            # 文件 fsync 不保证 rename 在掉电后留存；准许 POST 前也同步目录项。
            if os.name != "nt":
                directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def begin(self, key, now=None) -> bool:
        """仅返回 True 才可发送；保存/锁/读取错误向调用方传播。now 为 epoch 秒。"""
        key = _key(key)
        with self._lock():
            current = _timestamp(time.time() if now is None else now)
            accounts = self._load()
            previous = accounts.get(key)
            if previous is not None:
                if (previous["ok"] or previous["already"]
                        or current - previous["attempted_at"] < RETRY_INTERVAL):
                    return False
            elif len(accounts) >= _MAX_ACCOUNTS:
                raise ValueError("Trial ledger account limit reached")
            accounts[key] = {**_empty_record(), "attempted_at": current}
            self._save(accounts)
            return True

    def finish(self, key, result, now=None) -> None:
        """无 begin 则拒绝；永久状态不被迟到的失败覆盖；忽略额外/秘密字段。"""
        key, result = _key(key), _safe_result(result)
        with self._lock():
            current = _timestamp(time.time() if now is None else now)
            accounts = self._load()
            previous = accounts.get(key)
            if previous is None:
                raise ValueError("Trial finish requires a persisted attempt")
            if previous["ok"] or previous["already"]:
                return
            accounts[key] = {**result, "attempted_at": previous["attempted_at"],
                             "finished_at": max(current, previous["attempted_at"])}
            self._save(accounts)

    def summary(self, key) -> dict:
        """返回独立的安全快照，不包含指纹、路径、headers 或原始响应。"""
        key = _key(key)
        with self._lock():
            return dict(self._load().get(key, _empty_record()))


def attempt_trial(ledger: TrialLedger, key: str, headers: dict) -> dict:
    """推荐集成入口；先检查 profile，再落盘 attempt，最后一次 POST 和 finish。"""
    headers = _trial_headers(headers)
    if not ledger.begin(key):
        return _result()
    result = claim_trial(headers)
    ledger.finish(key, result)
    return result
