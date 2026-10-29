"""Resolve startup environment and initialize the management account."""
from __future__ import annotations

import io
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
import stat

from dotenv import load_dotenv
from dotenv.parser import parse_stream
import uvicorn

from .settings import SCHEMA
from . import admin_account


def load_startup_env(path=None):
    """Load only the explicit working-directory file, without overriding process variables."""
    path = Path.cwd() / ".env" if path is None else Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return set()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 256 * 1024:
        raise ValueError(".env 必须是普通文件且不超过 256 KiB")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)):
            raise ValueError(".env 文件在读取期间发生变化")
        content = stream.read(256 * 1024 + 1)
    if len(content) > 256 * 1024:
        raise ValueError(".env 文件过大")
    text = content.decode("utf-8-sig")
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise ValueError(f".env 第 {binding.original.line} 行语法错误")
    previous = set(os.environ)
    load_dotenv(stream=io.StringIO(text), override=False)
    return set(os.environ) - previous


def terminal_stream():
    """Open the controlling terminal, never a redirected standard stream or regular file."""
    device = "CONOUT$" if os.name == "nt" else "/dev/tty"
    fd = os.open(device, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0))
    try:
        if not stat.S_ISCHR(os.fstat(fd).st_mode) or not os.isatty(fd):
            raise OSError("No interactive console")
        return os.fdopen(fd, "w", encoding="utf-8", buffering=1)
    except BaseException:
        os.close(fd)
        raise


def allows_open_noauth():
    """Honor the existing explicit unsafe opt-in without overriding a configured key."""
    return os.environ.get("CODEBUDDY2API_ALLOW_OPEN_NOAUTH", "").lower() in ("1", "true", "yes")


def _external_key(config, args, dotenv_keys=()):
    sources = config["settings_sources"]
    for name, spec in SCHEMA.items():
        if sources.get(name) == "environment" and spec["env"] in dotenv_keys:
            sources[name] = "dotenv"
    source = sources.get("api_key")
    value = config.get("api_key") if source in ("cli", "environment", "dotenv") else ""
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        config["api_key"] = ""
        args.api_key = ""
        if source in ("cli", "environment", "dotenv"):
            variable = "CODEBUDDY2API_KEY" if source != "cli" else "--api-key"
            print(f"[auth] {variable} 为空，按未设置处理", file=sys.stderr)
        sources["api_key"] = "default"
        return ""
    config["api_key"] = args.api_key = value
    return value


def _announce_admin_password(password, *, username="admin"):
    message = f"\n管理账户：{username}\n初始管理密码：{password}\n请登录后尽快修改密码。\n"
    try:
        with terminal_stream() as terminal:
            terminal.write(message)
            terminal.flush()
        return
    except OSError:
        pass
    try:
        sys.stdout.write(message)
        sys.stdout.flush()
    except BaseException:
        raise ValueError("无法输出初始管理密码") from None


def _account_row(control):
    row = control._db.execute(
        "SELECT value,announced FROM gateway_secrets WHERE name='admin_account'").fetchone()
    if row is None:
        return None
    if row[1] != 1:
        raise ValueError("管理账户公告状态无效")
    try:
        return admin_account._validate_document(json.loads(row[0]))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ValueError("管理账户数据无效") from error


def _legacy_key_row(control):
    row = control._db.execute(
        "SELECT value,announced FROM gateway_secrets WHERE name='default_api_key'").fetchone()
    if row is None:
        return None
    if not isinstance(row[0], str) or not re.fullmatch(r"cb-[0-9a-f]{32}", row[0]) or row[1] not in (0, 1):
        raise ValueError("Invalid saved API key; restore the control database")
    return row[0], bool(row[1])


def resolve_startup_auth(config, args, dotenv_keys=()):
    """Initialize auth atomically; disclose generated passwords before committing them."""
    external = _external_key(config, args, dotenv_keys)
    control = config["control_store"]
    with control.state.transaction():
        account = admin_account.load(control)
        marker = control._db.execute(
            "SELECT 1 FROM state_imports WHERE name='admin_auth_v1'").fetchone()
        legacy = _legacy_key_row(control) if marker is None else None
        if marker is not None and account is None:
            raise ValueError("管理账户迁移记录存在但账户缺失")
        if account is None:
            if external:
                account = admin_account.new("admin", external, "external")
            elif legacy:
                if not legacy[1]:
                    _announce_admin_password(legacy[0])
                account = admin_account.new("admin", legacy[0], "migrated")
            else:
                password = secrets.token_urlsafe(12)
                _announce_admin_password(password)
                account = admin_account.new("admin", password, "generated")
        elif account.password_source != "user":
            if external:
                if admin_account.verify(account, account.username, external):
                    account = admin_account.Account(account.username, "external", account.password,
                                                    account.session_epoch, account.changed_at)
                else:
                    account = admin_account.with_password(account, external, source="external")
            elif account.password_source == "external":
                password = secrets.token_urlsafe(12)
                _announce_admin_password(password, username=account.username)
                account = admin_account.with_password(account, password, source="generated")
        if legacy and not external:
            secret = legacy[0]
            control._db.execute(
                "INSERT INTO api_keys(id,name,type,secret,secret_hash,prefix,created_at) "
                "VALUES(?,?,'admin',NULL,?,?,?)",
                ("k_" + secrets.token_urlsafe(9), "默认密钥",
                 hashlib.sha256(secret.encode()).hexdigest(), secret[:8], time.time()))
        admin_account.save(control, account)
        if marker is None:
            control._db.execute("DELETE FROM gateway_secrets WHERE name='default_api_key'")
            control._db.execute(
                "INSERT INTO state_imports(name,imported,migrated_at) VALUES('admin_auth_v1',?,?)",
                (int(legacy is not None), time.time()))


def _install_lease_release(config):
    """Release the running-instance lease before uvicorn re-raises the stop signal.

    uvicorn restores the original handlers and re-raises the captured signal, so a
    `finally` around `Server.run` never executes on SIGTERM. Our handler is that
    original, so it runs once more and then re-delivers the signal with defaults.
    """
    store = config.get("control_store")
    if store is None or threading.current_thread() is not threading.main_thread():
        return

    def release(signum, _frame):
        try:
            store.release_service_lease()
        finally:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, release)


def run_server(app, config, *, host, port):
    _install_lease_release(config)

    # Same behavior as uvicorn.run, but keeps the server so a restart can stop it gracefully.
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    config["server"] = server
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        config.pop("server", None)
    if not server.started:
        raise SystemExit(3)


def capture_launch():
    """Snapshot the command line, environment and directory before .env is loaded."""
    return {"argv": list(sys.orig_argv), "env": dict(os.environ), "cwd": os.getcwd()}


def request_restart(config, grace=30):
    """Stop serving gracefully; main() then re-executes the original command line."""
    server = config.get("server")
    if server is None or not config.get("launch"):
        raise RuntimeError("当前进程未通过 serve 启动，无法自动重启")
    config["restart_requested"] = True
    # In-flight streams get a bounded grace period instead of holding the restart forever.
    server.config.timeout_graceful_shutdown = grace
    server.should_exit = True


def restart_process(launch):
    """Replace this process with the exact original invocation, keeping its PID."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(launch["cwd"])
    argv = launch["argv"]
    if os.name == "nt":
        # Windows exec joins arguments without quoting them.
        argv = [subprocess.list2cmdline([argument]) for argument in argv]
    os.execve(sys.executable, argv, launch["env"])
