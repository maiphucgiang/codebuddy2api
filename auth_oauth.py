#!/usr/bin/env python3
"""auth_oauth.py — WorkBuddy/CodeBuddy 无感登录采集（OAuth state 轮询）与凭据入库校验。

无感登录采集流程（OAuth state 轮询 + 入库严格校验）：
  1. POST {apiHost}/v2/plugin/auth/state?platform=workbuddy 申请 state + 授权链接
  2. 用户在浏览器完成扫码授权（桌面端全程不退出、无需安装）
  3. 轮询 GET /v2/plugin/auth/token?state=... 拿 accessToken
  4. GET /v2/plugin/login/account?state=... 拉账号信息，拼成官方 .info 结构入库

仅依赖 httpx；文件落盘与凭证池热加载由调用方（converter.py）完成。
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid

import httpx

PLUGIN_PREFIX = "/v2/plugin"
OAUTH_TIMEOUT_S = 600          # 授权等待超时秒数
RESULT_RETENTION_S = 300       # 完成状态保留，供调用方重复轮询取结果
REQUEST_TIMEOUT_S = 15.0

# 各站点无感登录 apiHost（与 auth.domain 一致；签到/积分也打各自域名，不互用）
SITE_HOSTS = {
    "cn": "https://www.codebuddy.cn",
    "intl": "https://www.workbuddy.ai",
}

# 入库站点白名单：auth.domain 或 access token 的 JWT issuer 命中其一才收
ALLOWED_ORIGINS = {
    "https://www.workbuddy.cn",
    "https://www.codebuddy.cn",
    "https://copilot.tencent.com",   # 国内版新版 Keycloak issuer
    "https://www.workbuddy.ai",
    "https://www.codebuddy.ai",
}

DEFAULT_UA = "codebuddy2api"


def _normalize_origin(value) -> str:
    """域名/URL 归一化为小写 origin（无 scheme 补 https://）；无效返回 ''。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    m = re.match(r"(https?://[^/]+)", raw, re.I)
    return m.group(1).lower() if m else ""


def _token_issuer_origin(access_token: str) -> str:
    """解码 JWT payload 的 iss，返回 origin；失败返回 ''。"""
    try:
        part = access_token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return _normalize_origin(payload.get("iss") or "")
    except Exception:
        return ""


def validate_cred_data(data) -> tuple[str | None, str | None]:
    """入库校验（严格模式）：返回 (uid, None) 或 (None, 原因)。"""
    if not isinstance(data, dict):
        return None, "凭据不是有效的 JSON 对象"
    acct = data.get("account")
    if not isinstance(acct, dict):
        arr = data.get("accounts")
        acct = arr[0] if isinstance(arr, list) and arr and isinstance(arr[0], dict) else None
    uid = str((acct or {}).get("uid") or "")
    if not uid:
        return None, "缺少 account.uid"
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    token = str(auth.get("accessToken") or auth.get("access_token") or auth.get("token") or "")
    if not token:
        return None, "缺少 accessToken"
    domain = _normalize_origin(auth.get("domain") or auth.get("issuer") or "")
    issuer = _token_issuer_origin(token)
    if not any(o in ALLOWED_ORIGINS for o in (domain, issuer) if o):
        return None, f"认证域名不在允许列表（domain={domain or '-'} issuer={issuer or '-'}）"
    return uid, None


def _norm_ts(v) -> int | None:
    """时间戳归一化：秒/毫秒/数字字符串 → 毫秒；无效返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
    elif isinstance(v, str):
        try:
            ts = float(v)
        except ValueError:
            return None
    else:
        return None
    if ts <= 0:
        return None
    if ts < 1e10:  # 秒 → 毫秒
        ts *= 1000
    return round(ts)


def build_auth_file(token_data, account_data) -> dict:
    """把 OAuth token + 账号信息拼成官方 .info 结构（保留上游全部字段，不裁剪白名单）。"""
    now = round(time.time() * 1000)
    raw = token_data if isinstance(token_data, dict) else {}
    domain = str(raw.get("domain") or "")
    expires_at = _norm_ts(raw.get("expiresAt", raw.get("expires_at")))
    if expires_at is None:
        expires_in = raw.get("expiresIn", raw.get("expires_in"))
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
            expires_at = now + round(expires_in * 1000)
    refresh_expires_at = _norm_ts(raw.get("refreshExpiresAt", raw.get("refresh_expires_at")))
    if refresh_expires_at is None:
        refresh_expires_in = raw.get("refreshExpiresIn", raw.get("refresh_expires_in"))
        if isinstance(refresh_expires_in, (int, float)) and not isinstance(refresh_expires_in, bool) \
                and refresh_expires_in > 0:
            refresh_expires_at = now + round(refresh_expires_in * 1000)

    acc = dict(account_data) if isinstance(account_data, dict) else {}
    acc.update({
        "uid": str(acc.get("uid") or ""),
        "nickname": str(acc.get("nickname") or ""),
        "uin": acc.get("uin") or "",
        "phoneNumber": acc.get("phoneNumber") or "",
        "type": acc.get("type") or "personal",
        "lastLogin": True,
        "pluginEnabled": True,
    })

    auth = dict(raw)  # 保留 idToken/sessionState 等官方后续可能依赖的字段
    auth.update({
        "accessToken": str(raw.get("accessToken") or raw.get("access_token") or ""),
        "refreshToken": str(raw.get("refreshToken") or raw.get("refresh_token") or ""),
        "tokenType": str(raw.get("tokenType") or raw.get("token_type") or "Bearer"),
        "domain": domain,
        "lastRefreshTime": now,
        "scope": raw.get("scope") or "openid profile offline_access email",
        "notBeforePolicy": raw.get("notBeforePolicy") if raw.get("notBeforePolicy") is not None else 0,
        "sessionState": raw.get("sessionState") or "",
    })
    if expires_at is not None:
        auth["expiresAt"] = expires_at
        auth["expiresIn"] = max(0, round((expires_at - now) / 1000))
        auth["refreshExpiresAt"] = refresh_expires_at if refresh_expires_at is not None else expires_at
        auth["refreshExpiresIn"] = max(0, round((auth["refreshExpiresAt"] - now) / 1000))
    else:
        auth["expiresIn"] = 0
        auth["refreshExpiresIn"] = 0

    return {"account": acc, "auth": auth, "accounts": [acc], "allAccounts": [dict(acc)]}


def merge_existing_accounts(cred: dict, existing) -> dict:
    """把 existing 文件的 accounts/allAccounts 并入 cred（按 uid 去重，cred 内账号优先）。"""
    if not isinstance(existing, dict):
        return cred
    arr = existing.get("allAccounts") or existing.get("accounts")
    if not isinstance(arr, list):
        return cred
    own = [a for a in (cred.get("allAccounts") or []) if isinstance(a, dict)]
    own_uids = {a.get("uid") for a in own}
    others = [a for a in arr if isinstance(a, dict) and a.get("uid") not in own_uids]
    merged = others + own
    cred["accounts"] = merged
    cred["allAccounts"] = list(merged)
    return cred


class OAuthManager:
    """无感登录状态机：start 申请 state，poll 轮询直至授权完成。状态存内存，懒清理。"""

    def __init__(self, user_agent: str = DEFAULT_UA, timeout_s: int = OAUTH_TIMEOUT_S,
                 retention_s: int = RESULT_RETENTION_S, http_factory=None):
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}   # login_id -> {state, host, expires_at, done, ...}
        self._ua = user_agent
        self._timeout_s = timeout_s
        self._retention_s = retention_s
        self._http_factory = http_factory    # 测试注入；缺省 httpx.Client

    def _client(self):
        return self._http_factory() if self._http_factory else httpx.Client(timeout=REQUEST_TIMEOUT_S)

    def _headers(self) -> dict:
        return {"User-Agent": self._ua, "Accept": "application/json",
                "Content-Type": "application/json"}

    def _purge(self):
        """惰性清理：超时 + 结果保留期都过去的登录请求直接丢弃（调用时需已持锁）。"""
        now = time.time()
        drop = [k for k, s in self._states.items()
                if now > s["expires_at"] + self._retention_s]
        for k in drop:
            self._states.pop(k, None)

    def start(self, site: str = "cn") -> dict:
        """第一步：申请 state + 授权链接。site 仅支持 cn / intl。"""
        host = SITE_HOSTS.get(str(site or "").strip().lower())
        if not host:
            raise ValueError(f"未知站点: {site}（仅支持 cn / intl）")
        with self._client() as c:
            r = c.post(f"{host}{PLUGIN_PREFIX}/auth/state?platform=workbuddy",
                       headers=self._headers(), json={})
            resp = r.json()
        data = resp.get("data") or {} if isinstance(resp, dict) else {}
        state = data.get("state")
        if not state:
            raise RuntimeError(f"auth/state 响应缺少 state: "
                               f"{resp.get('msg') or resp.get('message') or resp}")
        auth_url = (data.get("authUrl") or data.get("auth_url") or data.get("url")
                    or f"{host}/login?state={state}")
        login_id = "oa_" + uuid.uuid4().hex
        with self._lock:
            self._purge()
            self._states[login_id] = {
                "state": state, "host": host,
                "expires_at": time.time() + self._timeout_s,
                "done": False, "result": None, "error": None,
            }
        return {"login_id": login_id, "verification_uri": auth_url, "expires_in": self._timeout_s}

    def poll(self, login_id: str) -> dict:
        """第二步：轮询授权结果。未完成 {"done": False}；完成带 uid/nickname/cred 或 error。"""
        with self._lock:
            self._purge()
            s = self._states.get(str(login_id or ""))
        if s is None:
            return {"done": True, "error": "登录请求不存在或已过期"}
        if s["done"]:
            return dict({"done": True}, **(s.get("result") or {}),
                        **({"error": s["error"]} if s.get("error") else {}))
        if time.time() > s["expires_at"]:
            s["done"] = True
            s["error"] = "登录超时，请重新发起"
            return {"done": True, "error": s["error"]}

        url = f"{s['host']}{PLUGIN_PREFIX}/auth/token?state={s['state']}"
        with self._client() as c:
            try:
                resp = c.get(url, headers=self._headers()).json()
            except Exception:
                return {"done": False}     # 上游抖动视为未完成，下轮再试
            data = resp.get("data") or {} if isinstance(resp, dict) else {}
            code = resp.get("code") if isinstance(resp, dict) else None
            if code not in (0, 200):
                return {"done": False}
            token = data.get("accessToken") or data.get("access_token")
            if not token:
                return {"done": False}
            acc_headers = {"Authorization": f"Bearer {token}"}
            if data.get("domain"):
                acc_headers["X-Domain"] = str(data["domain"])
            acc_headers.update(self._headers())
            acc_resp = c.get(f"{s['host']}{PLUGIN_PREFIX}/login/account?state={s['state']}",
                             headers=acc_headers).json()
        acc = acc_resp.get("data") or {} if isinstance(acc_resp, dict) else {}
        s["done"] = True
        if not acc.get("uid"):
            s["error"] = "官方接口未返回 uid，无法保存账号"
            return {"done": True, "error": s["error"]}
        cred = build_auth_file(data, acc)
        s["result"] = {"uid": str(acc["uid"]), "nickname": str(acc.get("nickname") or ""),
                       "cred": cred}
        return dict({"done": True}, **s["result"])
