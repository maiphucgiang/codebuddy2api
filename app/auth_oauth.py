#!/usr/bin/env python3
"""Acquire and validate WorkBuddy/CodeBuddy OAuth credentials; callers handle persistence and pool reload."""

from __future__ import annotations

import base64
import copy
import json
import re
import threading
import time
import uuid

import httpx

from .site_routing import profile_for_auth

PLUGIN_PREFIX = "/v2/plugin"
OAUTH_TIMEOUT_S = 600          # Authorization deadline in seconds
RESULT_RETENTION_S = 300       # Retain completed results for repeated polling
REQUEST_TIMEOUT_S = 15.0

# Keep OAuth hosts isolated by product and region.
SITE_HOSTS = {
    "cn": "https://www.codebuddy.cn",
    "intl": "https://www.workbuddy.ai",
    "intl-codebuddy": "https://www.codebuddy.ai",
}

# Require an allowlisted credential domain or JWT issuer.
ALLOWED_ORIGINS = {
    "https://www.workbuddy.cn",
    "https://www.codebuddy.cn",
    "https://copilot.tencent.com",   # Domestic Keycloak issuer
    "https://www.workbuddy.ai",
    "https://www.codebuddy.ai",
}

DEFAULT_UA = "codebuddy2api"


def _normalize_origin(value) -> str:
    """Normalize a domain or URL to a lowercase HTTPS origin, or return an empty string."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    m = re.match(r"(https?://[^/]+)", raw, re.I)
    return m.group(1).lower() if m else ""


def _token_issuer_origin(access_token: str) -> str:
    """Decode a JWT issuer origin, returning an empty string on failure."""
    try:
        part = access_token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return _normalize_origin(payload.get("iss") or "")
    except Exception:
        return ""


def _reject_constant(value):
    raise ValueError(f"非标准 JSON 常量: {value}")


def loads_strict(text):
    """Parse strict JSON without nonstandard NaN or Infinity constants."""
    return json.loads(text, parse_constant=_reject_constant)


def validate_cred_data(data) -> tuple[str | None, str | None]:
    """Validate credentials and return either the account UID or a safe failure reason."""
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
    token = auth.get("accessToken") or auth.get("access_token") or auth.get("token")
    if not isinstance(token, str) or not token:
        return None, "缺少有效的 accessToken"
    for field in ("expiresAt", "lastRefreshTime"):
        value = auth.get(field)
        if value is None:
            continue
        # Compare before float conversion to reject nonfinite values and oversized integers.
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not 0 < value < 4102444800000:  # Upper bound: 2100-01-01
            return None, f"{field} 必须是合理范围内的有限毫秒时间戳"
    domain = _normalize_origin(auth.get("domain") or auth.get("issuer") or "")
    issuer = _token_issuer_origin(token)
    if not any(o in ALLOWED_ORIGINS for o in (domain, issuer) if o):
        return None, f"认证域名不在允许列表（domain={domain or '-'} issuer={issuer or '-'}）"
    try:
        profile_for_auth({**auth, "accessToken": token, "domain": auth.get("domain") or auth.get("issuer")})
    except ValueError:
        return None, "凭据的地域或产品信息无效、不一致"
    return uid, None


def normalize_cred_data(data: dict) -> dict:
    """Normalize validated token aliases to the official runtime credential fields."""
    out = copy.deepcopy(data)
    auth = out.get("auth")
    if not isinstance(auth, dict):
        return out
    for canonical, aliases in (("accessToken", ("access_token", "token")),
                               ("refreshToken", ("refresh_token",)),
                               ("tokenType", ("token_type",))):
        if not auth.get(canonical):
            for alias in aliases:
                if auth.get(alias):
                    auth[canonical] = auth[alias]
                    break
        for alias in aliases:
            auth.pop(alias, None)
    return out


def _norm_ts(v) -> int | None:
    """Normalize numeric seconds or milliseconds to milliseconds; invalid values return None."""
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
    if ts < 1e10:  # Convert seconds to milliseconds.
        ts *= 1000
    return round(ts)


def build_auth_file(token_data, account_data) -> dict:
    """Build the official .info structure while preserving upstream credential fields."""
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

    auth = dict(raw)  # Preserve official session fields beyond the access token.
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
    """Merge existing account lists by UID, preferring accounts in the new credential."""
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
    """Manage in-memory OAuth authorization sessions with lazy expiry cleanup."""

    def __init__(self, user_agent: str = DEFAULT_UA, timeout_s: int = OAUTH_TIMEOUT_S,
                 retention_s: int = RESULT_RETENTION_S, http_factory=None):
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}   # login_id -> {state, host, expires_at, done, ...}
        self._ua = user_agent
        self._timeout_s = timeout_s
        self._retention_s = retention_s
        self._http_factory = http_factory    # Optional test transport factory

    def _client(self):
        return self._http_factory() if self._http_factory else httpx.Client(timeout=REQUEST_TIMEOUT_S)

    def _headers(self) -> dict:
        return {"User-Agent": self._ua, "Accept": "application/json",
                "Content-Type": "application/json"}

    def _purge(self):
        """Remove expired sessions and retained results while holding the session lock."""
        now = time.time()
        drop = [k for k, s in self._states.items()
                if now > s["expires_at"] + self._retention_s]
        for k in drop:
            self._states.pop(k, None)

    def start(self, site: str = "cn") -> dict:
        """Request authorization for the selected domestic, international WorkBuddy or CodeBuddy site."""
        site = str(site or "").strip().lower()
        host = SITE_HOSTS.get(site)
        if not host:
            raise ValueError(f"未知站点（仅支持 {' / '.join(SITE_HOSTS)}）")
        # CodeBuddy requires uppercase CLI; retain compatible parameters for other sites.
        platform = "CLI" if site == "intl-codebuddy" else "workbuddy"
        with self._client() as c:
            r = c.post(f"{host}{PLUGIN_PREFIX}/auth/state?platform={platform}",
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
        """Poll authorization and return pending state, credentials or a safe error."""
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
                return {"done": False}     # Retry transient upstream failures on the next poll.
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
