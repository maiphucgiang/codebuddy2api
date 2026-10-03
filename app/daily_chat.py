"""International WorkBuddy daily-activity turn over the console agent channel.

The official daily activity reward is granted for a *completed* agent session, not
for a direct `/v2/chat/completions` call. A gateway-only account therefore earns
nothing, which is why a dedicated path is needed at all: create a console
conversation, attach to its sandbox over ACP and drive one turn to completion.

Every attempt is one-shot per account per day with no retry. A turn can consume
credits on the account, so the switch defaults to off and international accounts
opt in explicitly, exactly like the check-in and travel preferences.
"""
import hashlib
import json
import time

import httpx

from . import acp_client, credits
from .credits import BROWSER_UA
from .site_routing import PROFILE_ENDPOINTS

HOST = PROFILE_ENDPOINTS["intl-work"]
CONVERSATIONS_PATH = "/console/as/conversations/"
SESSION_SUFFIX = "/session"
MODEL = "deepseek-v4.1-flash"
PROMPT = "Hi"
DEFAULT_CWD = "/workspace"
PLUGINS = [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}]
CONVERSATION_ORIGIN = "workbuddy-app"
# One turn needs several round trips; a hard wall-clock deadline keeps the
# maintenance lock free and a wedged sandbox from blocking the whole sweep.
TURN_SECONDS = 60.0
POLL_SECONDS = 3.0
# Sandbox provisioning is asynchronous; these bound the wait before the turn starts.
SANDBOX_SECONDS = 25.0
SANDBOX_POLL_SECONDS = 2.5
REQUEST_TIMEOUT = httpx.Timeout(30, connect=10, write=15, pool=10)
MAX_BODY_BYTES = 64 * 1024
TERMINAL_STATES = ("completed",)
FAILED_STATES = ("failed", "error", "cancelled", "canceled")

_MESSAGES = {
    "unsupported": "活跃打卡仅适用于国际 WorkBuddy 账号，国内账号及国际 CodeBuddy 不适用",
    "available": "今日尚未打卡，可手动向官方申请每日活跃奖励",
    "done": "今日打卡已确认，活跃奖励由官方次日结算",
    "pending": "上次打卡结果尚未确认，可能仍在执行；今日不重复发送",
    "unconfirmed": "打卡未确认成功，今日不重试；请查询会话状态或检查日志",
    "timeout": "会话未在限定时间内跑完，今日不重试；请核验账号状态",
    "sandbox_timeout": "官方沙箱未在限定时间内就绪，未向会话发送任何内容；今日可重试",
    "sandbox_pending": "官方沙箱仍在创建中，未向会话发送任何内容；今日可重试",
    "network_error": "连接官方失败或响应中断，今日不重试；请检查服务器网络、DNS 和代理",
    "http_error": "官方返回了错误状态，未确认打卡成功，今日不重试",
    "protocol_error": "官方响应格式无效，未确认打卡成功",
    "acp_http": "ACP 通道返回了错误状态，未确认打卡成功，今日不重试",
    "acp_network": "ACP 通道连接失败或中断，未确认打卡成功，今日不重试",
    "acp_protocol": "ACP 通道响应格式无效（缺少连接标识或会话信息），未确认打卡成功，今日不重试",
    "rejected": "官方未确认本次打卡，资格和额度由官方决定",
    "auth_error": "官方拒绝了当前凭证，请刷新 Token 或重新登录后核对",
    "storage_error": "打卡记录无法读取或保存，已停止操作；请检查数据目录权限和磁盘空间",
    "changed": "凭证身份、代次或启用状态已变化，已停止后续操作，请刷新列表核对",
}


class ChatFailure(ValueError):
    """One bounded failure; never carries upstream text, headers or file paths."""

    def __init__(self, kind, http_status=None, code=None):
        super().__init__("Daily activity turn was not confirmed")
        self.diagnostics = {"error_kind": kind, "http_status": http_status, "code": code}


def supported(profile):
    return profile == "intl-work"


def unavailable():
    return {"ok": False, "skipped": True, "state": "unsupported",
            "message": _MESSAGES["unsupported"]}


def failure_view():
    """Storage-unavailable view for the inventory, in the same shape as trial_management."""
    return {"ok": False, "state": "storage_error", "message": _MESSAGES["storage_error"],
            "day": None, "at": None, "conversation_id": None, "usage_before": None,
            "usage_after": None, "acp_usage": None}


def today(now=None):
    """The operator's local calendar day, which is the day the reward is granted for.

    ``time.strftime`` follows the process timezone, so a container left on UTC would
    key the once-per-day guard to a day that turns over at 08:00 Beijing: a Beijing
    day could then be visited twice. Compose pins TZ, and this reads the same clock.
    """
    return time.strftime("%Y-%m-%d", time.localtime(now))


def due_at(identity, day, *, window=6 * 3600):
    """A stable per-account offset inside ``window``, so turns never go out together.

    Random staggering would move on every sweep and could starve an account; hashing
    identity+day gives each account its own fixed slot that changes daily.
    """
    if not identity or window <= 0:
        return 0.0
    digest = hashlib.sha256(f"{identity}:{day}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % int(window)


def seconds_into_day(now=None):
    """Seconds elapsed since local midnight."""
    parts = time.localtime(now)
    return parts.tm_hour * 3600 + parts.tm_min * 60 + parts.tm_sec


def is_due(identity, day, now=None, *, window=6 * 3600):
    """Whether this account's slot for the day has arrived."""
    return seconds_into_day(now) >= due_at(identity, day, window=window)


def _console_headers(token, uid="", domain=""):
    """Console web headers: two credential headers only, no CLI X-IDE-* fingerprint."""
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "x-client-platform": "web",
        "origin": HOST,
        "referer": HOST + "/app",
        "authorization": "Bearer " + (token if isinstance(token, str) else ""),
        "x-user-id": str(uid or ""),
        "x-domain": str(domain or ""),
        "user-agent": BROWSER_UA,
    }


def _read(client, method, url, headers, *, body=None):
    """One request without redirects; the body is parsed here and never disclosed."""
    with client.stream(method, url, headers=headers, content=body, timeout=REQUEST_TIMEOUT,
                       follow_redirects=False) as response:
        status = response.status_code
        raw = bytearray()
        started = time.monotonic()
        for chunk in response.iter_bytes():
            if time.monotonic() - started > TURN_SECONDS or len(raw) + len(chunk) > MAX_BODY_BYTES:
                raise ChatFailure("protocol_error", status)
            raw.extend(chunk)
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ChatFailure("protocol_error" if status == 200 else "http_error", status) from None
    return status, payload


def _check(status, payload):
    if not isinstance(payload, dict):
        raise ChatFailure("protocol_error" if status == 200 else "http_error", status)
    code = payload.get("code")
    code = code if type(code) is int and -(2**31) <= code < 2**31 else None
    if status in (401, 403):
        raise ChatFailure("auth_error", status, code)
    if status not in (200, 201, 202):
        raise ChatFailure("http_error", status, code)
    if code not in (0, None):
        raise ChatFailure("rejected", status, code)
    return payload


def _post(client, url, headers, body):
    status, payload = _read(client, "POST", url, headers,
                            body=json.dumps(body, allow_nan=False).encode())
    return _check(status, payload)


def _get(client, url, headers):
    status, payload = _read(client, "GET", url, headers)
    return _check(status, payload)


def _text(value, limit=200):
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def create_conversation(client, headers):
    """Queue one conversation; the returned session stays CREATING until attached."""
    payload = _post(client, HOST + CONVERSATIONS_PATH, headers,
                    {"prompt": PROMPT, "model": MODEL,
                     "conversationOrigin": CONVERSATION_ORIGIN, "plugins": PLUGINS})
    data = payload.get("data")
    identity = _text(data.get("id"), 128) if isinstance(data, dict) else None
    if identity is None:
        raise ChatFailure("protocol_error")
    return identity


def sandbox_of(client, headers, conversation_id):
    """Read the sandbox endpoint and session identity the ACP turn needs."""
    payload = _get(client, HOST + CONVERSATIONS_PATH + conversation_id + SESSION_SUFFIX, headers)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ChatFailure("protocol_error")
    link = _text(data.get("link") or data.get("endpoint"), 2048)
    token = _text(data.get("token"), 2048)
    session_id = _text(data.get("sessionId") or data.get("session_id") or conversation_id, 256)
    cwd = _text(data.get("cwd"), 512) or DEFAULT_CWD
    if link is None or token is None:
        # The sandbox is still being provisioned; the caller may poll again.
        raise ChatFailure("sandbox_pending")
    return {"link": link, "token": token, "session_id": session_id, "cwd": cwd}


def _await_sandbox(client, headers, conversation_id):
    """Poll for a ready sandbox, bounded by the same wall clock as the turn."""
    deadline = time.monotonic() + SANDBOX_SECONDS
    last = None
    while True:
        try:
            return sandbox_of(client, headers, conversation_id)
        except ChatFailure as error:
            last = error
            if error.diagnostics.get("error_kind") != "sandbox_pending":
                raise
        if time.monotonic() >= deadline:
            raise ChatFailure("sandbox_timeout", None, None) from None
        time.sleep(SANDBOX_POLL_SECONDS)
        del last


def status_of(client, headers, conversation_id):
    """Console business status: the only reliable completion signal for a turn."""
    payload = _get(client, HOST + CONVERSATIONS_PATH + conversation_id, headers)
    data = payload.get("data")
    return (_text(data.get("status"), 64) or "") if isinstance(data, dict) else ""


def _usage_credits(token, uid, domain):
    """Best-effort same-day credit total; upstream usage lags by minutes, so None is normal."""
    try:
        snapshot = credits.fetch_request_usage(token, days=1, uid=uid, domain=domain)
    except Exception:
        return None
    rows = snapshot.get("by_day", {}).get(today(), {})
    return round(sum(rows.values()), 4) if isinstance(rows, dict) and rows else None


def perform(token, profile, *, uid="", domain="", can_write=lambda: True,
            store=None, identity=None):
    """Run one daily-activity turn under durable reservation and generation checks."""
    result = {"ok": False, "state": "unknown", "day": today(),
              "phase": "verify", "conversation_id": None, "sandbox_status": None,
              "usage_before": None, "usage_after": None, "acp_usage": None}
    day, attempt_id = result["day"], None
    conversation_id, sandbox_status, acp_usage = None, None, None

    def stop(reason, **fields):
        result.update(ok=False, state=reason, message=_MESSAGES[reason], **fields)
        return result

    def write_store(method, *args, **kwargs):
        if store is None or not identity:
            raise ChatFailure("storage_error")
        try:
            return getattr(store, method)(identity, *args, **kwargs)
        except Exception:
            raise ChatFailure("storage_error") from None

    def cancel_unless_sent():
        """Leave a never-posted turn retryable; never reopen one that was sent.

        'sent' is written immediately before the prompt POST, so a record still in
        'reserved' is knowably unsent and replaying it cannot spend credits twice.
        Without this an ACP failure would strand the day as an unretryable 'pending'.
        """
        if attempt_id is None:
            return
        try:
            current = store.daily_chat_record(identity, day) if store else None
        except Exception:
            current = None
        if current and current.get("phase") == "sent":
            return
        try:
            write_store("transition_daily_chat", day, attempt_id, "cancelled")
        except ChatFailure:
            pass

    if not supported(profile):
        return unavailable()
    try:
        previous = write_store("daily_chat_record", day)
        if previous:
            phase = previous.get("phase")
            if phase in {"confirmed", "reconciled"}:
                # Like a check-in that already ran: the day's goal is met, just not again.
                result.update(ok=True, state="done", skipped=True, message=_MESSAGES["done"],
                              conversation_id=previous.get("conversation_id"),
                              sandbox_status=previous.get("sandbox_status"),
                              usage_before=previous.get("usage_before"),
                              usage_after=previous.get("usage_after"),
                              acp_usage=previous.get("acp_usage"))
                return result
            if phase != "cancelled":
                # A pending or unconfirmed send is never replayed automatically.
                return stop("pending", skipped=True, conversation_id=previous.get("conversation_id"))
        if not can_write():
            return stop("changed", skipped=True)
        reserved = write_store("reserve_daily_chat", day)
        if reserved is None:
            return stop("pending", skipped=True)
        attempt_id = reserved["attempt_id"]
        if not can_write():
            write_store("transition_daily_chat", day, attempt_id, "cancelled")
            return stop("changed", skipped=True)
        result["phase"] = "conversation"
        result["usage_before"] = _usage_credits(token, uid, domain)
        with httpx.Client(follow_redirects=False) as client:
            headers = _console_headers(token, uid, domain)
            conversation_id = create_conversation(client, headers)
            result["conversation_id"] = conversation_id
            # Record the conversation immediately: if anything fails later, this is
            # what distinguishes "the turn ran" from "nothing ever reached the agent".
            write_store("daily_chat_checkpoint", day, conversation_id=conversation_id)
            # The sandbox is provisioned asynchronously, so an unready response is
            # retried briefly. Nothing has reached the agent yet, which is why this
            # failure path stays retryable for the rest of the day.
            sandbox = _await_sandbox(client, headers, conversation_id)
            if not can_write():
                raise ChatFailure("changed")
            result["phase"] = "turn"

            def committed():
                # Last point at which the day is knowably still unspent: the prompt
                # POST is about to go out and the turn can really run.
                write_store("transition_daily_chat", day, attempt_id, "sent")

            channel = acp_client.run_turn(sandbox["link"], sandbox["token"],
                                          sandbox["session_id"], sandbox["cwd"], PROMPT,
                                          on_prompt=committed)
            try:
                started = time.monotonic()
                while True:
                    if time.monotonic() - started > TURN_SECONDS:
                        raise ChatFailure("timeout")
                    if not can_write():
                        raise ChatFailure("changed")
                    for update in channel.drain(POLL_SECONDS):
                        cost = update.get("cost") if isinstance(update, dict) else None
                        amount = cost.get("amount") if isinstance(cost, dict) else None
                        if type(amount) in (int, float) and 0 <= amount <= 10**9:
                            acp_usage = round(float(amount), 6)
                    try:
                        sandbox_status = status_of(client, headers, conversation_id)
                    except ChatFailure as error:
                        # A transient status failure is retried on the next poll; an auth
                        # rejection is terminal because the turn cannot finish either.
                        if error.diagnostics.get("error_kind") == "auth_error":
                            raise
                        continue
                    result["sandbox_status"] = sandbox_status or None
                    if sandbox_status in TERMINAL_STATES:
                        break
                    if sandbox_status in FAILED_STATES:
                        raise ChatFailure("rejected")
            finally:
                channel.close()
        result["phase"] = "usage"
        result["usage_after"] = _usage_credits(token, uid, domain)
        result["acp_usage"] = acp_usage
        write_store("daily_chat_checkpoint", day, sandbox_status=sandbox_status,
                    usage_before=result["usage_before"], usage_after=result["usage_after"],
                    acp_usage=acp_usage, conversation_id=conversation_id)
        write_store("transition_daily_chat", day, attempt_id, "confirmed")
        result.update(ok=True, state="done", message=_MESSAGES["done"])
        return result
    except ChatFailure as error:
        fields = dict(error.diagnostics)
        kind = fields.pop("error_kind", "network_error")
        if kind == "network_error" and fields.get("http_status") in (401, 403):
            kind = "auth_error"
        cancel_unless_sent()
        result.update(state=kind, message=_MESSAGES.get(kind, _MESSAGES["network_error"]), **fields)
        return result
    except acp_client.AcpError as error:
        # Surface the channel's own reason instead of collapsing every ACP failure
        # into one opaque state: the diagnostics carry only a kind and a status.
        # A channel that never delivered the prompt is an unsent day, so it must be
        # cancelled here too; otherwise the day reads as unretryable 'pending'.
        fields = dict(error.diagnostics)
        kind = "acp_" + str(fields.pop("error_kind", "protocol"))
        cancel_unless_sent()
        result.update(state=kind, message=_MESSAGES.get(kind, _MESSAGES["protocol_error"]), **fields)
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        cancel_unless_sent()
        return stop("network_error" if result["phase"] in {"conversation", "usage"} else "protocol_error")
    except Exception:
        cancel_unless_sent()
        return stop("storage_error")


def view(record):
    """Public state for the inventory; never exposes paths, tokens or upstream text."""
    if not record:
        return {"state": "available", "message": _MESSAGES["available"], "day": None}
    state = record.get("state")
    if state not in _MESSAGES:
        state = "done" if record.get("phase") in {"confirmed", "reconciled"} else "unconfirmed"
    return {"state": state, "message": _MESSAGES[state], "day": record.get("day"),
            "at": record.get("confirmed_at"), "conversation_id": record.get("conversation_id"),
            "usage_before": record.get("usage_before"), "usage_after": record.get("usage_after"),
            "acp_usage": record.get("acp_usage")}
