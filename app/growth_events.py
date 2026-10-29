"""Growth-center behavior chains: desktop/web event reporting and real-chat helpers.

Payload shapes mirror the official WorkBuddy desktop client (5.9.3) field-for-field, as
validated by the upstream panel across multiple live accounts. Report hosts follow the
account's own product endpoint; the desktop fingerprint is stable per uid so repeated
chains look like one persistent device.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time

import httpx

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) WorkBuddyDesktop/5.9.3 "
              "Chrome/148.0.7778.97 Electron/41.8.1 Safari/537.36")
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
REPORT_PATH = "/v2/report"
APPEARANCE_PATH = "/v2/user-asset/appearance/set"
MARKET_PATH = "/portal/operation-platform/market/expert/list"
FAST_MODEL = "fast-model"
REPORT_GAP_S = 1.05
SERVER_ID_RE = re.compile(r"^(cmb-)?[0-9a-f]{32}$")


class Failure(ValueError):
    def __init__(self, kind, status=None, code=None, message=""):
        super().__init__(message or f"growth event {kind}")
        self.details = {"error_kind": kind, "http_status": status, "code": code}


def derive_id(uid, salt):
    """Stable per-account device identifiers: one uid always yields the same device."""
    digest = hashlib.sha256(f"{salt}:{uid}".encode("utf-8")).hexdigest()
    return digest[:36]


def fingerprint(uid, nickname=""):
    now = int(time.time() * 1000)
    return {"timezone": "Asia/Shanghai", "reportDelay": 2000, "userId": uid,
            "username": nickname, "userNickname": nickname, "product": "SaaS",
            "releaseDate": 1789632000000,
            "commit": "c41253385fa07bebfcae8a7a4728914f09af96d0",
            "ideName": "WorkBuddy", "ideType": "WorkBuddy", "ideVersion": "5.9.3",
            "machineId": derive_id(uid, "machine"), "sessionId": derive_id(uid, "session"),
            "extName": "workbuddy-desktop", "extVersion": "5.9.3",
            "os": "win32", "arch": "x64", "osVersion": "10.0.26200",
            "cpuCores": 24, "memorySize": 32, "timestamp": now, "presentAt": now}


def _post_json(base, path, headers, payload):
    with httpx.Client(follow_redirects=False) as client:
        response = client.post(base + path, headers=headers, json=payload, timeout=15)
    if response.status_code != 200:
        raise Failure("http", response.status_code)
    try:
        body = response.json()
    except ValueError:
        raise Failure("protocol", response.status_code) from None
    if isinstance(body, dict) and body.get("code") not in (None, 0):
        raise Failure("business", response.status_code, body.get("code"),
                      str(body.get("msg") or "")[:200])
    return body.get("data") if isinstance(body, dict) else body


def report_desktop(base, token, uid, nickname="", domain="", events=()):
    """Report desktop-fingerprint events to {base}/v2/report; business fields win over fingerprint."""
    if not events:
        raise Failure("protocol", message="no events")
    common = fingerprint(uid, nickname)
    payload = [{**common, **event} for event in events]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/plain, */*",
               "Content-Type": "application/json;charset=UTF-8", "User-Agent": DESKTOP_UA,
               "X-Domain": base, "X-Product": "SaaS",
               "X-Request-ID": derive_id(uid, "req") + f"{int(time.time() * 1000) % 1000000:06d}",
               "X-User-Id": uid}
    if domain:
        headers["X-Domain"] = domain or base
    _post_json(base, REPORT_PATH, headers, payload)


def report_web(base, token, uid, event_code, page_url, element_id, element_name,
               nickname="", enterprise_id=""):
    """Report one browser-shaped event to the web domain (Library_read and page-click tasks)."""
    event = {"eventCode": event_code, "timestamp": int(time.time() * 1000), "reportDelay": 0,
             "pageURL": page_url, "elementId": element_id, "elementName": element_name,
             "os": "Win32", "arch": "", "osVersion": "10.0", "userAgent": WEB_UA,
             "machineId": derive_id(uid, "webmachine"), "userId": uid,
             "userNickname": nickname, "enterpriseId": enterprise_id}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json", "x-client-platform": "web",
               "Origin": base, "Referer": page_url, "User-Agent": WEB_UA, "X-User-Id": uid}
    _post_json(base, REPORT_PATH, headers, [event])


def chat_request_event(uid, conversation_id, request_id="", model_id="", model_name=""):
    """Full official chat_request_send shape; the server does not verify session existence."""
    request_id = request_id or conversation_id
    model_id = model_id or "deepseek-v4-flash"
    now = int(time.time() * 1000)
    return {"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
            "mode": "craft", "conversationId": conversation_id, "requestId": request_id,
            "inputLength": 12, "requestModelId": model_id,
            "requestModelName": model_name or model_id, "isPlan": False,
            "isAutoExecuteTerminal": False, "isAutoModify": False, "codebaseEnable": False,
            "maxToken": 0, "maxSteps": 0, "temperature": 0.7, "maxRetries": 2,
            "mentionContexts": [], "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
            "mentionContextCount": 0, "command": "", "expertId": "", "recommendId": "",
            "skillId": "", "skillCount": 0, "totalCount": 0, "fileUri": "",
            "presentAt": now, "traceId": "", "rootRequestId": request_id,
            "parentConversationId": conversation_id, "agentName": "default",
            "agentType": "conversation", "device_id": uid, "device_type": "desktop",
            "sdk_version": "5.9.3", "chat_session_uuid": conversation_id,
            "platform": "windows", "title": "对话", "userId": uid}


def report_chat_activity(base, token, uid, conversation_id, request_id="", model_id="",
                         model_name="", nickname="", domain=""):
    report_desktop(base, token, uid, nickname, domain,
                   [chat_request_event(uid, conversation_id, request_id, model_id, model_name)])


def chat_sequence(conversation_id, request_id, message_id, model_id=FAST_MODEL,
                  model_name=FAST_MODEL):
    """Six-event desktop conversation chain: created → send → request → response → status → done."""
    stamp = int(time.time() * 1000)
    common = {"traceId": request_id, "rootRequestId": request_id,
              "parentConversationId": conversation_id, "agentName": "cli", "agentType": "main"}
    session = {"codebuddy.session_id": conversation_id,
               "codebuddy.conversation_request_id": request_id}
    return [
        {"eventCode": "agent_task_created", "source": "LOCAL", "name": "working",
         "task_target": "local", "mode": "craft", "requestModelId": model_id,
         "requestModelName": model_name, "has_repo": False, "repo_type": "none",
         "workspace_type": "empty", "has_connector": False, "connector_types": [],
         "has_mention": False, "mention_types": [], "has_template": False, "action": "",
         "template_name": "", "has_expert": False, "expert_id": "", "expert_name": "",
         "expert_industry_id": "", "has_skill": False, "skill_names": [],
         "conversationId": conversation_id, "messageId": message_id, "buddyId": "", "buddyName": ""},
        {"eventCode": "chat_message_send", "messageId": message_id + "-assistant",
         "historyCount": 0, "isContextTruncated": False, "currentStepCount": 1,
         **common},
        {"eventCode": "chat_request_send", "inputLength": 24, "isPlan": False,
         "isAutoExecuteTerminal": False, "isAutoModify": False, "codebaseEnable": False,
         "maxToken": 0, "maxSteps": 500, "temperature": 0, "maxRetries": 0,
         "mentionContexts": [], "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
         "mentionContextCount": 0, "command": "", "recommendId": "", "skillId": "",
         "skillCount": 0, "totalCount": 0, **common, **session},
        {"eventCode": "chat_message_response", "messageId": message_id + "-assistant",
         "responseModelId": model_id, "inputToken": 120, "outputToken": 80, "totalToken": 200,
         "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
         "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
         "firstTokenAt": stamp, "conversationId": conversation_id, **common, **session},
        {"eventCode": "chat_message_status", "messageId": message_id + "-assistant",
         "messageErrorCode": "0", **common},
        {"eventCode": "chat_request_response", "mode": "craft", "toolCallCount": 0,
         "inputToken": 120, "outputToken": 80, "totalToken": 200, "cachedTokens": 0,
         "cachedWriteTokens": 0, "cachedMissTokens": 0, "isSuccessful": True,
         "messageErrorCode": "", "finishReason": "stop", "rootRequestId": request_id,
         "parentConversationId": conversation_id},
    ]


def buddy_app_sequence(buddy_id, buddy_name):
    """Five-event Buddy-app entry chain (covers Buddy_App and Buddy_App_QQ together)."""
    def event(code, extra=None):
        return {"eventCode": code, "mode": "LOCAL", "buddyId": buddy_id, "buddyName": buddy_name,
                **(extra or {})}
    return [event("buddyapp_discover_click"),
            event("buddyapp_show", {"elementId": buddy_id, "elementName": buddy_name, "position": 2}),
            event("buddyapp_enter_click", {"elementId": buddy_id, "elementName": buddy_name,
                                           "position": 2, "isFirstPage": "1"}),
            event("buddyapp_auth_confirm_click", {"elementId": buddy_id, "elementName": buddy_name}),
            event("buddyapp_bindaccount_skip_click", {"elementId": buddy_id, "elementName": buddy_name})]


def automation_create_event(name):
    return {"eventCode": "automated_task_create_suc", "name": name, "source": "manually",
            "modelId": FAST_MODEL, "modelIsThinking": True, "connectorCount": 0,
            "skills": "", "skillCount": 0, "scheduleType": "once", "mode": "LOCAL"}


def template_use_sequence(conversation_id, request_id, template_id, template_name):
    events = chat_sequence(conversation_id, request_id, "msg-" + template_id)
    events += [{"eventCode": "agent_task_created_with_template", "mode": "working",
                "isCustomModel": False, "id": template_id, "name": template_name,
                "requestId": request_id},
               {"eventCode": "template_used", "template_id": template_id, "task_mode": "working"}]
    return events


def playbook_prompt_sequence(conversation_id, request_id, case_id, case_name):
    events = chat_sequence(conversation_id, request_id, "msg-pb")
    payload = {"id": case_id, "name": case_name, "type": "document",
               "categoryId": "", "categoryName": ""}
    events += [{"eventCode": "web_element_click", "pageName": "playbook_detail",
                "elementId": "playbook_ctaClick", "elementName": case_name, "source": "discover"},
               {"eventCode": "playbook_cta_click", "source": "discover", "position": 0, **payload},
               {"eventCode": "playbook_prompt_send", "conversationId": conversation_id,
                "requestId": request_id, **payload}]
    return events


def design_canvas_sequence(conversation_id, request_id):
    events = chat_sequence(conversation_id, request_id, "msg-canvas")
    events += [{"eventCode": "wbx_design_canvas_task_create", "conversationId": conversation_id,
                "requestId": request_id, "source": "summon_keyword", "cost": 12000,
                "isSuccessful": True},
               {"eventCode": "wbx_design_canvas_open", "conversationId": conversation_id,
                "requestId": request_id, "id": "ardot-file-" + request_id[len(request_id) - 8:],
                "source": "summon_keyword", "type": "page", "cost": 13000, "isSuccessful": True}]
    return events


def appearance_apply_event(theme_key):
    return {"eventCode": "appearance_skin_apply", "action": "apply", "source": "settings_close",
            "id": theme_key, "vipLevel": 0, "series": "", "type": "unknown"}


def set_appearance_theme(base, token, uid, resource_key):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/plain, */*",
               "Content-Type": "application/json;charset=UTF-8", "User-Agent": DESKTOP_UA,
               "X-Product": "SaaS", "X-User-Id": uid}
    _post_json(base, APPEARANCE_PATH, headers, {"kind": "theme", "resource_key": resource_key})


def skill_info_event(conversation_id, request_id, message_id, skill_name, skill_id,
                     version="1.0.0", file_count=56):
    return {"eventCode": "skill_info", "id": skill_name, "skillId": skill_id,
            "skillVersion": version, "toolStatus": "success", "fileCount": file_count,
            "source": "workbuddy-desktop", "conversationId": conversation_id,
            "requestId": request_id, "messageId": message_id,
            "requestModelId": FAST_MODEL, "requestModelName": FAST_MODEL,
            "traceId": request_id}


def market_expert_list(base, token, uid, expert_type="", domain=""):
    """Real marketplace experts; fabricated ids never count for expert tasks."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "User-Agent": DESKTOP_UA, "X-Domain": domain or base, "X-Product": "SaaS",
               "X-User-Id": uid}
    body = {"page": 1, "page_size": 20, "sort_by": "reco_rank", "sort_order": "desc"}
    if expert_type:
        body["expert_type"] = expert_type
    data = _post_json(base, MARKET_PATH, headers, body)
    experts = data.get("experts") if isinstance(data, dict) else None
    if not isinstance(experts, list):
        raise Failure("protocol")
    return [expert for expert in experts if isinstance(expert, dict) and expert.get("expert_id")]


def desktop_chat(base, headers, expert_id="", model=FAST_MODEL, prompt="1+1等于几？直接回答。"):
    """One real desktop-fingerprint chat turn; returns (conversation_id, server_request_id)."""
    conversation_id = "wb2api-conv-" + secrets.token_hex(8)
    body = {"model": model,
            "messages": [{"role": "system", "content": "You are a helpful assistant. 当前处于中文环境，使用简体中文回答。"},
                         {"role": "user", "content": prompt}],
            "agent": "cli", "temperature": 1, "stream": True,
            "stream_options": {"include_usage": True}}
    outgoing = {**headers, "X-Domain": base, "X-Product": "SaaS", "X-Conversation-ID": conversation_id,
                "X-Request-ID": str(int(time.time() * 1000)), "X-Agent-Intent": "craft",
                "X-Agent-Type": "auxiliary", "X-Agent-Runtime": "local-agent",
                "X-IDE-Name": "WorkBuddy", "X-IDE-Type": "WorkBuddy",
                "X-IDE-Version": "5.9.3", "x-codebuddy-request": "1",
                "X-Requested-With": "XMLHttpRequest", "User-Agent": DESKTOP_UA,
                "Accept": "text/event-stream"}
    outgoing.pop("X-Client-Platform", None)
    if expert_id:
        outgoing["X-Expert-Id"] = expert_id
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(60, connect=10)) as client:
        with client.stream("POST", base + "/v2/chat/completions", headers=outgoing, json=body) as response:
            if response.status_code != 200:
                raise Failure("http", response.status_code)
            searched = 0
            async_buffer = bytearray()
            for chunk in response.iter_bytes():
                async_buffer.extend(chunk)
                raw = bytes(async_buffer)
                index = raw.find(b'"id":"', searched)
                while index >= 0:
                    start = index + 6
                    end = raw.find(b'"', start)
                    if end > 0:
                        candidate = raw[start:end].decode("utf-8", "replace")
                        if SERVER_ID_RE.match(candidate):
                            return conversation_id, candidate
                        searched = index + 1
                        index = raw.find(b'"id":"', searched)
                    else:
                        break
                searched = max(0, len(raw) - 32)
    raise Failure("protocol", message="SSE 中未找到服务端 requestId")


def expert_summon_sequence(expert):
    category = (expert.get("categories") or ["expert-all"])[0] or "expert-all"
    if not isinstance(category, str):
        category = "expert-all"
    version = expert.get("version") or "1.0.0"
    identifier, name = expert["expert_id"], expert.get("display_name_zh") or expert["expert_id"]
    title = expert.get("profession_zh") or ""
    return [{"eventCode": "web_element_click", "source": identifier, "type": category,
             "version": version, "elementId": "expert_summon_click", "elementName": "立即召唤",
             "pageURL": "/C:/Program%20Files/WorkBuddy/resources/app.asar/renderer/index.html"},
            {"eventCode": "expert_summon_click", "id": identifier, "name": name,
             "expertTitle": title, "type": "expert-all", "position": 0,
             "expertType": expert.get("expert_type") or "agent", "version": version, "mode": "LOCAL"},
            {"eventCode": "expert_summoned", "id": identifier, "name": name,
             "expertTitle": title, "type": "expert-all"}]


def expert_actual_use(expert, conversation_id, request_id, *, mode="craft"):
    category = (expert.get("categories") or ["expert-all"])[0] or "expert-all"
    if not isinstance(category, str):
        category = "expert-all"
    version = expert.get("version") or "1.0.0"
    event = {"eventCode": "expert_actual_use", "id": expert["expert_id"],
             "name": expert.get("display_name_zh") or expert["expert_id"],
             "expertTitle": expert.get("profession_zh") or "", "type": category,
             "expertType": expert.get("expert_type") or "agent", "source": "builtin",
             "version": version, "cost": 9000, "characterCount": 14,
             "conversationId": conversation_id, "requestId": request_id,
             "messageId": "msg-" + request_id[len(request_id) - 8:],
             "requestModelId": FAST_MODEL, "requestModelName": FAST_MODEL, "mode": mode}
    if mode == "LOCAL":
        # 轻量云真实样本：LOCAL 形态 type 为空、cost 为 0。
        event["type"] = ""
        event["cost"] = 0
    return event
