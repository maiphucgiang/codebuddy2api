"""Management account persistence, password hashing and startup transitions."""
from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time


USERNAME = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")
PASSWORD_SOURCES = frozenset(("external", "generated", "migrated", "user"))
_HASH = re.compile(r"scrypt\$16384\$8\$1\$([A-Za-z0-9_-]+)\$([A-Za-z0-9_-]+)\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_DUMMY_SALT = secrets.token_bytes(16)
_DUMMY_DIGEST = secrets.token_bytes(32)
_VERIFY_SLOTS = threading.BoundedSemaphore(2)


class AccountError(ValueError):
    """Invalid or unusable management account state."""


class AccountBusy(RuntimeError):
    """Password verification capacity is temporarily exhausted."""


@dataclass(frozen=True)
class Account:
    username: str
    password_source: str
    password: str
    session_epoch: str
    changed_at: float

    @property
    def bootstrap(self) -> bool:
        return self.password_source != "user"

    def document(self) -> dict:
        return {"username": self.username, "password_source": self.password_source,
                "password": self.password, "session_epoch": self.session_epoch,
                "changed_at": self.changed_at}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_username(value: str) -> str:
    if not isinstance(value, str) or USERNAME.fullmatch(value) is None:
        raise AccountError("用户名格式无效")
    return value


def validate_password(value: str, username: str | None = None) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise AccountError("密码长度必须为 8 至 128 个字符")
    if username is not None and value == username:
        raise AccountError("密码不能与用户名相同")
    return value


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = secrets.token_bytes(16) if salt is None else salt
    digest = _derive(password, salt)
    return f"scrypt$16384$8$1${_b64(salt)}${_b64(digest)}"


def _parse_hash(value: str) -> tuple[bytes, bytes]:
    match = _HASH.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise AccountError("管理账户密码哈希无效")
    try:
        salt, expected = _unb64(match.group(1)), _unb64(match.group(2))
    except (ValueError, base64.binascii.Error):
        raise AccountError("管理账户密码哈希无效") from None
    if len(salt) != 16 or len(expected) != 32:
        raise AccountError("管理账户密码哈希无效")
    return salt, expected


def verify(account: Account, username: str, password: str) -> bool:
    """Run exactly one scrypt operation for every verification attempt."""
    if not isinstance(username, str) or not isinstance(password, str):
        username = "" if not isinstance(username, str) else username
        password = "" if not isinstance(password, str) else password
    username_ok = hmac.compare_digest(username.encode("utf-8"), account.username.encode("utf-8"))
    salt, expected = _parse_hash(account.password) if username_ok else (_DUMMY_SALT, _DUMMY_DIGEST)
    if not _VERIFY_SLOTS.acquire(timeout=2):
        raise AccountBusy("登录繁忙，请稍后重试")
    try:
        derived = _derive(password, salt)
        password_ok = hmac.compare_digest(derived, expected)
    finally:
        _VERIFY_SLOTS.release()
    return bool(username_ok and password_ok)


def _validate_document(value: object) -> Account:
    if not isinstance(value, dict) or set(value) != {"username", "password_source", "password",
                                                     "session_epoch", "changed_at"}:
        raise AccountError("管理账户数据无效")
    username = validate_username(value["username"])
    source = value["password_source"]
    if source not in PASSWORD_SOURCES:
        raise AccountError("管理账户密码来源无效")
    password = value["password"]
    _parse_hash(password)
    epoch = value["session_epoch"]
    if not isinstance(epoch, str) or _TOKEN.fullmatch(epoch) is None:
        raise AccountError("管理账户会话 epoch 无效")
    changed_at = value["changed_at"]
    if type(changed_at) not in (int, float) or not math.isfinite(changed_at) or changed_at <= 0:
        raise AccountError("管理账户时间无效")
    return Account(username, source, password, epoch, float(changed_at))


def load(control) -> Account | None:
    with control._lock:
        row = control._db.execute(
            "SELECT value,announced FROM gateway_secrets WHERE name='admin_account'").fetchone()
    if row is None:
        return None
    if row[1] != 1:
        raise AccountError("管理账户公告状态无效")
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AccountError("管理账户数据无效") from None
    return _validate_document(value)


def save(control, account: Account) -> None:
    account = _validate_document(account.document())
    content = json.dumps(account.document(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    control._db.execute(
        "INSERT INTO gateway_secrets(name,value,announced) VALUES('admin_account',?,1) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value, announced=1", (content,))


def new(username: str, password: str, source: str) -> Account:
    validate_username(username)
    if source not in PASSWORD_SOURCES:
        raise AccountError("管理账户密码来源无效")
    if source == "user":
        validate_password(password, username)
    return Account(username, source, hash_password(password), secrets.token_urlsafe(32), time.time())


def with_password(account: Account, password: str, *, source: str = "user") -> Account:
    if source == "user":
        validate_password(password, account.username)
    if source not in PASSWORD_SOURCES:
        raise AccountError("管理账户密码来源无效")
    return replace(account, password_source=source, password=hash_password(password),
                   session_epoch=secrets.token_urlsafe(32), changed_at=time.time())


def with_username(account: Account, username: str) -> Account:
    validate_username(username)
    return replace(account, username=username, session_epoch=secrets.token_urlsafe(32), changed_at=time.time())


def verify_and_change(account: Account, current_password: str, *, username: str | None = None,
                     new_password: str | None = None) -> Account:
    if not verify(account, account.username, current_password):
        raise AccountError("当前管理密码错误")
    if username is None and new_password is None:
        raise AccountError("至少需要修改用户名或密码")
    updated = account
    if username is not None:
        validate_username(username)
        updated = replace(updated, username=username)
    if new_password is not None:
        validate_password(new_password, username if username is not None else account.username)
        updated = replace(updated, password_source="user", password=hash_password(new_password))
    return replace(updated, session_epoch=secrets.token_urlsafe(32), changed_at=time.time())

