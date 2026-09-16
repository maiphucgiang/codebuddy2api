"""Query domestic Buddy travel, claim arrivals, and dispatch only confirmed idle accounts."""
import math
import random
import time

import httpx

from . import buddy

HOST = "https://www.workbuddy.cn"
PREFIX = "/activity/growth/buddy/travel/"
TIMEOUT = 12.0


class _Failure(ValueError):
    def __init__(self, kind, http_status=200, code=0, reason=None):
        super().__init__("Travel response was not confirmed")
        self.diagnostics = {"error_kind": kind, "http_status": http_status, "code": code}
        if reason:
            self.diagnostics["reason"] = reason


def supported(profile):
    return profile in {"cn-cli", "cn-work"}


def unavailable():
    return {"ok": False, "skipped": True, "state": "unavailable", "message": "旅行仅适用于国内账号"}


def _number(value):
    return value if type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value) else None


def _location_id(value):
    return value if type(value) is int and 0 < value <= 2**31 - 1 else None


def _location_name(value):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 80 or any(ord(c) < 32 for c in value):
        return None
    return value.strip()


def _request(client, token, operation, *, body=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "X-Product-Code": "workbuddy"}
    if operation in {"status", "config"}:
        response = client.get(HOST + PREFIX + operation, headers=headers, timeout=TIMEOUT)
    else:
        response = client.post(HOST + PREFIX + operation, headers=headers, json={} if body is None else body, timeout=TIMEOUT)
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        raise _Failure("protocol" if status == 200 else "http", status, None) from None
    code = payload.get("code") if isinstance(payload, dict) else None
    code = code if type(code) is int and -(2**31) <= code < 2**31 else None
    message = payload.get("msg", "") if isinstance(payload, dict) else ""
    reason = next((value for text, value in {
        "no active buddy": "no_active_buddy", "daily limit": "daily_limit",
        "already traveling": "already_traveling", "location not available": "location_unavailable",
    }.items() if isinstance(message, str) and len(message) <= 512 and text in message.lower()), None)
    if status != 200:
        raise _Failure("http", status, code, reason)
    if code is None:
        raise _Failure("protocol", status, None)
    if code != 0:
        raise _Failure("business", status, code, reason)
    data = payload.get("data")
    # A successful claim may have no business data; reads still require a valid object.
    if data is None and operation in {"claim", "depart"}:
        return {}
    if not isinstance(data, dict):
        raise _Failure("protocol", status, code)
    return data


def _locations(data):
    rows = data.get("locations")
    if not isinstance(rows, list) or not 0 < len(rows) <= 100:
        raise _Failure("protocol")
    locations = {}
    for row in rows:
        identity = _location_id(row.get("id")) if isinstance(row, dict) else None
        name = _location_name(row.get("name")) if isinstance(row, dict) else None
        if identity is None or name is None or identity in locations:
            raise _Failure("protocol")
        locations[identity] = name
    return locations


def _status(data):
    state = data.get("state")
    if not isinstance(state, str) or state not in {"idle", "traveling", "arrived"}:
        raise _Failure("protocol")
    location = data.get("location")
    location_id = _location_id(location.get("id")) if isinstance(location, dict) else None
    location_name = _location_name(location.get("name")) if isinstance(location, dict) else None
    arrive_at, server_now = _number(data.get("arrive_at")), _number(data.get("server_now"))
    remaining = max(0, arrive_at - server_now) if state == "traveling" and arrive_at is not None and server_now is not None else None
    return {"state": state, "daily_limit_reached": data.get("daily_limit_reached")
            if type(data.get("daily_limit_reached")) is bool else None,
            "location_id": location_id, "location_name": location_name if location_id is not None else None,
            "reward_credit": _number(data.get("reward_credit")), "arrive_at": arrive_at,
            "server_now": server_now, "remaining_seconds": remaining}


def perform(token, profile, *, read_only=False, can_write=lambda: True, buddy_context=None):
    if not supported(profile):
        return unavailable()
    result = {"ok": False, "state": "unknown", "claimed": False, "departed": False, "stale": False, "phase": "status"}
    phase = "status"
    context = buddy_context or {}
    store, identity = context.get("store"), context.get("identity")
    write_attempt, write_operation, write_sent = None, None, False
    observed_attempt = None

    def write_store(method, *args, **kwargs):
        if store is None or not identity:
            raise _Failure("storage", None, None)
        try:
            return getattr(store, method)(identity, *args, **kwargs)
        except Exception:
            raise _Failure("storage", None, None) from None

    def pending_write(record):
        nonlocal phase
        operation = record["operation"] if record else write_operation
        label, flag = ("领取", "claimed") if operation == "claim" else ("派遣", "departed")
        phase = "after_" + operation
        confirmed = bool(record and record["confirmed"])
        pending_key = "claim_pending" if operation == "claim" else "departure_pending"
        result.update(ok=False, skipped=True, stale=True, **{flag: confirmed, pending_key: True},
                      message="上次" + label + ("已确认，但状态尚未更新" if confirmed else "结果尚未确认") + "；仅查询核验，勿重复操作")
        return result

    def start_write(operation, location_id=None):
        nonlocal write_attempt, write_operation, write_sent
        write_operation, write_sent = operation, False
        write_attempt = write_store("reserve_travel_write", operation, location_id, expected_attempt=observed_attempt)
        if write_attempt is None:
            pending_write(write_store("travel_write_record"))
            return False
        try:
            allowed = can_write()
        except Exception:
            write_store("transition_travel_write", write_attempt, "cancelled")
            raise _Failure("storage", None, None) from None
        if not allowed:
            write_store("transition_travel_write", write_attempt, "cancelled")
            result.update(skipped=True, message="设置或凭证已变化，未发送" + ("领取" if operation == "claim" else "派遣") + "请求")
            return False
        write_store("transition_travel_write", write_attempt, "sent")
        write_sent = True
        return True

    if not read_only and not can_write():
        return {**result, "skipped": True, "message": "设置或凭证已变化，未执行旅行操作"}
    if not read_only and buddy_context and buddy_context.get("consent_revision") is not None:
        accepted = buddy.accept_consent(buddy_context, can_write)
        result.update(accepted)
        if not accepted["buddy_consent_accepted"]:
            return result
    try:
        def record_departure(stage, outcome):
            if not result.get("buddy_claimed"):
                return True
            context = buddy_context or {}
            return buddy.audit_event(context.get("audit"), context.get("identity"), profile, stage, outcome,
                                     result.get("consent_source"))
        with httpx.Client(follow_redirects=False) as client:
            previous = write_store("travel_write_record") if store is not None and identity else None
            observed_attempt = previous["attempt_id"] if previous else None
            result.update(_status(_request(client, token, "status")))
            if previous and previous["phase"] not in {"cancelled", "reconciled"}:
                pending_state = "arrived" if previous["operation"] == "claim" else "idle"
                if previous["phase"] == "reserved" or result["state"] == pending_state:
                    return pending_write(previous)
                write_store("transition_travel_write", previous["attempt_id"], "reconciled")
            if read_only:
                result.update(ok=True, message={"idle": "Buddy 空闲", "traveling": "Buddy 旅行中", "arrived": "Buddy 已到达，待领取"}[result["state"]])
                return result
            if result["state"] == "arrived":
                phase = "claim"
                if not start_write("claim"):
                    return result
                receipt = _request(client, token, "claim")
                result.update(claimed=True, claimed_credit=_number(receipt.get("reward_credit")))
                write_store("transition_travel_write", write_attempt, "confirmed")
                phase = "after_claim"
                result.update(_status(_request(client, token, "status")))
                if result["state"] == "arrived":
                    result.update(stale=True, message="领取已确认，但状态尚未更新，未派出")
                    return result
                write_store("transition_travel_write", write_attempt, "reconciled")
                observed_attempt = write_attempt
            prefix = "旅行积分已领取；" if result["claimed"] else ""
            if result["state"] == "traveling":
                result.update(ok=True, skipped=True, message=prefix + "Buddy 旅行中，无需派遣")
                return result
            if result["daily_limit_reached"] is True:
                result.update(ok=True, skipped=True, message=prefix + "今日派遣已达上限")
                return result
            if result["daily_limit_reached"] is not False:
                result.update(stale=True, message=prefix + "派遣上限状态未知，未派出")
                return result
            if not can_write():
                result.update(skipped=True, message=prefix + "设置或凭证已变化，未发送派遣请求")
                return result
            prepared = buddy.prepare(client, token, can_write=can_write, context=buddy_context)
            phase = prepared["phase"]
            result.update(prepared)
            if not prepared["buddy_ready"]:
                result["message"] = prefix + prepared["message"]
                return result
            if prepared.get("buddy_claimed"):
                phase = "buddy_verify"
                result.update(_status(_request(client, token, "status")))
                if result["state"] != "idle" or result["daily_limit_reached"] is not False:
                    result.update(ok=result["state"] != "idle" or result["daily_limit_reached"] is True,
                                  skipped=True, message="猫猫已领取，旅行状态已变化，请先查询核验")
                    return result
            if not can_write():
                result.update(skipped=True, message=prefix + "设置或凭证已变化，未发送派遣请求")
                return result
            phase = "config"
            locations = _locations(_request(client, token, "config"))
            location_id = random.choice(tuple(locations))
            if not can_write():
                result.update(skipped=True, message=prefix + "设置或凭证已变化，未发送派遣请求")
                return result
            phase = "depart"
            if not record_departure("departure_requested", "pending"):
                result.update(buddy_blocked=True, reason="buddy_storage_error",
                              message="猫猫已领取，但派遣审计无法保存，未发送派遣请求")
                return result
            if not start_write("depart", location_id):
                return result
            receipt = _request(client, token, "depart", body={"location_id": location_id})
            # The action is confirmed, but its current state requires a fresh read.
            result.update(departed=True, state="unknown", stale=True, daily_limit_reached=None,
                          location_id=location_id, location_name=locations[location_id], reward_credit=None,
                          arrive_at=_number(receipt.get("arrive_at")), server_now=None, remaining_seconds=None)
            write_store("transition_travel_write", write_attempt, "confirmed")
            if not record_departure("departure", "success"):
                result.update(buddy_blocked=True, reason="buddy_storage_error",
                              message="派遣已确认，但审计无法保存，请查询最新状态，勿重复派遣")
                return result
            phase = "after_depart"
            result.update(_status(_request(client, token, "status")))
            if result["state"] == "idle":
                result.update(message=prefix + "派遣已确认，但状态仍为空闲；请先查询核验，勿重复派出")
                return result
            write_store("transition_travel_write", write_attempt, "reconciled")
            if result["location_name"] is None:
                result["location_name"] = locations.get(result["location_id"])
            result.update(ok=True, stale=False, message=prefix + (
                "Buddy 已到达，待领取" if result["state"] == "arrived" else "Buddy 已派出，余额可另行同步"))
            return result
    except (httpx.HTTPError, ValueError, TypeError) as error:
        rejected = isinstance(error, _Failure) and (error.diagnostics.get("reason") is not None or (
            error.diagnostics["error_kind"] == "http" and error.diagnostics["http_status"] in {401, 403, 429}))
        confirmed = result["claimed"] if write_operation == "claim" else result["departed"]
        if write_attempt and not confirmed and (not write_sent or rejected or isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))):
            try:
                write_store("transition_travel_write", write_attempt, "cancelled")
            except _Failure:
                pass
        if result.get("buddy_claimed") and phase in {"depart", "after_depart"}:
            record_departure("departure_failed", "error")
        messages = {"status": "旅行状态查询失败，未执行写操作", "claim": "领取结果未确认，未派出；下次先查询状态",
                    "after_claim": "领取已确认，后续状态查询失败，未派出",
                    "buddy_verify": "猫猫已领取，后续旅行状态查询失败，未派遣",
                    "config": ("旅行积分已领取；" if result["claimed"] else "") + "地点配置查询失败，未派出",
                    "depart": ("旅行积分已领取；" if result["claimed"] else "") + "派遣结果未确认；下次先查询状态",
                    "after_depart": ("旅行积分已领取；" if result["claimed"] else "") + "派遣已确认，后续状态查询失败；勿重复派出"}
        diagnostics = error.diagnostics if isinstance(error, _Failure) else {
            "error_kind": "timeout" if isinstance(error, httpx.TimeoutException) else "network"
            if isinstance(error, httpx.HTTPError) else "protocol", "http_status": None, "code": None}
        result.update(ok=False, stale=True, message=messages.get(phase, "猫猫准备状态未确认，未派遣"), **diagnostics)
        refusal = {"no_active_buddy": "官方拒绝派遣：没有当前可用猫猫，请先领取或选择 Buddy",
                   "daily_limit": "官方拒绝派遣：今日派遣已达上限",
                   "already_traveling": "官方拒绝派遣：猫猫已在旅行，请查询最新状态",
                   "location_unavailable": "官方拒绝派遣：该地点暂时不可用"}.get(result.get("reason"))
        if phase == "depart" and refusal:
            result["message"] = ("旅行积分已领取；" if result["claimed"] else "") + refusal
        if diagnostics["error_kind"] == "storage":
            receipt = ("领取已确认，但" if write_operation == "claim" else "派遣已确认，但") if confirmed else ""
            result["message"] = receipt + "旅行状态记录不可用，已停止后续操作；请先查询核验"
        return result
    finally:
        result["phase"] = phase


def remember(ledger, cid, result):
    record = {**result, "at": time.time()}
    previous = ledger.entry(cid).get("travel") or {}
    if not result.get("ok"):
        known = previous if previous.get("ok") else previous.get("last_success")
        if known:
            record["last_success"] = {key: value for key, value in known.items() if key != "last_success"}
    ledger.update_travel(cid, record)
