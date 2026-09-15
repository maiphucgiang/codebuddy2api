"""Scoped manual credential maintenance without implicit check-in or trial claims."""
import time
from pathlib import Path

from fastapi import HTTPException

from . import checkin, model_policy, travel
from .credential_io import credential_file_lock


def run(gateway, action, identity=None):
    if action not in {"refresh", "checkin", "sync", "travel", "travel-status"} or (action in {"refresh", "travel", "travel-status"} and identity is None):
        raise HTTPException(404, "凭证操作不存在")
    config = gateway.CONFIG
    pool, ledger = config.get("cred_pool"), config.get("ledger")
    if pool is None or (action != "refresh" and (ledger is None or gateway.credits_mod is None)):
        raise HTTPException(503, "凭证维护尚未就绪")
    # Do not queue duplicate manual work behind the periodic maintenance sweep.
    if not gateway._HOUSEKEEP_LOCK.acquire(blocking=False):
        raise HTTPException(409, "凭证维护正在执行，请稍后刷新列表核验")
    try:
        pool._rescan()
        entries = [dict(e) for e in pool.entries() if identity is None or e.get("account_key") == identity]
        if identity is not None and not entries:
            raise HTTPException(404, "凭证不存在或身份已变化")
        results = []
        for entry in entries:
            result = {"id": entry.get("account_key"), "name": Path(entry["id"]).name, "action": action, "ok": False}
            if not model_policy.credential_enabled(config, entry):
                result.update(skipped=True, message="账号已人工停用")
            else:
                try:
                    result.update(_one(gateway, pool, ledger, entry, action))
                    if (action == "checkin" and result.get("state") not in {"changed", "cancelled"}
                            and model_policy.credential_auto_travel(config, entry)):
                        followup = _one(gateway, pool, ledger, entry, "travel", automatic=True)
                        result.update(travel=followup, checkin_ok=result["ok"], ok=result["ok"] and followup["ok"],
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
                    audit.event("admin", "credential." + action, {"credential": result["id"], "ok": result["ok"]})
                except Exception:
                    pass
        response = {"ok": bool(results) and all(r["ok"] for r in results), "results": results}
        if identity is None and ledger is not None:
            response["credits"] = ledger.snapshot()  # Keep the legacy check-in response field.
        return response
    finally:
        gateway._HOUSEKEEP_LOCK.release()


def _one(gateway, pool, ledger, entry, action, *, automatic=False):
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
                                read_only=action == "travel-status", can_write=can_write)
        if not pool.apply_if_current(cm, generation, lambda: travel.remember(ledger, cid, result)):
            return {"ok": False, "message": "凭证已变化，旅行结果未写入，请刷新核验"}
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
    ref = gateway._sync_credits(pool, ledger, entry, checkin=False, claim_trial=False, failed=failed,
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
