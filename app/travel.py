"""Domestic Buddy travel: query first, claim arrivals, then dispatch only confirmed idle accounts."""
import math
import random
import time

import httpx

HOST = "https://www.workbuddy.cn"
PREFIX = "/activity/growth/buddy/travel/"
TIMEOUT = 12.0
LOCATIONS = {1: "咖啡馆", 2: "商场店铺", 3: "健身房", 4: "古镇客栈"}


def supported(profile):
    return profile in {"cn-cli", "cn-work"}


def unavailable():
    return {"ok": False, "skipped": True, "state": "unavailable", "message": "旅行仅适用于国内账号"}


def _number(value):
    return value if type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value) else None


def _request(client, token, operation):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if operation == "status":
        response = client.get(HOST + PREFIX + operation, headers=headers, timeout=TIMEOUT)
    else:
        body = {"location_id": random.choice(tuple(LOCATIONS))} if operation == "depart" else {}
        response = client.post(HOST + PREFIX + operation, headers=headers, json=body, timeout=TIMEOUT)
    payload = response.json()
    if (response.status_code != 200 or not isinstance(payload, dict)
            or type(payload.get("code")) is not int or payload["code"] != 0
            or not isinstance(payload.get("data"), dict)):
        raise ValueError("Travel response was not confirmed")
    return payload["data"]


def _status(data):
    state = data.get("state")
    if state not in {"idle", "traveling", "arrived"}:
        raise ValueError("Travel state missing")
    location = data.get("location")
    location_id = location.get("id") if isinstance(location, dict) else None
    if type(location_id) is not int or location_id not in LOCATIONS:
        location_id = None
    return {"state": state, "daily_limit_reached": data.get("daily_limit_reached")
            if type(data.get("daily_limit_reached")) is bool else None,
            "location_id": location_id, "location_name": LOCATIONS.get(location_id),
            "reward_credit": _number(data.get("reward_credit")),
            "arrive_at": _number(data.get("arrive_at")), "server_now": _number(data.get("server_now"))}


def perform(token, profile, *, read_only=False, can_write=lambda: True):
    if not supported(profile):
        return unavailable()
    result = {"ok": False, "state": "unknown", "claimed": False, "departed": False, "stale": False}
    phase = "status"
    if not read_only and not can_write():
        return {**result, "skipped": True, "message": "设置或凭证已变化，未执行旅行操作"}
    try:
        with httpx.Client(follow_redirects=False) as client:
            result.update(_status(_request(client, token, "status")))
            if read_only:
                result.update(ok=True, message={"idle": "Buddy 空闲", "traveling": "Buddy 旅行中", "arrived": "Buddy 已到达，待领取"}[result["state"]])
                return result
            if result["state"] == "arrived":
                if not can_write():
                    result.update(skipped=True, message="设置或凭证已变化，未发送领取请求")
                    return result
                phase = "claim"
                receipt = _request(client, token, "claim")
                result.update(claimed=True, claimed_credit=_number(receipt.get("reward_credit")))
                phase = "after_claim"
                result.update(_status(_request(client, token, "status")))
                if result["state"] == "arrived":
                    result.update(stale=True, message="领取已确认，但状态尚未更新，未派出")
                    return result
            prefix = "旅行积分已领取；" if result["claimed"] else ""
            if result["state"] == "traveling":
                result.update(ok=True, skipped=True, message=prefix + "Buddy 旅行中，无需派遣")
                return result
            if result["daily_limit_reached"] is True:
                result.update(ok=True, skipped=True, message=prefix + "今日派遣已达上限")
                return result
            if result["daily_limit_reached"] is not False:
                result.update(message=prefix + "派遣上限状态未知，未派出")
                return result
            if not can_write():
                result.update(skipped=True, message=prefix + "设置或凭证已变化，未发送派遣请求")
                return result
            phase = "depart"
            receipt = _request(client, token, "depart")
            result.update(ok=True, departed=True, state="traveling", stale=False,
                          arrive_at=_number(receipt.get("arrive_at")), server_now=_number(receipt.get("server_now")),
                          message=prefix + "Buddy 已派出，余额可另行同步")
            location = receipt.get("location")
            location_id = location.get("id") if isinstance(location, dict) else None
            result.update(location_id=location_id if type(location_id) is int and location_id in LOCATIONS else None)
            result["location_name"] = LOCATIONS.get(result["location_id"])
            return result
    except (httpx.HTTPError, ValueError, TypeError):
        messages = {"status": "旅行状态查询失败，未执行写操作", "claim": "领取结果未确认，未派出；下次先查询状态",
                    "after_claim": "领取已确认，后续状态查询失败，未派出", "depart": "派遣结果未确认；下次先查询状态"}
        result.update(ok=False, stale=True, message=messages[phase])
        return result


def remember(ledger, cid, result):
    record = {**result, "at": time.time()}
    previous = ledger.entry(cid).get("travel") or {}
    if not result.get("ok"):
        known = previous if previous.get("ok") else previous.get("last_success")
        if known:
            record["last_success"] = {key: value for key, value in known.items() if key != "last_success"}
    ledger.update_travel(cid, record)
