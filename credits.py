#!/usr/bin/env python3
"""credits.py — 每日签到 + 积分查询 + 快过期优先调度支持。

HTTP 流程（官方 Web 端接口，Bearer 鉴权）：
  签到  POST {host}/billing/meter/daily-checkin（兜底 /v2/...）
  积分  POST {host}/v2/billing/meter/get-user-resource
接口域名按 access token 的 JWT issuer 映射，解析失败时按国内双 host 兜底。
"""

import base64
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = 12.0

ISSUER_HOST_MAP = {
    "https://www.workbuddy.ai": "https://www.workbuddy.ai",
    "https://www.workbuddy.cn": "https://www.workbuddy.cn",
    "https://www.codebuddy.cn": "https://www.codebuddy.cn",
    "https://www.codebuddy.ai": "https://www.codebuddy.ai",
}
DEFAULT_HOSTS = ["https://www.codebuddy.cn", "https://www.workbuddy.cn"]
CHECKIN_PATHS = ("/billing/meter/daily-checkin", "/v2/billing/meter/daily-checkin")
RESOURCE_PATH = "/v2/billing/meter/get-user-resource"
CONFIG_PATH = "/v3/config"  # cbc CLI CloudProductProvider 同源：云端模型表
RESOURCE_PRODUCT_CODE = "p_tcaca"

_INACTIVE_RE = re.compile(r"未开启|未开始|未开放|已过期|无.*活动|活动.*(?:结束|关闭|暂停)", re.I)
_ALREADY_RE = re.compile(r"已签到|已领取|已经.*(?:签到|领取)|重复签到|already", re.I)


class AuthExpiredError(Exception):
    """签到/积分接口 401：token 失效（重试无意义，由上层记录）。"""


# ---------------------------------------------------------------------------
# 域名选择
# ---------------------------------------------------------------------------

def token_issuer_origin(access_token: str) -> str | None:
    """解码 JWT payload 的 iss 字段，返回 origin（如 https://www.codebuddy.cn）。"""
    try:
        part = access_token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        iss = str(payload.get("iss") or "")
        m = re.match(r"(https?://[^/]+)", iss)
        return m.group(1).lower() if m else None
    except Exception:
        return None


def hosts_for_token(access_token: str) -> list[str]:
    """按 token issuer 选接口域名；未识别时国内双 host 兜底。"""
    host = ISSUER_HOST_MAP.get(token_issuer_origin(access_token) or "")
    return [host] if host else list(DEFAULT_HOSTS)


def _web_headers(api_host: str, access_token: str, uid: str = "", domain: str = "") -> dict:
    """与官方 Web 端一致的请求头。"""
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "x-client-platform": "web",
        "origin": api_host,
        "referer": f"{api_host}/profile/plans-usage",
        "authorization": f"Bearer {access_token}",
        "x-user-id": str(uid or ""),
        "x-domain": str(domain or ""),
        "user-agent": BROWSER_UA,
    }


def _post_json(client: httpx.Client, url: str, headers: dict, body: dict) -> tuple[int, dict]:
    """POST JSON 并解析响应；401 抛 AuthExpiredError；返回 (status, payload)。"""
    r = client.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    if r.status_code == 401:
        raise AuthExpiredError("登录身份过期")
    try:
        payload = r.json()
    except Exception:
        payload = {}
    return r.status_code, payload


# ---------------------------------------------------------------------------
# 每日签到
# ---------------------------------------------------------------------------

def classify_checkin_result(http_ok: bool, code, message: str) -> dict:
    """只接受明确成功：code=0；或 code=10001 且文案表明今日已签（幂等）。"""
    try:
        ncode = int(code) if code is not None and str(code).strip() != "" else None
    except (TypeError, ValueError):
        ncode = None
    text = str(message or "")
    inactive = bool(_INACTIVE_RE.search(text))
    already = ncode == 10001 and not inactive and bool(_ALREADY_RE.search(text))
    ok = not inactive and ((ncode == 0 and http_ok) or already)
    return {"ok": ok, "already": already, "inactive": inactive, "code": ncode, "message": text}


def daily_checkin(access_token: str, uid: str = "", domain: str = "") -> dict:
    """多 host × 多 path 兜底签到；返回 classify 结果 + status/url。全部失败返回 ok=False。"""
    endpoints = [h + p for h in hosts_for_token(access_token) for p in CHECKIN_PATHS]
    last_err = "未知错误"
    first_401: dict | None = None
    with httpx.Client() as client:
        for url in endpoints:
            host = url.split("/v2/billing")[0].split("/billing")[0]  # 先剥 /v2 再剥 /billing 取 origin
            try:
                status, payload = _post_json(client, url, _web_headers(host, access_token, uid, domain), {})
            except AuthExpiredError:
                first_401 = first_401 or {"ok": False, "already": False, "code": 401,
                                          "message": "登录身份过期", "status": 401, "url": url}
                last_err = "登录身份过期"
                continue
            except httpx.HTTPError as e:
                last_err = str(e)
                continue
            message = payload.get("msg") or payload.get("message") or ("ok" if 200 <= status < 300 else f"HTTP {status}")
            result = classify_checkin_result(200 <= status < 300, payload.get("code"), message)
            result.update(status=status, url=url)
            if result["ok"]:
                return result
            if 400 <= status < 500 and status != 404:
                return result  # 客户端错误（除 404 换 path）：不再兜底
            last_err = str(message)
    if first_401 is not None:
        return first_401
    return {"ok": False, "already": False, "inactive": False, "code": -1,
            "message": last_err, "url": endpoints[0] if endpoints else None}


# ---------------------------------------------------------------------------
# 积分查询（分段 + 过期时间）
# ---------------------------------------------------------------------------

_REMAINING_FIELDS = (
    "SlicePeriodCapacityRemainPrecise", "SlicePeriodCapacityRemain",
    "CycleCapacityRemainPrecise", "CycleCapacityRemain",
    "CapacityRemainPrecise", "CapacityRemain",
    "RemainPrecise", "Remain", "Remaining", "Balance",
)
_TOTAL_FIELDS = (
    "SlicePeriodCapacitySizePrecise", "SlicePeriodCapacitySize",
    "CycleCapacitySizePrecise", "CycleCapacitySize",
    "CycleCapacityPrecise", "CycleCapacity",
    "CapacityPrecise", "Capacity", "TotalCapacityPrecise", "TotalCapacity",
    "PackageCapacity", "Quota", "Amount",
)
_EXPIRY_FIELDS = (
    "DeductionEndTime", "ExpiredTime", "SlicePeriodEndTime", "PackageEndTime",
    "EndTime", "CycleEndTime", "ExpireTime", "ExpirationTime",
    "ValidEndTime", "ValidPeriodEndTime", "EndAt", "ExpireAt",
)
_LABEL_FIELDS = (
    "PackageName", "PackageTypeName", "AccountName", "ProductName",
    "Name", "RuleName", "Description",
)


def _first_number(item: dict, fields) -> float | None:
    for f in fields:
        raw = item.get(f)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _parse_ts(value) -> float | None:
    """秒/毫秒 epoch 或 'YYYY-MM-DD HH:MM:SS' → epoch 秒。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or re.match(r"^\d+(?:\.\d+)?$", str(value).strip()):
        n = float(value)
        return n / 1000.0 if n >= 1e12 else n
    try:
        from datetime import datetime
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None


def _first_ts(item: dict, fields) -> float | None:
    for f in fields:
        ts = _parse_ts(item.get(f))
        if ts is not None:
            return ts
    return None


def _first_text(item: dict, fields) -> str:
    for f in fields:
        v = item.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def extract_segments(accounts: list) -> list[dict]:
    """从资源 Account 列表提取积分段（remaining>0），SlicePeriodUsageDetails 有明细则展开。"""
    out = []
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        details = account.get("SlicePeriodUsageDetails")
        items = [dict(account, **d) for d in details if isinstance(d, dict)] \
            if isinstance(details, list) and details else [account]
        for item in items:
            remaining = _first_number(item, _REMAINING_FIELDS)
            if remaining is None or remaining <= 0:
                continue
            total = _first_number(item, _TOTAL_FIELDS)
            out.append({
                "remaining": round(remaining, 2),
                "total": round(max(total, remaining) if total is not None else remaining, 2),
                "expires_at": _first_ts(item, _EXPIRY_FIELDS),
                "source": _first_text(item, _LABEL_FIELDS) or "积分",
                "package_code": str(item.get("PackageCode") or ""),
            })
    return out


def merge_segments(segments: list) -> list[dict]:
    """按 (package_code|source, expires_at) 合并同包多记录，按过期时间升序（无过期时间排最后）。"""
    merged: dict = {}
    for s in segments or []:
        if not s or float(s.get("remaining") or 0) <= 0:
            continue
        key = (s.get("package_code") or s.get("source") or "积分", s.get("expires_at"))
        if key in merged:
            merged[key]["remaining"] += float(s["remaining"])
            merged[key]["total"] += float(s.get("total") or s["remaining"])
        else:
            merged[key] = {
                "remaining": float(s["remaining"]),
                "total": float(s.get("total") or s["remaining"]),
                "expires_at": s.get("expires_at"),
                "source": str(s.get("source") or "积分"),
                "package_code": str(s.get("package_code") or ""),
            }
    result = list(merged.values())
    for s in result:
        s["remaining"] = round(s["remaining"], 2)
        s["total"] = round(s["total"], 2)
    result.sort(key=lambda s: (s["expires_at"] is None, s["expires_at"] or 0))
    return result


def soonest_expiry(segments: list, now: float | None = None) -> float | None:
    """未过期且有余量积分段的最早过期时间；全部无过期时间/无段时返回 None。"""
    now = time.time() if now is None else now
    exps = [s["expires_at"] for s in segments or []
            if s.get("expires_at") is not None and s["expires_at"] > now
            and float(s.get("remaining") or 0) > 0]
    return min(exps) if exps else None


def _resource_body() -> dict:
    """与官方 Web 端一致：有效状态 [0,3]，结束时间范围 现在 ~ +101 年（只取未过期包）。"""
    fmt = "%Y-%m-%d %H:%M:%S"
    return {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": RESOURCE_PRODUCT_CODE,
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": time.strftime(fmt),
        "PackageEndTimeRangeEnd": time.strftime(fmt, time.localtime(time.time() + 101 * 365 * 86400)),
    }


def fetch_credits(access_token: str, uid: str = "", domain: str = "") -> dict:
    """查询剩余积分：{credits, count, segments, soonest_expiry}。空结果重试，401 抛 AuthExpiredError。"""
    host = hosts_for_token(access_token)[0]
    url = host + RESOURCE_PATH
    headers = _web_headers(host, access_token, uid, domain)
    last_err: Exception | None = None
    with httpx.Client() as client:
        for attempt in range(3):
            try:
                status, payload = _post_json(client, url, headers, _resource_body())
            except AuthExpiredError:
                raise
            except httpx.HTTPError as e:
                last_err = e
                time.sleep(0.3 * (attempt + 1))
                continue
            if status != 200:
                last_err = RuntimeError(f"积分接口 HTTP {status}")
                time.sleep(0.3 * (attempt + 1))
                continue
            code = payload.get("code")
            if code not in (0, None):
                raise RuntimeError(str(payload.get("msg") or f"积分接口 code={code}"))
            data = payload.get("data") or {}
            resp = (data.get("Response") or {}).get("Data") or (data.get("data") or {}).get("Response", {}).get("Data") or data
            accounts = resp.get("Accounts") or payload.get("data", {}).get("accounts") or []
            if not accounts and attempt < 2:  # 偶发空 Accounts，重试一次
                time.sleep(0.3 * (attempt + 1))
                continue
            segments = merge_segments(extract_segments(accounts))
            credits = round(sum(s["remaining"] for s in segments), 2)
            return {"credits": credits, "count": len(accounts), "segments": segments,
                    "soonest_expiry": soonest_expiry(segments),
                    "intl": is_international_host(host)}
    raise RuntimeError(f"积分查询失败: {last_err}")



def fetch_model_catalog(access_token: str, user_agent: str = "") -> list[dict]:
    """GET {host}/v3/config 拉云端模型表（cbc CLI CloudProductProvider 同源）。

    必须 x-client-platform: cli（否则 400）；只接受 GET（POST 404）。
    返回 [{id, name, credits, supportsToolCall, ...}]；失败抛异常。"""
    last_err: Exception | None = None
    for host in hosts_for_token(access_token):
        headers = {"accept": "application/json", "authorization": f"Bearer {access_token}",
                   "x-client-platform": "cli"}
        if user_agent:
            headers["user-agent"] = user_agent
        try:
            with httpx.Client() as client:
                r = client.get(host + CONFIG_PATH, headers=headers, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as e:
            last_err = e
            continue
        if r.status_code == 401:
            raise AuthExpiredError("登录身份过期")
        if r.status_code != 200:
            last_err = RuntimeError(f"模型配置接口 HTTP {r.status_code}")
            continue
        try:
            payload = r.json()
        except Exception:
            last_err = RuntimeError("模型配置接口返回无法解析")
            continue
        if payload.get("code") != 0:
            raise RuntimeError(str(payload.get("msg") or f"模型配置接口 code={payload.get('code')}"))
        models = (payload.get("data") or {}).get("models") or []
        return [m for m in models if isinstance(m, dict) and m.get("id")]
    raise RuntimeError(f"模型配置拉取失败: {last_err}")


# ---------------------------------------------------------------------------
# 积分折算为金额（OpenAI 余额口径）
# ---------------------------------------------------------------------------

# 官方无任何金额接口（实测 12 个候选端点全 404），也不公开 credit↔token 单价；
# 折算锚点取官方《计费概述》旗舰版连续包月 700 元 / 50,000 积分 = 0.014 元/Credit。
CREDIT_PRICE_CNY = 0.014   # 国内：旗舰版连续包月 700 元 / 50,000 积分摊算
CREDIT_PRICE_USD = 0.03    # 国际：Pro 加量包 $15 / 500 Credits（与国内不同体系，须分开折算）
USD_RATE_CNY = 7.15


def is_international_host(host: str) -> bool:
    """国际站判定。.ai 与国内站账号/积分完全隔离（实测国内 token 打 .ai 一律 401）。"""
    return bool(host) and ".ai" in str(host).lower()

USAGE_PATH = "/billing/meter/get-user-request-usage"
USAGE_MAX_DAYS = 30   # 官方硬限制：时间跨度 >31 天静默返回 total=0（不报错）
USAGE_PAGE_SIZE = 200
USAGE_MAX_PAGES = 30


def credits_to_usd(amount: float, price_cny: float = CREDIT_PRICE_CNY,
                   rate: float = USD_RATE_CNY) -> float:
    """Credits → 美元：先按订阅摊算折算人民币，再按汇率换美元。"""
    return float(amount or 0) * price_cny / rate


def usd_per_credit(is_intl: bool, price_cny: float = CREDIT_PRICE_CNY,
                   price_usd: float = CREDIT_PRICE_USD,
                   rate: float = USD_RATE_CNY) -> float:
    """每 Credit 的美元价值：国际站用 USD 单价，国内站按 CNY 单价除以汇率。"""
    return price_usd if is_intl else price_cny / rate


def aggregate_credits(creds_snapshot: dict) -> dict:
    """汇总 ledger 快照，并按国内/国际分组（两站积分独立、单价不同，必须分组折算）。

    顶层为合计值，groups 内为各组明细；含剩余、额度差已用、最早过期时间。"""
    groups = {k: {"remaining": 0.0, "used_by_quota": 0.0, "soonest_expiry": None}
              for k in ("domestic", "international")}
    for e in (creds_snapshot or {}).values():
        c = e.get("credits") or {}
        g = groups["international" if c.get("intl") else "domestic"]
        for s in c.get("segments") or []:
            rem = float(s.get("remaining") or 0)
            g["remaining"] += rem
            g["used_by_quota"] += max(float(s.get("total") or 0) - rem, 0.0)
            exp = s.get("expires_at")
            if exp and (g["soonest_expiry"] is None or exp < g["soonest_expiry"]):
                g["soonest_expiry"] = exp
    for g in groups.values():
        g["remaining"] = round(g["remaining"], 2)
        g["used_by_quota"] = round(g["used_by_quota"], 2)
    exps = [g["soonest_expiry"] for g in groups.values() if g["soonest_expiry"] is not None]
    return {"remaining": round(sum(g["remaining"] for g in groups.values()), 2),
            "used_by_quota": round(sum(g["used_by_quota"] for g in groups.values()), 2),
            "soonest_expiry": min(exps) if exps else None,
            "groups": groups}



def fetch_request_usage(access_token: str, days: int = USAGE_MAX_DAYS,
                        uid: str = "", domain: str = "") -> dict:
    """拉官方用量明细，按 日期×模型 聚合实际扣减的 credits。

    返回 {by_day: {'YYYY-MM-DD': {model: credits}}, total_credits, requests}。
    跨度超 31 天官方会静默返回空，故 days 强制夹到 USAGE_MAX_DAYS。"""
    days = max(1, min(int(days or USAGE_MAX_DAYS), USAGE_MAX_DAYS))
    host = hosts_for_token(access_token)[0]
    url = host + USAGE_PATH
    headers = _web_headers(host, access_token, uid, domain)
    fmt = "%Y-%m-%d %H:%M:%S"
    now = time.time()
    body_base = {"startTime": time.strftime(fmt, time.localtime(now - days * 86400)),
                 "endTime": time.strftime(fmt, time.localtime(now))}
    by_day: dict = {}
    total_credits = 0.0
    requests = 0
    with httpx.Client() as client:
        for page in range(1, USAGE_MAX_PAGES + 1):
            status, payload = _post_json(client, url, headers,
                                         dict(body_base, pageNum=page, pageSize=USAGE_PAGE_SIZE))
            if status != 200:
                raise RuntimeError(f"用量明细接口 HTTP {status}")
            data = payload.get("data") or {}
            rows = data.get("data") or []
            for row in rows:
                date = str(row.get("requestTime") or "")[:10]
                model = str(row.get("model") or "unknown")
                credit = float(row.get("credit") or 0)
                if not date:
                    continue
                by_day.setdefault(date, {})
                by_day[date][model] = round(by_day[date].get(model, 0.0) + credit, 6)
                total_credits += credit
                requests += 1
            if requests >= int(data.get("total") or 0) or not rows:
                break
    return {"by_day": by_day, "total_credits": round(total_credits, 2), "requests": requests}


# ---------------------------------------------------------------------------
# CreditLedger：按凭证缓存签到/积分状态，JSON 原子持久化
# ---------------------------------------------------------------------------

class CreditLedger:
    """{cred_id: {checkin, credits, error}} 持久化缓存；soonest_expiry 供凭证池排序。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict = {"version": 1, "creds": {}}
        self._load()

    def _load(self):
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if "creds" not in self._data:
                self._data = {"version": 1, "creds": {}}
        except Exception:
            self._data = {"version": 1, "creds": {}}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _entry(self, cred_id: str) -> dict:
        return self._data["creds"].setdefault(cred_id, {"checkin": {}, "credits": {}, "error": None})

    # ---- 签到 ----

    def checkin_done(self, cred_id: str, day: str) -> bool:
        with self._lock:
            c = self._entry(cred_id).get("checkin") or {}
            return c.get("date") == day and c.get("ok") is True

    def mark_checkin(self, cred_id: str, day: str, ok: bool, code, message: str):
        with self._lock:
            self._entry(cred_id)["checkin"] = {
                "date": day, "ok": bool(ok), "code": code,
                "message": str(message or "")[:200], "at": time.time(),
            }
            self._save()

    # ---- 积分 ----

    def update_credits(self, cred_id: str, result: dict):
        with self._lock:
            e = self._entry(cred_id)
            e["credits"] = {
                "credits": result.get("credits"),
                "count": result.get("count", 0),
                "segments": result.get("segments") or [],
                "soonest_expiry": result.get("soonest_expiry"),
                "fetched_at": time.time(),
                "intl": bool(result.get("intl")),  # 站点归属：国内/国际积分与单价均独立
            }
            e["error"] = None
            self._save()

    def note_error(self, cred_id: str, message: str):
        with self._lock:
            self._entry(cred_id)["error"] = str(message)[:200]
            self._save()

    def soonest_expiry_of(self, cred_id: str) -> float | None:
        """pick 排序键：该凭证最早过期积分时间；无数据返回 None（排最后）。"""
        with self._lock:
            return soonest_expiry((self._data["creds"].get(cred_id) or {}).get("credits", {}).get("segments"))

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data["creds"]))


class ModelCatalogCache:
    """云端模型表按站点分组持久化缓存，TTL 内不重复拉取。

    启动即加载缓存对外可用（不必等首轮同步）；仅 TTL 过期才打 /v3/config。"""

    def __init__(self, path: Path, ttl: float = 6 * 3600):
        self.path = Path(path)
        self.ttl = max(60.0, float(ttl or 0))
        self._lock = threading.Lock()
        self._data: dict = {"version": 1, "groups": {}}
        self._load()

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d.get("groups"), dict):
                self._data = d
        except Exception:
            self._data = {"version": 1, "groups": {}}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def group_for_token(access_token: str) -> str:
        """凭证所属站点组：domestic / international。"""
        return ("international" if is_international_host(hosts_for_token(access_token)[0])
                else "domestic")

    def fresh(self, group: str) -> bool:
        """该组缓存是否仍在 TTL 内（在则本轮无需拉云端）。"""
        with self._lock:
            g = self._data["groups"].get(group) or {}
            age = time.time() - float(g.get("fetched_at") or 0)
            return bool(g.get("models")) and age < self.ttl

    def models(self, group: str) -> list[dict]:
        with self._lock:
            return list((self._data["groups"].get(group) or {}).get("models") or [])

    def age(self, group: str) -> float | None:
        """缓存年龄秒数；无缓存返回 None。"""
        with self._lock:
            ts = (self._data["groups"].get(group) or {}).get("fetched_at")
            return (time.time() - float(ts)) if ts else None

    def put(self, group: str, models: list[dict]):
        with self._lock:
            self._data["groups"][group] = {"models": models, "fetched_at": time.time()}
            self._save()
