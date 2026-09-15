"""Activity-gated check-in with safe status labels shared by manual and scheduled work."""
from . import credits

MESSAGES = {
    "success": "签到成功，余额可另行同步",
    "already": "今日已签到",
    "inactive": "签到活动未开放或已结束",
    "not_eligible": "当前账号不具备签到资格",
    "unknown": "活动状态未确认，未尝试领取",
    "error": "签到未确认成功，请核验账号状态",
    "cancelled": "自动签到设置或凭证已变化，已取消未发送的领取",
    "changed": "凭证已变化，结果未写入，请刷新核验",
}


def normalize(result):
    state = result.get("state")
    if state not in MESSAGES:
        state = "already" if result.get("already") else "success" if result.get("ok") is True else "error"
    return {"state": state, "ok": state in {"success", "already"}, "already": state == "already",
            "skipped": state in {"inactive", "not_eligible", "cancelled"},
            "code": result.get("code"), "message": MESSAGES[state]}


def perform(access_token, uid="", domain="", *, can_claim=lambda: True):
    if not can_claim():
        return normalize({"state": "cancelled"})
    status = credits.fetch_checkin_status(access_token, uid=uid, domain=domain)
    if status.get("state") != "available":
        return normalize(status)
    # A setting change, account disable or new credential generation during the query cancels the claim.
    if not can_claim():
        return normalize({"state": "cancelled"})
    return normalize(credits.daily_checkin(access_token, uid=uid, domain=domain))


def view(record):
    if not record:
        return {"state": "unknown", "message": "尚未查询签到活动"}
    if record.get("state") in MESSAGES:
        result = normalize(record)
    elif record.get("ok") is True:
        result = normalize({"state": "success"})
    else:
        result = normalize(credits.classify_checkin_result(False, record.get("code"), record.get("message", "")))
        if result["ok"]:
            result = normalize({"state": "unknown"})
    return {"state": result["state"], "message": result["message"], "date": record.get("date"), "at": record.get("at")}
