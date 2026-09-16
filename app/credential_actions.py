"""Scoped manual credential maintenance without implicit check-in or trial claims."""
import time
from pathlib import Path

from fastapi import HTTPException

from . import buddy, checkin, model_policy, travel, trial_management
from .credential_io import credential_file_lock


def run(gateway, action, identity=None, *, consent_revision=None):
    if action not in {"refresh", "checkin", "sync", "travel", "travel-status", "trial"} or (action in {"refresh", "travel", "travel-status", "trial"} and identity is None):
        raise HTTPException(404, "凭证操作不存在")
    if consent_revision is not None and (action != "travel" or identity is None or consent_revision != buddy.AGREEMENT_REVISION):
        raise HTTPException(400, "首领确认无效或协议版本已变化")
    config = gateway.CONFIG
    pool, ledger = config.get("cred_pool"), config.get("ledger")
    if pool is None or (action not in {"refresh", "trial"} and (ledger is None or gateway.credits_mod is None)):
        raise HTTPException(503, "凭证维护尚未就绪")
    # Do not queue duplicate manual work behind the periodic maintenance sweep.
    if not gateway._HOUSEKEEP_LOCK.acquire(blocking=False):
        raise HTTPException(409, "凭证维护正在执行，请稍后刷新列表核验")
    try:
        pool._rescan()
        entries = [dict(e) for e in pool.entries() if identity is None or e.get("account_key") == identity]
        if identity is not None and not entries:
            raise HTTPException(404, "凭证不存在或身份已变化")
        if action in {"trial", "travel", "travel-status"}:
            entries = entries[:1]  # One account identity represents one manual action.
        results = []
        for entry in entries:
            started = time.monotonic()
            result = {"id": entry.get("account_key"), "name": Path(entry["id"]).name, "action": action, "ok": False}
            if not model_policy.credential_enabled(config, entry):
                result.update(trial_management.failure("changed", skipped=True) if action == "trial"
                              else {"skipped": True, "message": "账号已人工停用"})
            else:
                try:
                    result.update(_one(gateway, pool, ledger, entry, action, consent_revision=consent_revision))
                    if (action == "checkin" and result.get("state") not in {"changed", "cancelled"}
                            and model_policy.credential_auto_travel(config, entry)):
                        followup = _one(gateway, pool, ledger, entry, "travel", automatic=True)
                        result.update(travel=followup, checkin_ok=result["ok"],
                                      ok=result["ok"] if followup.get("buddy_blocked") else result["ok"] and followup["ok"],
                                      message=result["message"] + "；" + followup["message"])
                except Exception:
                    # Upstream exception text may contain headers or credential file paths.
                    if action == "checkin" and result["ok"]:
                        result["checkin_ok"] = True
                    result.update(ok=False, message="操作失败，保留已有数据；请检查账号状态后重试")
            results.append(result)
            audit = config.get("audit_store")
            if audit:
                try:
                    details = {"credential": result["id"], "ok": result["ok"]}
                    if action == "trial":
                        details.update(outcome="success" if result["ok"] else "error", stage=result.get("state"),
                                       status_code=result.get("status"),
                                       code=str(result["code"]) if result.get("code") is not None else None,
                                       duration_ms=(time.monotonic() - started) * 1000)
                    trip = result.get("travel") if action == "checkin" else result if action in {"travel", "travel-status"} else None
                    if isinstance(trip, dict):
                        details.update(outcome="success" if trip.get("ok") else "warning" if trip.get("buddy_blocked") else "error",
                                       stage=trip.get("phase"), status_code=trip.get("http_status"),
                                       code=trip.get("reason") or (str(trip["code"]) if trip.get("code") is not None else None),
                                       consent_source=trip.get("consent_source"))
                    audit.event("admin", "credential." + action, details)
                except Exception:
                    pass
        response = {"ok": bool(results) and all(r["ok"] for r in results), "results": results}
        if identity is None and ledger is not None:
            response["credits"] = ledger.snapshot()  # Keep the legacy check-in response field.
        return response
    finally:
        gateway._HOUSEKEEP_LOCK.release()


def _one(gateway, pool, ledger, entry, action, *, automatic=False, consent_revision=None):
    if action == "trial":
        return trial_management.perform(gateway, pool, entry)
    cm, cid = entry["cm"], entry["id"]
    if not model_policy.credential_enabled(gateway.CONFIG, entry):
        return {"ok": False, "skipped": True, "message": "账号已人工停用"}
    if action in {"travel", "travel-status"} and not travel.supported(entry.get("profile")):
        return travel.unavailable()
    with cm._lock:
        if cm.summary().get("account_key") != entry.get("account_key"):
            return {"ok": False, "state": "changed", "message": "凭证身份已变化，请刷新列表"}
        if action == "refresh":
            with credential_file_lock(cm.path.parent, cm.path.name):
                if cm.summary().get("account_key") != entry.get("account_key"):
                    return {"ok": False, "message": "凭证身份已变化，请刷新列表"}
                cm._refresh_locked()
            generation = cm._generation
        else:
            headers = cm.get_headers()
            generation = cm._generation
    if action == "refresh":
        pool.reload([cm.path], reset=False)
        current = pool.apply_if_current(cm, generation, lambda: None)
        return {"ok": current, "message": "凭证已刷新" if current else "凭证已变化，请刷新列表核验"}
    if action in {"travel", "travel-status"}:
        def can_write():
            return ((not automatic or model_policy.credential_auto_travel(gateway.CONFIG, entry))
                    and pool.apply_if_current(cm, generation, lambda: None))
        result = travel.perform(gateway._bearer_token(headers), gateway.profile_for_headers(headers),
                                read_only=action == "travel-status", can_write=can_write,
                                buddy_context=gateway._buddy_context(entry, headers, consent_revision))
        if not pool.apply_if_current(cm, generation, lambda: travel.remember(ledger, cid, result)):
            return {**result, "ok": False, "stale": True, "message": "凭证已变化，旅行结果未写入，请刷新核验"}
        if automatic:
            buddy.daily_warning(gateway.CONFIG, entry.get("account_key"), entry.get("profile"), result)
        return result
    if action == "checkin":
        day = time.strftime("%Y-%m-%d")
        if ledger.checkin_done(cid, day):
            current = pool.apply_if_current(cm, generation, lambda: None)
            return {"ok": current, "already": current, "state": "already" if current else "changed",
                    "message": "今日已签到" if current else "凭证已变化，请刷新列表核验"}
        result = checkin.perform(gateway._bearer_token(headers),
            uid=headers.get("X-User-Id", ""), domain=headers.get("X-Domain", ""),
            can_claim=lambda: pool.apply_if_current(cm, generation, lambda: None))
        current = pool.apply_if_current(cm, generation, lambda: ledger.mark_checkin(
            cid, day, result["ok"], result.get("code"), result["message"], state=result["state"]))
        if not current:
            return checkin.normalize({"state": "changed"})
        return result
    failed = set()
    ref = gateway._sync_credits(pool, ledger, entry, checkin=False, failed=failed,
                                expected_identity=entry.get("account_key"))
    if ref is not None:
        if not pool.apply_if_current(cm, ref[1], lambda: entry.update(catalog_dirty=True)):
            failed.add(cid)
        else:
            gateway._sync_model_catalogs(pool, ledger, {cid: ref}, failed)
            failed.update(gateway._sync_usage(pool, entries=[entry], expected_identity=entry.get("account_key")))
    partial = bool((ledger.entry(cid).get("credits") or {}).get("partial")) or bool(
        (gateway.CONFIG.get("usage_daily_accounts") or {}).get(cid, {}).get("partial"))
    ok = ref is not None and cid not in failed and not partial
    if ok:
        def complete():
            pool._sync_pending.discard(cid)
            pool.end_sync({cid})
            if not pool._sync_pending:
                pool._sync_event.clear()
        ok = pool.apply_if_current(cm, ref[1], complete)
    return {"ok": ok, "partial": partial, "message": "余额、目录和用量已同步" if ok else "同步不完整，保留上次成功数据"}
