"""A simulated CodeBuddy / WorkBuddy upstream that answers the gateway's real HTTP requests.

Every httpx request the preview gateway makes lands in :meth:`Upstream.handle`, so the
gateway's own request building, parsing, failover and accounting run unchanged. Requests
for endpoints that are not simulated get an explicit 501 instead of reaching the network.
Upstream state (balances, check-ins, trips, tasks) lives in memory and resets on restart.
"""

import asyncio
import gzip
from dataclasses import dataclass, field
import itertools
import json
import re
import sys
import threading
import time
import uuid
import zlib

import httpx

from app import media_generation
from app.site_routing import DOMAIN_PROFILES, PROFILE_DOMAINS
from preview.seed import (ACCOUNTS, CN_MODELS, COS_HOST, DAY, INTL_MODELS, MP4_BYTES, PNG_BYTES,
                          SEED_VIDEO_TASK_ID, Account)
DIRECTIVE = re.compile(r"\[preview:(\w+)(?:=([\w.-]+))?\]")
TOKEN = re.compile(r"preview-([a-z0-9]+)-(\d+|refresh)")
TRAVEL_SECONDS = 180
ARCHIVE_CHUNK = 64 * 1024
DAILY_TRIPS = 3
LOGIN_SECONDS = 4
VIDEO_SECONDS = 6  # Preview video tasks complete fast enough to demo end to end.
# Rates are per-model multipliers in the catalog's own "x1.00" format.
MODELS = {
    "glm-5.1": {"name": "GLM-5.1", "vendor": "Zhipu", "credits": "x0.30", "maxInputTokens": 200000,
                "maxOutputTokens": 32768, "supportsReasoning": True},
    "deepseek-v3.2": {"name": "DeepSeek-V3.2", "vendor": "DeepSeek", "credits": "x0.20",
                      "maxInputTokens": 128000, "maxOutputTokens": 16384, "supportsReasoning": True},
    "kimi-k2": {"name": "Kimi K2", "vendor": "Moonshot", "credits": "x0.00", "maxInputTokens": 128000,
                "maxOutputTokens": 16384},
    "claude-sonnet-4.6": {"name": "Claude Sonnet 4.6", "vendor": "Anthropic", "credits": "x1.20",
                          "maxInputTokens": 200000, "maxOutputTokens": 64000, "supportsReasoning": True,
                          "supportsImages": True},
    "gpt-5.1": {"name": "GPT-5.1", "vendor": "OpenAI", "credits": "x1.00", "maxInputTokens": 272000,
                "maxOutputTokens": 128000, "supportsReasoning": True, "supportsImages": True,
                "supportedEfforts": ["low", "medium", "high"]},
    "gemini-2.5-pro": {"name": "Gemini 2.5 Pro", "vendor": "Google", "credits": "x0.80",
                       "maxInputTokens": 1000000, "maxOutputTokens": 65536, "supportsReasoning": True,
                       "supportsImages": True},
}
LOCATIONS = ({"id": 1, "name": "杭州西湖"}, {"id": 2, "name": "厦门鼓浪屿"}, {"id": 3, "name": "大理洱海"},
             {"id": 4, "name": "成都宽窄巷子"}, {"id": 5, "name": "青岛栈桥"})
# (code, title, reward credit, target, platform, initial status, progress); the mini-program
# list is a superset of the web one.
TASKS = (
    ("chat_5", "完成 5 次对话", 10, 5, "web", "accepted", 3),
    ("Buddy_App", "在 Buddy 应用中聊一次", 5, 1, "web", "not_accepted", 0),
    ("automation_1", "创建 1 个自动化任务", 8, 1, "web", "not_accepted", 0),
    ("template_5", "使用 5 次模板", 6, 5, "web", "accepted", 5),
    ("Hp_Appearance", "更换一次外观主题", 3, 1, "web", "claimed", 1),
    ("Model_chat_GLM5.2", "与 GLM 模型对话", 12, 1, "web", "not_accepted", 0),
    ("skill_1", "使用 1 次技能", 5, 1, "web", "locked", 0),
    ("school_season", "开学季打卡", 20, 1, "miniprogram", "not_accepted", 0),
)


@dataclass
class Package:
    name: str
    code: str
    remaining: float
    total: float
    expires: float


@dataclass
class State:
    account: Account
    packages: list
    used: float = 0.0
    generation: int = 0
    checked_in: str = ""
    trial_claimed: bool = False
    trip: dict = field(default_factory=dict)
    departures: dict = field(default_factory=dict)  # day -> count
    tasks: dict = field(default_factory=dict)

    def credit(self, name: str, code: str, amount: float, days: int = 30):
        package = next((p for p in self.packages if p.code == code), None)
        if package is None:
            package = Package(name, code, 0.0, 0.0, time.time() + days * DAY)
            self.packages.append(package)
        package.remaining += amount
        package.total += amount


@dataclass
class Reply:
    status: int = 200
    payload: object = None
    chunks: list = field(default_factory=list)
    delay: float = 0.0
    media: str = "text/event-stream"

    def _headers(self):
        return {"content-type": self.media if self.chunks else "application/json"}

    def _body(self):
        return b"".join(self.chunks) if self.chunks else json.dumps(self.payload, ensure_ascii=False).encode()

    def response(self) -> httpx.Response:
        if not self.chunks or not self.delay:
            return httpx.Response(self.status, headers=self._headers(), content=self._body())

        def stream():
            for chunk in self.chunks:
                time.sleep(self.delay)
                yield chunk
        return httpx.Response(self.status, headers=self._headers(), content=stream())

    def async_response(self) -> httpx.Response:
        if not self.chunks or not self.delay:
            return self.response()

        async def stream():
            for chunk in self.chunks:
                await asyncio.sleep(self.delay)
                yield chunk
        return httpx.Response(self.status, headers=self._headers(), content=stream())


def ok(data=None, **extra) -> Reply:
    return Reply(payload={"code": 0, "msg": "ok", "data": data, **extra})


def fail(status: int, message: str, code=None) -> Reply:
    return Reply(status, {"code": status if code is None else code, "msg": message})


class Upstream:
    def __init__(self, accounts=ACCOUNTS):
        self._lock = threading.Lock()
        self._missing: set = set()
        self._guests = itertools.count(1)
        self._logins: dict = {}
        # One in-progress task seeded on disk that this simulation can finish on demand.
        self._video_tasks = {SEED_VIDEO_TASK_ID: {
            "owner": "aurora", "model": "hunyuan-video-art",
            "at": time.time() - VIDEO_SECONDS - 1, "charged": False, "url": None}}
        self.states = {account.key: self._state_for(account) for account in accounts}
        self.routes = {
            "/v2/plugin/auth/token/refresh": self.refresh,
            "/v2/plugin/auth/state": self.login_state,
            "/v2/plugin/auth/token": self.login_token,
            "/v2/plugin/login/account": self.login_account,
            "/v3/config": self.catalog,
            "/console/accounts": self.console_accounts,
            "/v2/billing/meter/get-user-resource": self.resources,
            "/v2/billing/meter/get-enterprise-user-usage": self.enterprise_usage,
            "/billing/meter/get-user-request-usage": self.request_usage,
            "/v2/billing/meter/checkin-activity-status": self.checkin_status,
            "/v2/billing/meter/daily-checkin": self.checkin,
            "/billing/ide/trial": self.trial,
            "/v2/chat/completions": self.chat,
            "/console/chat/completions": self.chat,
            "/agenttool/v1/search": self.search,
            "/agenttool/v1/webfetch": self.webfetch,
            "/v2/images/generations": self.images,
            "/v2/images/edits": self.images,
            "/v2/videos/generations": self.video_submit,
            "/v2/videos/tasks": self.video_tasks,
            "/activity/growth/buddy/info": self.buddy_info,
            "/activity/growth/buddy/travel/status": self.travel_status,
            "/activity/growth/buddy/travel/config": self.travel_config,
            "/activity/growth/buddy/travel/claim": self.travel_claim,
            "/activity/growth/buddy/travel/depart": self.travel_depart,
            "/v2/activity/growth/tasks": self.growth_tasks,
            "/v2/activity/growth/tasks/accept": self.growth_accept,
            "/v2/report": self.growth_progress,
            "/v2/user-asset/appearance/set": self.growth_progress,
            "/portal/operation-platform/market/expert/list": self.experts,
            "/console/as/conversations/": self.conversation_create,
        }
        self.patterns = (
            (re.compile(r"/activity/growth/tasks/([\w.-]+)/claim"), self.growth_claim),
            (re.compile(r"/console/as/conversations/([\w-]+)/session"), self.conversation_session),
            (re.compile(r"/console/as/conversations/([\w-]+)"), self.conversation_status),
        )

    def _state_for(self, account: Account) -> State:
        now = time.time()
        state = State(account, [Package(name, code, float(remaining), float(total), now + days * DAY)
                                for name, code, remaining, total, days in account.packages],
                      used=float(account.allowance[1]))
        trips = ({"state": "idle"},
                 {"state": "traveling", "location": LOCATIONS[1], "arrive_at": now + 40 * 60, "reward": 8},
                 {"state": "arrived", "location": LOCATIONS[2], "arrive_at": now - 600, "reward": 6})
        # Spread the trips so the panel shows every travel state at once.
        state.trip = dict(trips[int(account.uid) % 3])
        for code, title, reward, target, platform, status, current in TASKS:
            state.tasks[code] = {"task_code": code, "title": title, "description": f"{title}即可领取奖励",
                                 "reward_credit": reward, "reward_energy": reward * 2, "locked": status == "locked",
                                 "accept_status": "not_accepted" if status == "locked" else status,
                                 "current": current, "target": target, "platform": platform}
        state.tasks["first_buddy"] = {"task_code": "first_buddy", "title": "领取首只 Buddy", "reward_credit": 0,
                                      "locked": False, "accept_status": "claimed", "current": 1, "target": 1,
                                      "reward_buddy": True, "platform": "web"}
        return state

    # -- transport entry points -------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        return self.dispatch(request, request.read()).response()

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        return self.dispatch(request, await request.aread()).async_response()

    def dispatch(self, request: httpx.Request, raw: bytes) -> Reply:
        path = request.url.path
        if request.headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                return fail(400, "gzip 请求体无法解压")
        if request.url.host == "api.github.com":
            return self.releases()
        if path.endswith(".tar.gz"):
            return self.archive(request.url.host)
        if request.url.host == COS_HOST:
            return self.media_file(request)
        handler, arguments = self.routes.get(path), ()
        if handler is None:
            handler, arguments = next(((h, m.groups()) for pattern, h in self.patterns
                                       if (m := pattern.fullmatch(path))), (None, ()))
        if handler is None:
            label = f"{request.method} {request.url.host}{path}"
            if label not in self._missing:
                self._missing.add(label)
                sys.stderr.write(f"[preview] 未模拟的上游接口：{label}\n")
            return fail(501, f"预览环境未模拟该接口：{path}")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        with self._lock:
            if handler in (self.refresh, self.login_state, self.login_token, self.login_account):
                return handler(request, body, *arguments)
            state = self._state(request)
            if state is None or state.account.revoked:
                return fail(401, "登录已过期")
            return handler(request, body, state, *arguments)

    def _state(self, request: httpx.Request) -> State | None:
        value = request.headers.get("x-refresh-token") or request.headers.get("authorization", "")
        match = TOKEN.search(value)
        return self.states.get(match.group(1)) if match else None

    # -- sign-in and tokens -------------------------------------------------------------

    def refresh(self, request, body):
        state = self._state(request)
        if state is None or state.account.revoked:
            return ok(None, code=11001, msg="refresh token 已失效，请重新登录")
        return ok(self._tokens(state))

    def _tokens(self, state: State) -> dict:
        state.generation += 1
        account = state.account
        return {"accessToken": f"preview-{account.key}-{state.generation}",
                "refreshToken": f"preview-{account.key}-refresh", "expiresIn": 6 * 3600,
                "refreshExpiresIn": 30 * DAY, "domain": PROFILE_DOMAINS[account.profile]}

    def login_state(self, request, body):
        now = time.time()
        # The gateway abandons a login after ten minutes; forget it here too.
        self._logins = {key: value for key, value in self._logins.items() if now - value["started"] < 600}
        state = uuid.uuid4().hex
        self._logins[state] = {"host": request.url.host, "started": now}
        # The gateway only accepts official login pages; the preview signs in by itself after
        # LOGIN_SECONDS, so the link never has to be opened.
        return ok({"state": state, "authUrl": f"https://{request.url.host}/login?state={state}"})

    def login_token(self, request, body):
        login = self._logins.get(request.url.params.get("state", ""))
        if login is None or time.time() - login["started"] < LOGIN_SECONDS:
            return ok({})
        if "key" not in login:
            number = next(self._guests)
            profile = DOMAIN_PROFILES.get(login["host"], "cn-cli")
            account = Account(f"guest{number}", profile, str(900000 + number), f"Guest {number}",
                              models=INTL_MODELS if profile.startswith("intl") else CN_MODELS,
                              packages=(("新人权益包", "welcome", 500, 500, 30),))
            self.states[account.key] = self._state_for(account)
            login["key"] = account.key
        return ok(self._tokens(self.states[login["key"]]))

    def login_account(self, request, body):
        login = self._logins.get(request.url.params.get("state", ""))
        if login is None or "key" not in login:
            return fail(401, "登录尚未完成")
        account = self.states[login["key"]].account
        return ok({"uid": account.uid, "nickname": account.nickname})

    # -- catalog and billing ------------------------------------------------------------

    def catalog(self, request, body, state):
        models = [{"id": key, **MODELS[key], "supportsToolCall": True,
                   "descriptionZh": f"{MODELS[key]['name']}（预览数据）",
                   "descriptionEn": f"{MODELS[key]['name']} (preview data)"}
                  for key in state.account.models]
        # Media models live in the root table only (the account/serves scope) with the
        # real upstream tags; the cli selector never lists them, so chat is unaffected.
        kind_tags = {"image": "text-to-image", "edit": "image-to-image", "video": "text-to-video"}
        models.extend({"id": name, "name": name, "supportsToolCall": False,
                       "tags": [kind_tags[kind] for kind in media_generation.model_kinds(name)],
                       "credits": "x1.00"}
                      for name in state.account.media)
        agents = [{"name": "cli", "tags": ["default"],
                   "models": list(state.account.models)}]
        models[0]["isDefault"] = True
        return ok({"models": models, "agents": agents})

    def console_accounts(self, request, body, state):
        account = state.account
        item = {"uid": account.uid, "nickname": account.nickname}
        if account.enterprise:
            item.update(enterpriseId=account.enterprise, type="ultimate")
        return ok({"accounts": [item]})

    def resources(self, request, body, state):
        fmt = "%Y-%m-%d %H:%M:%S"
        rows = [{"PackageName": p.name, "PackageCode": p.code, "CycleCapacityRemainPrecise": f"{p.remaining:.2f}",
                 "CycleCapacitySizePrecise": f"{p.total:.2f}",
                 "DeductionEndTime": time.strftime(fmt, time.localtime(p.expires))}
                for p in state.packages if p.remaining > 0]
        # Every row fits on the first page; later pages are empty.
        first = all(int(body.get(key) or 1) == 1 for key in ("PageNumber", "pageNum", "page"))
        return ok({"Response": {"Data": {"Accounts": rows if first else []}}})

    def enterprise_usage(self, request, body, state):
        reset = time.time() + 12 * DAY
        return ok({"limitNum": state.account.allowance[0], "credit": round(state.used, 2),
                   "cycleResetTime": int(reset * 1000)})

    def request_usage(self, request, body, state):
        rows = self._usage_rows(state.account)
        page, size = int(body.get("pageNum") or 1), int(body.get("pageSize") or 200)
        return ok({"data": rows[(page - 1) * size:page * size], "total": len(rows)})

    @staticmethod
    def _usage_rows(account: Account):
        # Deterministic per account so repeated syncs agree with each other.
        rows, now = [], time.time()
        for day in range(29, -1, -1):
            for index in range(account.daily_requests):
                model = account.models[(day + index) % len(account.models)]
                rate = float(MODELS[model]["credits"][1:])
                when = time.localtime(now - day * DAY - index * 1800)
                rows.append({"requestTime": time.strftime("%Y-%m-%d %H:%M:%S", when), "model": model,
                             "credit": round(rate * (2 + (day * 7 + index * 3) % 9), 2)})
        return rows

    def checkin_status(self, request, body, state):
        return ok({"active": True, "today_checked_in": state.checked_in == time.strftime("%Y-%m-%d")})

    def checkin(self, request, body, state):
        today = time.strftime("%Y-%m-%d")
        if state.checked_in == today:
            return Reply(payload={"code": 1001, "msg": "今日已签到"})
        state.checked_in = today
        if not state.account.enterprise:
            state.credit("签到奖励", "checkin", 5, days=7)
        return ok({"credit": 5})

    def trial(self, request, body, state):
        if state.trial_claimed:
            return fail(400, "trial already claimed", code=14051)
        state.trial_claimed = True
        state.credit("Trial credits", "trial", 300, days=14)
        return ok({"success": True})

    # -- inference and search -----------------------------------------------------------

    def chat(self, request, body, state):
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        prompt = _last_text(messages, "user")
        directive = dict(DIRECTIVE.findall(prompt))
        if "429" in directive:
            reset = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 120))
            return fail(429, f"请求过于频繁，额度将于 {reset} UTC+0 恢复")
        if "500" in directive:
            return fail(500, "预览环境模拟的上游错误")
        model = str(body.get("model") or "")
        spec = MODELS.get(model, {})
        tools = body.get("tools") if isinstance(body.get("tools"), list) else []
        call = _tool_call(tools, directive) if messages and messages[-1].get("role") == "user" else None
        text = "" if call else _answer(prompt, messages, model, "long" in directive)
        reasoning = "先确认请求内容，再组织一段简短的模拟回复。" if spec.get("supportsReasoning") else ""
        prompt_tokens = max(1, len(json.dumps(messages, ensure_ascii=False)) // 3)
        completion_tokens = max(1, len(text or call["arguments"]) // 2 + len(reasoning) // 2)
        credit = round((prompt_tokens + completion_tokens) / 1000 * float(spec.get("credits", "x0")[1:]), 4)
        self._charge(state, credit)
        self._advance_tasks(state)
        deltas: list[dict] = [{"reasoning_content": reasoning}] if reasoning else []
        if call:
            deltas.append({"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                           "function": {"name": call["name"], "arguments": ""}}]})
            deltas.append({"tool_calls": [{"index": 0, "function": {"arguments": call["arguments"]}}]})
        else:
            deltas.extend({"content": text[i:i + 12]} for i in range(0, len(text), 12))
        # A 32-hex id matches what the official servers return, which the growth chains read.
        identity = uuid.uuid4().hex
        events = [{"id": identity, "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                  for delta in deltas]
        events.append({"id": identity, "model": model,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}],
                       "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                                 "total_tokens": prompt_tokens + completion_tokens, "credit": credit}})
        data = [f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode() for event in events]
        return Reply(chunks=data + [b"data: [DONE]\n\n"], delay=0.4 if "slow" in directive else 0.03)

    @staticmethod
    def _charge(state: State, credit: float):
        if state.account.enterprise:
            state.used += credit
            return
        for package in sorted(state.packages, key=lambda p: p.expires):
            spent = min(package.remaining, credit)
            package.remaining -= spent
            credit -= spent
            if credit <= 0:
                return

    def search(self, request, body, state):
        query = str(body.get("query") or "")[:80]
        count = max(1, min(int(body.get("max_results") or 5), 10))
        # example.com is reserved for documentation, so a demo link never leads anywhere real.
        return Reply(payload={"code": 0, "results": [
            {"title": f"{query} · 模拟结果 {index}", "url": f"https://example.com/preview/{index}",
             "snippet": f"预览环境为“{query}”生成的第 {index} 条搜索结果。"} for index in range(1, count + 1)]})

    def webfetch(self, request, body, state):
        url = str(body.get("url") or "")[:200]
        return Reply(payload={"url": url, "title": "预览抓取",
                              "content": f"# 预览环境为 {url} 生成的抓取内容。\n\n正文摘要。",
                              "content_type": "text/markdown", "content_length": 64,
                              "truncated": False, "status_code": 200})

    # -- media generation ----------------------------------------------------------------

    def media_file(self, request):
        """Serve the simulated COS bucket the media endpoints hand signed URLs for."""
        if request.url.path.endswith(".mp4"):
            return Reply(chunks=[MP4_BYTES], media="video/mp4")
        return Reply(chunks=[PNG_BYTES], media="image/png")

    def images(self, request, body, state):
        model = str(body.get("model") or "")
        if model in media_generation.VIDEO_MODELS:
            return fail(500, "model family not served on this endpoint")
        if model not in media_generation.IMAGE_TEXT_MODELS and model not in media_generation.IMAGE_EDIT_MODELS:
            return fail(400, "route config not found")
        count = max(1, min(int(body.get("n") or 1), 4))
        prompt = str(body.get("prompt") or "")[:60]
        items = [{"url": f"https://{COS_HOST}/images/{uuid.uuid4().hex}.png",
                  "revised_prompt": f"预览图像：{prompt}"} for _ in range(count)]
        return ok({"data": items, "usage": {"credit": None}})

    def video_submit(self, request, body, state):
        model = str(body.get("model") or "")
        if model not in media_generation.VIDEO_MODELS:
            return fail(400, "route config not found")
        task_id = uuid.uuid4().hex
        self._video_tasks[task_id] = {"owner": state.account.key, "model": model,
                                      "at": time.time(), "charged": False, "url": None}
        return ok({"id": task_id})

    def video_tasks(self, request, body, state):
        task_id = str(body.get("task_id") or "")
        task = self._video_tasks.get(task_id)
        if task is None or task["owner"] != state.account.key:
            return fail(404, "task not found")
        if time.time() - task["at"] < VIDEO_SECONDS:
            return ok({"status": "processing"})
        if not task["charged"]:
            task["charged"] = True
            task["url"] = f"https://{COS_HOST}/videos/{task_id}.mp4"
            self._charge(state, 5.0)
        return ok({"status": "completed", "data": [{"url": task["url"]}], "usage": {"credit": 5.0}})

    # -- growth center ------------------------------------------------------------------

    def buddy_info(self, request, body, state):
        return ok({"buddy": {"instance_id": 1000 + int(state.account.uid) % 1000, "name": "小橘"}})

    def _trip(self, state: State) -> dict:
        trip = state.trip
        if trip["state"] == "traveling" and time.time() >= trip["arrive_at"]:
            trip["state"] = "arrived"
        return trip

    def travel_status(self, request, body, state):
        trip = self._trip(state)
        data = {"state": trip["state"], "daily_limit_reached": _departed_today(state) >= DAILY_TRIPS,
                "server_now": time.time()}
        if trip["state"] != "idle":
            data.update(location=trip["location"], arrive_at=trip["arrive_at"], reward_credit=trip["reward"])
        return ok(data)

    def travel_config(self, request, body, state):
        return ok({"locations": list(LOCATIONS)})

    def travel_claim(self, request, body, state):
        trip = self._trip(state)
        if trip["state"] != "arrived":
            return Reply(payload={"code": 400, "msg": "buddy has not arrived"})
        state.credit("旅行奖励", "travel", trip["reward"])
        state.trip = {"state": "idle"}
        return ok({"reward_credit": trip["reward"]})

    def travel_depart(self, request, body, state):
        if self._trip(state)["state"] != "idle":
            return Reply(payload={"code": 400, "msg": "already traveling"})
        if _departed_today(state) >= DAILY_TRIPS:
            return Reply(payload={"code": 400, "msg": "daily limit reached"})
        location = next((l for l in LOCATIONS if l["id"] == body.get("location_id")), LOCATIONS[0])
        today = time.strftime("%Y-%m-%d")
        state.departures = {today: _departed_today(state) + 1}
        state.trip = {"state": "traveling", "location": location, "arrive_at": time.time() + TRAVEL_SECONDS,
                      "reward": 5 + location["id"]}
        return ok({"arrive_at": state.trip["arrive_at"]})

    def growth_tasks(self, request, body, state):
        mini = request.headers.get("x-client-platform") == "miniprogram"
        return ok({"tasks": [{key: value for key, value in task.items() if key != "platform"}
                             for task in state.tasks.values() if mini or task["platform"] == "web"]})

    def growth_accept(self, request, body, state):
        for code in body.get("task_codes") or []:
            task = state.tasks.get(code)
            if task is None or task["locked"]:
                return Reply(payload={"code": 404, "msg": "task not found"})
            if task["accept_status"] == "not_accepted":
                task["accept_status"] = "accepted"
        return ok({})

    def growth_claim(self, request, body, state, code):
        task = state.tasks.get(code)
        if task is None:
            return Reply(payload={"code": 404, "msg": "task not found"})
        if task["accept_status"] == "claimed":
            return ok({"already_claimed": True, "credit": 0, "energy": 0})
        if task["current"] < task["target"]:
            return Reply(payload={"code": 400, "msg": "task not completed"})
        task["accept_status"] = "claimed"
        state.credit("活动奖励", "activity", task["reward_credit"])
        return ok({"credit": task["reward_credit"], "energy": task.get("reward_energy", 0)})

    def growth_progress(self, request, body, state):
        self._advance_tasks(state)
        return ok({})

    @staticmethod
    def _advance_tasks(state: State):
        # Upstream scoring is opaque; here any reported activity completes the accepted tasks.
        for task in state.tasks.values():
            if task["accept_status"] in ("accepted", "in_progress"):
                task["current"] = task["target"]

    def experts(self, request, body, state):
        return ok({"experts": [{"expert_id": "ex_preview", "display_name_zh": "预览专家", "version": "1.0.0",
                                "categories": ["expert-all"]}]})

    # -- active check-in (international WorkBuddy) --------------------------------------

    def conversation_create(self, request, body, state):
        return ok({"id": f"conv-{uuid.uuid4().hex[:12]}"})

    def conversation_session(self, request, body, state, conversation):
        return ok({"link": "https://sandbox.preview.invalid/acp", "token": "preview-sandbox",
                   "sessionId": conversation, "cwd": "/workspace"})

    def conversation_status(self, request, body, state, conversation):
        self._charge(state, 0.5)
        return ok({"status": "completed"})

    # -- releases -----------------------------------------------------------------------

    @staticmethod
    def releases():
        gateway = sys.modules.get("converter")
        version = getattr(gateway, "APP_VERSION", "0.0.0")
        return Reply(payload=[{
            "tag_name": f"v{version}", "name": f"v{version}", "draft": False, "prerelease": False,
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * DAY)),
            "html_url": f"https://github.com/maiphucgiang/codebuddy2api/releases/tag/v{version}",
            "body": "## 预览环境\n\n- 发布列表为模拟数据，安装已停用。",
            "assets": [{"name": f"codebuddy2api-{version}.tar.gz", "digest": "sha256:" + "0" * 64},
                       {"name": "SHA256SUMS"}]}])

    @staticmethod
    def archive(host):
        """Serve gzip-looking bytes at a steady per-host pace, so the mirror speed test ranks sources."""
        chunks = [b"\x1f\x8b" + bytes(ARCHIVE_CHUNK - 2)] + [bytes(ARCHIVE_CHUNK)] * 7
        return Reply(chunks=chunks, delay=0.004 + zlib.crc32(host.encode()) % 30 / 1000, media="application/gzip")


def _departed_today(state: State) -> int:
    return state.departures.get(time.strftime("%Y-%m-%d"), 0)


class _Channel:
    """A finished ACP turn: one cost update, then silence."""

    def __init__(self):
        self._updates = [{"cost": {"amount": 0.5}}]

    def drain(self, timeout):
        updates, self._updates = self._updates, []
        return updates

    def close(self):
        pass


def acp_turn(link, token, session_id, cwd, prompt, *, on_prompt=None):
    """Stand in for ``acp_client.run_turn``, whose raw http.client channel bypasses httpx."""
    if on_prompt is not None:
        on_prompt()
    return _Channel()


def _last_text(messages, role):
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == role:
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _answer(prompt: str, messages, model: str, long: bool) -> str:
    if messages and messages[-1].get("role") == "tool":
        result = str(messages[-1].get("content") or "")[:120]
        return f"工具已返回结果：{result}\n\n这是预览环境的模拟回复，没有调用真实模型。"
    quoted = DIRECTIVE.sub("", prompt).strip()[:160] or "（空消息）"
    english = not re.search(r"[一-鿿]", quoted)
    text = (f"This is a simulated reply from {model} in the codebuddy2api preview. You said: {quoted}"
            if english else f"这是 codebuddy2api 预览环境中 {model} 的模拟回复。你刚才说：{quoted}")
    return text + ("\n\n" + "\n".join(f"{i}. 模拟的长输出第 {i} 行。" for i in range(1, 41)) if long else "")


def _tool_call(tools, directive):
    """Call a tool only on request: a client may execute whatever it is told to run."""
    if "tool" not in directive or not tools:
        return None
    functions = [t.get("function") or t for t in tools if isinstance(t, dict)]
    function = next((f for f in functions if f.get("name") == directive["tool"]), functions[0])
    schema = function.get("parameters") or function.get("input_schema") or {}
    arguments = {name: _placeholder(spec) for name, spec in (schema.get("properties") or {}).items()
                 if name in (schema.get("required") or [])}
    return {"id": "call_preview", "name": function.get("name") or "tool",
            "arguments": json.dumps(arguments, ensure_ascii=False)}


def _placeholder(spec):
    if not isinstance(spec, dict):
        return "preview"
    if spec.get("enum"):
        return spec["enum"][0]
    defaults = {"integer": 1, "number": 1, "boolean": False, "array": [], "object": {}}
    return defaults.get(str(spec.get("type")), "preview")
