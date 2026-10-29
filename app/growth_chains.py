"""Growth-task automation chains: one runner per task code, driven manually or by the sweep.

Every runner is idempotent from the task-state side (claimed/locked tasks are skipped) and
stops after the upstream progress reaches its target. Report hosts follow the panel-verified
split: desktop fingerprint to the CLI domain, web/browser events to the account domain,
mini-program fingerprint to the CodeBuddy CN domain. Upstream scoring is asynchronous, so
runners poll the task list within a bounded budget before claiming.
"""
from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime

from . import growth_events, growth_tasks

# 面板实测：桌面 /v2/report 走 CLI 域；mp 指纹走 CodeBuddy CN 域。
DESKTOP_FALLBACK_BASE = "https://copilot.tencent.com"
MP_BASE = "https://www.codebuddy.cn"
MP_PLATFORM = growth_tasks.MP_PLATFORM
# mp 对话事件反作弊：连发先计数后整体回滚（面板 2026-09-26 实测 2s 连发全灭）。
MP_CHAT_GAP_S = 45.0
EXPERT_SUMMON_GAP_S = 6.0
ACCEPT_VERIFY_GAP_S = 2.0
APPEARANCE_SETTLE_S = 2.0
POLL_ATTEMPTS = 4
POLL_GAP_S = 3.0
CLAIMED_INCOMPLETE = "reward_already_claimed"

_LOCKS_GUARD = threading.Lock()
_BUSY: set[str] = set()

SCHOOL_ACTIVITY_ID = "school_open_day_2026"
LIBRARY_URL = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"
QQ_BUDDY_ID = "cb_y5Dy46tPQGGWtueMxXbe"
QQ_BUDDY_NAME = "企鹅教师助手"
LIGHTHOUSE_EXPERT_ID = "ex_2cvvUZQhDyeJ"
NAMED_EXPERT_TASKS = {
    "WorkBuddy_Valorant": {
        "expert_id": "ex_xQHHD1u5Jkua", "expert_type": "agent",
        "display_name_zh": "无畏契约拼豆专家", "profession_zh": "无畏契约拼豆专家",
        "version": "1.2.7", "categories": ["03-GameSpatial", "06-ContentCreative"]},
    "expert_use_bowangwang": {
        "expert_id": "ex_WZY136wV5aMt", "expert_type": "agent",
        "display_name_zh": "播旺旺", "profession_zh": "直播电商教练",
        "version": "1.0.2", "categories": ["05-MarketingGrowth", "18-Double11"]},
    "expert_use_douyinyin": {
        "expert_id": "ex_FbY5nlWTqDod", "expert_type": "agent",
        "display_name_zh": "斗音音", "profession_zh": "抖音策略师",
        "version": "1.0.2", "categories": ["06-ContentCreative", "18-Double11"]},
    "expert_use_maidehao": {
        "expert_id": "ex_GDl31WahyIH0", "expert_type": "agent",
        "display_name_zh": "卖得好", "profession_zh": "中国电商运营专家",
        "version": "1.0.1", "categories": ["05-MarketingGrowth", "18-Double11"]},
    "expert_use_shangjia_xuanchuan": {
        "expert_id": "ex_Weo3x2EZuSDm", "expert_type": "agent",
        "display_name_zh": "商家宣传设计师", "profession_zh": "商家宣传页与海报设计师",
        "version": "1.6.0", "categories": ["01-ProductDesign", "05-MarketingGrowth", "18-Double11"]},
}
SKILL_NAME = "润泽小馆·日报撰写"
SKILL_ID = "skill_2097350077599879168"
THEME_KEY = "theme-tkmw7j"
DEFAULT_CHAT_MODEL = "glm-5.2"


def try_lock(account_key):
    with _LOCKS_GUARD:
        if account_key in _BUSY:
            return False
        _BUSY.add(account_key)
        return True


def unlock(account_key):
    with _LOCKS_GUARD:
        _BUSY.discard(account_key)


def in_night_window(now=None):
    hour = (now or datetime.now()).hour
    return hour >= 23 or hour < 8


class Context:
    """One account's upstream context for chain execution."""

    def __init__(self, *, base, token, uid, domain="", nickname="", headers=None, lock_key=None):
        self.base = base
        self.token = token
        self.uid = uid
        self.domain = domain
        self.nickname = nickname
        self.headers = headers or {}
        self.lock_key = lock_key or uid


def _find_task(tasks, code):
    return next((task for task in tasks if task["task_code"] == code), None)


def _task_with_poll(ctx, code, *, attempts=POLL_ATTEMPTS):
    """Read one task, polling within budget because upstream scores asynchronously."""
    tasks = growth_tasks.merged_tasks(ctx.token, ctx.uid, ctx.domain)
    task = _find_task(tasks, code)
    if task is None or task["claimable"] or task["claimed"]:
        return task
    for _ in range(attempts - 1):
        time.sleep(POLL_GAP_S)
        task = _find_task(growth_tasks.merged_tasks(ctx.token, ctx.uid, ctx.domain), code)
        if task is None or task["claimable"] or task["claimed"]:
            break
    return task


def _report_desktop(ctx, events):
    try:
        growth_events.report_desktop(ctx.base, ctx.token, ctx.uid, ctx.nickname,
                                     ctx.domain, events)
    except growth_events.Failure as error:
        # 桌面通道在 CLI 域（面板实测口径）；账号域拒绝时按实测基址兜底。
        if ctx.base != DESKTOP_FALLBACK_BASE and error.details.get("error_kind") in ("http", "protocol"):
            growth_events.report_desktop(DESKTOP_FALLBACK_BASE, ctx.token, ctx.uid,
                                         ctx.nickname, "", events)
            return
        raise


def _claim_if_ready(ctx, code, task, message):
    if task is None:
        return message + "（任务不存在）"
    if task["claimed"]:
        return message + "；奖励此前已领取"
    if not task["claimable"]:
        return (message + f"；进度 {task['current']}/{task['target']}（异步计分未归账，下次重试）")
    credit, energy, _ = growth_tasks.claim_reward(
        ctx.token, code, ctx.uid, ctx.domain,
        platform=MP_PLATFORM if code in growth_tasks.KNOWN_TASK_CODES
        and growth_tasks.GROUPS.get(code) == "miniprogram" else growth_tasks.WEB_PLATFORM)
    return message + f"；已领奖 +{credit}c +{energy}e"


def _ensure_accepted(ctx, task, code):
    if task["locked"]:
        return "任务未解锁（按官方节奏逐日开放），已跳过"
    if task["accept_status"] in ("", "not_accepted"):
        platform = MP_PLATFORM if growth_tasks.GROUPS.get(code) == "miniprogram" else growth_tasks.WEB_PLATFORM
        growth_tasks.accept_tasks(ctx.token, [code], ctx.uid, ctx.domain, platform=platform)
        return None
    return None


def _need(task, default_target):
    target = task["target"] or default_target
    return max(0, target - task["current"]), target


# ---------------------------------------------------------------------------
# B 组：行为事件链（零对话）
# ---------------------------------------------------------------------------

def run_chat_5(ctx, task):
    need, target = _need(task, 5)
    if need <= 0:
        return "进度已达标，无需上报"
    for index in range(need):
        growth_events.report_chat_activity(
            ctx.base, ctx.token, ctx.uid, f"wb2api-chat5-{int(time.time() * 1000)}-{index}",
            nickname=ctx.nickname, domain=ctx.domain)
        if index < need - 1:
            time.sleep(growth_events.REPORT_GAP_S)
    return f"已补报 {need} 条对话活跃事件（目标 {target}）"


def run_rich_meow(ctx, task):
    stamp = int(time.time() * 1000)
    _report_desktop(ctx, growth_events.chat_sequence(f"wb2api-rm-{stamp}", f"wb2api-rm-req-{stamp}",
                                                     f"req-{stamp}-user"))
    return "已上报桌面端完整对话事件链"


def run_buddy_app(ctx, task):
    _report_desktop(ctx, growth_events.buddy_app_sequence(QQ_BUDDY_ID, QQ_BUDDY_NAME))
    return "已上报 Buddy 应用进入五连事件（同时覆盖 Buddy_App 与 Buddy_App_QQ）"


def run_automation_1(ctx, task):
    _report_desktop(ctx, [growth_events.automation_create_event("wb2api 自动化")])
    return "已上报定时任务创建事件"


def run_library_read(ctx, task):
    growth_events.report_web(ctx.base, ctx.token, ctx.uid, "web_element_click", LIBRARY_URL,
                             "library_doc_intro_click", "WorkBuddy资料库介绍",
                             nickname=ctx.nickname)
    return "已上报资料库阅读事件"


def run_template_5(ctx, task):
    need, _target = _need(task, 5)
    if need <= 0:
        return "进度已达标，无需上报"
    templates = [("1", "深度研究"), ("2", "周报生成"), ("3", "竞品分析"), ("4", "活动策划"), ("5", "代码评审")]
    for index in range(min(need, len(templates))):
        stamp = int(time.time() * 1000)
        template_id, template_name = templates[index]
        _report_desktop(ctx, growth_events.template_use_sequence(
            f"wb2api-tpl-{stamp}-{index}", f"wb2api-tpl-req-{stamp}-{index}",
            template_id, template_name))
        if index < need - 1:
            time.sleep(0.3)
    return f"已上报 {min(need, len(templates))} 组模板使用事件"


def run_playbook_prompt(ctx, task):
    stamp = int(time.time() * 1000)
    _report_desktop(ctx, growth_events.playbook_prompt_sequence(
        f"wb2api-pb-{stamp}", f"wb2api-pb-req-{stamp}",
        "pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸"))
    return "已上报灵感案例做同款事件组"


def run_create_canvas(ctx, task):
    stamp = int(time.time() * 1000)
    _report_desktop(ctx, growth_events.design_canvas_sequence(
        f"wb2api-canvas-{stamp}", f"wb2api-canvas-req-{stamp}"))
    return "已上报设计画布创建事件组"


def run_hp_appearance(ctx, task):
    growth_events.set_appearance_theme(ctx.base, ctx.token, ctx.uid, THEME_KEY)
    time.sleep(APPEARANCE_SETTLE_S)
    _report_desktop(ctx, [growth_events.appearance_apply_event(THEME_KEY)])
    return "已设置主题并上报皮肤生效事件"


# ---------------------------------------------------------------------------
# C 组：真实对话链（消耗账号额度）
# ---------------------------------------------------------------------------

def _real_chat(ctx, expert_id="", model=DEFAULT_CHAT_MODEL):
    headers = {**ctx.headers, "Authorization": f"Bearer {ctx.token}"}
    return growth_events.desktop_chat(ctx.base, headers, expert_id=expert_id, model=model)


def run_model_chat_glm(ctx, task):
    message = _ensure_accepted(ctx, task, "Model_chat_GLM5.2") if task else None
    if message:
        return message
    _conversation, request_id = _real_chat(ctx, model="glm-5.2")
    time.sleep(growth_events.REPORT_GAP_S)
    growth_events.report_chat_activity(ctx.base, ctx.token, ctx.uid,
                                       f"wb2api-glm52-{int(time.time() * 1000)}",
                                       request_id, "glm-5.2", "GLM-5.2",
                                       nickname=ctx.nickname, domain=ctx.domain)
    return "已完成 glm-5.2 真实对话并上报"


def run_skill_1(ctx, task):
    conversation_id, request_id = _real_chat(ctx)
    message_id = "msg-" + request_id[len(request_id) - 8:]
    events = growth_events.chat_sequence(conversation_id, request_id, message_id)
    for event in events:
        if event["eventCode"] == "chat_message_response":
            event["finishReason"] = "tool_calls"   # 模型发起工具调用（技能加载）语义
    events.append(growth_events.skill_info_event(conversation_id, request_id, message_id,
                                                 SKILL_NAME, SKILL_ID))
    _report_desktop(ctx, events)
    return "已上报真实对话 + skill_info 技能加载事件"


def _run_expert_batch(ctx, expert_type, count):
    experts = growth_events.market_expert_list(ctx.base, ctx.token, ctx.uid, expert_type,
                                               domain=ctx.domain)
    if not experts:
        return "专家市场列表为空，跳过以防半程态"
    ok = 0
    for index, expert in enumerate(experts):
        if ok >= count:
            break
        _report_desktop(ctx, growth_events.expert_summon_sequence(expert))
        try:
            conversation_id, request_id = _real_chat(ctx, expert_id=expert["expert_id"])
        except growth_events.Failure:
            continue     # 单个专家失败继续下一个
        events = growth_events.chat_sequence(conversation_id, request_id,
                                             "msg-" + request_id[len(request_id) - 8:])
        events.append(growth_events.expert_actual_use(expert, conversation_id, request_id))
        _report_desktop(ctx, events)
        ok += 1
        if index < len(experts) - 1 and ok < count:
            time.sleep(EXPERT_SUMMON_GAP_S)
    return f"已对 {ok}/{count} 位真实{('专家团' if expert_type == 'team' else '专家')}完成召唤+使用链"


def run_expert_5(ctx, task):
    return _run_expert_batch(ctx, "agent", 5)


def run_expert_team_3(ctx, task):
    return _run_expert_batch(ctx, "team", 3)


def _run_named_expert(ctx, expert, *, local=False):
    expert = dict(expert)
    try:
        for candidate in growth_events.market_expert_list(ctx.base, ctx.token, ctx.uid, "agent",
                                                          domain=ctx.domain):
            if candidate.get("expert_id") == expert["expert_id"]:
                expert = candidate
                break
    except growth_events.Failure:
        pass  # Fall back to the verified task-specific id when the market is unavailable.
    _report_desktop(ctx, growth_events.expert_summon_sequence(expert))
    conversation_id, request_id = _real_chat(ctx, expert_id=expert["expert_id"])
    events = growth_events.chat_sequence(conversation_id, request_id,
                                         "msg-" + request_id[len(request_id) - 8:])
    for event in events:
        if event["eventCode"] == "agent_task_created":
            event.update(has_expert=True, expert_id=expert["expert_id"],
                         expert_name=expert.get("display_name_zh") or "", expert_industry_id="")
    events.append(growth_events.expert_actual_use(
        expert, conversation_id, request_id, mode="LOCAL" if local else "craft"))
    _report_desktop(ctx, events)
    return f"已上报「{expert.get('display_name_zh') or expert['expert_id']}」召唤+使用链（真实对话 requestId）"


def run_expert_lighthouse(ctx, task):
    _run_named_expert(ctx, {
        "expert_id": LIGHTHOUSE_EXPERT_ID, "expert_type": "agent",
        "display_name_zh": "腾讯轻量云专家", "profession_zh": "腾讯轻量云专家",
        "version": "1.0.2", "categories": []}, local=True)
    return "已上报轻量云专家召唤+使用链（真实对话 requestId）"


def run_workbuddy_valorant(ctx, task):
    return _run_named_expert(ctx, NAMED_EXPERT_TASKS["WorkBuddy_Valorant"])


def run_expert_bowangwang(ctx, task):
    return _run_named_expert(ctx, NAMED_EXPERT_TASKS["expert_use_bowangwang"])


def run_expert_douyinyin(ctx, task):
    return _run_named_expert(ctx, NAMED_EXPERT_TASKS["expert_use_douyinyin"])


def run_expert_maidehao(ctx, task):
    return _run_named_expert(ctx, NAMED_EXPERT_TASKS["expert_use_maidehao"])


def run_expert_shangjia_xuanchuan(ctx, task):
    return _run_named_expert(ctx, NAMED_EXPERT_TASKS["expert_use_shangjia_xuanchuan"])


def run_black_cat(ctx, task):
    if not in_night_window():
        return "当前不在 23:00–08:00 计数窗口，行为不计分；窗口内自动补足"
    need, _target = _need(task, 1)
    if need <= 0:
        return "进度已达标，无需补足"
    done = 0
    for index in range(need):
        try:
            _real_chat(ctx, model="glm-5.2", prompt="1+1等于几？直接回答。")
            growth_events.report_chat_activity(ctx.base, ctx.token, ctx.uid,
                                               f"wb2api-night-{int(time.time() * 1000)}-{index}",
                                               "", "glm-5.2", "GLM-5.2",
                                               nickname=ctx.nickname, domain=ctx.domain)
        except growth_events.Failure as error:
            return f"完成 {done}/{need} 次后中断: {error}"
        done += 1
        if index < need - 1:
            time.sleep(4)
    return f"已完成 {done} 次夜间对话并上报"


# ---------------------------------------------------------------------------
# D 组：小程序口径（mp 指纹 + mp 头全链路）
# ---------------------------------------------------------------------------

def _mp_fingerprint(uid, nickname=""):
    return {"timestamp": int(time.time() * 1000), "ideType": "WorkBuddy_MP",
            "ideVersion": "2.4.0", "extName": "workbuddy-mp", "extVersion": "2.4.0",
            "product": "SaaS", "ideName": "wx_app_cloud", "platform": "mini_program",
            "os": "windows", "osVersion": "11", "arch": "x64",
            "machineId": "0655736a-607f-4d9d-b430-58176ee9a090",
            "timezone": "Asia/Shanghai", "userId": uid, "userNickname": nickname}


def _report_mp(ctx, events):
    if not events:
        raise growth_events.Failure("protocol", message="no events")
    common = _mp_fingerprint(ctx.uid, ctx.nickname)
    payload = [{**common, **event} for event in events]
    headers = {"Authorization": f"Bearer {ctx.token}", "Content-Type": "application/json",
               "Accept": "application/json", "X-User-Id": ctx.uid,
               "X-Client-Product": "workbuddy-mp", "X-Client-Version": "2.4.0",
               "X-Client-Platform": "mp-weixin", "X-Platform": "wechatmp"}
    growth_events._post_json(MP_BASE, growth_events.REPORT_PATH, headers, payload)


def _mini_chat_event(conversation_id, *, activity_id=None, model=None):
    request_id = "wb2api-" + secrets_token()
    event = {"eventCode": "chat_request_send", "inputLength": 14, "isPlan": False,
             "isAutoExecuteTerminal": False, "isAutoModify": False, "codebaseEnable": False,
             "maxToken": 0, "maxSteps": 500, "temperature": 0, "maxRetries": 0,
             "mentionContexts": [], "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
             "mentionContextCount": 0, "command": "", "recommendId": "", "skillId": "",
             "skillCount": 0, "totalCount": 0, "traceId": request_id,
             "rootRequestId": request_id, "parentConversationId": conversation_id,
             "conversationId": conversation_id, "messageId": "msg-" + request_id[len(request_id) - 8:],
             "agentName": "mp", "agentType": "main",
             "codebuddy.session_id": conversation_id,
             "codebuddy.conversation_request_id": request_id}
    if activity_id:
        event["activityId"] = activity_id
    if model:
        event["requestModelId"] = model
        event["requestModelName"] = model
    return event


def secrets_token():
    return secrets.token_hex(8)


def _mp_task(ctx, code):
    tasks = growth_tasks.list_tasks(ctx.token, ctx.uid, ctx.domain, platform=MP_PLATFORM)
    return _find_task(tasks, code)


def _accept_with_verify_mp(ctx, code):
    for _attempt in (1, 2):
        growth_tasks.accept_tasks(ctx.token, [code], ctx.uid, ctx.domain, platform=MP_PLATFORM)
        time.sleep(ACCEPT_VERIFY_GAP_S)
        task = _mp_task(ctx, code)
        if task is not None and task["accept_status"] not in ("", "not_accepted"):
            return task
    return None


def _run_mp_mini_chat(ctx, code, *, activity_id=None, model=None):
    task = _mp_task(ctx, code)
    if task is None:
        return "mp 口径未下发该任务（活动可能已结束）"
    if task["claimed"]:
        return "已领取"
    if task["accept_status"] in ("", "not_accepted"):
        verified = _accept_with_verify_mp(ctx, code)
        if verified is None:
            return "accept 未登记生效（上游 200+OK 但未落账形态），待下次重试"
        task = verified     # accept 前 target 恒为 0，回读拿真实目标
    target = task["target"] or 1
    if task["current"] >= target or task["accept_status"] == "completed":
        credit, energy, _ = growth_tasks.claim_reward(ctx.token, code, ctx.uid, ctx.domain,
                                                      platform=MP_PLATFORM)
        return f"已领取奖励（+{credit}c +{energy}e）"
    need = target - task["current"]
    for index in range(need):
        time.sleep(MP_CHAT_GAP_S + (index % 4))
        conversation_id = f"wb2api-mp-{int(time.time() * 1000)}-{index}"
        _report_mp(ctx, [_mini_chat_event(conversation_id, activity_id=activity_id, model=model)])
    task = _mp_task(ctx, code) or task
    for _ in range(2):
        time.sleep(POLL_GAP_S)
        task = _mp_task(ctx, code) or task
        if task["claimable"] or task["claimed"] or task["current"] >= target:
            break
    if task["claimed"]:
        return "本轮已入账（claimed）"
    if task["current"] < target:
        return f"已上报 {need} 次但进度 {task['current']}/{target}（异步计分未归账，下次重试）"
    credit, energy, _ = growth_tasks.claim_reward(ctx.token, code, ctx.uid, ctx.domain,
                                                  platform=MP_PLATFORM)
    return f"任务点亮并领取奖励（+{credit}c +{energy}e）"


def _run_mp_event_task(ctx, code, events_builder):
    task = _mp_task(ctx, code)
    if task is None:
        return "mp 口径未下发该任务（活动可能已结束）"
    if task["claimed"]:
        return "已领取"
    locked = task["locked"]
    if not locked and task["accept_status"] in ("", "not_accepted"):
        verified = _accept_with_verify_mp(ctx, code)
        if verified is None:
            return "accept 未登记生效（上游 200+OK 但未落账形态），待下次重试"
    elif locked:
        return "任务未解锁（按官方节奏逐日开放），已跳过"
    target = task["target"] or 1
    if task["current"] >= target or task["accept_status"] == "completed":
        credit, energy, _ = growth_tasks.claim_reward(ctx.token, code, ctx.uid, ctx.domain,
                                                      platform=MP_PLATFORM)
        return f"已领取奖励（+{credit}c +{energy}e）"
    _report_mp(ctx, events_builder())
    task = _mp_task(ctx, code) or task
    for _ in range(2):
        time.sleep(POLL_GAP_S)
        task = _mp_task(ctx, code) or task
        if task["claimable"] or task["claimed"] or task["current"] >= target:
            break
    if task["claimable"] or task["accept_status"] == "completed":
        credit, energy, _ = growth_tasks.claim_reward(ctx.token, code, ctx.uid, ctx.domain,
                                                      platform=MP_PLATFORM)
        return f"任务点亮并领取奖励（+{credit}c +{energy}e）"
    return f"已上报但进度 {task['current']}/{target}（异步计分未归账，下次重试）"


def run_school_season(ctx, task):
    return _run_mp_mini_chat(ctx, "school_season", activity_id=SCHOOL_ACTIVITY_ID)


def run_sequential_1(ctx, task):
    return _run_mp_mini_chat(ctx, "Sequential_Tasks_1")


def run_sequential_2(ctx, task):
    def events():
        experts = growth_events.market_expert_list(ctx.base, ctx.token, ctx.uid, "agent",
                                                   domain=ctx.domain)
        if not experts:
            raise growth_events.Failure("protocol", message="专家市场不可用")
        expert = experts[0]
        name = expert.get("display_name_zh") or expert.get("profession_zh") or expert["expert_id"]
        return [{"eventCode": "expert_actual_use", "reportDelay": 0, "extVersion": "2.2.8",
                 "source": "mini_program", "id": expert["expert_id"], "name": expert["expert_id"],
                 "expertTitle": name, "type": "send_message", "characterCount": 12,
                 "expertType": expert.get("expert_type") or "agent"}]
    return _run_mp_event_task(ctx, "Sequential_Tasks_2", events)


def run_sequential_3(ctx, task):
    return _run_mp_mini_chat(ctx, "Sequential_Tasks_3")


def run_sequential_4(ctx, task):
    def events():
        return [_mini_chat_event(f"wb2api-mp-auto-{int(time.time() * 1000)}"),
                growth_events.automation_create_event("wb2api 自动化")]
    return _run_mp_event_task(ctx, "Sequential_Tasks_4", events)


def run_sequential_5(ctx, task):
    return _run_mp_mini_chat(ctx, "Sequential_Tasks_5", model="glm-5.2")


def run_sequential_6(ctx, task):
    return _run_mp_mini_chat(ctx, "Sequential_Tasks_6")


def run_sequential_7(ctx, task):
    def events():
        payload = {"id": "pm-gtm-launch-plan", "name": "新产品上市 GTM 发布计划一页纸",
                   "type": "document", "categoryId": "", "categoryName": "",
                   "skills": "", "skillNames": ""}
        return [{"eventCode": "playbook_cta_click", "source": "discover", "position": 1,
                 "extVersion": "2.2.8", **payload},
                {"eventCode": "playbook_prompt_send", "source": "discover", "promptLength": 96,
                 "isOfficial": 1, "conversationId": "wb2api-mp-pb-" + secrets_token(),
                 "extVersion": "2.2.8", **payload}]
    return _run_mp_event_task(ctx, "Sequential_Tasks_7", events)


RUNNERS = {
    "chat_5": run_chat_5, "RichMeow_Chat": run_rich_meow, "Buddy_App": run_buddy_app,
    "Buddy_App_QQ": run_buddy_app, "automation_1": run_automation_1,
    "Library_read": run_library_read, "template_5": run_template_5,
    "playbook_prompt": run_playbook_prompt, "create_canvas": run_create_canvas,
    "Hp_Appearance": run_hp_appearance,
    "Model_chat_GLM5.2": run_model_chat_glm, "skill_1": run_skill_1, "expert_5": run_expert_5,
    "Expert_team_use_3": run_expert_team_3, "Expert_lighthouse": run_expert_lighthouse,
    "WorkBuddy_Valorant": run_workbuddy_valorant,
    "expert_use_bowangwang": run_expert_bowangwang,
    "expert_use_douyinyin": run_expert_douyinyin,
    "expert_use_maidehao": run_expert_maidehao,
    "expert_use_shangjia_xuanchuan": run_expert_shangjia_xuanchuan,
    "black_cat": run_black_cat,
    "school_season": run_school_season, "Sequential_Tasks_1": run_sequential_1,
    "Sequential_Tasks_2": run_sequential_2, "Sequential_Tasks_3": run_sequential_3,
    "Sequential_Tasks_4": run_sequential_4, "Sequential_Tasks_5": run_sequential_5,
    "Sequential_Tasks_6": run_sequential_6, "Sequential_Tasks_7": run_sequential_7,
}


def run(code, ctx):
    """Run one chain with the account mutex held; returns the human-readable outcome."""
    runner = RUNNERS.get(code)
    if runner is None:
        return {"ok": False, "skipped": True, "message": "该任务暂无自动链"}
    if not try_lock(ctx.lock_key):
        return {"ok": False, "skipped": True, "message": "该账号有任务动作正在执行中，请稍后再试"}
    try:
        tasks = growth_tasks.merged_tasks(ctx.token, ctx.uid, ctx.domain)
        task = _find_task(tasks, code)
        if task is None:
            return {"ok": False, "skipped": True, "message": "该账号没有此任务"}
        if task["claimed"]:
            return {"ok": True, "skipped": True, "message": "该任务已领取过奖励"}
        message = runner(ctx, task)
        after = _task_with_poll(ctx, code, attempts=3)
        result = {"ok": True, "message": message,
                  "progress_after": (f"{after['current']}/{after['target']}"
                                     if after and after["target"] else None)}
        if after is not None and after["claimable"]:
            credit, energy, already = growth_tasks.claim_reward(
                ctx.token, code, ctx.uid, ctx.domain,
                platform=MP_PLATFORM if growth_tasks.GROUPS.get(code) == "miniprogram"
                else growth_tasks.WEB_PLATFORM)
            result["claimed"] = True
            result["message"] = message + ("；奖励此前已领取" if already
                                            else f"；已自动领奖 +{credit}c +{energy}e")
        elif after is not None and after.get("claimable") is False:
            result["claimable"] = False
        return result
    finally:
        unlock(ctx.lock_key)
