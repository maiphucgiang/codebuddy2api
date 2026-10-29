#!/usr/bin/env python3
"""Offline synthetic benchmark for multi-account routing and model aggregation.

Builds a real ``CredentialPool`` from synthetic ``.info`` files, a real
``CreditLedger`` and account-scoped catalogs, then replaces nothing but the
upstream network. It measures the request-side hot paths so a change can be
compared against the documented baseline on the same machine.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILES = ("cn-cli", "cn-work", "intl-cli", "intl-work")
DOMAINS = {"cn-cli": "www.codebuddy.cn", "cn-work": "www.workbuddy.cn",
           "intl-cli": "www.codebuddy.ai", "intl-work": "www.workbuddy.ai"}
MODELS = {
    "cn-cli": ["hy4-preview", "glm-5.3", "glm-5.3-flash", "deepseek-v4-flash"],
    "cn-work": ["auto", "glm-5.3", "glm-5.3-flash"],
    "intl-cli": ["default-model", "claude-sonnet-4-5", "gpt-5.1"],
    "intl-work": ["default-model", "claude-sonnet-4-5", "gpt-5.1"],
}
FREE_MODEL = "glm-5.3-flash"


def credential_payload(uid: str, profile: str) -> dict:
    """Return a credential shaped like a real export: account block plus plaintext token."""
    return {"account": {"uid": uid, "nickname": f"bench-{uid}"},
            "auth": {"accessToken": f"bench-token-{uid}", "refreshToken": f"bench-refresh-{uid}",
                     "domain": DOMAINS[profile], "expiresAt": int(time.time() * 1000) + 86400 * 1000,
                     "lastRefreshTime": int(time.time() * 1000)}}


def catalog(profile: str, free: bool) -> dict:
    models = [{"id": name, "supportsToolCall": True,
               "credits": "x0 credits" if free and name == FREE_MODEL else "x1 credits"}
              for name in MODELS[profile]]
    return {"profile": profile, "models": [dict(item) for item in models],
            "serves": [dict(item) for item in models]}


def build(converter, credits_mod, count: int, root: Path):
    """Populate the process-wide CONFIG with ``count`` synthetic accounts."""
    config = converter.CONFIG
    paths = []
    for index in range(count):
        profile = PROFILES[index % len(PROFILES)]
        path = root / f"account-{index:05d}.info"
        path.write_text(json.dumps(credential_payload(f"bench-{index:05d}", profile)), encoding="utf-8")
        paths.append(path)
    pool = converter.CredentialPool(paths, scan=False)
    ledger = credits_mod.CreditLedger(root / "credits.json")
    pool.set_ledger(ledger)
    accounts = {}
    for index, entry in enumerate(pool.entries()):
        profile = entry.get("profile") or PROFILES[index % len(PROFILES)]
        ledger.bind_identity(entry["id"], entry["account_key"])
        ledger.update_credits(entry["id"], {"credits": 1000 + index, "intl": profile.startswith("intl"),
                                            "segments": [{"remaining": 1000 + index,
                                                          "expires_at": time.time() + 3600 + index}]})
        accounts[entry["account_key"]] = catalog(profile, free=index % 3 == 0)
    catalogs = {profile: None for profile in PROFILES}
    for account in accounts.values():
        merged = catalogs[account["profile"]] = catalogs[account["profile"]] or []
        merged.extend(account["models"])
    config.update({"cred_pool": pool, "ledger": ledger, "account_catalogs": accounts,
                   "routing_scheduler": "indexed",  # The benchmark measures the indexed path.
                   "catalog_index": converter._publish_catalog_index(accounts),
                   "model_catalogs": catalogs, "models_remote": catalogs["cn-cli"],
                   "models_intl": catalogs["intl-cli"], "model_cache": None,
                   "model_guard": True, "max_inflight_per_account": 0,
                   "control_store": None, "log_path": None})
    return pool


def measure(converter, perf_metrics, case, operation, repeat):
    """Run one case and return wall-clock milliseconds plus per-op counters."""
    times = []
    counters = {}
    for _ in range(repeat):
        perf_metrics.reset()
        started = time.perf_counter()
        operation()
        times.append((time.perf_counter() - started) * 1000)
        for name, value in perf_metrics.snapshot()["counters"].items():
            counters[name] = counters.get(name, 0) + value
    return {"case": case, "median_ms": round(statistics.median(times), 3),
            "max_ms": round(max(times), 3),
            "counters": {name: round(value / repeat, 2) for name, value in counters.items()}}


def run(converter, credits_mod, perf_metrics, count, repeat, model):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    pool = build(converter, credits_mod, count, root)
    rows = []
    try:
        control = None
        rows.append(measure(converter, perf_metrics, "pick", lambda: pool.pick("session", model), repeat))
        rows.append(measure(converter, perf_metrics, "headers_for",
                            lambda: pool.headers_for(None, model), repeat))
        ledger = converter.CONFIG["ledger"]
        churn_cid = pool.entries()[0]["id"]
        churn_paths = [entry["id"] for entry in pool.entries()[:max(1, count // 10)]]
        churn_balance = {"credits": 1000, "intl": False,
                         "segments": [{"remaining": 1000, "expires_at": time.time() + 3600}]}

        def churned():
            # Token-refresh-like reconcile (reset=False) plus a maintenance-style ledger
            # write: neither moves a semantic revision, so the shard stays hot.
            pool.reload([Path(path) for path in churn_paths], reset=False)
            ledger.update_credits(churn_cid, churn_balance)
            pool.pick("session", model)
        cm = pool.pick("cooldown", model)
        pool.note_status(cm, 429, model=model)
        try:
            rows.append(measure(converter, perf_metrics, "pick + cooldown", lambda: pool.pick("session", model), repeat))
        finally:
            converter.CONFIG["model_cache"] = None
        tried = frozenset(entry["cm"] for entry in pool.entries()[:max(1, count // 10)])
        rows.append(measure(converter, perf_metrics, "pick + tried",
                            lambda: pool.pick("session", model, tried=tried), repeat))
        limit = converter.CONFIG["max_inflight_per_account"]
        converter.CONFIG["max_inflight_per_account"] = 1

        def saturated():
            try:
                pool.pick("session", model, with_capacity=True)
            except Exception:
                pass  # Only the ranking work is measured; the refusal itself is free.
        rows.append(measure(converter, perf_metrics, "pick + capacity full", saturated, repeat))
        converter.CONFIG["max_inflight_per_account"] = limit
        rows.append(measure(converter, perf_metrics, "current_models", converter.current_models, repeat))
        rows.append(measure(converter, perf_metrics, "current_model_details", converter.current_model_details, repeat))
        rows.append(measure(converter, perf_metrics, "pick + revision churn", churned, repeat))
        from app.api_keys import KeyStore
        from app.control_store import ControlStore
        control = ControlStore(root / "control.sqlite3")
        keys = KeyStore(control)
        _, secret = keys.create(name="benchmark", kind="standard", rpm=100000)
        auth_config = {"api_key": "", "allow_open_noauth": False, "trusted_proxies": ""}
        auth_scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
                      "headers": [(b"authorization", f"Bearer {secret}".encode())],
                      "client": ("127.0.0.1", 1234)}

        def authenticated():
            for _ in range(1000):
                keys.authenticate(auth_scope, auth_config, count_rpm=True)
        rows.append(measure(converter, perf_metrics, "api key auth x1000", authenticated, repeat))
    finally:
        if control is not None:
            control.close()
        temporary.cleanup()
        converter.CONFIG.update({"cred_pool": None, "ledger": None, "account_catalogs": None,
                                 "catalog_index": None, "model_catalogs": {},
                                 "models_remote": None, "models_intl": None})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", default="10,100,500", help="comma-separated account counts")
    parser.add_argument("--repeat", type=int, default=3, help="repetitions per case")
    parser.add_argument("--model", default="glm-5.3-flash", help="routed model to benchmark")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    os.environ.setdefault("CODEBUDDY_AUTH_DIR", tempfile.mkdtemp(prefix="bench-auth-"))
    import converter
    from app import perf_metrics, credits as credits_mod
    report = {}
    for count in [int(value) for value in args.accounts.split(",") if value.strip()]:
        report[count] = run(converter, credits_mod, perf_metrics, count, max(1, args.repeat), args.model)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("| 账号数 | 场景 | 中位耗时 ms | 最慢 ms | summary 调用 | stat 调用 | 候选检查 |")
    print("|---:|---|---:|---:|---:|---:|---:|")
    for count, rows in report.items():
        for row in rows:
            counters = row["counters"]
            print(f"| {count} | {row['case']} | {row['median_ms']} | {row['max_ms']} | "
                  f"{counters.get('credential_summary_calls', 0)} | {counters.get('credential_stat_calls', 0)} | "
                  f"{counters.get('candidate_checks', 0)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
