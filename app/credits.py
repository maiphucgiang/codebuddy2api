#!/usr/bin/env python3
"""Manage check-in, balances and credit-aware scheduling within each account's product and region."""

import base64
from copy import deepcopy
import json
import math
import os
import re
import threading
import time
from pathlib import Path

import httpx

from .client_profiles import catalog_headers
from .site_routing import PROFILE_ENDPOINTS, profile_for_auth, profile_product

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = 12.0

# Billing uses product websites rather than the CLI chat endpoint.
BILLING_PROFILE_HOSTS = {
    "cn-cli": "https://www.codebuddy.cn",
    "cn-work": "https://www.workbuddy.cn",
    "intl-cli": "https://www.codebuddy.ai",
    "intl-work": "https://www.workbuddy.ai",
}
CHECKIN_PATHS = ("/v2/billing/meter/daily-checkin",)
CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
RESOURCE_PATH = "/v2/billing/meter/get-user-resource"
CONFIG_PATH = "/v3/config"  # Official cloud model catalog
RESOURCE_PRODUCT_CODE = "p_tcaca"
CREDITS_PAGE_SIZE = 100
CREDITS_MAX_PAGES = 20  # Mark capped results partial instead of reporting complete balances.

_INACTIVE_RE = re.compile(r"未开启|未开始|未开放|已过期|无.*活动|活动.*(?:结束|关闭|暂停)", re.I)
_ALREADY_RE = re.compile(r"已签到|已领取|已经.*(?:签到|领取)|重复签到|already", re.I)


class AuthExpiredError(Exception):
    """Signal an expired token from a billing HTTP 401 response."""


# ---------------------------------------------------------------------------
# Billing host selection
# ---------------------------------------------------------------------------

def token_issuer_origin(access_token: str) -> str | None:
    """Decode the JWT issuer and return its origin."""
    try:
        part = access_token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        iss = str(payload.get("iss") or "")
        m = re.match(r"(https?://[^/]+)", iss)
        return m.group(1).lower() if m else None
    except Exception:
        return None


def hosts_for_token(access_token: str, domain: str = "") -> list[str]:
    """Resolve the credential's billing host, rejecting unknown or conflicting identities."""
    profile = profile_for_auth({"accessToken": access_token, "domain": domain})
    return [BILLING_PROFILE_HOSTS[profile]]


def _web_headers(api_host: str, access_token: str, uid: str = "", domain: str = "") -> dict:
    """Build website billing headers independently of CLI model catalog headers."""
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
    """POST JSON and return status/payload, raising AuthExpiredError on HTTP 401."""
    r = client.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    if r.status_code == 401:
        raise AuthExpiredError("登录身份过期")
    try:
        payload = r.json()
    except Exception:
        payload = {}
    return r.status_code, payload


# ---------------------------------------------------------------------------
# Daily check-in
# ---------------------------------------------------------------------------

def classify_checkin_result(http_ok: bool, code, message: str) -> dict:
    """Normalize check-in codes without confusing inactive activities with completed claims."""
    try:
        ncode = int(code) if type(code) in (int, str) else None
    except ValueError:
        ncode = None
    text = str(message or "")
    inactive = ncode == 1003 or bool(_INACTIVE_RE.search(text))
    already = not inactive and (ncode == 1001 or (ncode == 10001 and bool(_ALREADY_RE.search(text))))
    ok = not inactive and ((ncode == 0 and http_ok) or already)
    state = ("already" if already else "success" if ok else "inactive" if inactive else
             "not_eligible" if ncode == 1002 else "error")
    return {"ok": ok, "already": already, "inactive": inactive, "state": state, "code": ncode, "message": text}


def _checkin_request(access_token, uid, domain, path):
    host = hosts_for_token(access_token, domain)[0]
    headers = {"accept": "application/json", "content-type": "application/json",
               "authorization": f"Bearer {access_token}", "x-user-id": str(uid or ""),
               "x-domain": str(domain or "")}
    url = host + path
    with httpx.Client() as client:
        try:
            status, payload = _post_json(client, url, headers, {})
        except AuthExpiredError:
            return 401, {"code": 401}, url
        except httpx.HTTPError:
            return 0, {"code": -1}, url
    return status, payload if isinstance(payload, dict) else {}, url


def daily_checkin(access_token: str, uid: str = "", domain: str = "") -> dict:
    """Call the same-profile check-in endpoint without replaying uncertain claims."""
    status, payload, url = _checkin_request(access_token, uid, domain, CHECKIN_PATHS[0])
    message = payload.get("msg") or payload.get("message") or ""
    result = classify_checkin_result(200 <= status < 300, payload.get("code"), message)
    if not (200 <= status < 300 or status in (400, 409)):
        result.update(ok=False, already=False, inactive=False, state="error")
    result.update(status=status, url=url)
    return result


def fetch_checkin_status(access_token: str, uid: str = "", domain: str = "") -> dict:
    """Query activity status without authorizing claims from incomplete responses."""
    status, payload, _ = _checkin_request(access_token, uid, domain, CHECKIN_STATUS_PATH)
    result = classify_checkin_result(200 <= status < 300, payload.get("code"),
                                     payload.get("msg") or payload.get("message") or "")
    if not (200 <= status < 300 or status in (400, 409)):
        return {"ok": False, "state": "unknown", "code": result["code"]}
    if result["state"] in {"already", "inactive", "not_eligible"}:
        return result
    data = payload.get("data")
    if not (200 <= status < 300 and result["code"] == 0 and isinstance(data, dict)
            and type(data.get("active")) is bool):
        return {"ok": False, "state": "unknown", "code": result["code"]}
    if not data["active"]:
        return {"ok": False, "state": "inactive", "code": 0}
    if type(data.get("today_checked_in")) is not bool:
        return {"ok": False, "state": "unknown", "code": 0}
    return {"ok": data["today_checked_in"], "already": data["today_checked_in"],
            "state": "already" if data["today_checked_in"] else "available", "code": 0}


# ---------------------------------------------------------------------------
# Credit segments and expiry
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
    """Convert epoch seconds, milliseconds or formatted timestamps to epoch seconds."""
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
    """Extract positive credit segments, expanding slice-level usage when available."""
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
    """Merge matching package/expiry segments and sort unknown expiries last."""
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
    """Return the earliest known expiry among unexpired positive balances."""
    now = time.time() if now is None else now
    exps = [s["expires_at"] for s in segments or []
            if s.get("expires_at") is not None and s["expires_at"] > now
            and float(s.get("remaining") or 0) > 0]
    return min(exps) if exps else None


def _resource_body(page: int) -> dict:
    """Build the official active-package filter for future expiry dates."""
    fmt = "%Y-%m-%d %H:%M:%S"
    return {
        "PageNumber": page,
        "PageSize": CREDITS_PAGE_SIZE,
        "ProductCode": RESOURCE_PRODUCT_CODE,
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": time.strftime(fmt),
        "PackageEndTimeRangeEnd": time.strftime(fmt, time.localtime(time.time() + 101 * 365 * 86400)),
    }


def _fetch_accounts_page(client, url: str, headers: dict, page: int, *, retry_empty: bool) -> list:
    """Fetch one credit page with bounded retries, distinguishing expired authentication."""
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            status, payload = _post_json(client, url, headers, _resource_body(page))
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
        # Missing account structure is not a confirmed zero balance.
        accounts = None
        if isinstance(resp, dict) and isinstance(resp.get("Accounts"), list):
            accounts = resp["Accounts"]
        elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("accounts"), list):
            accounts = payload["data"]["accounts"]
        if accounts is None:
            last_err = RuntimeError("积分接口返回缺少 Accounts 结构")
            time.sleep(0.3 * (attempt + 1))
            continue
        if not accounts and retry_empty and attempt < 2:  # Retry a transient empty page once.
            time.sleep(0.3 * (attempt + 1))
            continue
        return accounts
    raise RuntimeError(f"积分查询失败: {last_err}")


def fetch_credits(access_token: str, uid: str = "", domain: str = "") -> dict:
    """Fetch credit segments and mark page-limited results partial; HTTP 401 raises AuthExpiredError."""
    host = hosts_for_token(access_token, domain)[0]
    url = host + RESOURCE_PATH
    headers = _web_headers(host, access_token, uid, domain)
    accounts: list = []
    partial = False
    with httpx.Client() as client:
        page = 0
        while True:
            page += 1
            if page > CREDITS_MAX_PAGES:
                partial = True
                break
            rows = _fetch_accounts_page(client, url, headers, page, retry_empty=True)  # Empty pages may be transient.
            accounts.extend(rows)
            if len(rows) < CREDITS_PAGE_SIZE:
                break
    segments = merge_segments(extract_segments(accounts))
    credits = round(sum(s["remaining"] for s in segments), 2)
    return {"credits": credits, "count": len(accounts), "segments": segments,
            "soonest_expiry": soonest_expiry(segments),
            "intl": is_international_host(host), "partial": partial}



def select_product_models(data: dict, product: str = "cli", *, scope: str = "picker") -> list[dict]:
    """Parse product selector/root catalogs while honoring disabled and available-model filters."""
    if product not in ("cli", "workbuddy"):
        raise ValueError("未知模型目录产品")
    if scope not in ("picker", "account"):
        raise ValueError("未知模型目录作用域")
    def invalid(field: str):
        raise ValueError(f"模型配置格式错误: {field}")

    def text(value) -> bool:
        return isinstance(value, str) and bool(value.strip())

    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        invalid("data.models")
    models = data["models"]
    by_id: dict = {}
    by_name: dict = {}
    by_alias: dict = {}
    for model in models:
        if not isinstance(model, dict) or not text(model.get("id")):
            invalid("data.models.id")
        if "name" in model and not text(model["name"]):
            invalid("data.models.name")
        aliases = model.get("aliases", [])
        if not isinstance(aliases, list) or not all(text(a) for a in aliases):
            invalid("data.models.aliases")
        if model["id"] in by_id:
            invalid("data.models.id duplicate")
        by_id[model["id"]] = model
        if "name" in model:
            by_name.setdefault(model["name"], model)
        for alias in aliases:
            by_alias.setdefault(alias, model)

    available = data.get("availableModels")
    if available is not None and (not isinstance(available, list) or not all(text(value) for value in available)):
        invalid("data.availableModels")

    # WorkBuddy treats an empty availableModels list as no additional filter.
    def finish(items):
        return deepcopy([model for model in items if not model.get("disabled")
                         and (not available or model["id"] in available)])

    cli = None
    if "agents" in data:
        if not isinstance(data["agents"], list):
            invalid("data.agents")
        for agent in data["agents"]:
            if not isinstance(agent, dict) or not text(agent.get("name")):
                invalid("data.agents.name")
            tags = agent.get("tags")
            if tags is not None and (not isinstance(tags, list) or not all(text(tag) for tag in tags)):
                invalid("data.agents.tags")
            if agent["name"] == "cli":
                if cli is not None:
                    invalid("data.agents.cli duplicate")
                cli = agent
    if product == "workbuddy" and isinstance(data.get("agents"), list):
        default = next((agent for agent in data["agents"] if "default" in (agent.get("tags") or [])), None)
        fallback = next((agent for agent in data["agents"] if isinstance(agent.get("models"), list) and agent["models"]), None)
        cli = default or cli or fallback
    if scope == "account" or cli is None or "models" not in cli:
        return finish(models)
    if not isinstance(cli["models"], list):
        invalid("data.agents.cli.models")

    selected = []
    seen = set()
    for reference in cli["models"]:
        if text(reference):
            keys = [reference]
        elif isinstance(reference, dict):
            keys = [reference[k] for k in ("id", "name") if k in reference]
            if not keys or not all(text(k) for k in keys):
                invalid("data.agents.cli.models reference")
        else:
            invalid("data.agents.cli.models reference")
        model = next((index[key] for index in (by_id, by_name, by_alias)
                      for key in keys if key in index), None)
        if model is not None and model["id"] not in seen:
            selected.append(model)
            seen.add(model["id"])
    if cli["models"] and not selected:
        invalid("data.agents.cli.models unresolved")
    return finish(selected)


def select_cli_models(data: dict) -> list[dict]:
    return select_product_models(data, "cli")


def fetch_model_scopes(access_token: str, user_agent: str = "", *, domain: str = "",
                       uid: str = "", enterprise_id: str = "") -> dict[str, list[dict]]:
    """Fetch selector and account-root catalogs without assuming every candidate is servable."""
    auth = {"accessToken": access_token, "domain": domain}
    profile = profile_for_auth(auth)
    headers = catalog_headers(auth, {"uid": uid, "enterpriseId": enterprise_id}, user_agent=user_agent)
    try:
        with httpx.Client() as client:
            response = client.get(PROFILE_ENDPOINTS[profile] + CONFIG_PATH, headers=headers, timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError:
        raise RuntimeError("模型配置接口网络错误") from None
    if response.status_code == 401:
        raise AuthExpiredError("登录身份过期")
    if response.status_code != 200:
        raise RuntimeError(f"模型配置接口 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise ValueError("模型配置接口返回无法解析") from None
    if not isinstance(payload, dict):
        raise ValueError("模型配置格式错误: response")
    if payload.get("code") != 0:
        raise RuntimeError("模型配置接口返回非成功状态")
    product = profile_product(profile)
    return {"picker": select_product_models(payload.get("data"), product),
            "account": select_product_models(payload.get("data"), product, scope="account")}


def fetch_model_catalog(access_token: str, user_agent: str = "", *, domain: str = "",
                        uid: str = "", enterprise_id: str = "") -> list[dict]:
    """Return selector models for this credential's product."""
    return fetch_model_scopes(access_token, user_agent, domain=domain, uid=uid,
                              enterprise_id=enterprise_id)["picker"]


# ---------------------------------------------------------------------------
# OpenAI-compatible billing estimates
# ---------------------------------------------------------------------------

# Estimate monetary value using independent domestic and international package rates.
CREDIT_PRICE_CNY = 0.014   # CNY 700 / 50,000 credits
CREDIT_PRICE_USD = 0.03    # USD 15 / 500 credits
USD_RATE_CNY = 7.15


def is_international_host(host: str) -> bool:
    """Identify international .ai sites with independent accounts and credits."""
    return bool(host) and ".ai" in str(host).lower()

USAGE_PATH = "/billing/meter/get-user-request-usage"
USAGE_MAX_DAYS = 30   # The upstream returns empty totals beyond its 31-day window.
USAGE_PAGE_SIZE = 200
USAGE_MAX_PAGES = 30


def credits_to_usd(amount: float, price_cny: float = CREDIT_PRICE_CNY,
                   rate: float = USD_RATE_CNY) -> float:
    """Estimate USD value from the CNY credit price and exchange rate."""
    return float(amount or 0) * price_cny / rate


def usd_per_credit(is_intl: bool, price_cny: float = CREDIT_PRICE_CNY,
                   price_usd: float = CREDIT_PRICE_USD,
                   rate: float = USD_RATE_CNY) -> float:
    """Return the regional USD value per credit."""
    return price_usd if is_intl else price_cny / rate


def dedupe_by_identity(creds_snapshot: dict) -> dict:
    """Deduplicate account balances, preferring segmented and newer data while retaining unknown identities."""
    winners: dict[str, tuple] = {}
    for cred_id, entry in (creds_snapshot or {}).items():
        if not isinstance(entry, dict):
            continue
        identity = str(entry.get("identity") or "")
        if not identity:
            continue          # Unknown owners cannot be merged safely.
        balance = entry.get("credits") or {}
        rank = (bool(balance.get("segments")), float(balance.get("fetched_at") or 0.0))
        if identity not in winners or rank > winners[identity][0]:
            winners[identity] = (rank, cred_id)
    keep = {cred_id for _, cred_id in winners.values()}
    out = {}
    for cred_id, entry in (creds_snapshot or {}).items():
        identity = str(entry.get("identity") or "") if isinstance(entry, dict) else ""
        if identity and cred_id not in keep:      # Drop duplicate paths for a known identity.
            continue
        out[cred_id] = entry
    return out


def aggregate_credits(creds_snapshot: dict) -> dict:
    """Deduplicate accounts and aggregate regional balances, usage and earliest expiry."""
    groups = {k: {"remaining": 0.0, "used_by_quota": 0.0, "soonest_expiry": None}
              for k in ("domestic", "international")}
    for e in dedupe_by_identity(creds_snapshot).values():
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
            "partial": any(bool((e.get("credits") or {}).get("partial"))
                           for e in dedupe_by_identity(creds_snapshot).values()),
            "groups": groups}



def fetch_request_usage(access_token: str, days: int = USAGE_MAX_DAYS,
                        uid: str = "", domain: str = "") -> dict:
    """Query the supported usage window and aggregate actual credit deductions by day and model."""
    days = max(1, min(int(days or USAGE_MAX_DAYS), USAGE_MAX_DAYS))
    host = hosts_for_token(access_token, domain)[0]
    url = host + USAGE_PATH
    headers = _web_headers(host, access_token, uid, domain)
    fmt = "%Y-%m-%d %H:%M:%S"
    now = time.time()
    body_base = {"startTime": time.strftime(fmt, time.localtime(now - days * 86400)),
                 "endTime": time.strftime(fmt, time.localtime(now))}
    by_day: dict = {}
    total_credits = 0.0
    requests = 0
    partial = False
    with httpx.Client() as client:
        for page in range(1, USAGE_MAX_PAGES + 1):
            status, payload = _post_json(client, url, headers,
                                         dict(body_base, pageNum=page, pageSize=USAGE_PAGE_SIZE))
            if status != 200:
                raise RuntimeError(f"用量明细接口 HTTP {status}")
            code = payload.get("code")
            if code not in (0, None):
                raise RuntimeError(f"用量明细接口 code={code}: {str(payload.get('msg'))[:120]}")
            data = payload.get("data")
            # Missing business data must not be treated as zero usage or complete pagination.
            if not isinstance(data, dict) or not isinstance(data.get("data"), list) \
                    or not isinstance(data.get("total"), (int, float)):
                raise RuntimeError("用量明细接口返回缺少 data.data/total 结构")
            rows = data["data"]
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
            if requests >= int(data["total"]) or not rows:
                break
        else:
            partial = True  # The page cap may hide additional usage.
    return {"by_day": by_day, "total_credits": round(total_credits, 2), "requests": requests,
            "partial": partial}


# ---------------------------------------------------------------------------
# Atomic JSON persistence for per-credential credit and check-in state
# ---------------------------------------------------------------------------

class CreditLedger:
    """Persist per-credential check-in and balance snapshots for expiry-aware scheduling."""

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

    def entry(self, cred_id: str) -> dict:
        """Return a credential snapshot without creating unknown entries."""
        with self._lock:
            return deepcopy(self._data["creds"].get(cred_id) or {})

    def bind_identity(self, cred_id: str, identity: str) -> bool:
        """Bind balance ownership independently of file paths, rejecting stale identity data."""
        with self._lock:
            entry = self._data["creds"].get(cred_id) or {}
            if entry.get("identity") == identity:
                return False
            self._data["creds"][cred_id] = {"identity": identity, "checkin": {}, "credits": {}, "error": None}
            self._save()
            return True

    def remove(self, cred_id: str):
        """Clear persisted credit, check-in and error state after an identity change."""
        with self._lock:
            self._data["creds"].pop(cred_id, None)
            self._save()

    # Check-in state

    def checkin_done(self, cred_id: str, day: str) -> bool:
        with self._lock:
            c = self._entry(cred_id).get("checkin") or {}
            return c.get("date") == day and c.get("ok") is True

    def mark_checkin(self, cred_id: str, day: str, ok: bool, code, message: str, *, state=None):
        with self._lock:
            self._entry(cred_id)["checkin"] = {
                "date": day, "ok": bool(ok), "code": code,
                "message": str(message or "")[:200], "at": time.time(),
            }
            if state is not None:
                self._entry(cred_id)["checkin"]["state"] = state
            self._save()

    def update_travel(self, cred_id: str, result: dict):
        with self._lock:
            self._entry(cred_id)["travel"] = deepcopy(result)
            self._save()

    # Credit balances

    def update_credits(self, cred_id: str, result: dict):
        with self._lock:
            e = self._entry(cred_id)
            e["credits"] = {
                "credits": result.get("credits"),
                "count": result.get("count", 0),
                "segments": deepcopy(result.get("segments") or []),
                "soonest_expiry": result.get("soonest_expiry"),
                "fetched_at": time.time(),
                "intl": bool(result.get("intl")),  # Regional credits use independent pricing.
                "partial": bool(result.get("partial")),  # Expose incomplete pagination.
            }
            e["error"] = None
            self._save()

    def note_error(self, cred_id: str, message: str):
        with self._lock:
            self._entry(cred_id)["error"] = str(message)[:200]
            self._save()

    def soonest_expiry_of(self, cred_id: str) -> float | None:
        """Return the earliest credit expiry, or None when unavailable."""
        with self._lock:
            return soonest_expiry((self._data["creds"].get(cred_id) or {}).get("credits", {}).get("segments"))

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data["creds"]))


class ModelCatalogCache:
    """Cache scoped model catalogs by client version; legacy shared catalogs are not fresh account data."""

    SCHEMA_VERSION = 2

    def __init__(self, path: Path, ttl: float = 6 * 3600):
        self.path = Path(path)
        self.ttl = max(60.0, float(ttl or 0))
        self._lock = threading.Lock()
        self._data: dict = {"version": self.SCHEMA_VERSION, "groups": {}}
        self._load()

    def _load(self):
        with self._lock:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if (not isinstance(d, dict) or d.get("version") not in (1, self.SCHEMA_VERSION)
                        or not isinstance(d.get("groups"), dict)):
                    return
                groups = {}
                for group, entry in d["groups"].items():
                    if (not isinstance(entry, dict) or not isinstance(entry.get("models"), list)
                            or not all(isinstance(m, dict) for m in entry["models"])):
                        continue
                    ts = entry.get("fetched_at", 0)
                    if not isinstance(ts, (int, float)) or not math.isfinite(ts):
                        ts = 0
                    groups[group] = {"models": entry["models"], "fetched_at": ts,
                                     "version": (self.SCHEMA_VERSION
                                                 if d["version"] == self.SCHEMA_VERSION
                                                 and entry.get("version") == self.SCHEMA_VERSION
                                                 else 1)}
                    # Ignore malformed root entries without discarding the selector catalog.
                    serves = entry.get("serves")
                    if isinstance(serves, list) and all(isinstance(m, dict) for m in serves):
                        groups[group]["serves"] = deepcopy(serves)
                self._data = {"version": self.SCHEMA_VERSION, "groups": groups}
            except (OSError, ValueError):
                pass  # Keep the loaded catalog when disk reads fail.

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
        """Return the credential's domestic or international cache group."""
        return ("international" if is_international_host(hosts_for_token(access_token)[0])
                else "domestic")

    def fresh(self, group: str) -> bool:
        """Check whether the group's cache remains within its TTL."""
        with self._lock:
            g = self._data["groups"].get(group) or {}
            age = time.time() - float(g.get("fetched_at") or 0)
            return g.get("version") == self.SCHEMA_VERSION and 0 <= age < self.ttl

    def models(self, group: str) -> list[dict]:
        with self._lock:
            return deepcopy((self._data["groups"].get(group) or {}).get("models") or [])

    def age(self, group: str) -> float | None:
        """Return cache age in seconds, including empty catalogs; unknown groups return None."""
        with self._lock:
            g = self._data["groups"].get(group)
            return time.time() - float(g.get("fetched_at") or 0) if g is not None else None

    def put(self, group: str, models: list[dict], serves: list[dict] | None = None):
        with self._lock:
            entry = {"models": deepcopy(models), "fetched_at": time.time(),
                     "version": self.SCHEMA_VERSION}
            if serves is not None:
                entry["serves"] = deepcopy(serves)
            self._data["groups"][group] = entry
            self._save()

    def serves(self, group: str) -> list[dict]:
        """Return root catalog candidates, or an empty list when legacy caches omit them."""
        with self._lock:
            return deepcopy((self._data["groups"].get(group) or {}).get("serves") or [])
