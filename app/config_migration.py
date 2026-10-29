"""Offline `.env` -> control store migration with target identity checks.

The service is the only writer of `control.sqlite3`; this module therefore refuses to
touch a database that is still leased by a running instance, and it verifies that the
target really is this project's control store before opening it read-write.
"""

from __future__ import annotations

from pathlib import Path
import io
import json
import os
import sqlite3
import stat
import time
from urllib.parse import quote

from dotenv import parser as dotenv_parser

from .control_store import ControlStore
from .settings import SCHEMA, validate_settings

MAX_ENV_BYTES = 256 * 1024
LEASE_STALE_S = 90              # Three heartbeat intervals (see converter.LEASE_INTERVAL_S)
SQLITE_HEADER = b"SQLite format 3\x00"
CONTROL_TABLES = ("control", "runtime_state", "gateway_secrets", "api_keys")
SUPPORTED_VERSION = 3


class MigrationError(ValueError):
    """A user-facing refusal that leaves the target untouched."""


def read_env_file(path) -> dict:
    """Parse one env file without mutating `os.environ`; mirror startup's file checks."""
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise MigrationError(f"env 文件不存在: {path}") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise MigrationError(f"env 文件必须是普通文件，且不支持符号链接: {path}")
    if metadata.st_size > MAX_ENV_BYTES:
        raise MigrationError(f"env 文件超过 {MAX_ENV_BYTES // 1024} KiB: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)):
            raise MigrationError(f"env 文件在读取期间发生变化: {path}")
        content = stream.read(MAX_ENV_BYTES + 1)
    if len(content) > MAX_ENV_BYTES:
        raise MigrationError(f"env 文件超过 {MAX_ENV_BYTES // 1024} KiB: {path}")
    text = content.decode("utf-8-sig")
    values = {}
    for binding in dotenv_parser.parse_stream(io.StringIO(text)):
        if binding.error:
            raise MigrationError(f"env 文件第 {binding.original.line} 行语法错误")
        values[binding.key] = binding.value
    return values


def resolve_targets(*, env_file=None, no_env=False, data_dir=None, db=None,
                    environ=None, cwd=None) -> dict:
    """Resolve the env file and control database, honouring the file's own data directory."""
    environ = os.environ if environ is None else environ
    cwd = Path.cwd() if cwd is None else Path(cwd)
    if no_env and env_file:
        raise MigrationError("--no-env 与 --env-file 不能同时使用")
    if db and data_dir:
        raise MigrationError("--db 与 --data-dir 不能同时使用")
    env_path = None
    env_values = {}
    if not no_env:
        env_path = Path(env_file).expanduser().resolve() if env_file else cwd / ".env"
        if env_file or env_path.exists():
            env_values = read_env_file(env_path)
        else:
            env_path = None  # A missing default file simply means "no .env".
    if db:
        db_path = Path(db).expanduser().resolve()
    else:
        base = data_dir or env_values.get("CODEBUDDY_AUTH_DIR") or environ.get("CODEBUDDY_AUTH_DIR")
        if base:
            base_path = Path(base).expanduser()
            db_path = (base_path if base_path.is_absolute() else (cwd / base_path).resolve()) / "control.sqlite3"
        else:
            db_path = Path(__file__).resolve().parents[1] / "auth" / "control.sqlite3"
    return {"env_path": env_path, "env_values": env_values, "db_path": db_path.resolve()}


def inspect_database(path) -> dict:
    """Read-only identity check; never creates or upgrades the file."""
    path = Path(path)
    if not path.exists():
        raise MigrationError(
            f"控制库不存在: {path}；请用 --data-dir 或 --db 指定，容器内通常是 --data-dir /data/auth")
    if not path.is_file():
        raise MigrationError(f"控制库必须是普通文件: {path}")
    if path.name == "logs.sqlite3":
        raise MigrationError("目标是日志库 logs.sqlite3；迁移需要 control.sqlite3")
    with path.open("rb") as stream:
        header = stream.read(16)
    if not header:
        raise MigrationError(f"文件为空，不是 codebuddy2api 控制库: {path}")
    if header != SQLITE_HEADER:
        raise MigrationError(f"不是 SQLite 数据库: {path}")
    uri = f"file:{quote(str(path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1, isolation_level=None)
    except sqlite3.Error as error:
        raise MigrationError(f"无法只读打开控制库: {error}") from None
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            raise MigrationError("目标不是 codebuddy2api 控制库（user_version=0，且未包含本项目数据表）")
        if version != SUPPORTED_VERSION:
            raise MigrationError(f"控制库 schema={version}，需要先用当前版本服务启动一次完成升级")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in CONTROL_TABLES if name not in tables]
        if missing:
            raise MigrationError(f"目标缺少必需的数据表 {', '.join(missing)}，不是 codebuddy2api 控制库")
        row = conn.execute("SELECT revision,payload FROM control WHERE id=1").fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise MigrationError("控制库状态缺失或 revision 非法")
        try:
            payload = json.loads(row[1])
        except (ValueError, UnicodeError):
            raise MigrationError("控制库状态不是有效 JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
            raise MigrationError("控制库状态缺少 settings，不是有效的控制库")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MigrationError("控制库 quick_check 失败；请先恢复备份")
        lease = None
        if "service_lease" in tables:
            found = conn.execute("SELECT pid,started_at,heartbeat_at FROM service_lease WHERE id=1").fetchone()
            if found:
                lease = {"pid": found[0], "started_at": found[1], "heartbeat_at": found[2]}
        return {"path": path, "version": version, "revision": row[0], "settings": payload["settings"],
                "accounts": len(payload.get("credentials") or {}), "lease": lease,
                "mtime": path.stat().st_mtime}
    except sqlite3.Error as error:
        raise MigrationError(f"读取控制库失败: {error}") from None
    finally:
        conn.close()


def lease_is_fresh(lease, *, now=None) -> bool:
    """A heartbeat inside the stale window means an instance is still running."""
    now = time.time() if now is None else now
    return bool(lease) and now - float(lease.get("heartbeat_at") or 0) < LEASE_STALE_S


def _coerce(spec, raw):
    """Convert an env string to the schema type before validation runs."""
    if spec["type"] == "boolean":
        lowered = str(raw).strip().lower()
        if lowered not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
            raise ValueError("布尔值无效")
        return lowered in ("1", "true", "yes", "on")
    if spec["type"] == "integer":
        return int(str(raw).strip())
    if spec["type"] == "number":
        return float(str(raw).strip())
    return raw


def collect_runtime_values(env_values, environ=None) -> dict:
    """Effective values for runtime-scoped keys; process env wins, exactly like startup."""
    environ = os.environ if environ is None else environ
    collected = {}
    for key, spec in SCHEMA.items():
        name = spec.get("env")
        if not name or spec.get("sensitive") or spec.get("hidden") or spec.get("env_scope") != "runtime":
            continue
        raw = environ.get(name, env_values.get(name))
        if raw is None:
            continue
        try:
            collected[key] = validate_settings({key: _coerce(spec, raw)})[key]
        except (ValueError, TypeError) as error:
            raise MigrationError(f"{name}: {error}") from None
    return collected


def plan(values, settings, *, keys=None) -> dict:
    """Split effective env values into pending changes and already-equal entries."""
    if keys:
        unknown = sorted(set(keys) - set(SCHEMA))
        if unknown:
            raise MigrationError(f"未知配置项: {', '.join(unknown)}")
        values = {key: value for key, value in values.items() if key in keys}
    changes = {key: value for key, value in values.items() if settings.get(key) != value}
    unchanged = {key: value for key, value in values.items() if settings.get(key) == value}
    return {"changes": changes, "unchanged": unchanged}


def _probe_write_lock(path) -> None:
    """Fail fast when another process holds the write lock at this moment."""
    try:
        conn = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    except sqlite3.Error as error:
        raise MigrationError(f"无法打开控制库: {error}") from None
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            raise MigrationError("控制库正在被写入，请先停止服务后重试") from None
    finally:
        conn.close()


def migrate(*, env_file=None, no_env=False, data_dir=None, db=None, dry_run=False,
            force=False, keys=None, language=None, environ=None, cwd=None) -> dict:
    """Copy runtime-scoped env values into the control store and mute the notice."""
    targets = resolve_targets(env_file=env_file, no_env=no_env, data_dir=data_dir, db=db,
                              environ=environ, cwd=cwd)
    info = inspect_database(targets["db_path"])
    lease = info["lease"]
    report = {"env_path": targets["env_path"], "db_path": targets["db_path"],
              "version": info["version"], "revision": info["revision"],
              "accounts": info["accounts"], "mtime": info["mtime"],
              "lease": lease, "lease_fresh": lease_is_fresh(lease),
              "dry_run": dry_run, "written": False, "muted": False}
    if report["lease_fresh"] and not force:
        raise MigrationError(
            f"检测到运行中的实例（pid {lease.get('pid')}，心跳 {int(time.time() - lease['heartbeat_at'])} 秒前）；"
            "请先停止服务，或确认后使用 --force")
    values = collect_runtime_values(targets["env_values"], environ)
    report.update(plan(values, info["settings"], keys=keys))
    language_update = bool(language) and info["settings"].get("language") != language
    report["language"] = language if language_update else None
    if dry_run:
        return report
    if not report["changes"] and not language_update:
        report["muted"] = bool(info["settings"].get("config_notice_muted"))
        return report
    _probe_write_lock(targets["db_path"])
    store = ControlStore(targets["db_path"])
    try:
        snapshot = store.snapshot()
        if snapshot["revision"] != info["revision"]:
            raise MigrationError("控制库在检查后被其它进程修改，请重新执行")
        updates = {**report["changes"], "config_notice_muted": True}
        if language_update:
            updates["language"] = language
        store.update_settings(updates, snapshot["revision"])
        report["written"] = True
        report["muted"] = True
        report["revision"] = snapshot["revision"] + 1
    finally:
        store.close()
    return report
