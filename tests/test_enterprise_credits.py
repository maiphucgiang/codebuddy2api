#!/usr/bin/env python3
"""Test enterprise tenant identification and the enterprise billing dispatch.

Enterprise credentials do not appear in the personal package list: the personal
resource endpoint answers with an empty Accounts structure for them. These tests
pin the tenant detection, the payload parsing (including the unlimited marker) and
the guarantee that a personal account keeps using the personal endpoint.
"""

import base64
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

from app import credits
import converter
from app.client_profiles import (
    enterprise_id_of, is_enterprise_account,
)
from app.credits import (
    UNLIMITED_ALLOWANCE,
    enterprise_id_from_accounts,
    fetch_credits,
    fetch_enterprise_credits,
    parse_enterprise_usage,
)


def _jwt(iss: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"iss": iss}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


CN_TOKEN = _jwt("https://www.codebuddy.cn/auth/realms/copilot")

# Captured from a live enterprise credential (values are structural, not account data).
ENTERPRISE_PAYLOAD = {
    "code": 0,
    "msg": "OK",
    "data": {"credit": 4.07, "cycleStartTime": "2026-09-21 00:00:00",
             "cycleEndTime": "2026-10-21 23:59:59", "limitNum": 2000,
             "cycleResetTime": "2026-10-22 00:00:00"},
}


# Capture the real client before any patching so building the fake cannot recurse.
_REAL_HTTPX_CLIENT = httpx.Client


class _FakeClient:
    """Stand-in for httpx.Client so patching the module attribute cannot recurse."""

    def __init__(self, handler):
        self._client = _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))

    def __enter__(self):
        self._client.__enter__()
        return self._client

    def __exit__(self, *exc):
        return self._client.__exit__(*exc)


def _patch_client(handler):
    """Patch the client the module under test opens, with one fresh client per call."""
    return patch.object(credits.httpx, "Client", lambda *a, **k: _FakeClient(handler))


class EnterpriseIdentityTests(TestCase):
    """Tenant detection must accept both naming conventions and the type fallback."""

    def test_reads_camel_and_snake_case_ids(self):
        self.assertEqual(enterprise_id_of({"enterpriseId": "tenant-a"}), "tenant-a")
        self.assertEqual(enterprise_id_of({"enterprise_id": "tenant-b"}), "tenant-b")
        # A populated candidate must not be shadowed by an empty alias.
        self.assertEqual(enterprise_id_of({"enterpriseId": "", "enterprise_id": "tenant-c"}), "tenant-c")

    def test_ignores_blank_and_non_string_ids(self):
        for account in ({}, {"enterpriseId": ""}, {"enterpriseId": "   "},
                        {"enterpriseId": None}, {"enterpriseId": 123},
                        {"enterprise_id": ["x"]}, None, "not-a-dict"):
            with self.subTest(account=account):
                self.assertIsNone(enterprise_id_of(account))

    def test_identity_is_camel_snake_equivalent(self):
        """Both conventions must hash to one key so routing never splits an account."""
        from app.client_profiles import account_key
        camel = account_key("cn-cli", "uid-1", enterprise_id_of({"enterpriseId": "t"}) or "")
        snake = account_key("cn-cli", "uid-1", enterprise_id_of({"enterprise_id": "t"}) or "")
        self.assertEqual(camel, snake)

    def test_enterprise_id_present_is_a_direct_hit(self):
        self.assertTrue(is_enterprise_account({"enterpriseId": "t", "type": "personal"}))
        self.assertTrue(is_enterprise_account({"enterprise_id": "t"}))

    def test_type_fallback_matches_only_enterprise_tiers(self):
        for account_type in ("ultimate", "exclusive", "ULTIMATE", " Exclusive "):
            with self.subTest(account_type=account_type):
                self.assertTrue(is_enterprise_account({"type": account_type}))
        for account_type in ("personal", "", "free", "pro", None, 123):
            with self.subTest(account_type=account_type):
                self.assertFalse(is_enterprise_account({"type": account_type}))

    def test_personal_account_is_not_enterprise(self):
        self.assertFalse(is_enterprise_account({"type": "personal", "uid": "u"}))


class EnterprisePayloadTests(TestCase):
    """Parsing must tolerate every observed wrapping and both key conventions."""

    def test_parses_live_shape(self):
        usage = parse_enterprise_usage(ENTERPRISE_PAYLOAD)
        self.assertEqual(usage["limit"], 2000.0)
        self.assertEqual(usage["used"], 4.07)
        self.assertIsNotNone(usage["reset_at"])

    def test_parses_all_wrappings(self):
        inner = {"limitNum": 100, "credit": 25}
        for payload in ({"data": {"data": dict(inner)}}, {"data": dict(inner)}, dict(inner)):
            with self.subTest(payload=payload):
                usage = parse_enterprise_usage(payload)
                self.assertIsNotNone(usage)
                self.assertEqual(usage["limit"], 100.0)
                self.assertEqual(usage["used"], 25.0)

    def test_parses_snake_case_keys(self):
        usage = parse_enterprise_usage({"data": {"limit_num": 500, "used_num": 125}})
        self.assertEqual(usage["limit"], 500.0)
        self.assertEqual(usage["used"], 125.0)

    def test_missing_limit_is_not_parsed(self):
        """A missing limit must fail loudly rather than report a zero balance."""
        for payload in ({}, {"data": {}}, {"data": {"credit": 5}}, None, "text"):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_enterprise_usage(payload))

    def test_negative_used_is_clamped(self):
        usage = parse_enterprise_usage({"data": {"limitNum": 10, "credit": -5}})
        self.assertEqual(usage["used"], 0.0)

    def test_missing_used_defaults_to_zero(self):
        usage = parse_enterprise_usage({"data": {"limitNum": 10}})
        self.assertEqual(usage["used"], 0.0)


class EnterpriseFetchTests(TestCase):
    """The enterprise endpoint replaces the personal package list for tenants."""

    def _capture(self, payload, status=200):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            return httpx.Response(status, json=payload)

        return seen, handler

    def test_remaining_is_limit_minus_used(self):
        seen, handler = self._capture(ENTERPRISE_PAYLOAD)
        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                result = fetch_enterprise_credits(CN_TOKEN, uid="uid-1",
                                                  domain="www.codebuddy.cn",
                                                  enterprise_id="tenant-a")
        self.assertTrue(result["enterprise"])
        self.assertFalse(result["unlimited"])
        self.assertEqual(result["credits"], 1995.93)
        self.assertEqual(result["segments"][0]["total"], 2000.0)
        self.assertEqual(result["segments"][0]["remaining"], 1995.93)
        self.assertEqual(result["segments"][0]["source"], "企业配额")
        # The tenant id must travel on the request; the endpoint rejects otherwise.
        self.assertEqual(seen["headers"].get("x-enterprise-id"), "tenant-a")
        self.assertEqual(seen["headers"].get("x-tenant-id"), "tenant-a")
        self.assertIn(credits.ENTERPRISE_USAGE_PATH, seen["url"])

    def test_unlimited_is_flagged_without_a_number(self):
        """limitNum == -1 means unlimited: never report a fabricated balance."""
        payload = {"code": 0, "data": {"limitNum": UNLIMITED_ALLOWANCE, "credit": 12.0}}
        _seen, handler = self._capture(payload)
        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                result = fetch_enterprise_credits(CN_TOKEN, uid="u", domain="www.codebuddy.cn",
                                                  enterprise_id="t")
        self.assertTrue(result["unlimited"])
        self.assertIsNone(result["credits"])
        self.assertEqual(result["segments"], [])

    def test_exhausted_allowance_yields_no_segments(self):
        payload = {"code": 0, "data": {"limitNum": 100, "credit": 100}}
        _seen, handler = self._capture(payload)
        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                result = fetch_enterprise_credits(CN_TOKEN, uid="u", domain="www.codebuddy.cn",
                                                  enterprise_id="t")
        self.assertEqual(result["credits"], 0.0)
        self.assertEqual(result["segments"], [])

    def test_api_error_raises_readable_failure(self):
        payload = {"code": 40001, "msg": "tenant not found"}
        _seen, handler = self._capture(payload)
        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                with self.assertRaises(RuntimeError) as caught:
                    fetch_enterprise_credits(CN_TOKEN, uid="u", domain="www.codebuddy.cn",
                                             enterprise_id="t")
        self.assertIn("企业配额", str(caught.exception))

    def test_unparsable_payload_raises_instead_of_reporting_zero(self):
        _seen, handler = self._capture({"code": 0, "data": {"unexpected": True}})
        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                with self.assertRaises(RuntimeError):
                    fetch_enterprise_credits(CN_TOKEN, uid="u", domain="www.codebuddy.cn",
                                             enterprise_id="t")


class BillingDispatchTests(TestCase):
    """Dispatch: a tenant id selects the enterprise endpoint, everything else does not."""

    def test_enterprise_id_selects_enterprise_endpoint(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json=ENTERPRISE_PAYLOAD)

        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                fetch_credits(CN_TOKEN, uid="u", domain="www.codebuddy.cn", enterprise_id="tenant-a")

        self.assertIn(credits.ENTERPRISE_USAGE_PATH, seen["url"])
        self.assertNotIn(credits.RESOURCE_PATH, seen["url"])

    def test_personal_account_keeps_using_the_personal_endpoint(self):
        """The regression guard: no tenant id must not change existing behaviour."""
        seen = {"urls": []}

        def handler(request):
            seen["urls"].append(str(request.url))
            # The personal list answers with an empty Accounts array.
            return httpx.Response(200, json={"code": 0, "data": {"Response": {"Data": {"Accounts": []}}}})

        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                result = fetch_credits(CN_TOKEN, uid="u", domain="www.codebuddy.cn")

        self.assertTrue(seen["urls"], "the personal endpoint must be called")
        for url in seen["urls"]:
            self.assertIn(credits.RESOURCE_PATH, url)
            self.assertNotIn(credits.ENTERPRISE_USAGE_PATH, url)
        self.assertFalse(result.get("enterprise"))

    def test_empty_enterprise_id_falls_through_to_personal(self):
        seen = {"urls": []}

        def handler(request):
            seen["urls"].append(str(request.url))
            return httpx.Response(200, json={"code": 0, "data": {"Response": {"Data": {"Accounts": []}}}})

        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                fetch_credits(CN_TOKEN, uid="u", domain="www.codebuddy.cn", enterprise_id="")

        for url in seen["urls"]:
            self.assertNotIn(credits.ENTERPRISE_USAGE_PATH, url)


class EnterpriseIdResolutionTests(TestCase):
    """Historical credentials may carry a type but no tenant id."""

    def test_extracts_first_tenant_from_console_accounts(self):
        payload = {"code": 0, "data": {"accounts": [{"enterpriseId": "t-1"}, {"enterpriseId": "t-2"}]}}
        self.assertEqual(enterprise_id_from_accounts(payload), "t-1")

    def test_accepts_capitalised_accounts_key(self):
        payload = {"code": 0, "data": {"Accounts": [{"enterprise_id": "t-9"}]}}
        self.assertEqual(enterprise_id_from_accounts(payload), "t-9")

    def test_rejects_failed_or_shapeless_responses(self):
        for payload in ({"code": 500, "data": {"accounts": [{"enterpriseId": "t"}]}},
                        {"code": 0, "data": {}},
                        {"code": 0, "data": {"accounts": []}},
                        {"code": 0, "data": {"accounts": ["nope"]}},
                        {}, None, "text"):
            with self.subTest(payload=payload):
                self.assertIsNone(enterprise_id_from_accounts(payload))

    def test_resolution_returns_id_and_swallows_transport_failure(self):
        payload = {"code": 0, "data": {"accounts": [{"enterpriseId": "t-resolved"}]}}

        def handler(request):
            return httpx.Response(200, json=payload)

        with httpx.Client(transport=httpx.MockTransport(handler)):
            with _patch_client(handler):
                resolved = credits.resolve_enterprise_id(CN_TOKEN, uid="u", domain="www.codebuddy.cn")
        self.assertEqual(resolved, "t-resolved")

        def failing(_request):
            raise httpx.ConnectError("offline")

        with httpx.Client(transport=httpx.MockTransport(failing)):
            with _patch_client(failing):
                self.assertIsNone(credits.resolve_enterprise_id(CN_TOKEN, uid="u",
                                                                domain="www.codebuddy.cn"))


class UnlimitedLedgerTests(TestCase):
    """The unlimited marker only survives storage when it came from the enterprise dispatch."""

    def _snapshot(self, td, cred_id, result):
        ledger = credits.CreditLedger(Path(td) / "ledger.json")
        ledger.update_credits(cred_id, result)
        return ledger.snapshot()[cred_id]["credits"]

    def test_unlimited_enterprise_flags_are_stored(self):
        with tempfile.TemporaryDirectory() as td:
            stored = self._snapshot(td, "ent", {"credits": None, "count": 0, "segments": [],
                                                "soonest_expiry": None, "intl": False,
                                                "unlimited": True, "enterprise": True})
        self.assertIsNone(stored["credits"])
        self.assertTrue(stored["enterprise"])
        self.assertTrue(stored["unlimited"])

    def test_personal_result_can_never_store_unlimited(self):
        # Even a malformed personal result carrying the marker cannot pass the gate.
        with tempfile.TemporaryDirectory() as td:
            stored = self._snapshot(td, "p1", {"credits": 5.0, "count": 1, "segments": [],
                                               "soonest_expiry": None, "intl": False,
                                               "unlimited": True})
            plain = self._snapshot(td, "p2", {"credits": 5.0, "count": 1, "segments": [],
                                              "soonest_expiry": None, "intl": False})
        self.assertFalse(stored["unlimited"])
        self.assertFalse(stored["enterprise"])
        self.assertFalse(plain["unlimited"])
        self.assertFalse(plain["enterprise"])


class UnlimitedAggregateTests(TestCase):
    """Unlimited enterprise tenants stay out of the monetary conversion."""

    def _entry(self, identity, balance):
        return {"identity": identity, "credits": balance}

    def test_unlimited_enterprise_is_counted_not_converted(self):
        snap = {
            "ent": self._entry("i-ent", {"credits": None, "segments": [], "intl": False,
                                         "partial": False, "unlimited": True, "enterprise": True}),
            "p1": self._entry("i-p1", {"credits": 10.0, "intl": False, "partial": False,
                                       "segments": [{"remaining": 10.0, "total": 20.0,
                                                     "expires_at": None}]}),
        }
        agg = credits.aggregate_credits(snap)
        self.assertEqual(agg["unlimited_accounts"], 1)
        self.assertEqual(agg["remaining"], 10.0)
        self.assertEqual(agg["groups"]["domestic"]["remaining"], 10.0)

    def test_marker_without_enterprise_still_counts_numerically(self):
        # The double gate: unlimited without enterprise is not honored anywhere.
        snap = {"x": self._entry("i-x", {"credits": 3.0, "intl": False, "partial": False,
                                         "unlimited": True, "enterprise": False,
                                         "segments": [{"remaining": 3.0, "total": 3.0,
                                                       "expires_at": None}]})}
        agg = credits.aggregate_credits(snap)
        self.assertEqual(agg["unlimited_accounts"], 0)
        self.assertEqual(agg["remaining"], 3.0)


class UnlimitedRoutingTests(TestCase):
    """An unlimited enterprise tenant is routable; zero-balance restriction still applies otherwise."""

    def _pool(self, td):
        path = Path(td) / "cred.info"
        path.write_text(json.dumps({
            "auth": {"accessToken": "t", "expiresAt": int(time.time() * 1000) + 86400000},
            "account": {"uid": "u1"}}), encoding="utf-8")
        pool = converter.CredentialPool([path])
        ledger = credits.CreditLedger(Path(td) / "ledger.json")
        pool.set_ledger(ledger)
        entry = pool.entries()[0]
        return pool, ledger, entry, entry["profile"]

    def test_unlimited_enterprise_is_never_zero_balance(self):
        with tempfile.TemporaryDirectory() as td:
            pool, ledger, entry, profile = self._pool(td)
            ledger.update_credits(entry["id"], {"credits": None, "count": 0, "segments": [],
                                                "soonest_expiry": None, "intl": False,
                                                "unlimited": True, "enterprise": True})
            self.assertFalse(pool._zero_balance(entry, profile))
            self.assertTrue(pool._has_credit(entry, profile))

    def test_zero_balance_personal_account_stays_restricted(self):
        with tempfile.TemporaryDirectory() as td:
            pool, ledger, entry, profile = self._pool(td)
            ledger.update_credits(entry["id"], {"credits": 0, "count": 0, "segments": [],
                                                "soonest_expiry": None, "intl": False})
            self.assertTrue(pool._zero_balance(entry, profile))
            self.assertFalse(pool._has_credit(entry, profile))

    def test_smuggled_unlimited_marker_keeps_zero_balance_rules(self):
        # The ledger gate drops unlimited without enterprise; a number-less personal
        # balance is still zero, so the restriction cannot be bypassed.
        with tempfile.TemporaryDirectory() as td:
            pool, ledger, entry, profile = self._pool(td)
            ledger.update_credits(entry["id"], {"credits": None, "count": 0, "segments": [],
                                                "soonest_expiry": None, "intl": False,
                                                "unlimited": True})
            self.assertTrue(pool._zero_balance(entry, profile))
            self.assertFalse(pool._has_credit(entry, profile))


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
