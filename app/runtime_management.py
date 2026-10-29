"""Initialize management only for an explicitly started server, never at import."""

import os
import sqlite3
import sys
import threading
from pathlib import Path

from . import buddy
from .admin_api import install_admin
from .api_keys import KeyStore
from .audit_store import AuditStore
from .backups import Backups
from .control_store import ControlStore
from .gateway_management import Management, install_pages
from .inference_auth import InferenceAuthMiddleware
from .media_store import MediaStore
from .model_policy import PolicyScopeMiddleware
from .observability import AuditMiddleware
from .settings import SCHEMA, apply_persisted_settings
from .updates import Updater


class UnavailableAudit:
    """An explicit degraded state; never silently recreate a damaged log database."""
    def __init__(self, code):
        self.code = code
        self.dropped = 0

    def ticket(self):
        return {"epoch": 0, "detail_generation": -1}

    def record_request(self, record):
        self.dropped += 1
        return {"ok": False, "recorded": False}

    def event(self, *args, **kwargs):
        self.dropped += 1
        return {"ok": False, "recorded": False}

    def note_failure(self, *args, **kwargs):
        self.dropped += 1

    def storage(self):
        return {"available": False, "degraded": True, "last_error": self.code, "dropped": self.dropped,
                "warning": "日志存储不可用；保留原文件，未自动重建。统计可能不完整。"}

    def dashboard(self, days=30, granularity="auto"):
        return {"summary": None, "series": [], "models": [], "profiles": [], "degraded": True, "storage": self.storage()}

    def list_records(self, *args, **kwargs):
        return {"items": [], "next_cursor": None, "has_more": False, "degraded": True}

    def api_key_stats(self, *args, **kwargs):
        return {"degraded": True, "items": {}}

    def get_request(self, id):
        return None

    def configure(self, **kwargs):
        return {"ok": False}

    def clear(self, scope):
        return {"ok": False}

    def close(self):
        pass


def initialize(gateway, args, argv=None, *, parser=None, dotenv_keys=()):
    config = gateway.CONFIG
    config["auto_accept_buddy"] = buddy.auto_accept_from_env(os.environ)
    config["auto_accept_buddy_source"] = "environment" if "CODEBUDDY2API_AUTO_ACCEPT_BUDDY" in os.environ else "default"
    root = gateway.managed_auth_dir()
    control = ControlStore(root / "control.sqlite3")
    config["control_store"] = control
    config["state_store"] = control.state
    try:
        control.state.migrate(root)
    except BaseException:
        control.close()
        raise
    config.update(vars(args))
    config["model_guard"] = not args.no_model_guard
    aliases = {"log": "log_path", "no_model_guard": "model_guard"}
    explicit = set()
    options = parser._option_string_actions if parser is not None else {}
    for argument in (sys.argv[1:] if argv is None else argv):
        if argument.startswith("--"):
            flag = argument.split("=", 1)[0]
            matches = [flag] if flag in options else [name for name in options if name.startswith(flag)]
            # Resolve argparse's accepted abbreviations before assigning precedence.
            key = options[matches[0]].dest if len(matches) == 1 else flag[2:].replace("-", "_")
            explicit.add(aliases.get(key, key))
    apply_persisted_settings(config, explicit=explicit)
    from .startup import allows_open_noauth, resolve_startup_auth
    try:
        resolve_startup_auth(config, args, dotenv_keys)
    except BaseException:
        control.close()
        raise
    for key in SCHEMA:
        if hasattr(args, key) and key != "api_key":
            setattr(args, key, config[key])
    config["allow_open_noauth"] = allows_open_noauth()
    config["key_store"] = KeyStore(control)
    if config.get("api_key") or config["key_store"].has_any():
        if config["allow_open_noauth"]:
            print("[auth] 已存在 API key，OPEN_NOAUTH 不生效", file=sys.stderr)
    elif config["allow_open_noauth"]:
        print("[auth] 推理接口与账号池额度（billing）匿名开放", file=sys.stderr)
    else:
        print("[auth] 尚未创建 API key，请登录 WebUI 创建", file=sys.stderr)
    config["trial_ledger"] = gateway.trial_rewards.TrialLedger(store=control.state)
    try:
        config["audit_store"] = AuditStore(root / "logs.sqlite3", max_bytes=config["audit_max_bytes"],
                                            retention_days=config["audit_retention_days"],
                                            preview_limit=config["audit_diagnostic_bytes"])
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        config["audit_store"] = UnavailableAudit(type(error).__name__)
        print("[audit] 日志库不可用，推理服务继续；请检查目录权限或使用备份恢复日志库。", file=sys.stderr)
    try:
        config["media_store"] = MediaStore(root)
    except (OSError, ValueError, sqlite3.Error) as error:
        config["media_store"] = None
        print(f"[media] 媒体存储不可用（{type(error).__name__}），图像/视频接口将返回 503。", file=sys.stderr)
    config["management"] = Management(gateway)
    config["backups"] = Backups(config, data_dir=root, version=gateway.APP_VERSION)
    config["updater"] = Updater(config, root=Path(gateway.__file__).resolve().parent,
                                data_dir=root, version=gateway.APP_VERSION, backups=config["backups"])
    return config["management"]


def install(gateway):
    config, app = gateway.CONFIG, gateway.app
    install_admin(app, config, config["management"])
    app.add_middleware(AuditMiddleware, config=config)
    app.add_middleware(PolicyScopeMiddleware)
    from .inbound_limits import ConcurrencyLimitMiddleware, InboundBodyLimitMiddleware
    app.add_middleware(InboundBodyLimitMiddleware, config=config)
    app.add_middleware(ConcurrencyLimitMiddleware, config=config)
    # Authenticate headers before consuming inference capacity or buffering request bodies.
    app.add_middleware(InferenceAuthMiddleware, config=config)
    from .request_context import RequestContextMiddleware
    app.add_middleware(RequestContextMiddleware, config=config)
    install_pages(app, Path(gateway.__file__).resolve().parent / "web" / "dist")


def close(config):
    stop = config.get("key_housekeeping_stop")
    if stop is not None:
        stop.set()
        thread = config.get("key_housekeeping_thread")
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
    if config.get("key_store") is not None:
        config["key_store"].flush_used()
    for key in ("audit_store", "control_store", "media_store"):
        store = config.get(key)
        if store is not None:
            store.close()


def start_key_housekeeping(config):
    stop = threading.Event()

    def run():
        while not stop.wait(60):
            try:
                config["key_store"].flush_used()
            except (OSError, ValueError, sqlite3.Error):
                print("[auth] 密钥最后使用时间暂时无法保存", file=sys.stderr)

    thread = threading.Thread(target=run, daemon=True, name="api-key-housekeeping")
    config.update(key_housekeeping_stop=stop, key_housekeeping_thread=thread)
    thread.start()
