"""Disposable browser fixture: real management API, synthetic credentials, no upstream sockets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import asynccontextmanager
import json
import os
import socket
import tempfile
import time

import httpx
import uvicorn

import converter as gateway
from app.audit_store import AuditStore
from app.control_store import ControlStore
from app.gateway_management import Management
from app.runtime_management import install, close
from app.settings import apply_persisted_settings


@asynccontextmanager
async def fake_backend(url, headers, body, **kwargs):
    chunks = [
        {"model": body["model"], "choices": [{"index": 0, "delta": {"content": "isolated response", "reasoning_content": "synthetic reasoning"}, "finish_reason": None}]},
        {"model": body["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "credit": 0}},
    ]
    data = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    yield httpx.Response(200, content=data.encode(), headers={"Content-Type": "text/event-stream"})


def forbid_connect(*args, **kwargs):
    raise AssertionError("Fixture must never open an upstream connection")


def main():
    with tempfile.TemporaryDirectory(prefix="codebuddy-webui-browser-") as directory:
        root = Path(directory)
        os.environ["CODEBUDDY_AUTH_DIR"] = directory
        gateway.CONFIG.update(api_key="synthetic-e2e-key", log_path=None, auto_trial=False,
                              control_store=ControlStore(root / "control.sqlite3"),
                              audit_store=AuditStore(root / "logs.sqlite3"))
        apply_persisted_settings(gateway.CONFIG, explicit=("api_key",), environ={})
        credential = {"account": {"uid": "fixture-account", "nickname": "集成测试凭证"},
                      "auth": {"domain": "www.workbuddy.cn", "accessToken": "synthetic-browser-access",
                               "refreshToken": "synthetic-browser-refresh", "expiresAt": (time.time() + 86400) * 1000}}
        path = gateway.atomic_write_credential(root, "fixture.info", json.dumps(credential).encode())
        pool = gateway.CredentialPool([path])
        gateway.CONFIG["cred_pool"], gateway.CONFIG["cred"] = pool, pool.first()
        ledger = gateway.credits_mod.CreditLedger(root / "credits-ledger.json")
        pool.set_ledger(ledger)
        ledger.update_credits(str(path), {"credits": 125, "intl": False, "segments": []})
        identity = pool.entries()[0]["account_key"]
        gateway.CONFIG.update(ledger=ledger, model_cache=None, account_catalogs={identity: {
            "profile": "cn-work", "models": [{"id": "fixture-model", "supportsToolCall": True, "credits": "x0.00"}]}})
        pool._sync_pending.clear()
        pool._sync_event.clear()
        gateway.CONFIG["management"] = Management(gateway)
        gateway.open_backend_stream = fake_backend
        socket.socket.connect = forbid_connect
        install(gateway)
        try:
            uvicorn.run(gateway.app, host="127.0.0.1", port=5175, log_level="info")
        finally:
            close(gateway.CONFIG)


if __name__ == "__main__":
    main()
