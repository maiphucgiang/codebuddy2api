"""Prepare the first Buddy with explicit consent, eligibility checks and durable write reservations."""
import hashlib
import json
import time

import httpx

from . import buddy_task
from .control_store import buddy_claim_reserved

HOST = "https://www.workbuddy.cn"
RETRY_SECONDS = 86400
MAX_RESPONSE_BYTES = 1024 * 1024
AGREEMENT_TITLE = "首领奖励领取确认协议"
AGREEMENT_TERMS = (
    "1. 用户确认当前账号为本人实际参与活动所使用的 WorkBuddy 账号，领取后的奖励与账号状态将进行绑定记录。",
    "2. 用户理解本次首领 Buddy 奖励为活动体验型内容，实际展示样式、发放顺序及后续联动规则，平台有权根据活动节奏进行调整。",
    "3. 用户同意在领取后，相关奖励状态、徽章点亮状态及页面展示进度会同步写入活动页，用于展示个人成长轨迹与后续任务解锁凭证。",
    "4. 如因账号异常、作弊行为、批量注册或非正常使用路径触发风控，平台有权取消领取资格并回收对应奖励权益。",
)
AUTHORIZATION = ("同意自动接取并完成 first_buddy 新手任务，再确认官方协议、领取猫猫及派遣。"
                 "必要时向当前账号的 WorkBuddy 发起一次独立文本对话，最多请求 32 个输出 token；"
                 "优先零倍率模型，否则使用最低已知倍率模型，可能消耗少量积分。"
                 "不调用工具、访问文件、开付费盒子或执行其他奖励任务；结果不确定不重复对话。")
AGREEMENT_REVISION = hashlib.sha256("\n".join((*AGREEMENT_TERMS, AUTHORIZATION)).encode()).hexdigest()
OFFICIAL_URL = "https://www.workbuddy.cn/profile/growth-center"
_ENDPOINTS = {
    "info": ("GET", "/activity/growth/buddy/info"),
    "list": ("GET", "/activity/growth/buddy/list"),
    "tasks": ("GET", "/v2/activity/growth/tasks"),
    "agreement": ("GET", "/activity/growth/buddy/agreement"),
    "agree": ("POST", "/activity/growth/buddy/agreement"),
    "first": ("POST", "/activity/growth/buddy/first"),
}
_MESSAGES = {
    "buddy_confirmation_required": "尚未领取猫猫，请确认首次领取后再派遣",
    "buddy_not_eligible": "首次领猫任务不存在、已锁定或状态不支持，未继续操作",
    "buddy_selection_required": "没有当前可用的猫猫，请到官方成长中心核验或选择已有 Buddy",
    "buddy_unknown": "猫猫资格或状态未确认，未继续操作，请稍后查询核验",
    "buddy_retry_later": "上次首领结果尚未确认或正在退避，请先核验猫猫状态，勿重复领取",
    "buddy_changed": "设置或凭证已变化，未继续领猫或派遣",
    "buddy_storage_error": "首领记录或审计无法保存，已停止后续操作，请检查存储状态",
    "buddy_write_unconfirmed": "首领操作结果未确认，未派遣；仅查询核验，不会自动重新领取",
    "buddy_reconciled": "猫猫已确认领取，本次未派遣，请查询旅行状态后继续",
}


class Failure(ValueError):
    def __init__(self, kind, status=None, code=None):
        super().__init__("Buddy response was not confirmed")
        self.details = {"error_kind": kind, "http_status": status, "code": code}


def _request(client, token, operation):
    method, path = _ENDPOINTS[operation]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "X-Product-Code": "workbuddy"}
    kwargs = {"json": {"agree": True}} if operation == "agree" else {}
    with client.stream(method, HOST + path, headers=headers, timeout=12, **kwargs) as response:
        raw = bytearray()
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                raise Failure("protocol", response.status_code)
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            raise Failure("protocol" if response.status_code == 200 else "http", response.status_code) from None
        code = payload.get("code") if isinstance(payload, dict) else None
        code = code if type(code) is int and -(2**31) <= code < 2**31 else None
        if response.status_code != 200:
            raise Failure("http", response.status_code, code)
        if code is None or code != 0:
            raise Failure("protocol" if code is None else "business", response.status_code, code)
        data = payload.get("data")
        if data is None and method == "POST":
            return {}
        if not isinstance(data, dict):
            raise Failure("protocol", response.status_code, code)
        return data


def auto_accept_from_env(environ):
    raw = environ.get("CODEBUDDY2API_AUTO_ACCEPT_BUDDY", "false").lower()
    if raw not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError("CODEBUDDY2API_AUTO_ACCEPT_BUDDY 必须是布尔值")
    return raw in {"true", "1", "yes", "on"}


def _active(data):
    if "buddy" not in data:
        raise Failure("protocol", 200, 0)
    value = data["buddy"]
    if value is None:
        return False
    identity = value.get("instance_id") if isinstance(value, dict) else None
    if type(identity) is int and 0 < identity < 2**63:
        return True
    if isinstance(identity, str) and identity.isascii() and identity.isdecimal() and 0 < len(identity) <= 19 and int(identity) > 0:
        return True
    raise Failure("protocol", 200, 0)


def confirmation(can_claim=False):
    return {"can_claim": can_claim, "revision": AGREEMENT_REVISION, "title": AGREEMENT_TITLE,
            "terms": list(AGREEMENT_TERMS), "authorization": AUTHORIZATION, "url": OFFICIAL_URL}


def audit_event(audit, identity, profile, stage, outcome, source=None, **fields):
    if audit is None:
        return False
    try:
        result = audit.event("admin", "buddy." + stage, {
            "credential": identity, "profile": profile, "stage": stage, "outcome": outcome,
            "consent_source": source, "agreement_revision": AGREEMENT_REVISION, **fields})
        return isinstance(result, dict) and result.get("ok") is True and result.get("recorded") is True
    except Exception:
        return False


def daily_warning(config, identity, profile, result):
    """Deduplicate one prerequisite warning per account and local calendar day in the audit store."""
    if not result.get("buddy_blocked") or not identity:
        return
    audit = config.get("audit_store")
    if audit is None:
        return
    event_id = "buddy-warning-" + hashlib.sha256((identity + ":" + time.strftime("%Y-%m-%d")).encode()).hexdigest()
    try:
        audit.event("runtime", "buddy.attention_required", {
            "event_id": event_id, "credential": identity, "profile": profile,
            "outcome": "warning", "stage": result.get("phase"), "code": result.get("reason"),
            "status_code": result.get("http_status")})
    except Exception:
        pass


def context(config, entry, consent_revision=None, *, headers=None, task_model=None):
    return {"store": config.get("control_store"), "audit": config.get("audit_store"),
            "identity": entry.get("account_key"), "profile": entry.get("profile"),
            "headers": dict(headers or {}), "task_model": task_model,
            "auto_accept": config.get("auto_accept_buddy") is True, "consent_revision": consent_revision}


def accept_consent(context, can_write):
    """Persist explicit account consent independently of upstream eligibility or availability."""
    result = {"ok": False, "buddy_consent_accepted": False, "phase": "buddy_consent", "buddy_blocked": True}
    try:
        if context.get("consent_revision") != AGREEMENT_REVISION or not can_write():
            return {**result, "reason": "buddy_changed", "message": _MESSAGES["buddy_changed"]}
        store, identity = context.get("store"), context.get("identity")
        if store is None or not identity:
            raise ValueError("Consent storage unavailable")
        if not store.has_buddy_consent(identity, AGREEMENT_REVISION):
            if not audit_event(context.get("audit"), identity, context.get("profile"), "consent", "success", "manual"):
                raise ValueError("Consent audit unavailable")
            if not can_write():
                return {**result, "reason": "buddy_changed", "message": _MESSAGES["buddy_changed"]}
            store.save_buddy_consent(identity, AGREEMENT_REVISION)
        return {"buddy_consent_accepted": True, "consent_source": "manual"}
    except Exception:
        return {**result, "reason": "buddy_storage_error", "message": _MESSAGES["buddy_storage_error"]}


def prepare(client, token, *, can_write, context=None):
    context = context or {}
    store, audit = context.get("store"), context.get("audit")
    identity, profile = context.get("identity"), context.get("profile")
    source = "manual" if context.get("consent_revision") == AGREEMENT_REVISION else (
        "environment" if context.get("auto_accept") is True else None)
    result = {"buddy_ready": False, "buddy_claimed": False, "agreement_accepted": False,
              "phase": "buddy_info", "auto_accept_buddy": context.get("auto_accept") is True}
    attempt = None

    def stop(reason, **fields):
        result.update(ok=False, skipped=True, buddy_blocked=True, reason=reason,
                      message=_MESSAGES[reason], **fields)
        if result.get("buddy_consent_accepted"):
            result["message"] = "同意已保存；" + result["message"]
        return result

    def checkpoint(stage, outcome):
        store.buddy_checkpoint(identity, attempt, stage, outcome,
                               agreed=result["agreement_accepted"], claimed=result["buddy_claimed"])

    def event(stage, outcome, **fields):
        return audit_event(audit, identity, profile, stage, outcome, source, **fields)

    try:
        if store is not None and identity and store.has_buddy_consent(identity, AGREEMENT_REVISION):
            result["buddy_consent_accepted"] = True
            source = source or "manual"
        if source is not None:
            result["consent_source"] = source
        active = _active(_request(client, token, "info"))
        previous = store.buddy_record(identity) if store is not None and identity else None
        if previous:
            result["agreement_accepted"] = bool(previous["agreed"])
        if active:
            if previous and previous["outcome"] != "success":
                attempt = previous["attempt_id"]
                source = previous["consent_source"]
                result["consent_source"] = source
                result["buddy_claimed"] = True
                result["agreement_accepted"] = bool(previous["agreed"])
                if not event("reconciled", "success"):
                    return stop("buddy_storage_error")
                checkpoint("reconciled", "success")
            result["buddy_ready"] = True
            return result
        result["buddy_required"] = True
        if source is None:
            result["buddy_confirmation"] = confirmation()
        result["phase"] = "buddy_list"
        inventory = _request(client, token, "list")
        rows, count = inventory.get("buddies"), inventory.get("count")
        if not isinstance(rows, list) or type(count) is not int or count < 0 or count < len(rows):
            raise Failure("protocol", 200, 0)
        if rows or count or previous and previous["claimed"]:
            return stop("buddy_selection_required")
        if buddy_claim_reserved(previous):
            return stop("buddy_write_unconfirmed", stale=True)
        if previous and previous["retry_at"] > time.time():
            return stop("buddy_retry_later", retry_at=previous["retry_at"])
        result["phase"] = "buddy_tasks"
        task = buddy_task.first_task(_request(client, token, "tasks"))
        if task is None:
            return stop("buddy_not_eligible")
        if source is not None:
            progress = buddy_task.perform(client, token, task, context=context, can_write=can_write,
                                          request=_request, event=event)
            result.update(progress)
            if not progress.get("buddy_task_completed"):
                if result.get("buddy_consent_accepted"):
                    result["message"] = "同意已保存；" + result["message"]
                return result
        elif task["accept_status"] != "completed":
            return stop("buddy_confirmation_required")
        else:
            result["buddy_task_completed"] = True
        result["phase"] = "buddy_agreement"
        agreed = _request(client, token, "agreement").get("agreed")
        if type(agreed) is not bool:
            raise Failure("protocol", 200, 0)
        result["agreement_accepted"] = agreed
        if source is None:
            result["buddy_confirmation"] = confirmation(True)
            return stop("buddy_confirmation_required")
        result["consent_source"] = source
        if not can_write():
            return stop("buddy_changed")
        if store is None or audit is None or not identity:
            return stop("buddy_storage_error")
        attempt = store.reserve_buddy(identity, source, AGREEMENT_REVISION, retry_seconds=RETRY_SECONDS)
        if attempt is None:
            return stop("buddy_retry_later")
        if not event("authorization_used", "success"):
            return stop("buddy_storage_error")
        if not agreed:
            result["phase"] = "buddy_agree"
            checkpoint("agree", "pending")
            if not can_write():
                return stop("buddy_changed")
            _request(client, token, "agree")
            result["agreement_accepted"] = True
            checkpoint("agree", "pending")
            if not event("agreement", "success"):
                return stop("buddy_storage_error")
        result["phase"] = "buddy_first"
        checkpoint("first", "pending")
        if not can_write():
            return stop("buddy_changed")
        _request(client, token, "first")
        result["buddy_claimed"] = True
        checkpoint("first", "pending")
        if not event("first", "success"):
            return stop("buddy_storage_error")
        result["phase"] = "buddy_verify"
        if not _active(_request(client, token, "info")):
            checkpoint("verify", "uncertain")
            return stop("buddy_write_unconfirmed", stale=True)
        checkpoint("verify", "success")
        result["buddy_ready"] = True
        return result
    except (httpx.HTTPError, Failure, buddy_task.TaskFailure) as error:
        details = error.details if isinstance(error, (Failure, buddy_task.TaskFailure)) else {
            "error_kind": "timeout" if isinstance(error, httpx.TimeoutException) else "network",
            "http_status": None, "code": None}
        if attempt:
            try:
                reconciled = False
                if result["phase"] == "buddy_agree":
                    try:
                        result["agreement_accepted"] = _request(client, token, "agreement").get("agreed") is True
                    except (httpx.HTTPError, Failure):
                        pass
                elif result["phase"] == "buddy_first":
                    try:
                        reconciled = _active(_request(client, token, "info"))
                        result["buddy_claimed"] = reconciled
                    except (httpx.HTTPError, Failure):
                        pass
                if reconciled:
                    checkpoint("reconciled", "success")
                    if not event("reconciled", "success"):
                        return stop("buddy_storage_error", stale=True)
                    return stop("buddy_reconciled", stale=False, **details)
                checkpoint(result["phase"], "uncertain")
                event("failed", "error", status_code=details["http_status"],
                      code=str(details["code"]) if details["code"] is not None else None)
            except Exception:
                return stop("buddy_storage_error", stale=True)
        return stop("buddy_write_unconfirmed" if attempt else "buddy_unknown", stale=True, **details)
    except Exception:
        return stop("buddy_storage_error", stale=True)
