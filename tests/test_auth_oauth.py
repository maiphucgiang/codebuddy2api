#!/usr/bin/env python3
"""test_auth_oauth.py — 验证 auth_oauth.py 的入库校验/.info 拼装/OAuth 状态机与 converter 保活调度。

直接运行：python3 tests/test_auth_oauth.py
"""

import base64
import json
import sys
import tempfile
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

from app import auth_oauth
from app.auth_oauth import (
    OAuthManager, build_auth_file, merge_existing_accounts, validate_cred_data,
    _norm_ts, _normalize_origin, _token_issuer_origin,
)
import converter


def _jwt(iss: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"iss": iss}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def _cred(uid="u1", domain="www.codebuddy.cn", token=None):
    return {
        "account": {"uid": uid, "nickname": "n"},
        "auth": {"accessToken": token or _jwt(f"https://{domain}/auth/realms/copilot"),
                 "refreshToken": "r", "domain": domain},
    }


def test_validate_cred_data():
    assert validate_cred_data(_cred()) == ("u1", None)
    assert validate_cred_data(_cred(domain="www.workbuddy.ai"))[1] is None       # 国际站
    assert validate_cred_data(_cred(domain="copilot.tencent.com"))[1] is None    # 新版 Keycloak issuer
    # domain 缺失时靠 JWT issuer 兜底
    c = _cred()
    c["auth"].pop("domain")
    assert validate_cred_data(c) == ("u1", None)
    # accounts[0] 兜底（无 account 键）
    c = _cred()
    c["accounts"] = [c.pop("account")]
    assert validate_cred_data(c) == ("u1", None)
    # 拒绝项
    assert validate_cred_data(None)[1]
    assert validate_cred_data({"account": {"uid": "u"}})[1]                      # 缺 token
    assert validate_cred_data({"auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}})[1]  # 缺 uid
    bad = _cred(domain="evil.example.com")
    bad["auth"]["accessToken"] = "not-a-jwt"
    uid, err = validate_cred_data(bad)
    assert uid is None and "允许列表" in err
    print("✅ test_validate_cred_data")


def test_helpers():
    assert _normalize_origin("www.codebuddy.cn") == "https://www.codebuddy.cn"
    assert _normalize_origin("https://WWW.workbuddy.ai/x") == "https://www.workbuddy.ai"
    assert _normalize_origin("") == ""
    assert _token_issuer_origin(_jwt("https://www.workbuddy.cn/auth/realms/c")) == "https://www.workbuddy.cn"
    assert _token_issuer_origin("bad") == ""
    assert _norm_ts(1_700_000_000) == 1_700_000_000_000        # 秒 → 毫秒
    assert _norm_ts(1_700_000_000_000) == 1_700_000_000_000    # 毫秒保持
    assert _norm_ts("1700000000") == 1_700_000_000_000
    assert _norm_ts(True) is None and _norm_ts(-1) is None and _norm_ts("x") is None
    print("✅ test_helpers")


def test_build_auth_file():
    now = time.time() * 1000
    tok = {"accessToken": "at", "refreshToken": "rt", "domain": "www.codebuddy.cn",
           "expiresIn": 7200, "refreshExpiresIn": 864000, "idToken": "keep-me"}
    acc = {"uid": "u9", "nickname": "九", "extra": 1}
    c = build_auth_file(tok, acc)
    a, au = c["account"], c["auth"]
    assert a["uid"] == "u9" and a["lastLogin"] is True and a["pluginEnabled"] is True
    assert a["type"] == "personal" and a["extra"] == 1                       # 保留上游额外字段
    assert au["accessToken"] == "at" and au["refreshToken"] == "rt"
    assert au["tokenType"] == "Bearer" and au["idToken"] == "keep-me"        # 不裁剪白名单
    assert abs(au["expiresAt"] - (now + 7200_000)) < 2000
    assert au["expiresIn"] <= 7200 and au["lastRefreshTime"] >= now - 2000
    assert au["refreshExpiresAt"] > au["expiresAt"]
    assert c["accounts"] == c["allAccounts"] and c["accounts"][0]["uid"] == "u9"
    # snake_case + expiresAt 秒级时间戳兼容
    c2 = build_auth_file({"access_token": "x", "refresh_token": "y", "expires_at": 1_800_000_000},
                         {"uid": "u"})
    assert c2["auth"]["accessToken"] == "x" and c2["auth"]["expiresAt"] == 1_800_000_000_000
    # 无过期信息
    c3 = build_auth_file({"accessToken": "x"}, {"uid": "u"})
    assert c3["auth"]["expiresIn"] == 0 and "expiresAt" not in c3["auth"]
    print("✅ test_build_auth_file")


def test_merge_existing_accounts():
    cred = build_auth_file({"accessToken": "x"}, {"uid": "new"})
    existing = {"allAccounts": [{"uid": "old1"}, {"uid": "new", "stale": True}, {"uid": "old2"}]}
    merged = merge_existing_accounts(cred, existing)
    uids = [a["uid"] for a in merged["allAccounts"]]
    assert uids == ["old1", "old2", "new"]                # 去重且新账号最后
    assert merged["accounts"][-1]["uid"] == "new"
    same = merge_existing_accounts(build_auth_file({"accessToken": "x"}, {"uid": "new"}), None)
    assert [a["uid"] for a in same["allAccounts"]] == ["new"]
    print("✅ test_merge_existing_accounts")


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeClient:
    """按 URL 路由的假 httpx.Client；calls 记录 (method, path)。"""
    routes = {}
    calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        type(self).calls.append(("POST", url))
        for k, v in type(self).routes.items():
            if k in url:
                return _Resp(v() if callable(v) else v)
        raise AssertionError(f"未 mock 的 POST: {url}")

    def get(self, url, headers=None):
        type(self).calls.append(("GET", url))
        type(self).last_headers = headers
        for k, v in type(self).routes.items():
            if k in url:
                return _Resp(v() if callable(v) else v)
        raise AssertionError(f"未 mock 的 GET: {url}")


def _manager(routes, **kw):
    _FakeClient.routes = routes
    _FakeClient.calls = []
    return OAuthManager(http_factory=_FakeClient, **kw)


def test_oauth_full_flow():
    granted = {"v": False}
    routes = {
        "/auth/state": {"code": 0, "data": {"state": "st-1"}},
        "/auth/token": lambda: ({"code": 0, "data": {"accessToken": "at", "refreshToken": "rt",
                                                     "domain": "www.codebuddy.cn", "expiresIn": 7200}}
                                if granted["v"] else {"code": 10001, "msg": "pending"}),
        "/login/account": {"code": 0, "data": {"uid": "u1", "nickname": "甲"}},
    }
    m = _manager(routes)
    r = m.start("cn")
    assert r["login_id"].startswith("oa_") and r["expires_in"] == 600
    assert r["verification_uri"] == "https://www.codebuddy.cn/login?state=st-1"  # 缺省兜底链接
    assert m.poll(r["login_id"]) == {"done": False}                              # 未授权
    granted["v"] = True
    r2 = m.poll(r["login_id"])
    assert r2["done"] and r2["uid"] == "u1" and r2["nickname"] == "甲"
    assert r2["cred"]["auth"]["accessToken"] == "at"
    assert _FakeClient.last_headers["Authorization"] == "Bearer at"
    assert _FakeClient.last_headers["X-Domain"] == "www.codebuddy.cn"
    r3 = m.poll(r["login_id"])                                                   # 重复轮询取缓存结果
    assert r3["done"] and r3["uid"] == "u1"
    assert validate_cred_data(r3["cred"])[1] is None                             # 产出必过入库校验
    print("✅ test_oauth_full_flow")


def test_oauth_edge_cases():
    m = _manager({"/auth/state": {"code": 0, "data": {"state": "s", "authUrl": "https://x/qr"}},
                  "/auth/token": {"code": 0, "data": {"accessToken": "at"}},
                  "/login/account": {"code": 0, "data": {}}})
    assert m.start("intl")["verification_uri"] == "https://x/qr"                 # 优先用上游 authUrl
    try:
        m.start("xx")
        raise AssertionError("应拒绝未知站点")
    except ValueError:
        pass
    m2 = _manager({"/auth/state": {"code": -1, "msg": "限流"}})
    try:
        m2.start()
        raise AssertionError("缺少 state 应抛错")
    except RuntimeError as e:
        assert "限流" in str(e)
    assert m2.poll("oa_ghost")["error"] == "登录请求不存在或已过期"
    # 账号接口无 uid
    m_acc = _manager({"/auth/state": {"code": 0, "data": {"state": "s"}},
                      "/auth/token": {"code": 0, "data": {"accessToken": "at"}},
                      "/login/account": {"code": 0, "data": {}}})
    r = m_acc.start("cn")
    rr = m_acc.poll(r["login_id"])
    assert rr["done"] and "uid" in rr["error"]
    # 超时
    m3 = _manager({"/auth/state": {"code": 0, "data": {"state": "s"}}}, timeout_s=1)
    r = m3.start()
    m3._states[r["login_id"]]["expires_at"] = time.time() - 1
    assert m3.poll(r["login_id"])["error"] == "登录超时，请重新发起"
    # 上游抖动视为未完成
    class _Boom(_FakeClient):
        def get(self, url, headers=None):
            raise ConnectionError("boom")
    _Boom.routes = {}
    m4 = OAuthManager(http_factory=_Boom)
    m4._states["oa_x"] = {"state": "s", "host": "https://www.codebuddy.cn",
                          "expires_at": time.time() + 60, "done": False, "result": None, "error": None}
    assert m4.poll("oa_x") == {"done": False}
    print("✅ test_oauth_edge_cases")


class _StubCM:
    """假 CredentialManager：summary 可控，refresh_if_due 记录调度调用。"""
    def __init__(self, summary, fail=False):
        self._s = summary
        self._lock = threading.RLock()
        self._generation = 0
        self._fail = fail
        self.refreshed = 0

    def summary(self):
        return dict(self._s)

    def refresh_if_due(self, margin_s, keepalive_s):
        self.refreshed += 1
        if self._fail:
            raise RuntimeError("refresh token 已失效")
        self._s.update(last_refresh_time=time.time() * 1000,
                       token_expires_at=(time.time() + 3600) * 1000, token_expired=False)
        return True


def _pool_with(entries):
    pool = converter.CredentialPool([], scan=False)
    pool._entries = entries
    return pool


def test_keepalive_refresh():
    now = time.time()
    far_future = (now + 30 * 86400) * 1000
    fresh = _StubCM({"token_expired": False, "token_expires_at": far_future,
                     "last_refresh_time": now * 1000})
    stale = _StubCM({"token_expired": False, "token_expires_at": far_future,
                     "last_refresh_time": (now - 25 * 3600) * 1000})
    never = _StubCM({"token_expired": False, "token_expires_at": far_future,
                     "last_refresh_time": 0})
    expiring = _StubCM({"token_expired": False, "token_expires_at": (now + 60) * 1000,
                        "last_refresh_time": now * 1000})
    failing = _StubCM({"token_expired": False, "token_expires_at": far_future,
                       "last_refresh_time": 0}, fail=True)
    es = [{"id": f"/tmp/{i}.info", "cm": cm, "fail_until": 0.0, "uid": str(i)}
          for i, cm in enumerate([fresh, stale, never, expiring, failing])]
    pool = _pool_with(es)
    pool.cooldown = lambda cm, reason="", **kw: None  # 单测不触发熔断副作用
    pool.refresh_due()
    assert fresh.refreshed == 0                    # 刚刷过 → 不动
    assert stale.refreshed == 1                    # >24h → 保活
    assert never.refreshed == 1                    # 无记录 → 保活
    assert expiring.refreshed == 1                 # 临期 → 原有逻辑
    assert failing.refreshed == 1                  # 保活失败已尝试
    assert es[4]["keepalive_after"] > now          # 失败后退避 1h
    failing.refreshed = 0
    pool.refresh_due()
    assert failing.refreshed == 0                  # 退避期内不再骚扰
    # 关掉保活
    stale2 = _StubCM({"token_expired": False, "token_expires_at": far_future, "last_refresh_time": 0})
    pool2 = _pool_with([{"id": "/tmp/x.info", "cm": stale2, "fail_until": 0.0, "uid": "x"}])
    pool2.refresh_due(keepalive_s=0)
    assert stale2.refreshed == 0
    print("✅ test_keepalive_refresh")


def test_oauth_endpoint_import(tmp_path=None):
    """poll 完成后的入库路径：同 uid 覆盖已有文件并热加载（不起服务，直接调处理函数）。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        old = _cred(uid="u-old")
        (d / "old.info").write_text(json.dumps(old), encoding="utf-8")
        same = _cred(uid="u1", token=_jwt("https://www.codebuddy.cn/x"))
        (d / "named.info").write_text(json.dumps(same), encoding="utf-8")

        cred = build_auth_file({"accessToken": "new-at", "refreshToken": "new-rt",
                                "domain": "www.codebuddy.cn", "expiresIn": 7200},
                               {"uid": "u1", "nickname": "新"})
        # 模拟端点入库段：同 uid 覆盖 named.info
        target = next((f for f in sorted(d.glob("*.info")) if converter._cred_uid(f) == "u1"), None)
        assert target and target.name == "named.info"
        existing = json.loads(target.read_text(encoding="utf-8"))
        merged = merge_existing_accounts(cred, existing)
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        final = json.loads(target.read_text(encoding="utf-8"))
        assert final["auth"]["accessToken"] == "new-at"
        assert final["account"]["lastLogin"] is True
        assert validate_cred_data(final)[1] is None
        # 新账号（无同名文件）→ <uid>.info
        assert not (d / "u2.info").exists()
        cred2 = build_auth_file({"accessToken": "z", "domain": "www.codebuddy.cn"}, {"uid": "u2"})
        (d / "u2.info").write_text(json.dumps(cred2), encoding="utf-8")
        assert validate_cred_data(json.loads((d / "u2.info").read_text()))[0] == "u2"
    print("✅ test_oauth_endpoint_import")


if __name__ == "__main__":
    test_validate_cred_data()
    test_helpers()
    test_build_auth_file()
    test_merge_existing_accounts()
    test_oauth_full_flow()
    test_oauth_edge_cases()
    test_keepalive_refresh()
    test_oauth_endpoint_import()
    print("\n全部通过 ✅")
