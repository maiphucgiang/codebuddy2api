"""Complete first-Buddy onboarding through one durable, account-scoped real conversation."""
import json
import time

import httpx

from .site_routing import PROFILE_ENDPOINTS, profile_for_headers
from .upstream_io import ChatSSEAccumulator, UpstreamResponseError

MAX_OUTPUT_TOKENS = 32
MAX_CHAT_BYTES = 64 * 1024
CHAT_SECONDS = 30
PROMPT = "Say OK."
_MESSAGES = {
    "buddy_task_pending": "新手任务等待官方状态更新，自动旅行将继续查询，不重复发送对话",
    "buddy_task_unconfirmed": "新手任务请求结果未确认，已停止自动重发，请查询官方状态或检查日志",
    "buddy_task_no_model": "当前账号没有可用且倍率已知的模型，请同步余额和目录后重试",
    "buddy_task_unsupported": "自动新手对话需要国内 WorkBuddy 凭证，不借用其他账号或产品身份",
    "buddy_task_changed": "设置或凭证已变化，已停止新手任务后续操作",
    "buddy_task_storage_error": "新手任务记录或审计不可用，已停止后续操作",
}


class TaskFailure(ValueError):
    def __init__(self, kind, status=None):
        super().__init__("Buddy task response was not confirmed")
        self.details = {"error_kind": kind, "http_status": status, "code": None}


def first_task(data):
    rows = data.get("tasks")
    if not isinstance(rows, list) or len(rows) > 1000 or any(not isinstance(row, dict) for row in rows):
        raise TaskFailure("protocol", 200)
    matches = [row for row in rows if row.get("task_code") == "first_buddy"]
    if len(matches) != 1:
        return None
    task = matches[0]
    if (task.get("locked") is not False or task.get("reward_buddy") is not True
            or task.get("accept_status") not in {"not_accepted", "accepted", "in_progress", "completed"}):
        return None
    return task


def _chat(client, headers, record, model):
    conversation, request_id = record["conversation_id"], record["request_id"]
    headers = {**headers, "Accept": "text/event-stream", "Content-Type": "application/json",
               "X-Conversation-ID": conversation, "X-Session-ID": conversation,
               "X-Parent-Conversation-ID": conversation, "X-Request-ID": request_id,
               "X-Root-Request-ID": request_id, "X-Conversation-Request-ID": request_id,
               "X-Conversation-Message-ID": request_id, "X-Agent-Intent": "craft",
               "X-Agent-Purpose": "conversation", "X-Agent-Type": "main"}
    event = {"eventCode": "chat_request_send", "id": conversation, "extra": {
        "inputLength": len(PROMPT), "requestModelId": model["id"], "requestModelName": model["name"],
        "mode": "craft", "command": "", "expertId": ""}}
    body = {"model": model["id"], "messages": [
        {"role": "system", "content": "Reply with OK only. Do not use tools."},
        {"role": "user", "content": PROMPT}], "stream": True, "stream_options": {"include_usage": True},
        "max_tokens": MAX_OUTPUT_TOKENS, "extra_vars": {"growthEvent": json.dumps([event], separators=(",", ":"))}}
    started = time.monotonic()
    with client.stream("POST", PROFILE_ENDPOINTS["cn-work"] + "/v2/chat/completions", headers=headers,
                       json=body, timeout=httpx.Timeout(15, connect=5, write=10, pool=5)) as response:
        if response.status_code != 200:
            raise TaskFailure("http", response.status_code)
        if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
            raise TaskFailure("protocol", 200)
        raw = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() - started > CHAT_SECONDS:
                raise TaskFailure("timeout", 200)
            if len(raw) + len(chunk) > MAX_CHAT_BYTES:
                raise TaskFailure("protocol", 200)
            raw.extend(chunk)
    accumulator = ChatSSEAccumulator(max_collect_bytes=MAX_CHAT_BYTES)
    try:
        for line in raw.decode("utf-8").splitlines():
            accumulator.feed_line(line)
        result = accumulator.result()
    except (UnicodeError, RecursionError, httpx.HTTPError, UpstreamResponseError):
        raise TaskFailure("protocol", 200) from None
    if result["tool_calls"] or accumulator.filter_detector.detected:
        raise TaskFailure("protocol", 200)
    usage = result.get("usage") or {}
    total = usage.get("total_tokens")
    return total if type(total) is int and 0 <= total <= 10**9 else None


def perform(client, token, task, *, context, can_write, request, event):
    store, identity = context.get("store"), context.get("identity")
    result = {"buddy_task_completed": False, "buddy_task_chat_sent": False, "phase": "buddy_task_verify"}

    def stop(reason, **fields):
        return {**result, "ok": False, "skipped": True, "buddy_blocked": True,
                "reason": reason, "message": _MESSAGES[reason], **fields}

    def cancel_unsent(reason, reserved):
        if not store.release_buddy_task(identity, reserved["request_id"]):
            return stop("buddy_task_storage_error")
        if not event("task_chat", "skipped", code=reason, model=reserved["model"],
                     conversation_id=reserved["conversation_id"], request_id=reserved["request_id"]):
            return stop("buddy_task_storage_error")
        return stop(reason)

    def complete():
        if not event("task_completed", "success"):
            return stop("buddy_task_storage_error")
        store.buddy_task_checkpoint(identity, completed=True)
        return {**result, "buddy_task_completed": True}

    def verify():
        return first_task(request(client, token, "tasks"))

    def details(error):
        return getattr(error, "details", {"error_kind": "timeout" if isinstance(error, httpx.TimeoutException) else "network",
                                          "http_status": None, "code": None})

    try:
        if store is None or not identity:
            return stop("buddy_task_storage_error")
        previous = store.buddy_task_record(identity)
        if previous:
            result["buddy_task_chat_sent"] = bool(previous["chat_started"])
        if task["accept_status"] == "completed":
            return complete() if previous and not previous["completed"] else {**result, "buddy_task_completed": True}
        headers = context.get("headers") or {}
        if (context.get("profile") != "cn-work" or profile_for_headers(headers) != "cn-work"
                or headers.get("Authorization") != f"Bearer {token}"):
            return stop("buddy_task_unsupported")
        if previous and (previous["chat_started"] or previous["completed"]):
            return stop("buddy_task_pending" if previous["chat_state"] == "success" else "buddy_task_unconfirmed")
        selector = context.get("task_model")
        model = selector() if callable(selector) else None
        if not model:
            return stop("buddy_task_no_model")
        if not can_write():
            return stop("buddy_task_changed")
        # first_buddy records real activity directly, including from not_accepted.
        result["phase"] = "buddy_task_chat"
        model = selector(model["id"])
        if not model:
            return stop("buddy_task_no_model")
        if not can_write():
            return stop("buddy_task_changed")
        if not event("task_chat", "pending", model=model["id"], attempt=1, max_attempts=1):
            return stop("buddy_task_storage_error")
        if not can_write():
            return stop("buddy_task_changed")
        reserved = store.reserve_buddy_task(identity, "chat", model=model["id"])
        if reserved is None:
            return stop("buddy_task_unconfirmed")
        reason = None
        try:
            if not can_write():
                reason = "buddy_task_changed"
            elif not selector(model["id"]):
                reason = "buddy_task_no_model"
        except Exception:
            reason = "buddy_task_storage_error"
        if reason:
            return cancel_unsent(reason, reserved)
        result["buddy_task_chat_sent"] = True
        failure, usage = None, None
        try:
            usage = _chat(client, headers, reserved, model)
        except (httpx.HTTPError, ValueError) as error:
            failure = details(error)
        store.buddy_task_checkpoint(identity, chat_state="uncertain" if failure else "success", total_tokens=usage)
        if not event("task_chat", "error" if failure else "success", model=model["id"],
                     conversation_id=reserved["conversation_id"], request_id=reserved["request_id"],
                     total_tokens=usage, status_code=(failure or {}).get("http_status")):
            return stop("buddy_task_storage_error")
        result["phase"] = "buddy_task_verify"
        task = verify()
        if task and task["accept_status"] == "completed":
            return complete()
        return stop("buddy_task_unconfirmed" if failure else "buddy_task_pending", **(failure or {}))
    except (httpx.HTTPError, ValueError) as error:
        event("task_failed", "error", status_code=details(error).get("http_status"))
        return stop("buddy_task_unconfirmed", **details(error))
    except Exception:
        return stop("buddy_task_storage_error")
