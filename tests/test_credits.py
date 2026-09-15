#!/usr/bin/env python3
"""test_credits.py — 验证 credits.py 的签到判定/域名选择/积分分段/ledger 与快过期优先调度。

直接运行：python3 tests/test_credits.py
"""

import base64
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

from app import credits
from app.credits import (
    AuthExpiredError, CreditLedger,
    classify_checkin_result, token_issuer_origin, hosts_for_token,
    extract_segments, merge_segments, soonest_expiry,
)


def _jwt(iss: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"iss": iss}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def test_issuer_origin():
    assert token_issuer_origin(_jwt("https://www.codebuddy.cn/auth/realms/copilot")) == "https://www.codebuddy.cn"
    assert token_issuer_origin(_jwt("https://www.workbuddy.cn/auth/realms/copilot")) == "https://www.workbuddy.cn"
    assert token_issuer_origin("not-a-jwt") is None
    assert token_issuer_origin("") is None
    print("✅ test_issuer_origin")


def test_hosts_for_token():
    assert hosts_for_token(_jwt("https://www.codebuddy.cn/auth/realms/copilot")) == ["https://www.codebuddy.cn"]
    assert hosts_for_token(_jwt("https://www.workbuddy.ai/auth/realms/copilot")) == ["https://www.workbuddy.ai"]
    # 无显式提示时与 site_routing 一致，仅默认国内 CLI，绝不再遍历另一产品。
    assert hosts_for_token("bad") == ["https://www.codebuddy.cn"]
    for brand in ("codebuddy", "workbuddy"):
        for suffix in ("cn", "ai"):
            domain = f"www.{brand}.{suffix}"
            expected = [f"https://{domain}"]
            assert hosts_for_token(_jwt(f"https://{domain}/auth/realms/copilot")) == expected
            assert hosts_for_token("opaque", domain) == expected
            assert hosts_for_token("opaque", f"https://{domain}/") == expected
            assert hosts_for_token(_jwt(f"https://{domain}/x"), domain) == expected
    assert hosts_for_token("opaque", "copilot.tencent.com") == ["https://www.codebuddy.cn"]
    assert hosts_for_token(_jwt("https://copilot.tencent.com/x"), "www.workbuddy.cn") == [
        "https://www.workbuddy.cn"]
    assert hosts_for_token(_jwt("https://www.workbuddy.cn/x"), "copilot.tencent.com") == [
        "https://www.workbuddy.cn"]
    print("✅ test_hosts_for_token")


def test_financial_hints_rejected_before_network():
    """显式未知/不安全提示与跨地域、跨产品冲突均在建立 Client 前拒绝。"""
    invalid = [
        ("opaque", "unknown.example"),
        ("opaque", "http://www.codebuddy.cn"),
        ("opaque", "https://www.codebuddy.ai:443"),
        ("opaque", "https://www.workbuddy.ai/other"),
        (_jwt("https://unknown.example/x"), ""),
        (_jwt("https://unknown.example/x"), "www.workbuddy.ai"),
        (_jwt("https://www.codebuddy.cn/x"), "www.workbuddy.cn"),
        (_jwt("https://www.workbuddy.ai/x"), "www.codebuddy.ai"),
        (_jwt("https://www.workbuddy.ai/x"), "www.workbuddy.cn"),
        (_jwt("https://www.codebuddy.ai/x"), "copilot.tencent.com"),
    ]
    with patch("app.credits.httpx.Client") as factory:
        for token, domain in invalid:
            for operation in (hosts_for_token, credits.daily_checkin, credits.fetch_credits,
                              credits.fetch_request_usage):
                with TestCase().assertRaises(ValueError):
                    operation(token, domain=domain)
        factory.assert_not_called()
    # 对外纯解析函数保留宽松解析兼容性，不把它当作受信任路由。
    assert token_issuer_origin(_jwt("https://unknown.example/x")) == "https://unknown.example"
    print("test_financial_hints_rejected_before_network passed")


def test_financial_profile_hosts_and_web_headers():
    """各品牌不混用，签到走桌面 Bearer 协议，余额和用量保留 Web 协议。"""
    for domain in ("www.codebuddy.cn", "copilot.tencent.com", "www.workbuddy.cn",
                   "www.codebuddy.ai", "www.workbuddy.ai"):
        host = "https://" + ("www.codebuddy.cn" if domain == "copilot.tencent.com" else domain)
        for operation, path in ((credits.daily_checkin, credits.CHECKIN_PATHS[0]),
                                (credits.fetch_credits, credits.RESOURCE_PATH),
                                (credits.fetch_request_usage, credits.USAGE_PATH)):
            client = MagicMock()
            client.post.return_value.status_code = 200
            client.post.return_value.json.return_value = {"code": 0, "data": {
                "Accounts": [{"CapacityRemain": 3}], "data": [], "total": 0}}
            with patch("app.credits.httpx.Client") as factory:
                factory.return_value.__enter__.return_value = client
                result = operation("opaque", uid="test-user", domain=domain)
            client.post.assert_called_once()
            args, kwargs = client.post.call_args
            assert args == (host + path,)
            headers = httpx.Headers(kwargs["headers"])
            if operation is credits.daily_checkin:
                assert "x-client-platform" not in headers and "origin" not in headers and "referer" not in headers
            else:
                assert headers["x-client-platform"] == "web"
                assert headers["origin"] == host
                assert headers["referer"] == host + "/profile/plans-usage"
                assert headers["user-agent"] == credits.BROWSER_UA
            assert headers["authorization"] == "Bearer opaque"
            assert headers["x-user-id"] == "test-user" and headers["x-domain"] == domain
            assert headers["content-type"] == "application/json"
            assert "x-ide-type" not in headers
            assert kwargs["timeout"] == credits.REQUEST_TIMEOUT
            if operation is credits.fetch_credits:
                assert result["credits"] == 3 and result["intl"] == domain.endswith(".ai")
    print("test_financial_profile_hosts_and_web_headers passed")


def test_financial_failures_stay_on_profile():
    """保留原 POST 尝试次数/同 host 签到路径，失败也不转发 token 到其他产品。"""
    for domain in ("", "www.codebuddy.cn", "www.workbuddy.cn", "www.codebuddy.ai", "www.workbuddy.ai"):
        host = "https://" + (domain or "www.codebuddy.cn")
        for failure in (404, 401, "network"):
            for operation, paths in (
                    (credits.daily_checkin, list(credits.CHECKIN_PATHS)),
                    (credits.fetch_credits, [credits.RESOURCE_PATH] * (1 if failure == 401 else 3)),
                    (credits.fetch_request_usage, [credits.USAGE_PATH])):
                client = MagicMock()
                client.post.return_value.status_code = failure if isinstance(failure, int) else 500
                client.post.return_value.json.return_value = {}
                if failure == "network":
                    client.post.side_effect = httpx.ConnectError("mock connection failure")
                with patch("app.credits.httpx.Client") as factory, patch("app.credits.time.sleep"):
                    factory.return_value.__enter__.return_value = client
                    if operation is credits.daily_checkin:
                        result = operation("opaque", domain=domain)
                        assert result["ok"] is False
                        if failure == 401:
                            assert result["code"] == 401
                    else:
                        expected_error = (AuthExpiredError if failure == 401 else
                                          httpx.HTTPError if failure == "network" and operation is credits.fetch_request_usage
                                          else RuntimeError)
                        with TestCase().assertRaises(expected_error):
                            operation("opaque", domain=domain)
                assert [call.args[0] for call in client.post.call_args_list] == [host + path for path in paths]
    print("test_financial_failures_stay_on_profile passed")


def test_classify_checkin():
    assert classify_checkin_result(True, 0, "ok")["ok"] is True
    r = classify_checkin_result(True, 10001, "今日已签到，请勿重复")
    assert r["ok"] is True and r["already"] is True
    r = classify_checkin_result(True, 10001, "活动未开启")  # 10001 但文案是未开启 → 不算已签
    assert r["ok"] is False and r["inactive"] is True
    assert classify_checkin_result(False, 0, "x")["ok"] is False      # HTTP 非 2xx 不算成功
    assert classify_checkin_result(True, 1, "fail")["ok"] is False
    assert classify_checkin_result(True, None, "")["ok"] is False
    print("✅ test_classify_checkin")


def test_extract_segments():
    accounts = [
        {  # 有切片明细：展开，过期字段优先 DeductionEndTime
            "PackageName": "月度包", "PackageCode": "p1",
            "SlicePeriodUsageDetails": [
                {"SlicePeriodCapacityRemainPrecise": "300", "SlicePeriodCapacitySizePrecise": "500",
                 "DeductionEndTime": 1700000100},
                {"SlicePeriodCapacityRemainPrecise": "0"},  # 余量 0 被过滤
            ],
        },
        {  # 无明细：周期字段
            "PackageName": "赠送包", "PackageCode": "p2",
            "CycleCapacityRemainPrecise": "200.5", "CycleCapacitySizePrecise": "500",
            "ExpiredTime": "2027-01-01 00:00:00",
        },
        {"PackageName": "空包", "CapacityRemain": 0},  # 余量 0 过滤
    ]
    segs = extract_segments(accounts)
    assert len(segs) == 2, segs
    assert segs[0]["remaining"] == 300 and segs[0]["expires_at"] == 1700000100
    assert segs[1]["remaining"] == 200.5 and segs[1]["source"] == "赠送包"
    assert segs[1]["expires_at"] is not None
    print("✅ test_extract_segments")


def test_merge_and_sort_segments():
    segs = merge_segments([
        {"remaining": 100, "total": 100, "expires_at": 3000, "source": "包A", "package_code": "a"},
        {"remaining": 50, "total": 50, "expires_at": 3000, "source": "包A", "package_code": "a"},   # 同包同期 → 合并
        {"remaining": 200, "total": 200, "expires_at": 1000, "source": "包B", "package_code": "b"},
        {"remaining": 999, "total": 999, "expires_at": None, "source": "永久", "package_code": ""},  # 无过期排最后
        {"remaining": 0, "total": 0, "expires_at": 500, "source": "空", "package_code": "c"},
    ])
    assert len(segs) == 3
    assert segs[0]["package_code"] == "b"                       # 最早过期排最前
    assert segs[1]["remaining"] == 150 and segs[1]["total"] == 150  # 合并结果
    assert segs[2]["expires_at"] is None                        # 无过期时间排最后
    print("✅ test_merge_and_sort_segments")


def test_soonest_expiry():
    now = time.time()
    segs = [
        {"remaining": 10, "expires_at": now + 86400},
        {"remaining": 10, "expires_at": now + 3600},   # 最早未过期
        {"remaining": 10, "expires_at": now - 100},    # 已过期，不算
        {"remaining": 10, "expires_at": None},
    ]
    assert soonest_expiry(segs, now=now) == now + 3600
    assert soonest_expiry([{"remaining": 10, "expires_at": now - 1}], now=now) is None  # 全过期
    assert soonest_expiry([{"remaining": 10, "expires_at": None}], now=now) is None     # 无过期时间
    assert soonest_expiry([], now=now) is None
    print("✅ test_soonest_expiry")


def test_ledger(tmp_path=None):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        ledger = CreditLedger(path)
        day = time.strftime("%Y-%m-%d")
        assert not ledger.checkin_done("c1", day)
        ledger.mark_checkin("c1", day, True, 0, "ok")
        assert ledger.checkin_done("c1", day)
        assert not ledger.checkin_done("c1", "1999-01-01")  # 跨日重新签

        now = time.time()
        ledger.update_credits("c1", {"credits": 300.0, "count": 1, "segments": [
            {"remaining": 300, "total": 300, "expires_at": now + 7200, "source": "包", "package_code": "x"}],
            "soonest_expiry": now + 7200})
        assert ledger.soonest_expiry_of("c1") == now + 7200
        assert ledger.soonest_expiry_of("c2") is None  # 无数据

        # 持久化往返
        ledger2 = CreditLedger(path)
        assert ledger2.checkin_done("c1", day)
        assert ledger2.soonest_expiry_of("c1") == now + 7200
        snap = ledger2.snapshot()
        assert snap["c1"]["credits"]["credits"] == 300.0
    print("✅ test_ledger")


def test_ledger_remove_and_entry():
    """同路径换账号/站点时彻底清旧状态；快照不泄露内部引用。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        ledger = CreditLedger(path)
        assert ledger.entry("missing") == {}
        assert ledger.snapshot() == {}  # 只读 entry 不创建条目
        result = {"credits": 10, "intl": False, "segments": [
            {"remaining": 10, "total": 10, "expires_at": time.time() + 3600}]}
        ledger.update_credits("same-path", result)
        ledger.mark_checkin("same-path", "2026-01-01", True, 0, "ok")
        ledger.note_error("same-path", "old-account-error")
        ledger.update_credits("other", {"credits": 20, "intl": True})
        other = ledger.entry("other")
        result["segments"][0]["remaining"] = 999
        entry = ledger.entry("same-path")
        assert entry["credits"]["segments"][0]["remaining"] == 10
        entry["credits"]["segments"][0]["remaining"] = 888
        entry["checkin"]["ok"] = False
        assert ledger.entry("same-path")["credits"]["segments"][0]["remaining"] == 10
        assert ledger.checkin_done("same-path", "2026-01-01")
        ledger.remove("same-path")
        ledger.remove("missing")  # 幂等且不创建幽灵条目
        assert ledger.entry("same-path") == {}
        assert ledger.soonest_expiry_of("same-path") is None
        reloaded = CreditLedger(path)
        assert reloaded.snapshot() == {"other": other}
        reloaded.update_credits("same-path", {"credits": 3, "intl": True})
        replacement = reloaded.entry("same-path")
        assert replacement["credits"]["intl"] is True
        assert replacement["credits"]["credits"] == 3
        assert replacement["checkin"] == {} and replacement["error"] is None
        assert not reloaded.checkin_done("same-path", "2026-01-01")
    print("test_ledger_remove_and_entry passed")


def test_ledger_threaded_entries():
    """不同凭证并发更新/删除与深拷贝读取仍可持久化。"""
    from concurrent.futures import ThreadPoolExecutor
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        ledger = CreditLedger(path)

        def update(cred_id):
            for i in range(10):
                ledger.update_credits(cred_id, {"credits": i, "segments": [{"remaining": i}]})
                snap = ledger.entry(cred_id)
                snap["credits"]["segments"][0]["remaining"] = -1
                assert ledger.entry(cred_id)["credits"]["segments"][0]["remaining"] == i
                ledger.remove(cred_id)
                assert ledger.entry(cred_id) == {}
            ledger.update_credits(cred_id, {"credits": 10})

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(update, ["domestic", "international"]))
        restored = CreditLedger(path).snapshot()
        assert set(restored) == {"domestic", "international"}
        assert all(e["credits"]["credits"] == 10 for e in restored.values())
    print("test_ledger_threaded_entries passed")


def test_daily_checkin_http(monkey_response=None):
    """mock httpx：首 host 404 换 path 后 code=0 成功。"""
    calls = []

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
        def json(self):
            return self._payload

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, headers=None, json=None, timeout=None):
            calls.append(url)
            if "/v2/" in url:
                return FakeResp(200, {"code": 0, "msg": "签到成功"})
            return FakeResp(404, {})

    orig = credits.httpx.Client
    credits.httpx.Client = FakeClient
    try:
        r = credits.daily_checkin(_jwt("https://www.codebuddy.cn/x"))
    finally:
        credits.httpx.Client = orig
    assert r["ok"] is True, r
    assert calls == ["https://www.codebuddy.cn" + path for path in credits.CHECKIN_PATHS]  # 只换 path
    print("✅ test_daily_checkin_http")


def test_fetch_credits_http():
    """mock httpx：get-user-resource 返回切片明细，验证汇总与最早过期。"""
    now = time.time()

    class FakeResp:
        status_code = 200
        def json(self):
            return {"code": 0, "data": {"Response": {"Data": {"Accounts": [
                {"PackageName": "月度包", "PackageCode": "m",
                 "SlicePeriodUsageDetails": [
                     {"SlicePeriodCapacityRemainPrecise": "300", "DeductionEndTime": now + 3600},
                     {"SlicePeriodCapacityRemainPrecise": "200", "DeductionEndTime": now + 7200},
                 ]},
            ]}}}}

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, headers=None, json=None, timeout=None):
            assert url.endswith("/v2/billing/meter/get-user-resource")
            body = json
            assert body["ProductCode"] == "p_tcaca" and body["Status"] == [0, 3]
            return FakeResp()

    orig = credits.httpx.Client
    credits.httpx.Client = FakeClient
    try:
        r = credits.fetch_credits(_jwt("https://www.codebuddy.cn/x"), uid="u1")
    finally:
        credits.httpx.Client = orig
    assert r["credits"] == 500.0 and r["count"] == 1
    assert r["soonest_expiry"] == now + 3600
    assert len(r["segments"]) == 2
    print("✅ test_fetch_credits_http")


def test_pick_expiry_priority():
    """凭证池 pick：快过期积分的凭证优先；同级轮询；无数据排最后。"""
    import converter
    with tempfile.TemporaryDirectory() as td:
        # 三个假凭证文件
        paths = []
        for i, uid in enumerate(["u1", "u2", "u3"]):
            p = Path(td) / f"cred{i}.info"
            p.write_text(json.dumps({
                "auth": {"accessToken": "t", "expiresAt": int(time.time() * 1000) + 86400000},
                "account": {"uid": uid}}), encoding="utf-8")
            paths.append(p)
        pool = converter.CredentialPool(paths)
        ledger = CreditLedger(Path(td) / "ledger.json")
        pool.set_ledger(ledger)

        # 无数据时：全部同级，轮询
        seen = {pool.pick(None).path.name for _ in range(3)}
        assert len(seen) == 3, seen

        now = time.time()
        ids = [str(p.resolve()) for p in paths]
        ledger.update_credits(ids[2], {"credits": 10, "count": 1, "segments": [
            {"remaining": 10, "total": 10, "expires_at": now + 3600, "source": "s", "package_code": "a"}],
            "soonest_expiry": now + 3600})
        ledger.update_credits(ids[0], {"credits": 10, "count": 1, "segments": [
            {"remaining": 10, "total": 10, "expires_at": now + 86400, "source": "s", "package_code": "b"}],
            "soonest_expiry": now + 86400})

        # cred2 最早过期 → 恒优先
        for _ in range(3):
            assert pool.pick(None).path.name == "cred2.info"

        # cred2 数据清空后 → cred0 优先（cred1 无数据排最后）
        ledger.update_credits(ids[2], {"credits": 0, "count": 0, "segments": [], "soonest_expiry": None})
        for _ in range(2):
            assert pool.pick(None).path.name == "cred0.info"
    print("✅ test_pick_expiry_priority")


def test_fetch_model_catalog():
    """mock httpx：/v3/config 返回模型表；校验 cli 平台头与解析。"""
    seen_headers = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"code": 0, "data": {"models": [
                {"id": "glm-9.9", "name": "G", "supportsToolCall": True},
                {"id": "img-1", "supportsToolCall": False},
            ]}}

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, headers=None, timeout=None):
            seen_headers.update(headers or {})
            assert url.endswith("/v3/config")
            return FakeResp()

    orig = credits.httpx.Client
    credits.httpx.Client = FakeClient
    try:
        models = credits.fetch_model_catalog(_jwt("https://www.codebuddy.cn/x"), user_agent="CLI/9.9")
    finally:
        credits.httpx.Client = orig
    assert [m["id"] for m in models] == ["glm-9.9", "img-1"]
    assert seen_headers.get("x-client-platform") == "cli"  # 必须 cli,否则 400
    assert httpx.Headers(seen_headers)["user-agent"] == "CLI/9.9"
    print("✅ test_fetch_model_catalog")


def test_current_models_merge():
    """显式国内内部视图：已知目录不补静态模型，明确空表不回退。"""
    import converter
    with patch.dict(converter.CONFIG, {"cred_pool": None, "cred": None, "model_catalogs": {},
                                       "account_catalogs": None, "model_cache": None, "ledger": None,
                                       "models_intl": None, "models_remote": [
            {"id": "glm-9.9", "supportsToolCall": True},
            {"id": "hunyuan-image", "supportsToolCall": False},
        ]}):
        out = converter.current_models(region="cn")
        assert out == ["glm-9.9", "auto"]  # 图像模型不进表，调度别名保留
        assert not [m for m in out if m.endswith("-free")]
        assert "deepseek-v4.1-flash" in converter.DEFAULT_MODELS
        assert "glm-5.3" not in out  # 已知目录禁止借用默认表扩大产品能力
        converter.CONFIG["models_remote"] = []
        assert converter.current_models(region="cn") == []
        converter.CONFIG["models_remote"] = None
        # 无账号的旧式内部展示兜底不代表生产账号获得该目录的路由权限。
        assert converter.current_models(region="cn") == converter.DEFAULT_MODELS
    print("✅ test_current_models_merge")


def test_credits_to_usd():
    """单价→美元换算：0.014 元/Credit @ 汇率 7.15。"""
    assert abs(credits.credits_to_usd(1000) - 1000 * 0.014 / 7.15) < 1e-9
    assert credits.credits_to_usd(0) == 0.0
    assert abs(credits.credits_to_usd(50000, 0.014, 7.0) - 100.0) < 1e-9  # 汇率可覆盖
    assert credits.CREDIT_PRICE_CNY == 0.014 and credits.USAGE_MAX_DAYS == 30
    print("✅ test_credits_to_usd")


def test_aggregate_credits():
    """ledger 汇总：按国内/国际分组累加、额度差已用、最早过期；空条目容错。"""
    snap = {"a": {"credits": {"intl": False, "segments": [
        {"remaining": 100, "total": 200, "expires_at": 500},
        {"remaining": 50, "total": 50, "expires_at": 900}]}},
        "b": {"credits": {"segments": []}},
        "c": {},
        "ai": {"credits": {"intl": True, "segments": [
               {"remaining": 300, "total": 400, "expires_at": 700}]}},
        "d": {"credits": {"segments": [{"remaining": 30, "total": 10, "expires_at": None}]}}}
    agg = credits.aggregate_credits(snap)
    assert agg["remaining"] == 480, agg          # 国内 180 + 国际 300
    assert agg["used_by_quota"] == 200, agg      # 国内 100 + 国际 100；负差不计
    assert agg["soonest_expiry"] == 500, agg
    g = agg["groups"]
    assert g["domestic"]["remaining"] == 180 and g["domestic"]["used_by_quota"] == 100, g
    assert g["international"]["remaining"] == 300, g
    assert g["international"]["soonest_expiry"] == 700, g
    assert credits.aggregate_credits({})["remaining"] == 0.0
    assert credits.aggregate_credits({})["soonest_expiry"] is None
    print("✅ test_aggregate_credits")


def _fake_client(pages, seen=None):
    """按 pageNum 返回预置响应的 httpx.Client 替身。"""
    class FakeResp:
        status_code = 200
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, headers=None, json=None, timeout=None):
            if seen is not None:
                seen.append(json.get("pageNum", json.get("PageNumber")))
            return FakeResp(pages[min((json.get("pageNum") or json.get("PageNumber")) - 1, len(pages) - 1)])
    return FakeClient


def _with_client(fake, fn):
    orig = credits.httpx.Client
    credits.httpx.Client = fake
    try:
        return fn()
    finally:
        credits.httpx.Client = orig


def test_fetch_request_usage_rejects_invalid_success_payloads():
    """HTTP 200 但业务码失败或结构缺失：必须报错，不能当作零用量。"""
    token = _jwt("https://www.codebuddy.cn/x")
    for pages in ([{"code": 1059, "msg": "rate limited", "data": {"total": 0, "data": []}}],
                  [{"code": 0, "data": {}}],                      # 缺 data.data/total
                  [{"data": {"data": [], "total": 0}}],           # code 缺失但结构完整 → 合法空
                  ):
        try:
            result = _with_client(_fake_client(pages),
                                  lambda: credits.fetch_request_usage(token))
        except RuntimeError:
            assert pages[0].get("code") not in (0, None) or "data" not in pages[0].get("data", {}) \
                or "data" not in pages[0]["data"]
        else:
            assert pages[0].get("code") is None and result["requests"] == 0  # 合法空结果照旧可用
    print("✅ test_fetch_request_usage_rejects_invalid_success_payloads")


def test_fetch_credits_distinguishes_empty_from_missing_structure():
    """Accounts 键存在但为空 = 合法零余额；结构整体缺失 = 报错，不得覆盖缓存为零。"""
    token = _jwt("https://www.codebuddy.cn/x")
    empty = _with_client(_fake_client([{"code": 0, "data": {"Response": {"Data": {"Accounts": []}}}}]),
                         lambda: credits.fetch_credits(token))
    assert empty["credits"] == 0.0 and empty["count"] == 0
    for bad in ({"code": 0, "data": {}}, {"code": 0, "data": {"Response": {"Data": {}}}}):
        try:
            _with_client(_fake_client([bad]), lambda: credits.fetch_credits(token))
            raise AssertionError("missing Accounts structure must raise")
        except RuntimeError as error:
            assert "Accounts" in str(error)
    print("✅ test_fetch_credits_distinguishes_empty_from_missing_structure")


def test_fetch_credits_paginates_until_short_page():
    """积分包超过一页时翻页累加；不足一页停止；达到页数上限标记 partial。"""
    token = _jwt("https://www.codebuddy.cn/x")
    account = lambda i: {"PackageName": f"p{i}", "PackageCode": f"c{i}",
                         "SlicePeriodUsageDetails": [{"SlicePeriodCapacityRemainPrecise": "1",
                                                      "DeductionEndTime": None}]}
    full_page = {"code": 0, "data": {"Response": {"Data": {"Accounts": [account(i) for i in range(100)]}}}}
    short_page = {"code": 0, "data": {"Response": {"Data": {"Accounts": [account(1000)]}}}}
    seen = []
    result = _with_client(_fake_client([full_page, short_page], seen), lambda: credits.fetch_credits(token))
    assert seen == [1, 2] and result["count"] == 101 and result["credits"] == 101.0
    assert result["partial"] is False

    seen = []
    result = _with_client(_fake_client([full_page] * credits.CREDITS_MAX_PAGES, seen),
                          lambda: credits.fetch_credits(token))
    assert len(seen) == credits.CREDITS_MAX_PAGES
    assert result["partial"] is True

    # 第 2 页的瞬时空响应也要重试：不能在非首页把空页当作结束
    calls = {"n": 0}
    sequence = [full_page, {"code": 0, "data": {"Response": {"Data": {"Accounts": []}}}}, short_page]

    class FlakyClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, headers=None, json=None, timeout=None):
            calls["n"] += 1
            payload = sequence[min(calls["n"] - 1, len(sequence) - 1)]

            class Resp:
                status_code = 200
                def json(self):
                    return payload
            return Resp()

    result = _with_client(FlakyClient, lambda: credits.fetch_credits(token))
    assert result["count"] == 101 and result["partial"] is False, result
    print("✅ test_fetch_credits_paginates_until_short_page")


def test_fetch_request_usage_marks_partial_at_page_cap():
    """用量明细达到页数上限且 total 更大时必须标记 partial。"""
    token = _jwt("https://www.codebuddy.cn/x")
    big_total = credits.USAGE_MAX_PAGES * credits.USAGE_PAGE_SIZE + 1
    row = {"requestTime": "2026-09-01 10:00:00", "model": "m", "credit": 0.01}
    page = {"code": 0, "data": {"total": big_total, "data": [row]}}
    result = _with_client(_fake_client([page]), lambda: credits.fetch_request_usage(token))
    assert result["partial"] is True and result["requests"] == credits.USAGE_MAX_PAGES
    print("✅ test_fetch_request_usage_marks_partial_at_page_cap")


def test_sync_usage_keeps_per_account_snapshots_on_failure():
    """单账号同步失败：聚合保留其上次成功快照并标记 stale/partial，不再整体覆盖丢失。"""
    import converter
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for uid in ("u1", "u2"):
            p = Path(td) / f"{uid}.info"
            p.write_text(json.dumps({
                "auth": {"accessToken": f"token-{uid}", "refreshToken": "r",
                         "domain": "https://www.codebuddy.cn",
                         "expiresAt": int(time.time() * 1000) + 86400000,
                         "lastRefreshTime": time.time() * 1000},
                "account": {"uid": uid, "enterpriseId": "e"}}), encoding="utf-8")
            paths.append(p)
        pool = converter.CredentialPool(paths)
        snapshots = {
            "token-u1": {"by_day": {"2026-09-01": {"m": 10.0}}, "total_credits": 10.0, "requests": 1, "partial": False},
            "token-u2": {"by_day": {"2026-09-02": {"m": 20.0}}, "total_credits": 20.0, "requests": 2, "partial": False},
        }
        failing = set()

        def fake_fetch(token, uid="", domain=""):
            if token in failing:
                raise RuntimeError("synthetic sync failure")
            return snapshots[token]

        saved = (converter.CONFIG.get("usage_daily"), converter.CONFIG.get("usage_daily_accounts"),
                 converter.CONFIG.get("control_store"))
        orig_fetch = credits.fetch_request_usage
        credits.fetch_request_usage = fake_fetch
        converter.CONFIG.update(usage_daily=None, usage_daily_accounts=None, control_store=None)
        try:
            from fastapi.testclient import TestClient

            def check_billing_stale(names):
                with patch.dict(converter.CONFIG, {"api_key": "", "ledger": None}), \
                        TestClient(converter.app) as client:
                    sub = client.get("/v1/dashboard/billing/subscription")
                    usage = client.get("/v1/dashboard/billing/usage")
                assert sub.status_code == usage.status_code == 200
                assert sub.json()["codebuddy_partial"] is bool(names)
                assert sub.json().get("codebuddy_stale_accounts", []) == names
                assert usage.json().get("partial", False) is bool(names)
                assert usage.json().get("stale_accounts", []) == names

            failing.update(snapshots)
            for previous in (None, {"total_credits": 999, "fetched_at": 123, "partial": False}):
                converter.CONFIG["usage_daily"] = previous
                converter._sync_usage(pool)
                view = converter.CONFIG["usage_daily"]
                assert view["by_day"] == view["groups"] == {}
                assert view["total_credits"] == view["requests"] == view["fetched_at"] == 0
                assert view["partial"] is True and view["stale_accounts"] == ["u1.info", "u2.info"]
                check_billing_stale(["u1.info", "u2.info"])
                assert converter._billing_totals()["used_source"] == "quota_delta"
            failing.clear()

            # 首轮即有账号失败且无任何历史快照：也必须标 stale/partial，不能装作精确
            failing.add("token-u2")
            converter._sync_usage(pool)
            view = converter.CONFIG["usage_daily"]
            assert view["total_credits"] == 10.0 and view["requests"] == 1
            assert view["partial"] is True and view["stale_accounts"] == ["u2.info"]

            failing.clear()
            converter._sync_usage(pool)
            view = converter.CONFIG["usage_daily"]
            assert view["total_credits"] == 30.0 and view["requests"] == 3
            assert view["partial"] is False and "stale_accounts" not in view

            failing.update(snapshots)
            last_good = dict(view)
            converter._sync_usage(pool)
            view = converter.CONFIG["usage_daily"]
            for key in ("by_day", "groups", "total_credits", "requests", "fetched_at"):
                assert view[key] == last_good[key]
            check_billing_stale(["u1.info", "u2.info"])
            failing.clear()

            failing.add("token-u2")
            converter._sync_usage(pool)
            view = converter.CONFIG["usage_daily"]
            assert view["total_credits"] == 30.0 and view["requests"] == 3  # u2 历史保留
            assert view["partial"] is True and view["stale_accounts"] == ["u2.info"]

            failing.clear()
            snapshots["token-u2"] = {"by_day": {"2026-09-02": {"m": 25.0}},
                                     "total_credits": 25.0, "requests": 4, "partial": False}
            converter._sync_usage(pool)
            view = converter.CONFIG["usage_daily"]
            assert view["total_credits"] == 35.0 and view["partial"] is False  # 成功后自愈

            paths[1].unlink()
            pool.prune()
            converter._sync_usage(pool)
            view = converter.CONFIG["usage_daily"]
            assert view["total_credits"] == 10.0  # 凭证删除后其快照不再计入

            with patch.object(converter.model_policy, "credential_enabled", return_value=False):
                converter._sync_usage(pool)
            assert converter.CONFIG["usage_daily"]["total_credits"] == 0
            check_billing_stale([])
            paths[0].unlink()
            pool.prune()
            converter._sync_usage(pool)
            assert converter.CONFIG["usage_daily"]["groups"] == {}
            check_billing_stale([])
        finally:
            credits.fetch_request_usage = orig_fetch
            converter.CONFIG["usage_daily"], converter.CONFIG["usage_daily_accounts"], \
                converter.CONFIG["control_store"] = saved
    print("✅ test_sync_usage_keeps_per_account_snapshots_on_failure")


def test_fetch_request_usage_paging():
    """mock 分页明细：跨页聚合 credit，按 日期×模型 归并；请求天数夹到 30 天。"""
    pages = [
        {"code": 0, "data": {"total": 3, "data": [
            {"requestTime": "2026-09-01 10:00:00", "model": "glm-5.3", "credit": 0.5},
            {"requestTime": "2026-09-01 11:00:00", "model": "glm-5.3", "credit": 0.25}]}},
        {"code": 0, "data": {"total": 3, "data": [
            {"requestTime": "2026-09-02 09:00:00", "model": "hy4-preview", "credit": 0}]}},
    ]
    seen = {"pages": [], "days": []}

    class FakeResp:
        status_code = 200
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, headers=None, json=None, timeout=None):
            seen["pages"].append(json["pageNum"])
            seen["days"].append(json["startTime"])
            return FakeResp(pages[min(json["pageNum"] - 1, len(pages) - 1)])

    orig = credits.httpx.Client
    credits.httpx.Client = FakeClient
    try:
        u = credits.fetch_request_usage(_jwt("https://www.codebuddy.cn/x"), days=365)
    finally:
        credits.httpx.Client = orig
    assert seen["pages"] == [1, 2], seen       # 按 total 停止分页
    assert u["requests"] == 3 and abs(u["total_credits"] - 0.75) < 1e-9 and u["partial"] is False
    assert u["by_day"]["2026-09-01"]["glm-5.3"] == 0.75
    assert "hy4-preview" in u["by_day"]["2026-09-02"]  # 免费模型 0 credit 也计入请求数
    import time as _t
    span = (_t.mktime(_t.strptime(seen["days"][0][:19], "%Y-%m-%d %H:%M:%S")))
    assert abs((_t.time() - span) - 30 * 86400) < 3600  # days=365 被夹回 30（官方 >31 天返回空）
    print("✅ test_fetch_request_usage_paging")


def test_billing_balance_identity():
    """核心不变式：hard_limit_usd − total_usage/100 == 剩余余额（One-API 算法自洽）。"""
    import converter
    with tempfile.TemporaryDirectory() as td:
        led = credits.CreditLedger(Path(td) / "ledger.json")
        led.update_credits("c1", {"credits": 1000.0, "count": 1, "segments": [
            {"remaining": 1000.0, "total": 1600.0, "expires_at": time.time() + 864000}],
            "soonest_expiry": time.time() + 864000})
        saved_led, saved_usage = converter.CONFIG.get("ledger"), converter.CONFIG.get("usage_daily")
        try:
            converter.CONFIG["ledger"] = led
            # 明细可用：已用取官方明细（真实消耗 200 credits）
            day_map = {"2026-09-01": {"glm-5.3": 200.0}}
            converter.CONFIG["usage_daily"] = {"by_day": day_map,
                "groups": {"domestic": {"by_day": day_map, "total_credits": 200.0,
                                        "requests": 10}},
                "total_credits": 200.0, "requests": 10, "fetched_at": time.time()}
            t = converter._billing_totals()
            assert t["remaining"] == 1000.0 and t["used"] == 200.0 and t["quota"] == 1200.0
            assert t["used_source"] == "official_usage_detail"
            # 端点级恒等式：客户端按 hard_limit_usd − total_usage/100 算出的正是真实剩余
            sub = converter.billing_subscription(None, None)
            usage = converter.billing_usage(None, None, None, None)
            assert sub["codebuddy_partial"] is False  # 数据完整时显式 False
            assert abs(sub["hard_limit_usd"] - usage["total_usage"] / 100 - t["remaining_usd"]) < 0.01
            assert sub["codebuddy_credits_remaining"] == 1000.0
            assert sub["plan"]["title"].startswith("CodeBuddy Credits")
            assert usage["object"] == "list"
            assert usage["daily_costs"][0]["line_items"][0]["name"] == "glm-5.3"
            # 明细缺失：回退额度差（1600-1000=600）且恒等式仍成立
            converter.CONFIG["usage_daily"] = None
            t2 = converter._billing_totals()
            assert t2["used"] == 600.0 and t2["used_source"] == "quota_delta"
            assert abs(t2["quota_usd"] - t2["used_usd"] - t2["remaining_usd"]) < 0.01
            assert converter.billing_usage(None, None, None, None)["daily_costs"] == []  # 无明细则不出 daily_costs
            # 区间过滤只统计窗口内明细
            converter.CONFIG["usage_daily"] = {"by_day": {"2026-09-01": {"glm-5.3": 200.0},
                                                          "2026-09-05": {"glm-5.3": 50.0}},
                                               "groups": {"domestic": {"by_day": {
                                                   "2026-09-01": {"glm-5.3": 200.0},
                                                   "2026-09-05": {"glm-5.3": 50.0}},
                                                   "total_credits": 250.0, "requests": 3}},
                                               "total_credits": 250.0, "requests": 3,
                                               "fetched_at": time.time()}
            filtered = converter.billing_usage("2026-09-04", "2026-09-30", None, None)
            assert len(filtered["daily_costs"]) == 1
            assert filtered["daily_costs"][0]["line_items"][0]["cost"] > 0
        finally:
            converter.CONFIG["ledger"] = saved_led
            converter.CONFIG["usage_daily"] = saved_usage
    print("✅ test_billing_balance_identity")


def test_billing_usage_prices_each_day_by_site():
    """两站单价不同且用量发生在不同天：逐日金额必须按本站单价，而不是全局平均价。"""
    import converter
    with tempfile.TemporaryDirectory() as td:
        led = credits.CreditLedger(Path(td) / "ledger.json")
        led.update_credits("cn", {"credits": 100.0, "segments": [
            {"remaining": 100.0, "total": 200.0, "expires_at": None}], "intl": False})
        led.update_credits("ai", {"credits": 100.0, "segments": [
            {"remaining": 100.0, "total": 200.0, "expires_at": None}], "intl": True})
        saved = (converter.CONFIG.get("ledger"), converter.CONFIG.get("usage_daily"))
        try:
            converter.CONFIG["ledger"] = led
            # 国内 100 credits @ $0.014/7.15 在 09-01；国际 100 credits @ $0.03 在 09-02
            converter.CONFIG["usage_daily"] = {
                "by_day": {"2026-09-01": {"m": 100.0}, "2026-09-02": {"m": 100.0}},
                "groups": {"domestic": {"by_day": {"2026-09-01": {"m": 100.0}},
                                        "total_credits": 100.0, "requests": 1},
                           "international": {"by_day": {"2026-09-02": {"m": 100.0}},
                                             "total_credits": 100.0, "requests": 1}},
                "total_credits": 200.0, "requests": 2, "fetched_at": time.time()}
            usage = converter.billing_usage(None, None, None, None)
            days = {d["timestamp"]: d["line_items"] for d in usage["daily_costs"]}
            import time as _t
            d1 = _t.mktime(_t.strptime("2026-09-01", "%Y-%m-%d"))
            d2 = _t.mktime(_t.strptime("2026-09-02", "%Y-%m-%d"))
            cn_cents = 100 * 0.014 / 7.15 * 100   # ≈ 19.58 美分
            assert abs(days[d1][0]["cost"] - cn_cents) < 0.01, days[d1]
            assert abs(days[d2][0]["cost"] - 300.0) < 0.01, days[d2]  # 100 × $0.03 = 300 美分
            # 恒等式：Σdaily ≈ total_usage（全量口径取 used_usd）
            assert abs(sum(i["cost"] for d in usage["daily_costs"] for i in d["line_items"])
                       - usage["total_usage"]) < 0.02
        finally:
            converter.CONFIG["ledger"], converter.CONFIG["usage_daily"] = saved
    print("✅ test_billing_usage_prices_each_day_by_site")


def test_billing_intl_split():
    """国内/国际分组折算：单价各按站点，合计与恒等式仍成立。"""
    import converter
    with tempfile.TemporaryDirectory() as td:
        led = credits.CreditLedger(Path(td) / "ledger.json")
        led.update_credits("cn", {"credits": 1000.0, "segments": [
            {"remaining": 1000.0, "total": 1000.0, "expires_at": None}], "intl": False})
        led.update_credits("ai", {"credits": 500.0, "segments": [
            {"remaining": 500.0, "total": 500.0, "expires_at": None}], "intl": True})
        saved = (converter.CONFIG.get("ledger"), converter.CONFIG.get("usage_daily"))
        try:
            converter.CONFIG["ledger"] = led
            converter.CONFIG["usage_daily"] = None   # 无明细，走额度差口径
            t = converter._billing_totals()
            assert t["groups"]["domestic"]["credits_remaining"] == 1000.0
            assert t["groups"]["international"]["credits_remaining"] == 500.0
            cn_usd = 1000 * 0.014 / 7.15          # 国内：CNY 单价 / 汇率
            assert abs(t["groups"]["domestic"]["balance_usd"] - cn_usd) < 0.01, t["groups"]
            assert abs(t["groups"]["international"]["balance_usd"] - 15.0) < 0.01  # 500×$0.03
            assert abs(t["remaining_usd"] - (cn_usd + 15.0)) < 0.01
            # 跨组线性可加，余额恒等式不被破坏
            assert abs(t["quota_usd"] - t["used_usd"] - t["remaining_usd"]) < 0.01, t
            # 人民币合计：国内按元计，国际按美元×汇率
            assert abs(t["remaining_cny"] - (1000 * 0.014 + 500 * 0.03 * 7.15)) < 0.01, t
            sub = converter.billing_subscription(None, None)
            assert sub["codebuddy_sites"]["international"]["credits_remaining"] == 500.0
            assert "INTL" in sub["plan"]["title"]
        finally:
            converter.CONFIG["ledger"], converter.CONFIG["usage_daily"] = saved
    print("✅ test_billing_intl_split")


def test_current_models_intl_condition():
    """默认视图合并有额度的国际来源；显式地域内部过滤仍相互隔离。"""
    import converter
    with tempfile.TemporaryDirectory() as td, patch.dict(converter.CONFIG, {
            "cred_pool": None, "cred": None, "model_catalogs": {}, "ledger": None,
            "account_catalogs": None, "model_cache": None,
            "models_remote": [{"id": "glm-5.3", "supportsToolCall": True}],
            "models_intl": [{"id": "gpt-5.5", "supportsToolCall": True},
                            {"id": "img-1", "supportsToolCall": False}]}):
        assert converter.current_models("cn") == ["glm-5.3", "auto"]
        assert converter.current_models("intl") == []  # 无可信国际余额
        assert set(converter.current_models()) == {"glm-5.3", "auto"}
        led = credits.CreditLedger(Path(td) / "l.json")
        led.update_credits("ai", {"credits": 0.0, "segments": [], "intl": True})
        converter.CONFIG["ledger"] = led
        assert converter.current_models("intl") == []  # 国际额度为 0
        assert set(converter.current_models()) == {"glm-5.3", "auto"}
        led.update_credits("ai", {"credits": 120.0, "segments": [
            {"remaining": 120.0, "total": 120.0, "expires_at": None}], "intl": True})
        assert converter.current_models("intl") == ["gpt-5.5"]
        assert converter.current_models("cn") == ["glm-5.3", "auto"]
        assert set(converter.current_models()) == {"glm-5.3", "gpt-5.5", "auto"}
        converter.CONFIG["models_intl"] = []
        assert converter.current_models("intl") == []  # 有额度也不绕过明确空表
        assert set(converter.current_models()) == {"glm-5.3", "auto"}
    print("✅ test_current_models_intl_condition")


def test_guard_model():
    """表外模型本地拦截为 404；表内/别名/关闭开关时放行。"""
    import converter
    from fastapi import HTTPException
    saved = (converter.CONFIG.get("model_guard"), converter.CONFIG.get("models_remote"))
    try:
        converter.CONFIG["model_guard"] = True
        converter.CONFIG["models_remote"] = [{"id": "glm-5.3", "supportsToolCall": True}]
        converter.invalidate_model_table()
        converter.guard_model("glm-5.3")   # 表内
        converter.guard_model("auto")      # 已知非空国内 CLI 目录的旧调度别名
        with TestCase().assertRaises(HTTPException) as raised:
            converter.guard_model("")  # 显式空模型不是默认模型别名
        assert raised.exception.status_code == 400
        assert raised.exception.detail["error"]["param"] == "model"
        try:
            converter.guard_model("gpt-9.9")
        except HTTPException as e:
            assert e.status_code == 404, e
            assert e.detail["error"]["code"] == "model_not_found", e
            assert "gpt-9.9" in e.detail["error"]["message"]
        else:
            raise AssertionError("表外模型必须被本地拦截")
        converter.CONFIG["model_guard"] = False
        converter.guard_model("gpt-9.9")   # 关闭开关后放行
    finally:
        converter.CONFIG["model_guard"], converter.CONFIG["models_remote"] = saved
    print("✅ test_guard_model")


def test_model_catalog_cache():
    """模型表缓存：TTL 命中免拉云端、持久化可重载、站点组判定正确。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "catalog.json"
        c1 = credits.ModelCatalogCache(p, ttl=3600)
        assert not c1.fresh("domestic") and c1.models("domestic") == []
        assert c1.age("domestic") is None
        c1.put("domestic", [{"id": "glm-5.3", "supportsToolCall": True}])
        assert c1.fresh("domestic") and len(c1.models("domestic")) == 1
        assert not c1.fresh("international")        # 未拉过的组不暴露
        c2 = credits.ModelCatalogCache(p, ttl=3600)  # 重新加载：持久化生效
        assert c2.fresh("domestic") and c2.models("domestic")[0]["id"] == "glm-5.3"
        assert c2.age("domestic") >= 0
        c3 = credits.ModelCatalogCache(p, ttl=60)
        c3._data["groups"]["domestic"]["fetched_at"] = time.time() - 120
        assert not c3.fresh("domestic")             # TTL 过期后需重拉
        g = credits.ModelCatalogCache.group_for_token
        assert g(_jwt("https://www.codebuddy.cn/x")) == "domestic"
        assert g(_jwt("https://www.workbuddy.ai/x")) == "international"
        assert credits.is_international_host("https://www.codebuddy.ai") is True
        assert credits.is_international_host("https://www.codebuddy.cn") is False
    print("✅ test_model_catalog_cache")


if __name__ == "__main__":
    test_issuer_origin()
    test_hosts_for_token()
    test_financial_hints_rejected_before_network()
    test_financial_profile_hosts_and_web_headers()
    test_financial_failures_stay_on_profile()
    test_classify_checkin()
    test_extract_segments()
    test_merge_and_sort_segments()
    test_soonest_expiry()
    test_ledger()
    test_ledger_remove_and_entry()
    test_ledger_threaded_entries()
    test_daily_checkin_http()
    test_fetch_credits_http()
    test_pick_expiry_priority()
    test_fetch_model_catalog()
    test_current_models_merge()
    test_credits_to_usd()
    test_aggregate_credits()
    test_fetch_request_usage_rejects_invalid_success_payloads()
    test_fetch_credits_distinguishes_empty_from_missing_structure()
    test_sync_usage_keeps_per_account_snapshots_on_failure()
    test_fetch_credits_paginates_until_short_page()
    test_fetch_request_usage_marks_partial_at_page_cap()
    test_fetch_request_usage_paging()
    test_billing_balance_identity()
    test_billing_usage_prices_each_day_by_site()
    test_billing_intl_split()
    test_current_models_intl_condition()
    test_guard_model()
    test_model_catalog_cache()
    print("\n全部通过 ✅")
