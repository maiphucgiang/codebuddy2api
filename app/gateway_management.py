"""Management views over the existing credential pool, without storing tokens."""

import hashlib
import re
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from .background_jobs import JobBoard
from . import (checkin, daily_chat, growth_chains, growth_events, growth_tasks, model_policy, model_rates,
               output_probes, travel, trial_management)


# One job per account: a second start while one runs returns the running job.
GROWTH_JOBS = JobBoard()


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
        trials = trial_management.inventory(self.CONFIG.get("trial_ledger"))
        daily_chats = self.CONFIG.get("control_store")
        with pool._lock:
            entries = {entry["id"]: entry for entry in pool.entries()}
            result = []
            policy = model_policy.view(self.CONFIG)
            for row in pool.snapshot():
                path = row.pop("auth_file", "")
                entry = entries[path]
                identity = entry.get("account_key") or hashlib.sha256(path.encode()).hexdigest()
                ledger = self.CONFIG.get("ledger")
                balance = ledger.entry(path) if ledger else {}
                record = policy.credential(entry)
                enabled = record.get("enabled", True)
                until = entry.get("fail_until", 0)
                restricted = entry.get("restricted") is True
                restricted_until = entry.get("restricted_until") if restricted else None
                cooldowns = [{"model": model, "until": deadline,
                              "remaining_seconds": max(0, round(deadline - now))}
                             for (cid, model), deadline in pool._model_fail.items()
                             if cid == path and deadline > now]
                # Provenance lives in the store so it survives restarts and cooldown resets.
                auto_disabled = record.get("auto_disabled") is True and not enabled and not restricted
                state = ("restricted" if restricted else
                         "auto_disabled" if auto_disabled else
                         "disabled" if not enabled else "error" if row.get("error") else
                         "circuit_open" if until > now else "expired" if row.get("token_expired") else "ready")
                row.update(id=identity, name=Path(path).name, enabled=enabled, health=state,
                           auto_disabled=auto_disabled, restricted=restricted,
                           restricted_until=restricted_until,
                           auto_checkin=model_policy.credential_auto_checkin(self.CONFIG, entry),
                           checkin=checkin.view(balance.get("checkin") or {}),
                           auto_travel=model_policy.credential_auto_travel(self.CONFIG, entry),
                           travel_supported=travel.supported(entry.get("profile"), entry.get("enterprise") is True),
                           travel=balance.get("travel") or {"state": "unknown", "message": "尚未查询旅行状态"},
                           trial_supported=entry.get("profile") == "intl-work",
                           trial=(trial_management.view(trials.get(identity), now=now) if trials is not None
                                  else trial_management.failure("storage_error"))
                                 if entry.get("profile") == "intl-work" else trial_management.failure("not_applicable"),
                           daily_chat_supported=daily_chat.supported(entry.get("profile")),
                           auto_daily_chat=model_policy.credential_auto_daily_chat(self.CONFIG, entry),
                           daily_chat=self._daily_chat_view(daily_chats, identity, now),
                           growth_supported=growth_tasks.supported(entry.get("profile"), entry.get("enterprise") is True),
                           auto_growth=model_policy.credential_auto_growth(self.CONFIG, entry),
                           growth_auto=dict((daily_chats.snapshot()["credentials"].get(identity, {})
                                             if daily_chats else {}) or {}).get("growth_auto") or {},
                           fail_until=until, cooldown_until=until,
                           cooldown_remaining=max(0, round(until - now)),
                           last_error_code=("restricted" if restricted else
                                            "http_401" if entry.get("last_error") == "backend HTTP 401" else
                                            "http_403" if entry.get("last_error") == "backend HTTP 403" else
                                            "credential_error" if entry.get("last_error") else None),
                           last_failure_at=entry.get("last_failure_at"),
                           last_error_body=entry.get("last_error_body"),
                           sync_error=(balance.get("error") or entry.get("last_error") or None),
                           cooldowns=cooldowns, credits=balance.get("credits") or None,
                           sync_pending=path in pool._sync_pending or path in pool._syncing or path in pool._sync_retry,
                           catalog_ready=(self.CONFIG.get("account_catalogs") or {}).get(identity, {}).get("models") is not None,
                           bindings=[rule.get("public_id", source) for source, rule in model_policy.snapshot(self.CONFIG)["models"].items()
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
                # Re-enabling returns the credential to a fresh state: reset_cooldowns_for
                # lifts every cooldown and drops the recorded failure evidence.
                pool.reset_cooldowns_for(identity)
                pool._queue_sync(entry["id"])
            else:
                pool._sync_pending.discard(entry["id"])
                # entries() hands out copies; clear the flag on the live entry.
                live = pool.entry_by_id(entry["id"])
                if live is not None:
                    live["auto_disabled"] = None
                pool._sync_retry.pop(entry["id"], None)
                if not pool._sync_pending:
                    pool._sync_event.clear()
            self.gateway.invalidate_model_table()
        return next(row for row in self.admin_credential_inventory() if row["id"] == identity)

    def admin_set_auto_checkin(self, identity, enabled):
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            raise HTTPException(404, "凭证不存在")
        with pool._lock:
            if not any(entry.get("account_key") == identity for entry in pool.entries()):
                raise HTTPException(404, "凭证不存在")
            self.CONFIG["control_store"].set_auto_checkin(identity, enabled)
        # Persist only: enabling does not launch a claim or enqueue unrelated synchronization.

    def admin_set_auto_travel(self, identity, enabled):
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            raise HTTPException(404, "凭证不存在")
        with pool._lock:
            entry = next((entry for entry in pool.entries() if entry.get("account_key") == identity), None)
            if entry is None:
                raise HTTPException(404, "凭证不存在")
            if enabled and not travel.supported(entry.get("profile"), entry.get("enterprise") is True):
                raise HTTPException(400, "企业账号不参与成长中心，无法旅行"
                                    if entry.get("enterprise") is True else "旅行仅适用于国内账号")
            self.CONFIG["control_store"].set_auto_travel(identity, enabled)

    def admin_set_auto_daily_chat(self, identity, enabled):
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            raise HTTPException(404, "凭证不存在")
        with pool._lock:
            entry = next((entry for entry in pool.entries() if entry.get("account_key") == identity), None)
            if entry is None:
                raise HTTPException(404, "凭证不存在")
            if enabled and not daily_chat.supported(entry.get("profile")):
                raise HTTPException(400, "活跃打卡仅适用于国际 WorkBuddy 账号")
            self.CONFIG["control_store"].set_auto_daily_chat(identity, enabled)
        # Persist only: enabling never launches a turn; the sweep decides when to run it.

    def admin_set_auto_growth(self, identity, enabled):
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            raise HTTPException(404, "凭证不存在")
        with pool._lock:
            entry = next((entry for entry in pool.entries() if entry.get("account_key") == identity), None)
            if entry is None:
                raise HTTPException(404, "凭证不存在")
            if enabled and not growth_tasks.supported(entry.get("profile"), entry.get("enterprise") is True):
                raise HTTPException(400, "成长计划仅适用于国内个人账号")
            self.CONFIG["control_store"].set_auto_growth(identity, enabled)
        # Persist only: the sweep decides when to run; enabling never launches requests.

    def admin_set_growth_auto(self, identity, mapping):
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            raise HTTPException(404, "凭证不存在")
        with pool._lock:
            entry = next((entry for entry in pool.entries() if entry.get("account_key") == identity), None)
            if entry is None:
                raise HTTPException(404, "凭证不存在")
            if any(mapping.get(code) for code in mapping) and \
                    not growth_tasks.supported(entry.get("profile"), entry.get("enterprise") is True):
                raise HTTPException(400, "成长计划仅适用于国内个人账号")
            self.CONFIG["control_store"].set_growth_auto(identity, mapping)

    def admin_growth_tasks(self, identity):
        """Live dual-platform task list plus this account's automation switches."""
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            raise HTTPException(404, "凭证不存在")
        with pool._lock:
            entry = next((entry for entry in pool.entries() if entry.get("account_key") == identity), None)
            if entry is None:
                raise HTTPException(404, "凭证不存在")
            supported = growth_tasks.supported(entry.get("profile"), entry.get("enterprise") is True)
            cm, generation = entry["cm"], entry["cm"]._generation
        if not supported:
            return {"supported": False, "auto_growth": False, "growth_auto": {}, "tasks": [],
                    "message": "成长计划仅适用于国内个人账号"}
        with cm._lock:
            headers = cm.get_headers()
        if not pool.apply_if_current(cm, generation, lambda: None):
            return {"supported": True, "stale": True, "auto_growth": False, "growth_auto": {},
                    "tasks": [], "message": "凭证已变化，请刷新核验"}
        token = self.gateway._bearer_token(headers)
        tasks = growth_tasks.merged_tasks(token, uid=headers.get("X-User-Id", ""),
                                          domain=headers.get("X-Domain", ""))
        state = self.CONFIG.get("control_store").snapshot()["credentials"].get(identity, {}) or {}
        for task in tasks:
            # Only codes with a chain can be switched or run; others are left to the claim sweep.
            task["automatable"] = task["task_code"] in growth_chains.RUNNERS
        return {"supported": True, "auto_growth": model_policy.credential_auto_growth(self.CONFIG, entry),
                "growth_auto": dict(state.get("growth_auto") or {}), "tasks": tasks}

    def _growth_chain_context(self, identity):
        """Build the chain context or raise 404/400; caller releases nothing (run() self-locks)."""
        from .site_routing import PROFILE_ENDPOINTS, profile_for_headers
        pool = self.CONFIG.get("cred_pool")
        if pool is None:
            raise HTTPException(404, "凭证不存在")
        with pool._lock:
            entry = next((entry for entry in pool.entries() if entry.get("account_key") == identity), None)
            if entry is None:
                raise HTTPException(404, "凭证不存在")
            if not growth_tasks.supported(entry.get("profile"), entry.get("enterprise") is True):
                raise HTTPException(400, "成长计划仅适用于国内个人账号")
            if not model_policy.credential_enabled(self.CONFIG, entry):
                raise HTTPException(400, "账号已人工停用")
            cm, generation = entry["cm"], entry["cm"]._generation
            summary = cm.summary()
        with cm._lock:
            headers = cm.get_headers()
        if not pool.apply_if_current(cm, generation, lambda: None):
            raise HTTPException(409, "凭证已变化，请刷新核验后重试")
        token = self.gateway._bearer_token(headers)
        profile = profile_for_headers(headers)
        return growth_chains.Context(
            base=PROFILE_ENDPOINTS[profile], token=token, uid=headers.get("X-User-Id", ""),
            domain=headers.get("X-Domain", ""),
            nickname=(summary.get("email") or "").split("@")[0],
            headers=headers, lock_key=identity)

    @staticmethod
    def _run_growth_chain(code, ctx):
        """Upstream failures become a readable outcome instead of an opaque 400."""
        try:
            return growth_chains.run(code, ctx)
        except (growth_tasks.Failure, growth_events.Failure) as error:
            return {"ok": False, "message": f"执行失败：{error}"}

    def _start_growth_job(self, identity, ctx, codes, tasks, kind):
        """Run task chains one by one in the background; the banner shows the current task."""
        titles = {task["task_code"]: task.get("title") or task["task_code"] for task in tasks or ()}

        def run(code, _progress):
            return {"task_code": code, "title": titles.get(code, code), **self._run_growth_chain(code, ctx)}
        return GROWTH_JOBS.start(f"growth:{identity}", codes, run, meta={"account": identity, "kind": kind},
                                 label=lambda code: {"task_code": code, "title": titles.get(code, code)})

    @staticmethod
    def _growth_task_list(ctx):
        try:
            return growth_tasks.merged_tasks(ctx.token, ctx.uid, ctx.domain)
        except growth_tasks.Failure:
            return None

    def admin_growth_auto(self, identity, task_code):
        """Start one task chain manually, regardless of its automation switch."""
        if task_code not in growth_chains.RUNNERS:
            raise HTTPException(400, "该任务暂无自动链")
        ctx = self._growth_chain_context(identity)
        return self._start_growth_job(identity, ctx, [task_code], self._growth_task_list(ctx), "single")

    def admin_growth_auto_all(self, identity):
        """Start every task whose switch is on, or every open task when none is switched on.

        A manual run ignores the scheduled master switch.
        """
        ctx = self._growth_chain_context(identity)
        state = self.CONFIG["control_store"].snapshot()["credentials"].get(identity, {}) or {}
        enabled = sorted(code for code, on in (state.get("growth_auto") or {}).items()
                         if on is True and code in growth_chains.RUNNERS)
        tasks = self._growth_task_list(ctx)
        if not enabled:
            if tasks is None:
                raise HTTPException(502, "成长任务列表获取失败")
            enabled = [task["task_code"] for task in tasks if task["task_code"] in growth_chains.RUNNERS
                       and not task["claimed"] and not task["locked"]]
        return self._start_growth_job(identity, ctx, enabled, tasks, "all")

    @staticmethod
    def admin_growth_jobs():
        """Every account's current or last growth job, for the progress banner."""
        return GROWTH_JOBS.views("growth:")

    @staticmethod
    def _daily_chat_view(store, identity, now):
        """Today's turn state, or the availability prompt when nothing is recorded yet."""
        if store is None:
            return daily_chat.failure_view()
        try:
            record = store.daily_chat_record(identity, daily_chat.today(now))
        except Exception:
            return daily_chat.failure_view()
        return daily_chat.view(record)

    def admin_delete_guard(self, name, *, unbind=False):
        """Block a still-bound delete, or return the identity to unbind after removal."""
        rows = self.admin_credential_inventory()
        row = next((row for row in rows if row["name"] == name or row["id"] == name), None)
        if row is None or not row["bindings"]:
            return None
        if not unbind:
            raise HTTPException(status_code=409, detail={"message": "凭证仍被模型规则引用，请先移除绑定",
                                                        "models": row["bindings"]})
        return row["id"]

    def admin_unbind_credential(self, identity):
        """Unbind now and return the rollback record used when removal fails."""
        control = self.CONFIG.get("control_store")
        if control is None:
            return {}
        return {"identity": identity, "rules": control.unbind_credential(identity)}

    def admin_restore_bindings(self, rollback):
        """Put bindings back after a failed removal so routing never widens silently."""
        control = self.CONFIG.get("control_store")
        if control is not None:
            control.restore_bindings(rollback["identity"], rollback["rules"])

    def admin_model_inventory(self):
        pool = self.CONFIG.get("cred_pool")
        if pool:
            pool._rescan()
        model_rates.refresh(self.gateway, max_age=model_rates.REFRESH_MAX_AGE_S)
        facts, served, declared = {}, {}, {}
        for account in (self.CONFIG.get("account_catalogs") or {}).values():
            for item in self.gateway._usable_models(
                    self.gateway._effective_account_scope(account, "serves") or []):
                source = item["id"]
                served.setdefault(source, set()).add(account["profile"])
                if "credits_source" in item:
                    declared.setdefault(source, {})[account["profile"]] = \
                        self.gateway._multiplier_value(item.get("credits_declared"))
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
            target = facts.get(rule["upstream_id"], row)
            metadata = self.gateway.model_capabilities.route_metadata(
                self.gateway, rule["upstream_id"], rule=rule, include_disabled=True)
            prices = self.gateway.model_capabilities.declaration_prices(self.gateway, metadata)
            upstream = rule["upstream_id"]
            rates = model_rates.details(self.gateway, upstream, sorted(served.get(upstream, ())))
            for profile, value in (declared.get(upstream) or {}).items():
                rates[profile]["declared"] = value
            result.append({**target, **metadata, **prices, **rule, "id": source, "rate_details": rates,
                           "available_credentials": len(preview["candidates"]),
                           "available": bool(preview["candidates"])})
        return sorted(result, key=lambda row: row["id"])

    def admin_rate_probe_model(self, source):
        """Probe one rule's upstream model on every product that serves it, in the background."""
        upstream = model_policy.rule_for(self.CONFIG, source)["upstream_id"]
        return model_rates.start_probe(self.gateway, upstream, scope=source)

    def admin_rate_probe_all(self):
        return model_rates.start_probe(self.gateway)

    def admin_rate_probe_view(self):
        return model_rates.job_view(self.gateway)

    def admin_output_probes(self):
        control = self.CONFIG.get("control_store")
        return output_probes.read(output_probes.default_path(control.path if control is not None else None))

    def admin_model_preview(self, source, rule):
        upstream = rule.get("upstream_id", source)
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
                elif not pool._model_healthy(entry, upstream):
                    reason = "模型额度冷却"
                elif not pool._model_servable(entry, upstream):
                    reason = "后端模型暂时不可用"
                elif not pool._eligible(entry, upstream, rule=rule):
                    account = (self.CONFIG.get("account_catalogs") or {}).get(identity, {})
                    models = self.gateway._effective_account_scope(account, "serves", model_id=self.gateway._upstream_model(upstream, profile))
                    if (self.CONFIG.get("account_catalogs") is not None or self.CONFIG.get("model_cache") is not None) and (
                            models is None or account.get("profile") != profile):
                        reason = "目录尚未就绪"
                    elif not (pool._has_credit(entry, profile) or pool._model_free(entry, upstream)):
                        reason = "额度不足或未知"
                    else:
                        reason = "账号自身目录不支持模型"
                item = {"id": identity, "name": Path(entry["id"]).name, "profile": profile}
                if reason:
                    rejected.append({**item, "reason": reason})
                else:
                    accepted.append(item)
        return {"candidates": accepted, "excluded": rejected,
                **self.gateway.model_capabilities.route_metadata(
                    self.gateway, upstream, rule=rule, include_disabled=True)}

    def admin_apply_settings(self, values):
        restart = {"host", "port", "auth_file", "auth_dir", "import_dir", "skip_check", "log_db"}
        for key, value in values.items():
            if key not in restart:
                self.CONFIG[key] = value
        if "model_catalog_ttl" in values and self.CONFIG.get("model_cache") is not None:
            self.CONFIG["model_cache"].ttl = values["model_catalog_ttl"]
        self.gateway.invalidate_model_table()


def install_pages(app, directory):
    """Fallback only applies to pages; management/client APIs never serve HTML."""
    root = Path(directory).resolve()
    pages = {"", "models", "credentials", "logs", "media", "storage", "settings", "updates", "login"}
    # Release detail pages carry the tag, whose dots would otherwise read as a file suffix.
    release_page = re.compile(r"updates/v\d+\.\d+\.\d+").fullmatch
    assets = StaticFiles(directory=root / "assets", check_dir=False)

    @app.api_route("/dashboard/assets/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def dashboard_asset(asset_path: str, request: Request):
        if "\x00" in asset_path:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Delegate containment to Starlette; assets may be built after startup.
        response = await assets.get_response(asset_path, request.scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.api_route("/dashboard", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/dashboard/{page:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def dashboard_page(page=""):
        if page == "assets" or page.startswith("assets/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if page not in pages and not release_page(page):
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
