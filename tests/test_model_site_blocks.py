#!/usr/bin/env python3
"""Test backend/model backoff, routing and recovery with synthetic offline credentials."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import converter
from app.model_blocks import ModelBlocks

DOMESTIC_PROFILE = "cn-cli"
INTL_PROFILE = "intl-cli"
DOMAINS = {DOMESTIC_PROFILE: "www.codebuddy.cn", INTL_PROFILE: "www.codebuddy.ai"}
DOMESTIC_ENDPOINT = converter.PROFILE_ENDPOINTS[DOMESTIC_PROFILE]
INTL_ENDPOINT = converter.PROFILE_ENDPOINTS[INTL_PROFILE]
MODEL = "tested-model"
OTHER_MODEL = "other-tested-model"


def _error_body(code, message, request_id="0198f5a6b7c8d9e0f1a2b3c4d5e6f7a8"):
    return json.dumps({"code": code, "msg": message, "requestId": request_id}).encode()


def test_parse_not_servable_reads_code_field():
    """Match unsupported-model codes only in dedicated response fields."""
    message = f"model [{MODEL}] service info not found"
    assert converter._parse_not_servable(_error_body(11102, message), 404) == ("11102", message)
    assert converter._parse_not_servable(_error_body(11102, "no such model"), 400) is not None
    print("✅ test_parse_not_servable_reads_code_field")


def test_parse_not_servable_ignores_other_errors():
    """Exclude quota, authentication and filter errors from model backoff."""
    cases = [
        (_error_body(11001, "quota exceeded"), 429),
        (_error_body(1002, "token expired"), 401),
        (_error_body(0, "ok"), 200),
        (b"<html>500</html>", 500),
        (b"", 404),
        (_error_body("other", "internal error"), 400),
        (json.dumps({"error": {"message": "rate limit reached"}}).encode(), 429),
        (_error_body("other", "boom", request_id="req-11102"), 404),   # Incidental request ID
        (_error_body(11102, "service info not found"), 429),           # Only 400/404 qualifies.
    ]
    for raw, status in cases:
        assert converter._parse_not_servable(raw, status) is None, (raw, status)
    print("✅ test_parse_not_servable_ignores_other_errors")


def test_parse_not_servable_reads_wrapped_error_object():
    """Recognize unsupported-model errors inside OpenAI error envelopes."""
    raw = json.dumps({"error": {"code": "11102", "message": "model service info not found"}}).encode()
    assert converter._parse_not_servable(raw, 404) is not None
    assert converter._parse_not_servable(b'{"requestId": "11102"}', 404) is None   # IDs are not error codes.
    print("✅ test_parse_not_servable_reads_wrapped_error_object")


class ModelBlocksTests(unittest.TestCase):
    """Test backoff expiry, exponential delays, persistence and immediate clearing."""

    def test_expiry_is_half_open_not_blacklist(self):
        blocks = ModelBlocks(ttl_s=60, max_ttl_s=600)
        now = time.time()
        blocks.note("https://a", "m", code="11102", now=now)
        self.assertTrue(blocks.blocked("https://a", "m", now=now))
        self.assertEqual(blocks.until("https://a", "m", now=now + 59), blocks.until("https://a", "m", now=now))
        self.assertFalse(blocks.blocked("https://a", "m", now=now + 61))   # Allow probes after expiry.
        self.assertEqual(blocks.until("https://a", "m", now=now + 61), 0.0)

    def test_repeated_hits_back_off(self):
        blocks = ModelBlocks(ttl_s=60, max_ttl_s=600)
        now = time.time()
        for hits, expected in ((1, 60), (2, 120), (3, 240), (4, 480), (5, 600), (6, 600)):
            row = blocks.note("https://a", "m", code="11102", now=now)
            self.assertEqual(row["hits"], hits)
            self.assertAlmostEqual(row["until"] - now, expected, delta=1)

    def test_clear_on_success(self):
        blocks = ModelBlocks(ttl_s=3600)
        now = time.time()
        blocks.note("https://a", "m", now=now)
        self.assertTrue(blocks.clear("https://a", "m", now=now + 1))
        self.assertFalse(blocks.blocked("https://a", "m", now=now + 1))
        self.assertFalse(blocks.clear("https://a", "m", now=now + 1))   # Idempotent clearing

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model-site-blocks.json"
            blocks = ModelBlocks(path, ttl_s=3600)
            blocks.note("https://a", "m", code="11102", msg="service info not found")
            restored = ModelBlocks(path, ttl_s=3600)
            self.assertTrue(restored.blocked("https://a", "m"))
            self.assertEqual(restored.detail()[0]["hits"], 1)
            self.assertEqual(restored.detail()[0]["code"], "11102")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unreadable_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model-site-blocks.json"
            path.write_text("{ not json", encoding="utf-8")
            blocks = ModelBlocks(path)
            self.assertEqual(blocks.view(), {})
            blocks.note("https://a", "m")
            self.assertTrue(blocks.blocked("https://a", "m"))

    def test_isolated_per_endpoint_and_model(self):
        blocks = ModelBlocks(ttl_s=3600)
        blocks.note("https://a", "m1")
        self.assertTrue(blocks.blocked("https://a", "m1"))
        self.assertFalse(blocks.blocked("https://a", "m2"))
        self.assertFalse(blocks.blocked("https://b", "m1"))


class PoolRoutingTests(unittest.TestCase):
    """Route around unavailable models and clear backoff after confirmed availability."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": str(self.root)}))
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "ledger": None,
            "model_catalogs": {}, "account_catalogs": None, "model_cache": None,
            "model_guard": True, "models_remote": None, "models_intl": None,
            "max_images": 16, "image_policy": "truncate", "log_path": None,
            "max_request_bytes": 32 * 1024 * 1024, "log_body_limit": 65536,
            "desensitize": False, "no_compact": False, "admin_csrf": True,
        }))
        for profile in (DOMESTIC_PROFILE, INTL_PROFILE):
            uid = "synthetic-" + profile
            value = {"account": {"uid": uid, "enterpriseId": "synthetic-enterprise"},
                     "auth": {"domain": DOMAINS[profile], "accessToken": "synthetic-access",
                              "refreshToken": "synthetic-refresh",
                              "expiresAt": (time.time() + 86400) * 1000,
                              "lastRefreshTime": time.time() * 1000}}
            (self.root / (uid + ".info")).write_text(json.dumps(value), encoding="utf-8")
        def entry(identifier):
            return {"id": identifier, "name": identifier, "supportsToolCall": True,
                    "credits": {"input": 1, "output": 2}}
        converter.CONFIG["model_catalogs"] = {profile: [entry(MODEL), entry(OTHER_MODEL)]
                                              for profile in DOMAINS}
        self.pool = converter.CredentialPool(
            [self.root / ("synthetic-" + profile + ".info") for profile in DOMAINS],
            blocks_path=self.root / "model-site-blocks.json")
        converter.CONFIG["cred_pool"] = self.pool
        self.by_endpoint = {}
        for entry in self.pool.entries():
            self.by_endpoint[self.pool._entry_endpoint(entry)] = (entry["cm"], entry["id"])
        self.assertEqual(set(self.by_endpoint), {DOMESTIC_ENDPOINT, INTL_ENDPOINT})
        self.addCleanup(converter.invalidate_model_table)

    def cred_for(self, model=MODEL, region=None):
        return converter._cred_for({"model": model, "messages": [{"role": "user", "content": "hi"}]},
                                   model, region=region)

    def test_missing_model_is_not_picked_again(self):
        """Route to an eligible domestic backend after an international unsupported-model response."""
        cm, _ = self.by_endpoint[INTL_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        self.assertTrue(self.pool._blocks.blocked(INTL_ENDPOINT, MODEL))
        self.assertIsNone(self.pool.model_block_until(MODEL))            # Another backend remains eligible.
        (picked_cm, _generation), _headers = self.cred_for()
        self.assertIs(picked_cm, self.by_endpoint[DOMESTIC_ENDPOINT][0])
        self.assertFalse(self.pool._blocks.blocked(DOMESTIC_ENDPOINT, MODEL))

    def test_error_code_field_decodes_to_block(self):
        """Separate unsupported-model responses from quota cooldowns."""
        cm, cid = self.by_endpoint[DOMESTIC_ENDPOINT]
        self.pool.note_status(cm, 429, model=MODEL, raw=_error_body(4290, "quota"))
        self.assertFalse(self.pool._blocks.blocked(DOMESTIC_ENDPOINT, MODEL))
        self.assertGreater(self.pool._model_fail[(cid, MODEL)], time.time())
        cm, _ = self.by_endpoint[INTL_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        self.assertTrue(self.pool._blocks.blocked(INTL_ENDPOINT, MODEL))

    def test_fast_failure_when_no_backend_serves_it(self):
        """Return HTTP 404 when every backend lacks the requested model."""
        for endpoint in (DOMESTIC_ENDPOINT, INTL_ENDPOINT):
            cm, _ = self.by_endpoint[endpoint]
            self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        until = self.pool.model_block_until(MODEL)
        self.assertIsNotNone(until)
        with self.assertRaises(HTTPException) as caught:
            self.cred_for()
        self.assertEqual(caught.exception.status_code, 404)
        detail = caught.exception.detail["error"]
        self.assertIn(MODEL, detail["message"])
        self.assertEqual(detail["type"], "invalid_request_error")
        # Backend/model backoff does not affect other models on the same backend.
        (picked_cm, _), _headers = self.cred_for(model=OTHER_MODEL)
        self.assertIn(picked_cm, [cm for cm, _ in self.by_endpoint.values()])

    def test_block_is_reported_when_only_one_backend_lists_the_model(self):
        """Return HTTP 404 when the only capable backend is blocked."""
        converter.CONFIG["model_catalogs"][INTL_PROFILE] = [
            {"id": OTHER_MODEL, "name": OTHER_MODEL, "supportsToolCall": True,
             "credits": {"input": 1, "output": 2}}]
        converter.invalidate_model_table()
        cm, _ = self.by_endpoint[DOMESTIC_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        self.assertIsNotNone(self.pool.model_block_until(MODEL), "唯一能服务它的后端已避让")
        with self.assertRaises(HTTPException) as caught:
            self.cred_for()
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn(MODEL, caught.exception.detail["error"]["message"])
        # Other international models remain eligible.
        (picked_cm, _), _headers = self.cred_for(model=OTHER_MODEL)
        self.assertIn(picked_cm, [cm for cm, _ in self.by_endpoint.values()])

    def add_account(self, domain, uid):
        path = self.root / (uid + ".info")
        value = {"account": {"uid": uid, "enterpriseId": "synthetic-enterprise"},
                 "auth": {"domain": domain, "accessToken": "synthetic-access",
                          "refreshToken": "synthetic-refresh",
                          "expiresAt": (time.time() + 86400) * 1000,
                          "lastRefreshTime": time.time() * 1000}}
        path.write_text(json.dumps(value), encoding="utf-8")
        self.pool.reload([Path(e["id"]) for e in self.pool.entries()] + [path])
        return next(e for e in self.pool.entries() if e["uid"] == uid)

    def test_same_region_product_without_root_model_does_not_cancel_block(self):
        self.add_account("www.workbuddy.cn", "synthetic-cn-work")
        other = [{"id": OTHER_MODEL, "supportsToolCall": True}]
        root = [{"id": MODEL, "supportsToolCall": True}]
        converter.CONFIG["account_catalogs"] = {
            e["account_key"]: {"profile": e["profile"], "models": other,
                               "serves": root if e["profile"] == DOMESTIC_PROFILE else other}
            for e in self.pool.entries()}
        converter.invalidate_model_table()
        self.assertEqual(converter._model_profiles(MODEL, "cn"), {DOMESTIC_PROFILE})
        cm, _ = self.by_endpoint[DOMESTIC_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        with self.assertRaises(HTTPException) as caught:
            self.cred_for()
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail["error"]["code"], "model_not_found")


    def test_unknown_account_catalog_keeps_retryable_readiness(self):
        model = {"id": MODEL, "supportsToolCall": True}
        accounts = {e["account_key"]: {"profile": e["profile"],
                     "models": [model] if e["profile"] == DOMESTIC_PROFILE else None}
                    for e in self.pool.entries()}
        converter.CONFIG["account_catalogs"] = accounts
        converter.invalidate_model_table()
        cm, _ = self.by_endpoint[DOMESTIC_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        self.assertEqual(self.pool._candidates(MODEL), [], "未知目录不能获得派发资格")
        with self.assertRaises(HTTPException) as caught:
            self.cred_for()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.headers["Retry-After"], "3")
        other = next(a for a in accounts.values() if a["profile"] == INTL_PROFILE)
        other["models"] = []
        converter.invalidate_model_table()
        with self.assertRaises(HTTPException) as caught:
            self.cred_for()
        self.assertEqual(caught.exception.status_code, 404, "已知空目录与尚未就绪不同")
        other["models"] = [model]
        converter.invalidate_model_table()
        self.assertIsNone(self.pool.model_block_until(MODEL))

    def test_unknown_account_is_not_hidden_by_same_profile_empty_catalog(self):
        pending = self.add_account("www.codebuddy.ai", "synthetic-intl-pending")
        converter.CONFIG["account_catalogs"] = {
            e["account_key"]: {"profile": e["profile"], "models":
                ([{"id": MODEL, "supportsToolCall": True}] if e["profile"] == DOMESTIC_PROFILE
                 else None if e["account_key"] == pending["account_key"] else [])}
            for e in self.pool.entries()}
        converter.invalidate_model_table()
        self.assertEqual(converter._catalog_for(INTL_PROFILE, "serves"), [])
        cm, _ = self.by_endpoint[DOMESTIC_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        with self.assertRaises(HTTPException) as caught:
            self.cred_for()
        self.assertEqual(caught.exception.status_code, 503)

    def test_unknown_legacy_profile_still_prevents_premature_model_rejection(self):
        converter.CONFIG["model_catalogs"][INTL_PROFILE] = None
        converter.invalidate_model_table()
        cm, _ = self.by_endpoint[DOMESTIC_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        with self.assertRaises(HTTPException) as caught:
            self.cred_for()
        self.assertEqual(caught.exception.status_code, 503)

    def test_single_profile_passthrough_still_reports_measured_rejection(self):
        _, identifier = self.by_endpoint[DOMESTIC_ENDPOINT]
        _, other_identifier = self.by_endpoint[INTL_ENDPOINT]
        Path(other_identifier).unlink()
        self.pool.prune()
        self.pool.reload([Path(identifier)])
        converter.CONFIG["model_catalogs"] = {DOMESTIC_PROFILE: [
            {"id": OTHER_MODEL, "supportsToolCall": True}]}
        converter.CONFIG["model_guard"] = False
        converter.invalidate_model_table()
        cm, _ = self.by_endpoint[DOMESTIC_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        with self.assertRaises(HTTPException) as caught:
            self.cred_for()
        self.assertEqual(caught.exception.status_code, 404)


    def test_region_scoped_fast_failure(self):
        """Keep international model backoff isolated from domestic requests."""
        cm, _ = self.by_endpoint[INTL_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        self.assertIsNotNone(self.pool.model_block_until(MODEL, region="intl"))
        with self.assertRaises(HTTPException) as caught:
            self.cred_for(region="intl")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIsNone(self.pool.model_block_until(MODEL, region="cn"))
        self.assertIsNotNone(self.cred_for(region="cn"))

    def test_success_clears_the_block(self):
        """Clear model backoff immediately after a successful response."""
        cm, _ = self.by_endpoint[INTL_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        self.assertTrue(self.pool._blocks.blocked(INTL_ENDPOINT, MODEL))
        self.assertTrue(self.pool.note_model_ok(cm, MODEL))
        self.assertFalse(self.pool._blocks.blocked(INTL_ENDPOINT, MODEL))
        self.assertFalse(self.pool.note_model_ok(cm, MODEL))

    def test_blocks_survive_restart(self):
        """Restore persisted model backoff across restarts."""
        cm, _ = self.by_endpoint[INTL_ENDPOINT]
        self.pool.note_status(cm, 404, model=MODEL, raw=_error_body(11102, "service info not found"))
        reopened = converter.CredentialPool(
            [self.root / ("synthetic-" + profile + ".info") for profile in DOMAINS],
            blocks_path=self.root / "model-site-blocks.json")
        self.assertTrue(reopened._blocks.blocked(INTL_ENDPOINT, MODEL))
        self.assertEqual([row["endpoint"] for row in reopened.model_blocks_detail()], [INTL_ENDPOINT])

    def test_alias_auto_is_blocked_under_client_name(self):
        """Track the public auto model despite its international upstream alias."""
        cm, _ = self.by_endpoint[INTL_ENDPOINT]
        self.pool.note_status(cm, 404, model="default-model",
                              raw=_error_body(11102, "service info not found"))
        self.assertTrue(self.pool._blocks.blocked(INTL_ENDPOINT, "auto"))
        self.assertFalse(self.pool._blocks.blocked(INTL_ENDPOINT, "default-model"))


if __name__ == "__main__":
    # Direct CI execution runs module-level checks before unittest without requiring pytest.
    for fn in (test_parse_not_servable_reads_code_field,
               test_parse_not_servable_ignores_other_errors,
               test_parse_not_servable_reads_wrapped_error_object):
        fn()
    unittest.main(verbosity=2)
