#!/usr/bin/env python3
"""test_credits.py — 验证 credits.py 的签到判定/域名选择/积分分段/ledger 与快过期优先调度。

直接运行：python3 test_credits.py
"""

import base64
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

import credits
from credits import (
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
    assert hosts_for_token("bad") == credits.DEFAULT_HOSTS  # 未识别 → 国内双 host 兜底
    print("✅ test_hosts_for_token")


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
    assert calls[0].endswith("/billing/meter/daily-checkin")
    assert calls[1].endswith("/v2/billing/meter/daily-checkin")  # 404 后换 v2 path
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
    assert seen_headers.get("user-agent") == "CLI/9.9"
    print("✅ test_fetch_model_catalog")


def test_current_models_merge():
    """云端表优先(过滤非对话模型),auto 调度别名保留,无云端数据回退默认表;-free 别名已移除。"""
    import converter
    saved = converter.CONFIG.get("models_remote")
    try:
        converter.CONFIG["models_remote"] = [
            {"id": "glm-9.9", "supportsToolCall": True},
            {"id": "hunyuan-image", "supportsToolCall": False},  # 图像模型不进表
        ]
        out = converter.current_models()
        assert out[0] == "glm-9.9" and "hunyuan-image" not in out
        assert "auto" in out                                  # 网关调度别名保留
        assert not [m for m in out if m.endswith("-free")]     # -free 别名已彻底移除
        assert "deepseek-v4.1-flash" in converter.DEFAULT_MODELS  # 兜底表已补齐官方新模型
        assert "glm-5.3" in out                              # 默认表补充
        converter.CONFIG["models_remote"] = []
        assert converter.current_models() == converter.DEFAULT_MODELS  # 回退
    finally:
        converter.CONFIG["models_remote"] = saved
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
    assert u["requests"] == 3 and abs(u["total_credits"] - 0.75) < 1e-9
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
                                               "total_credits": 250.0, "requests": 3,
                                               "fetched_at": time.time()}
            filtered = converter.billing_usage("2026-09-04", "2026-09-30", None, None)
            assert len(filtered["daily_costs"]) == 1
            assert filtered["daily_costs"][0]["line_items"][0]["cost"] > 0
        finally:
            converter.CONFIG["ledger"] = saved_led
            converter.CONFIG["usage_daily"] = saved_usage
    print("✅ test_billing_balance_identity")


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
    """国际模型仅在「有国际表 + 池内有额度的国际凭证」时对外暴露。"""
    import converter
    saved = (converter.CONFIG.get("models_remote"), converter.CONFIG.get("models_intl"),
             converter.CONFIG.get("ledger"))
    with tempfile.TemporaryDirectory() as td:
        try:
            converter.CONFIG["models_remote"] = [{"id": "glm-5.3", "supportsToolCall": True}]
            converter.CONFIG["models_intl"] = [{"id": "gpt-5.5", "supportsToolCall": True},
                                               {"id": "img-1", "supportsToolCall": False}]
            converter.CONFIG["ledger"] = None          # 无国际凭证
            out = converter.current_models()
            assert "glm-5.3" in out and "gpt-5.5" not in out, out
            led = credits.CreditLedger(Path(td) / "l.json")
            led.update_credits("ai", {"credits": 0.0, "segments": [], "intl": True})
            converter.CONFIG["ledger"] = led          # 有国际凭证但额度为 0
            assert "gpt-5.5" not in converter.current_models()
            led.update_credits("ai", {"credits": 120.0, "segments": [
                {"remaining": 120.0, "total": 120.0, "expires_at": None}], "intl": True})
            out = converter.current_models()          # 有额度 → 暴露
            assert "gpt-5.5" in out and "img-1" not in out, out
        finally:
            (converter.CONFIG["models_remote"], converter.CONFIG["models_intl"],
             converter.CONFIG["ledger"]) = saved
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
        converter.guard_model("auto")      # 网关调度别名（来自兜底表）
        converter.guard_model("")          # 空值不拦截，交由默认值逻辑
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
    test_classify_checkin()
    test_extract_segments()
    test_merge_and_sort_segments()
    test_soonest_expiry()
    test_ledger()
    test_daily_checkin_http()
    test_fetch_credits_http()
    test_pick_expiry_priority()
    test_fetch_model_catalog()
    test_current_models_merge()
    test_credits_to_usd()
    test_aggregate_credits()
    test_fetch_request_usage_paging()
    test_billing_balance_identity()
    test_billing_intl_split()
    test_current_models_intl_condition()
    test_guard_model()
    test_model_catalog_cache()
    print("\n全部通过 ✅")
