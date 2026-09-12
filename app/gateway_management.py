"""Management views over the existing credential pool, without storing tokens."""

import hashlib
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import model_policy


class Management:
    def __init__(self, gateway):
        self.gateway = gateway
        self.CONFIG = gateway.CONFIG

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    def admin_credential_inventory(self):
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            return []
        pool._rescan()
        now = time.time()
        with pool._lock:
            entries = {entry["id"]: entry for entry in pool.entries()}
            result = []
            for row in pool.snapshot():
                path = row.pop("auth_file", "")
                entry = entries[path]
                identity = entry.get("account_key") or hashlib.sha256(path.encode()).hexdigest()
                ledger = self.CONFIG.get("ledger")
                balance = ledger.entry(path) if ledger else {}
                enabled = model_policy.credential_enabled(self.CONFIG, entry)
                until = entry.get("fail_until", 0)
                cooldowns = [{"model": model, "until": deadline,
                              "remaining_seconds": max(0, round(deadline - now))}
                             for (cid, model), deadline in pool._model_fail.items()
                             if cid == path and deadline > now]
                state = ("disabled" if not enabled else "error" if row.get("error") else
                         "circuit_open" if until > now else "expired" if row.get("token_expired") else "ready")
                row.update(id=identity, name=Path(path).name, enabled=enabled, health=state,
                           fail_until=until, cooldown_until=until,
                           cooldown_remaining=max(0, round(until - now)),
                           last_error_code=("http_401" if entry.get("last_error") == "backend HTTP 401" else
                                            "http_403" if entry.get("last_error") == "backend HTTP 403" else
                                            "credential_error" if entry.get("last_error") else None),
                           last_failure_at=entry.get("last_failure_at"),
                           cooldowns=cooldowns, credits=balance.get("credits") or None,
                           sync_pending=path in pool._sync_pending or path in pool._syncing or path in pool._sync_retry,
                           catalog_ready=(self.CONFIG.get("account_catalogs") or {}).get(identity, {}).get("models") is not None,
                           bindings=[source for source, rule in model_policy.snapshot(self.CONFIG)["models"].items()
                                     if identity in rule.get("credential_ids", [])])
                result.append(row)
            return result

    def admin_set_credential_enabled(self, identity, enabled):
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            raise HTTPException(404, "凭证不存在")
        with pool._lock:
            entry = next((entry for entry in pool.entries() if entry.get("account_key") == identity), None)
            if entry is None:
                raise HTTPException(404, "凭证不存在")
            self.CONFIG["control_store"].set_credential(identity, enabled)
            pool._sticky.clear()
            if enabled:
                pool._queue_sync(entry["id"])
            else:
                pool._sync_pending.discard(entry["id"])
                pool._sync_retry.pop(entry["id"], None)
                if not pool._sync_pending:
                    pool._sync_event.clear()
            self.gateway.invalidate_model_table()
        return next(row for row in self.admin_credential_inventory() if row["id"] == identity)

    def admin_delete_guard(self, name):
        rows = self.admin_credential_inventory()
        row = next((row for row in rows if row["name"] == name or row["id"] == name), None)
        if row and row["bindings"]:
            raise HTTPException(status_code=409, detail={"message": "凭证仍被模型规则引用，请先移除绑定",
                                                        "models": row["bindings"]})

    def admin_model_inventory(self):
        pool = self.CONFIG.get("cred_pool")
        if pool:
            pool._rescan()
        facts = {}
        for account in (self.CONFIG.get("account_catalogs") or {}).values():
            for item in self.gateway._usable_models(account.get("models")):
                source = item["id"]
                row = facts.setdefault(source, {"id": source, "credits": None, "credits_by_profile": {}})
                price = self.gateway._multiplier_value(item.get("credits"))
                if price is not None:
                    row["credits_by_profile"][account["profile"]] = price
                    row["credits"] = min(row["credits"] if row["credits"] is not None else price, price)
        for row in self.gateway.current_model_details():
            facts.setdefault(row["id"], row)
        for source in model_policy.snapshot(self.CONFIG)["models"]:
            facts.setdefault(source, {"id": source, "credits": None, "credits_by_profile": {}})
        result = []
        for source, row in facts.items():
            rule = model_policy.rule_for(self.CONFIG, source)
            preview = self.admin_model_preview(source, rule)
            result.append({**row, **rule, "available_credentials": len(preview["candidates"]),
                           "available": bool(preview["candidates"])})
        return sorted(result, key=lambda row: row["id"])

    def admin_model_preview(self, source, rule):
        pool = self.CONFIG.get("cred_pool")
        accepted, rejected = [], []
        if pool is None:
            return {"candidates": [], "excluded": []}
        with pool._lock:
            for entry in pool.entries():
                identity = entry.get("account_key")
                profile = entry.get("profile")
                reason = None
                if not model_policy.credential_enabled(self.CONFIG, entry):
                    reason = "人工停用"
                elif not rule.get("enabled", True):
                    reason = "模型已停用"
                elif not model_policy.route_allowed(self.CONFIG, entry, source, rule=rule):
                    reason = "不在绑定范围"
                elif not pool._healthy(entry):
                    reason = "认证熔断"
                elif not pool._model_healthy(entry, source):
                    reason = "模型额度冷却"
                else:
                    account = (self.CONFIG.get("account_catalogs") or {}).get(identity, {})
                    models = account.get("models")
                    if self.CONFIG.get("account_catalogs") is not None:
                        if models is None:
                            reason = "目录尚未就绪"
                        elif not any(item["id"] == self.gateway._upstream_model(source, profile)
                                     for item in self.gateway._usable_models(models)) and not (
                                         source == "auto" and profile == "cn-cli" and self.gateway._usable_models(models)):
                            reason = "账号自身目录不支持模型"
                    if reason is None and not (pool._has_credit(entry, profile) or pool._model_free(entry, source)):
                        reason = "额度不足或未知"
                item = {"id": identity, "name": Path(entry["id"]).name, "profile": profile}
                if reason:
                    rejected.append({**item, "reason": reason})
                else:
                    accepted.append(item)
        return {"candidates": accepted, "excluded": rejected}

    def admin_apply_settings(self, values):
        restart = {"host", "port", "auth_file", "auth_dir", "import_dir", "skip_check", "log_db"}
        for key, value in values.items():
            if key not in restart:
                self.CONFIG[key] = value
        if "model_catalog_ttl" in values and self.CONFIG.get("model_cache") is not None:
            self.CONFIG["model_cache"].ttl = values["model_catalog_ttl"]
        if "auto_trial" in values and values["auto_trial"] and self.CONFIG.get("trial_ledger") is None:
            self.CONFIG["trial_ledger"] = self.gateway.trial_rewards.TrialLedger(self.gateway.managed_auth_dir() / "trial-ledger.json")
        self.gateway.invalidate_model_table()


def install_pages(app, directory):
    """Fallback only applies to pages; management/client APIs never serve HTML."""
    root = Path(directory).resolve()
    pages = {"", "models", "credentials", "logs", "settings", "login"}

    @app.api_route("/dashboard", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/dashboard/{page:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def dashboard_page(page=""):
        if page.startswith("assets/"):
            target = (root / page).resolve()
            if root in target.parents and target.is_file():
                return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if page not in pages:
            if Path(page).suffix:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return RedirectResponse("/dashboard", status_code=302)
        target = root / "index.html"
        if not target.is_file():
            return JSONResponse({"detail": "WebUI 尚未构建；在 web/ 运行 vp install && vp build。API 服务不受影响。"},
                                status_code=503, headers={"Cache-Control": "no-store"})
        return FileResponse(target, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                             "Referrer-Policy": "same-origin"})

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    def page_fallback(path=""):
        if (path.split("/", 1)[0] in {"v1", "admin", "health"} or Path(path).suffix
                or path.startswith(("cn/v1", "intl/v1"))):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return RedirectResponse("/dashboard", status_code=302)
