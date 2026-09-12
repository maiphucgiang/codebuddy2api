"""Initialize management only for an explicitly started server, never at import."""

import sqlite3
import sys
from pathlib import Path

from .admin_api import install_admin
from .audit_store import AuditStore
from .control_store import ControlStore
from .gateway_management import Management, install_pages
from .model_policy import PolicyScopeMiddleware
from .observability import AuditMiddleware
from .settings import SCHEMA, apply_persisted_settings


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

    def dashboard(self, days=30):
        return {"summary": None, "series": [], "models": [], "profiles": [], "degraded": True, "storage": self.storage()}

    def list_records(self, *args, **kwargs):
        return {"items": [], "next_cursor": None, "has_more": False, "degraded": True}

    def get_request(self, id):
        return None

    def configure(self, **kwargs):
        return {"ok": False}

    def clear(self, scope):
        return {"ok": False}

    def close(self):
        pass


def initialize(gateway, args, argv=None):
    config = gateway.CONFIG
    root = gateway.managed_auth_dir()
    control = ControlStore(root / "control.sqlite3")
    config["control_store"] = control
    config.update(vars(args))
    config["model_guard"] = not args.no_model_guard
    aliases = {"log": "log_path", "no_model_guard": "model_guard"}
    explicit = set()
    for argument in (sys.argv[1:] if argv is None else argv):
        if argument.startswith("--"):
            key = argument[2:].split("=", 1)[0].replace("-", "_")
            explicit.add(aliases.get(key, key))
    apply_persisted_settings(config, explicit=explicit)
    for key in SCHEMA:
        if hasattr(args, key) and key != "api_key":
            setattr(args, key, config[key])
    config["trial_ledger"] = (gateway.trial_rewards.TrialLedger(root / "trial-ledger.json")
                              if config["auto_trial"] else None)
    try:
        config["audit_store"] = AuditStore(root / "logs.sqlite3", max_bytes=config["audit_max_bytes"],
                                            retention_days=config["audit_retention_days"],
                                            preview_limit=config["audit_diagnostic_bytes"])
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        config["audit_store"] = UnavailableAudit(type(error).__name__)
        print("[audit] 日志库不可用，推理服务继续；请检查目录权限或使用备份恢复日志库。", file=sys.stderr)
    config["management"] = Management(gateway)
    return config["management"]


def install(gateway):
    config, app = gateway.CONFIG, gateway.app
    install_admin(app, config, config["management"])
    app.add_middleware(AuditMiddleware, config=config)
    app.add_middleware(PolicyScopeMiddleware)
    install_pages(app, Path(gateway.__file__).resolve().parent / "web" / "dist")


def close(config):
    for key in ("audit_store", "control_store"):
        store = config.get(key)
        if store is not None:
            store.close()
