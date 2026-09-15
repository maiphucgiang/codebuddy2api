#!/usr/bin/env python3
"""Test check-in, billing hosts, credit segments, persistence and expiry-aware scheduling."""

import base64
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

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
    # Absent identity hints default only to domestic CLI, never another product.
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
    """Reject unsafe or conflicting identity hints before opening a client."""
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
    # Permissive parsing alone must not authorize routing.
    assert token_issuer_origin(_jwt("https://unknown.example/x")) == "https://unknown.example"
    print("test_financial_hints_rejected_before_network passed")


def test_financial_profile_hosts_and_web_headers():
    """Keep product-specific billing and desktop check-in headers isolated."""
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
    """Preserve request counts and same-host check-in without cross-product token forwarding."""
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
    r = classify_checkin_result(True, 10001, "活动未开启")  # Inactive does not mean already claimed.
    assert r["ok"] is False and r["inactive"] is True
    assert classify_checkin_result(False, 0, "x")["ok"] is False      # Non-2xx cannot succeed.
    assert classify_checkin_result(True, 1, "fail")["ok"] is False
    assert classify_checkin_result(True, None, "")["ok"] is False
    print("✅ test_classify_checkin")


def test_extract_segments():
    accounts = [
        {  # Expand slices and prefer their deduction expiry.
            "PackageName": "月度包", "PackageCode": "p1",
            "SlicePeriodUsageDetails": [
                {"SlicePeriodCapacityRemainPrecise": "300", "SlicePeriodCapacitySizePrecise": "500",
                 "DeductionEndTime": 1700000100},
                {"SlicePeriodCapacityRemainPrecise": "0"},  # Filter zero balances.
            ],
        },
        {  # Use package-period fields when slices are absent.
            "PackageName": "赠送包", "PackageCode": "p2",
            "CycleCapacityRemainPrecise": "200.5", "CycleCapacitySizePrecise": "500",
            "ExpiredTime": "2027-01-01 00:00:00",
        },
        {"PackageName": "空包", "CapacityRemain": 0},  # Filter zero balances.
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
        {"remaining": 50, "total": 50, "expires_at": 3000, "source": "包A", "package_code": "a"},   # Merge matching package periods.
        {"remaining": 200, "total": 200, "expires_at": 1000, "source": "包B", "package_code": "b"},
        {"remaining": 999, "total": 999, "expires_at": None, "source": "永久", "package_code": ""},  # Sort unknown expiry last.
        {"remaining": 0, "total": 0, "expires_at": 500, "source": "空", "package_code": "c"},
    ])
    assert len(segs) == 3
    assert segs[0]["package_code"] == "b"                       # Earliest expiry first
    assert segs[1]["remaining"] == 150 and segs[1]["total"] == 150  # Merged balance
    assert segs[2]["expires_at"] is None                        # Unknown expiry last
    print("✅ test_merge_and_sort_segments")


def test_soonest_expiry():
    now = time.time()
    segs = [
        {"remaining": 10, "expires_at": now + 86400},
        {"remaining": 10, "expires_at": now + 3600},   # Earliest active expiry
        {"remaining": 10, "expires_at": now - 100},    # Exclude expired credits.
        {"remaining": 10, "expires_at": None},
    ]
    assert soonest_expiry(segs, now=now) == now + 3600
    assert soonest_expiry([{"remaining": 10, "expires_at": now - 1}], now=now) is None  # All expired
    assert soonest_expiry([{"remaining": 10, "expires_at": None}], now=now) is None     # No known expiry
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
        assert not ledger.checkin_done("c1", "1999-01-01")  # Check-in is scoped to one day.

        now = time.time()
        ledger.update_credits("c1", {"credits": 300.0, "count": 1, "segments": [
            {"remaining": 300, "total": 300, "expires_at": now + 7200, "source": "包", "package_code": "x"}],
            "soonest_expiry": now + 7200})
        assert ledger.soonest_expiry_of("c1") == now + 7200
        assert ledger.soonest_expiry_of("c2") is None  # No balance data

        # Persistence round trip
        ledger2 = CreditLedger(path)
        assert ledger2.checkin_done("c1", day)
        assert ledger2.soonest_expiry_of("c1") == now + 7200
        snap = ledger2.snapshot()
        assert snap["c1"]["credits"]["credits"] == 300.0
    print("✅ test_ledger")


def test_ledger_remove_and_entry():
    """Clear stale state on identity changes and return independent snapshots."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        ledger = CreditLedger(path)
        assert ledger.entry("missing") == {}
        assert ledger.snapshot() == {}  # Reads do not create entries.
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
        ledger.remove("missing")  # Idempotent removal without creating entries.
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
    """Persist concurrent credential updates, removals and snapshot reads safely."""
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
    """Exercise same-host check-in path fallback with mocked responses."""
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
    assert calls == ["https://www.codebuddy.cn" + path for path in credits.CHECKIN_PATHS]  # Same-host fallback only
    print("✅ test_daily_checkin_http")


def test_fetch_credits_http():
    """Aggregate mocked credit slices and their earliest expiry."""
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
    """Prefer expiring credits, rotate equal candidates and place unknown balances last."""
    import converter
    with tempfile.TemporaryDirectory() as td:
        # Synthetic credential files
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

        # Unknown balances share round-robin priority.
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

        # Prefer cred2's earlier expiry.
        for _ in range(3):
            assert pool.pick(None).path.name == "cred2.info"

        # After clearing cred2, prefer cred0 over unknown balances.
        ledger.update_credits(ids[2], {"credits": 0, "count": 0, "segments": [], "soonest_expiry": None})
        for _ in range(2):
            assert pool.pick(None).path.name == "cred0.info"
    print("✅ test_pick_expiry_priority")


def test_fetch_model_catalog():
    """Validate CLI catalog headers and model parsing with mocked config responses."""
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
    assert seen_headers.get("x-client-platform") == "cli"  # Required by the catalog endpoint.
    assert httpx.Headers(seen_headers)["user-agent"] == "CLI/9.9"
    print("✅ test_fetch_model_catalog")


def test_current_models_merge():
    """Preserve explicit domestic catalogs without static expansion or empty-list fallback."""
    import converter
    with patch.dict(converter.CONFIG, {"cred_pool": None, "cred": None, "model_catalogs": {},
                                       "account_catalogs": None, "model_cache": None, "ledger": None,
                                       "models_intl": None, "models_remote": [
            {"id": "glm-9.9", "supportsToolCall": True},
            {"id": "hunyuan-image", "supportsToolCall": False},
        ]}):
        out = converter.current_models(region="cn")
        assert out == ["glm-9.9", "auto"]  # Exclude image models and retain the scheduling alias.
        assert not [m for m in out if m.endswith("-free")]
        assert "deepseek-v4.1-flash" in converter.DEFAULT_MODELS
        assert "glm-5.3" not in out  # Do not expand known product capabilities with defaults.
        converter.CONFIG["models_remote"] = []
        assert converter.current_models(region="cn") == []
        converter.CONFIG["models_remote"] = None
        # Legacy display fallback does not authorize production account routing.
        assert converter.current_models(region="cn") == converter.DEFAULT_MODELS
    print("✅ test_current_models_merge")


def test_credits_to_usd():
    """Convert credit prices to USD using the configured exchange rate."""
    assert abs(credits.credits_to_usd(1000) - 1000 * 0.014 / 7.15) < 1e-9
    assert credits.credits_to_usd(0) == 0.0
    assert abs(credits.credits_to_usd(50000, 0.014, 7.0) - 100.0) < 1e-9  # Exchange-rate override
    assert credits.CREDIT_PRICE_CNY == 0.014 and credits.USAGE_MAX_DAYS == 30
    print("✅ test_credits_to_usd")


def test_aggregate_credits():
    """Aggregate regional balances, quota usage and expiry while tolerating empty entries."""
    snap = {"a": {"credits": {"intl": False, "segments": [
        {"remaining": 100, "total": 200, "expires_at": 500},
        {"remaining": 50, "total": 50, "expires_at": 900}]}},
        "b": {"credits": {"segments": []}},
        "c": {},
        "ai": {"credits": {"intl": True, "segments": [
               {"remaining": 300, "total": 400, "expires_at": 700}]}},
        "d": {"credits": {"segments": [{"remaining": 30, "total": 10, "expires_at": None}]}}}
    agg = credits.aggregate_credits(snap)
    assert agg["remaining"] == 480, agg          # Domestic 180 plus international 300
    assert agg["used_by_quota"] == 200, agg      # Sum regional usage without negative differences.
    assert agg["soonest_expiry"] == 500, agg
    g = agg["groups"]
    assert g["domestic"]["remaining"] == 180 and g["domestic"]["used_by_quota"] == 100, g
    assert g["international"]["remaining"] == 300, g
    assert g["international"]["soonest_expiry"] == 700, g
    assert credits.aggregate_credits({})["remaining"] == 0.0
    assert credits.aggregate_credits({})["soonest_expiry"] is None
    print("✅ test_aggregate_credits")


def _fake_client(pages, seen=None):
    """Return predefined HTTP responses by page number."""
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
    """Reject HTTP 200 responses with business errors or missing usage structure."""
    token = _jwt("https://www.codebuddy.cn/x")
    for pages in ([{"code": 1059, "msg": "rate limited", "data": {"total": 0, "data": []}}],
                  [{"code": 0, "data": {}}],                      # Missing nested data and total
                  [{"data": {"data": [], "total": 0}}],           # Valid empty structure without code
                  ):
        try:
            result = _with_client(_fake_client(pages),
                                  lambda: credits.fetch_request_usage(token))
        except RuntimeError:
            assert pages[0].get("code") not in (0, None) or "data" not in pages[0].get("data", {}) \
                or "data" not in pages[0]["data"]
        else:
            assert pages[0].get("code") is None and result["requests"] == 0  # Preserve valid empty results.
    print("✅ test_fetch_request_usage_rejects_invalid_success_payloads")


def test_fetch_credits_distinguishes_empty_from_missing_structure():
    """Distinguish confirmed zero balances from missing response structure."""
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
    """Aggregate credit pages, stop on short pages and mark capped results partial."""
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

    # Retry transient empty later pages before treating them as pagination completion.
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
    """Mark usage partial when the page cap is below the advertised total."""
    token = _jwt("https://www.codebuddy.cn/x")
    big_total = credits.USAGE_MAX_PAGES * credits.USAGE_PAGE_SIZE + 1
    row = {"requestTime": "2026-09-01 10:00:00", "model": "m", "credit": 0.01}
    page = {"code": 0, "data": {"total": big_total, "data": [row]}}
    result = _with_client(_fake_client([page]), lambda: credits.fetch_request_usage(token))
    assert result["partial"] is True and result["requests"] == credits.USAGE_MAX_PAGES
    print("✅ test_fetch_request_usage_marks_partial_at_page_cap")


def test_sync_usage_keeps_per_account_snapshots_on_failure():
    """Retain a failed account's prior usage snapshot with explicit stale and partial flags."""
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

            # Expose first-sync failures even when no historical snapshot exists.
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
            assert view["total_credits"] == 30.0 and view["requests"] == 3  # Retain u2's snapshot.
            assert view["partial"] is True and view["stale_accounts"] == ["u2.info"]

            failing.clear()
            snapshots["token-u2"] = {"by_day": {"2026-09-02": {"m": 25.0}},
                                     "total_credits": 25.0, "requests": 4, "partial": False}
            converter._sync_usage(pool)
            view = converter.CONFIG["usage_daily"]
            assert view["total_credits"] == 35.0 and view["partial"] is False  # Clear staleness after recovery.

            paths[1].unlink()
            pool.prune()
            converter._sync_usage(pool)
            view = converter.CONFIG["usage_daily"]
            assert view["total_credits"] == 10.0  # Exclude deleted credentials.

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
    """Aggregate usage pages by day and model within the supported 30-day window."""
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
    assert seen["pages"] == [1, 2], seen       # Stop at the advertised total.
    assert u["requests"] == 3 and abs(u["total_credits"] - 0.75) < 1e-9 and u["partial"] is False
    assert u["by_day"]["2026-09-01"]["glm-5.3"] == 0.75
    assert "hy4-preview" in u["by_day"]["2026-09-02"]  # Count zero-credit requests.
    import time as _t
    span = (_t.mktime(_t.strptime(seen["days"][0][:19], "%Y-%m-%d %H:%M:%S")))
    assert abs((_t.time() - span) - 30 * 86400) < 3600  # Clamp to the supported window.
    print("✅ test_fetch_request_usage_paging")


def test_billing_balance_identity():
    """Preserve the subscription limit minus usage equals balance identity."""
    import converter
    with tempfile.TemporaryDirectory() as td:
        led = credits.CreditLedger(Path(td) / "ledger.json")
        led.update_credits("c1", {"credits": 1000.0, "count": 1, "segments": [
            {"remaining": 1000.0, "total": 1600.0, "expires_at": time.time() + 864000}],
            "soonest_expiry": time.time() + 864000})
        saved_led, saved_usage = converter.CONFIG.get("ledger"), converter.CONFIG.get("usage_daily")
        try:
            converter.CONFIG["ledger"] = led
            # Prefer official usage details over quota differences.
            day_map = {"2026-09-01": {"glm-5.3": 200.0}}
            converter.CONFIG["usage_daily"] = {"by_day": day_map,
                "groups": {"domestic": {"by_day": day_map, "total_credits": 200.0,
                                        "requests": 10}},
                "total_credits": 200.0, "requests": 10, "fetched_at": time.time()}
            t = converter._billing_totals()
            assert t["remaining"] == 1000.0 and t["used"] == 200.0 and t["quota"] == 1200.0
            assert t["used_source"] == "official_usage_detail"
            # Subscription limit minus usage must equal the remaining balance.
            sub = converter.billing_subscription(None, None)
            usage = converter.billing_usage(None, None, None, None)
            assert sub["codebuddy_partial"] is False  # Explicitly report complete data.
            assert abs(sub["hard_limit_usd"] - usage["total_usage"] / 100 - t["remaining_usd"]) < 0.01
            assert sub["codebuddy_credits_remaining"] == 1000.0
            assert sub["plan"]["title"].startswith("CodeBuddy Credits")
            assert usage["object"] == "list"
            assert usage["daily_costs"][0]["line_items"][0]["name"] == "glm-5.3"
            # Quota-difference fallback must preserve the balance identity.
            converter.CONFIG["usage_daily"] = None
            t2 = converter._billing_totals()
            assert t2["used"] == 600.0 and t2["used_source"] == "quota_delta"
            assert abs(t2["quota_usd"] - t2["used_usd"] - t2["remaining_usd"]) < 0.01
            assert converter.billing_usage(None, None, None, None)["daily_costs"] == []  # No fabricated daily details
            # Include only usage within the requested interval.
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
    """Apply each region's credit rate to its own daily usage."""
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
            # Each region's 100-credit usage falls on a different day.
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
            cn_cents = 100 * 0.014 / 7.15 * 100   # Approximately 19.58 cents
            assert abs(days[d1][0]["cost"] - cn_cents) < 0.01, days[d1]
            assert abs(days[d2][0]["cost"] - 300.0) < 0.01, days[d2]  # 100 credits at USD 0.03
            # Daily amounts sum to total monetary usage.
            assert abs(sum(i["cost"] for d in usage["daily_costs"] for i in d["line_items"])
                       - usage["total_usage"]) < 0.02
        finally:
            converter.CONFIG["ledger"], converter.CONFIG["usage_daily"] = saved
    print("✅ test_billing_usage_prices_each_day_by_site")


def test_billing_intl_split():
    """Preserve regional pricing and additive balance identities."""
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
            converter.CONFIG["usage_daily"] = None   # Use quota-difference fallback.
            t = converter._billing_totals()
            assert t["groups"]["domestic"]["credits_remaining"] == 1000.0
            assert t["groups"]["international"]["credits_remaining"] == 500.0
            cn_usd = 1000 * 0.014 / 7.15          # Convert domestic CNY pricing to USD.
            assert abs(t["groups"]["domestic"]["balance_usd"] - cn_usd) < 0.01, t["groups"]
            assert abs(t["groups"]["international"]["balance_usd"] - 15.0) < 0.01  # 500×$0.03
            assert abs(t["remaining_usd"] - (cn_usd + 15.0)) < 0.01
            # Regional amounts remain additive without breaking the balance identity.
            assert abs(t["quota_usd"] - t["used_usd"] - t["remaining_usd"]) < 0.01, t
            # Convert international USD amounts before combining CNY totals.
            assert abs(t["remaining_cny"] - (1000 * 0.014 + 500 * 0.03 * 7.15)) < 0.01, t
            sub = converter.billing_subscription(None, None)
            assert sub["codebuddy_sites"]["international"]["credits_remaining"] == 500.0
            assert "INTL" in sub["plan"]["title"]
        finally:
            converter.CONFIG["ledger"], converter.CONFIG["usage_daily"] = saved
    print("✅ test_billing_intl_split")


def test_current_models_intl_condition():
    """Merge eligible international models while preserving explicit regional isolation."""
    import converter
    with tempfile.TemporaryDirectory() as td, patch.dict(converter.CONFIG, {
            "cred_pool": None, "cred": None, "model_catalogs": {}, "ledger": None,
            "account_catalogs": None, "model_cache": None,
            "models_remote": [{"id": "glm-5.3", "supportsToolCall": True}],
            "models_intl": [{"id": "gpt-5.5", "supportsToolCall": True},
                            {"id": "img-1", "supportsToolCall": False}]}):
        assert converter.current_models("cn") == ["glm-5.3", "auto"]
        assert converter.current_models("intl") == []  # No trusted international balance
        assert set(converter.current_models()) == {"glm-5.3", "auto"}
        led = credits.CreditLedger(Path(td) / "l.json")
        led.update_credits("ai", {"credits": 0.0, "segments": [], "intl": True})
        converter.CONFIG["ledger"] = led
        assert converter.current_models("intl") == []  # Empty international balance
        assert set(converter.current_models()) == {"glm-5.3", "auto"}
        led.update_credits("ai", {"credits": 120.0, "segments": [
            {"remaining": 120.0, "total": 120.0, "expires_at": None}], "intl": True})
        assert converter.current_models("intl") == ["gpt-5.5"]
        assert converter.current_models("cn") == ["glm-5.3", "auto"]
        assert set(converter.current_models()) == {"glm-5.3", "gpt-5.5", "auto"}
        converter.CONFIG["models_intl"] = []
        assert converter.current_models("intl") == []  # Positive balance cannot override an empty catalog.
        assert set(converter.current_models()) == {"glm-5.3", "auto"}
    print("✅ test_current_models_intl_condition")


def test_guard_model():
    """Reject unknown models unless an explicit guard bypass authorizes them."""
    import converter
    from fastapi import HTTPException
    saved = (converter.CONFIG.get("model_guard"), converter.CONFIG.get("models_remote"))
    try:
        converter.CONFIG["model_guard"] = True
        converter.CONFIG["models_remote"] = [{"id": "glm-5.3", "supportsToolCall": True}]
        converter.invalidate_model_table()
        converter.guard_model("glm-5.3")   # Known model
        converter.guard_model("auto")      # Legacy alias in a known nonempty domestic CLI catalog
        with TestCase().assertRaises(HTTPException) as raised:
            converter.guard_model("")  # Empty IDs are not default-model aliases.
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
        converter.guard_model("gpt-9.9")   # Explicitly disabled model guard
    finally:
        converter.CONFIG["model_guard"], converter.CONFIG["models_remote"] = saved
    print("✅ test_guard_model")


def test_model_catalog_cache():
    """Test catalog TTL, persisted reload and regional cache grouping."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "catalog.json"
        c1 = credits.ModelCatalogCache(p, ttl=3600)
        assert not c1.fresh("domestic") and c1.models("domestic") == []
        assert c1.age("domestic") is None
        c1.put("domestic", [{"id": "glm-5.3", "supportsToolCall": True}])
        assert c1.fresh("domestic") and len(c1.models("domestic")) == 1
        assert not c1.fresh("international")        # Unsynchronized cache group
        c2 = credits.ModelCatalogCache(p, ttl=3600)  # Reload persisted data.
        assert c2.fresh("domestic") and c2.models("domestic")[0]["id"] == "glm-5.3"
        assert c2.age("domestic") >= 0
        c3 = credits.ModelCatalogCache(p, ttl=60)
        c3._data["groups"]["domestic"]["fetched_at"] = time.time() - 120
        assert not c3.fresh("domestic")             # Expired cache requires refresh.
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
