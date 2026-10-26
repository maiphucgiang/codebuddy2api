"""WorkBuddy growth-center tasks: dual-platform listing, acceptance and reward claiming.

Claim judgement follows the progress contract only (``!claimed && target>0 &&
current>=target``): ``accept_status`` briefly flips to ``completed`` upstream and then
rolls back, so it must never trigger a claim. The mini-program (mp) task list is a
superset of the default one; callers merge by task_code and submit accept/claim through
the matching platform header, because mixing them makes mp tasks fail with
``task not found``.
"""
from __future__ import annotations

import json
from urllib.parse import quote

import httpx

HOST = "https://www.workbuddy.cn"
TASKS_PATH = "/v2/activity/growth/tasks"
ACCEPT_PATH = "/v2/activity/growth/tasks/accept"
TIMEOUT = 12.0
MAX_RESPONSE_BYTES = 1 << 20
MP_PLATFORM = "miniprogram"
WEB_PLATFORM = "web"

# 任务分组与中文标签（存储键=上游 task_code；UI 改名只动这里）。
GROUPS = {
    "chat_5": "behavior", "RichMeow_Chat": "behavior", "Buddy_App": "behavior",
    "Buddy_App_QQ": "behavior", "automation_1": "behavior", "Library_read": "behavior",
    "template_5": "behavior", "playbook_prompt": "behavior", "create_canvas": "behavior",
    "Hp_Appearance": "behavior",
    "Model_chat_GLM5.2": "chat", "skill_1": "chat", "expert_5": "chat",
    "Expert_team_use_3": "chat", "Expert_lighthouse": "chat", "black_cat": "chat",
    "school_season": "miniprogram", "Sequential_Tasks_1": "miniprogram",
    "Sequential_Tasks_2": "miniprogram", "Sequential_Tasks_3": "miniprogram",
    "Sequential_Tasks_4": "miniprogram", "Sequential_Tasks_5": "miniprogram",
    "Sequential_Tasks_6": "miniprogram", "Sequential_Tasks_7": "miniprogram",
}
KNOWN_TASK_CODES = frozenset(GROUPS)
GROUP_LABELS = {"behavior": "行为上报", "chat": "真实对话", "miniprogram": "小程序"}


class Failure(ValueError):
    def __init__(self, kind, status=None, code=None, message=""):
        super().__init__(message or f"growth {kind}")
        self.details = {"error_kind": kind, "http_status": status, "code": code}


def supported(profile, enterprise=False):
    """Growth center serves personal CN accounts (cn-cli and cn-work), like travel."""
    from . import travel     # 函数内导入：control_store 经本模块加载，避免环。
    return profile in travel.PERSONAL_PROFILES and not enterprise


def _headers(token, uid="", domain="", *, platform=WEB_PLATFORM, claim=False):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/plain, */*",
               "Content-Type": "application/json", "x-client-platform": platform,
               "X-Product-Code": "workbuddy"}
    if claim and platform == WEB_PLATFORM:
        # Web 成长中心领奖的浏览器形状；缺 Origin/Referer 时实测会被拒。
        headers["Origin"] = HOST
        headers["Referer"] = HOST + "/profile/growth-center"
    if uid:
        headers["X-User-Id"] = uid
    if domain:
        headers["X-Domain"] = domain
    return headers


def _growth_request(client, token, uid, domain, method, path, *, body=None, platform=WEB_PLATFORM,
                    claim=False):
    headers = _headers(token, uid, domain, platform=platform, claim=claim)
    with client.stream(method, HOST + path, headers=headers,
                       json=body if body is not None else None, timeout=TIMEOUT) as response:
        raw = bytearray()
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                raise Failure("protocol", response.status_code)
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            if response.status_code != 200:
                raise Failure("http", response.status_code) from None
            raise Failure("protocol", response.status_code) from None
        code = payload.get("code") if isinstance(payload, dict) else None
        message = str(payload.get("msg") or "")[:200] if isinstance(payload, dict) else ""
        if response.status_code != 200:
            raise Failure("http", response.status_code, code if type(code) is int else None, message)
        if code != 0:
            raise Failure("business", response.status_code,
                          code if type(code) is int else None, message)
        data = payload.get("data")
        return data if data is not None else {}


def _number(value):
    return value if type(value) is int and -(2**31) <= value < 2**31 else 0


def _parse_task(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("task_code"), str):
        return None
    current, target = _number(raw.get("current")), _number(raw.get("target"))
    progress = raw.get("progress")
    if isinstance(progress, dict):
        # progress 可能是 {current,target} 对象（实测口径），覆盖平铺字段。
        obj_current, obj_target = _number(progress.get("current")), _number(progress.get("target"))
        if obj_target > 0 or obj_current > 0:
            current, target = obj_current, obj_target
    claimed = raw.get("accept_status") == "claimed"
    return {"task_code": raw["task_code"], "title": raw.get("title") or "",
            "description": raw.get("description") or raw.get("task_desc") or "",
            "credit": _number(raw.get("reward_credit")), "energy": _number(raw.get("reward_energy")),
            "locked": raw.get("locked") is True, "accept_status": raw.get("accept_status") or "",
            "target": target, "current": current,
            "claimable": (not claimed and target > 0 and current >= target), "claimed": claimed,
            "group": GROUPS.get(raw["task_code"], "other")}


def list_tasks(token, uid="", domain="", *, platform=WEB_PLATFORM):
    """Fetch one platform's task list; returns normalized task dicts."""
    with httpx.Client(follow_redirects=False) as client:
        data = _growth_request(client, token, uid, domain, "GET", TASKS_PATH, platform=platform)
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        raise Failure("protocol")
    return [task for task in (_parse_task(item) for item in tasks) if task is not None]


def merged_tasks(token, uid="", domain=""):
    """Merge the mp superset with the default list by task_code; mp entries win."""
    merged = {task["task_code"]: task for task in list_tasks(token, uid, domain)}
    for task in list_tasks(token, uid, domain, platform=MP_PLATFORM):
        merged[task["task_code"]] = task
    return sorted(merged.values(), key=lambda task: task["task_code"])


def accept_tasks(token, codes, uid="", domain="", *, platform=WEB_PLATFORM):
    if not isinstance(codes, list) or not codes or not all(isinstance(code, str) for code in codes):
        raise Failure("protocol")
    with httpx.Client(follow_redirects=False) as client:
        _growth_request(client, token, uid, domain, "POST", ACCEPT_PATH,
                        body={"task_codes": codes}, platform=platform)


def claim_reward(token, code, uid="", domain="", *, platform=WEB_PLATFORM):
    """Claim one reward; returns (credit, energy, already_claimed)."""
    path = "/activity/growth/tasks/" + quote(code, safe="") + "/claim"
    with httpx.Client(follow_redirects=False) as client:
        try:
            data = _growth_request(client, token, uid, domain, "POST", path,
                                   body=None, platform=platform, claim=True)
        except Failure as error:
            # mp 口径在部分任务/租户形态 400：降级 Web 域（面板实测可领）。
            if platform == WEB_PLATFORM or error.details.get("http_status") != 400:
                raise
            return claim_reward(token, code, uid, domain, platform=WEB_PLATFORM)
    already = data.get("already_claimed") is True if isinstance(data, dict) else False
    return _number(data.get("credit")) if isinstance(data, dict) else 0, \
        _number(data.get("energy")) if isinstance(data, dict) else 0, already


MP_ONLY_CODES = frozenset({"wb_wechat_oa_subscribe_task"})


def sweep_account(token, uid="", domain=""):
    """Idempotent accept/claim pass over both platforms; runs no behavior chains.

    Returns a bounded summary; single-task failures are counted and never abort the pass.
    """
    accepted, claimed, credit, energy, errors = [], [], 0, 0, 0
    try:
        tasks = merged_tasks(token, uid, domain)
    except Failure as error:
        return {"ok": False, "error": error.details, "accepted": [], "claimed": [],
                "credit": 0, "energy": 0, "errors": 1}
    for task in tasks:
        code = task["task_code"]
        platform = MP_PLATFORM if code in MP_ONLY_CODES or task.get("group") == "miniprogram" else WEB_PLATFORM
        try:
            if not task["locked"] and not task["claimed"] and task["accept_status"] in ("", "not_accepted"):
                accept_tasks(token, [code], uid, domain, platform=platform)
                accepted.append(code)
            if task["claimable"]:
                got_credit, got_energy, _already = claim_reward(token, code, uid, domain, platform=platform)
                claimed.append(code)
                credit += got_credit
                energy += got_energy
        except Failure as error:
            errors += 1
            if error.details.get("code") == 11140:
                return {"ok": False, "error": error.details, "accepted": accepted, "claimed": claimed,
                        "credit": credit, "energy": energy, "errors": errors}
    return {"ok": errors == 0, "accepted": accepted, "claimed": claimed, "credit": credit,
            "energy": energy, "errors": errors}
