#!/usr/bin/env python3
"""Test measured billing for nominally free models, manual rates and output-limit probes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import gzip
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

import converter
from app import model_rates, output_probes, rate_measurement
from app.audit_store import AuditStore
from app.control_store import ControlStore, validate_model
from app.credits import CreditLedger, ModelCatalogCache
from app.rate_measurement import RateBook, rate_label, summarize


def sample(credit, *, at=0.0, profile="intl-work", model="promo-f", tokens=(20000, 0, 100), rate=None):
    row = {"started_at": at, "profile": profile, "model": model, "credit": credit,
           "input_tokens": tokens[0], "cache_read_tokens": tokens[1], "output_tokens": tokens[2]}
    if rate is not None:
        row["rate"] = rate   # Only calibrated probe samples carry a rate.
    return row


class SummarizeTests(unittest.TestCase):
    def test_traffic_proves_billing_but_never_sets_a_rate(self):
        row = summarize([sample(0, at=1), sample(0.2, at=2), sample(0.2, at=3)])[("intl-work", "promo-f")]
        self.assertTrue(row["billed"])
        self.assertEqual(row["charged"], 2)
        self.assertIsNone(row["rate"])
        self.assertEqual(row["avg_credit"], 0.2)

    def test_newest_calibrated_probe_sets_the_rate(self):
        rows = [sample(0.59, at=1, rate=0.9), sample(0.4, at=2), sample(0.59, at=3, rate=0.95)]
        row = summarize(rows)[("intl-work", "promo-f")]
        self.assertEqual((row["rate"], row["rated_at"]), (0.95, 3))

    def test_free_model_stays_unbilled(self):
        row = summarize([sample(0, at=index) for index in range(5)])[("intl-work", "promo-f")]
        self.assertFalse(row["billed"])
        self.assertIsNone(row["rate"])

    def test_old_charge_ages_out_after_enough_free_requests(self):
        rows = [sample(0.3, at=0)] + [sample(0, at=index + 1) for index in range(rate_measurement.RECENT)]
        self.assertFalse(summarize(rows)[("intl-work", "promo-f")]["billed"])



class CalibrationTests(unittest.TestCase):
    def test_rate_scales_the_reference_by_per_token_charge(self):
        # Measured 2026-10-19 on intl-work: hy4-preview-f vs glm-5.3-flash (declared x0.06).
        target = {"credit": 0.59, "input_tokens": 8239}
        reference = {"credit": 0.04, "input_tokens": 8821}
        self.assertAlmostEqual(rate_measurement.calibrated_rate(target, reference, 0.06), 0.9476, places=3)

    def test_uncharged_reference_cannot_calibrate(self):
        self.assertIsNone(rate_measurement.calibrated_rate({"credit": 0.5, "input_tokens": 8000},
                                                           {"credit": 0, "input_tokens": 8000}, 0.06))

    def test_references_are_declared_paid_models_nearest_the_reference_rate_first(self):
        items = [{"id": "promo-f", "credits": "x0.00"}, {"id": "cheap", "credits": "x0.01"},
                 {"id": "near", "credits": "x0.08 credits"}, {"id": "pricey", "credits": "x2.0"},
                 {"id": "unknown", "credits": None}]
        self.assertEqual(rate_measurement.reference_candidates(items, converter._multiplier_value, "promo-f"),
                         [("near", 0.08), ("cheap", 0.01), ("pricey", 2.0)])
        self.assertEqual(rate_measurement.reference_candidates(items[:1], converter._multiplier_value, "promo-f"), [])


class RateLabelTests(unittest.TestCase):
    def test_labels_parse_back_through_the_gateway(self):
        self.assertEqual(converter._multiplier_value(rate_label(0.037)), 0.037)
        self.assertEqual(converter._multiplier_value(rate_label(2)), 2.0)
        self.assertTrue(converter._free_multiplier(rate_label(0)))
        self.assertFalse(converter._free_multiplier(rate_label(0.0001)))


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.items = [{"id": "promo-f", "credits": "x0.00"}, {"id": "paid", "credits": "x0.50 credits"}]

    def book(self, samples=(), manual=None):
        book = RateBook(manual=lambda: manual or {})
        book.refresh(list(samples))
        return book

    def test_measured_billing_replaces_a_declared_zero(self):
        book = self.book([sample(0.2, at=1)])
        items = book.overlay("intl-work", self.items, converter._free_multiplier)
        self.assertEqual(items[0]["credits_source"], "measured")
        self.assertEqual(items[0]["credits_declared"], "x0.00")
        self.assertFalse(converter._free_multiplier(items[0]["credits"]))
        self.assertIs(items[1], self.items[1])

    def test_declared_paid_rate_is_never_overridden_by_measurement(self):
        book = self.book([sample(9, at=1, model="paid")])
        items = book.overlay("intl-work", self.items, converter._free_multiplier)
        self.assertEqual(items[1]["credits"], "x0.50 credits")

    def test_unknown_measured_rate_still_reads_as_not_free(self):
        book = self.book([sample(0.01, at=1, tokens=(72, 0, 35))])
        item = book.overlay("intl-work", self.items, converter._free_multiplier)[0]
        self.assertIsNone(item["credits"])
        self.assertFalse(converter._free_multiplier(item["credits"]))

    def test_tiny_measured_rate_never_rounds_back_to_free(self):
        book = self.book([sample(0.05, at=1, rate=0.00001)])
        item = book.overlay("intl-work", self.items, converter._free_multiplier)[0]
        self.assertFalse(converter._free_multiplier(item["credits"]))

    def test_manual_rate_wins_and_is_scoped_to_its_product(self):
        book = self.book([sample(0.2, at=1)], manual={("intl-work", "promo-f"): 0.0})
        item = book.overlay("intl-work", self.items, converter._free_multiplier)[0]
        self.assertEqual(item["credits_source"], "manual")
        self.assertTrue(converter._free_multiplier(item["credits"]))
        self.assertIs(book.overlay("cn-cli", self.items, converter._free_multiplier), self.items)

    def test_refresh_reports_only_routing_relevant_changes(self):
        book = RateBook()
        self.assertTrue(book.refresh([sample(0.2, at=1)]))
        self.assertFalse(book.refresh([sample(0.2, at=1)]))
        self.assertTrue(book.refresh([]))


class RuleOverrideTests(unittest.TestCase):
    def test_override_is_validated_and_empty_mapping_is_dropped(self):
        rule = validate_model("promo-f", {"credits_override": {"intl-work": 0.04}})
        self.assertEqual(rule["credits_override"], {"intl-work": 0.04})
        self.assertNotIn("credits_override", validate_model("promo-f", {"credits_override": {}}))
        for bad in ({"moon": 1}, {"intl-work": -1}, {"intl-work": True}, {"intl-work": float("nan")}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_model("promo-f", {"credits_override": bad})

    def test_custom_models_cannot_carry_a_manual_rate(self):
        with self.assertRaises(ValueError):
            validate_model("custom:x", {"public_id": "mine", "upstream_id": "promo-f", "custom": True,
                                        "credits_override": {"intl-work": 1}})

    def test_manual_rates_follow_the_upstream_id_and_policy_revision(self):
        with tempfile.TemporaryDirectory() as root:
            store = ControlStore(Path(root) / "control.sqlite3")
            self.addCleanup(store.close)
            config = {"control_store": store}
            store.update_model("promo-f", validate_model("promo-f", {"credits_override": {"cn-cli": 0.2}}), 0)
            self.assertEqual(model_rates.manual_rates(config), {("cn-cli", "promo-f"): 0.2})
            store.update_model("promo-f", validate_model("promo-f", {}), store.snapshot()["revision"])
            self.assertEqual(model_rates.manual_rates(config), {})


class ProbeStoreTests(unittest.TestCase):
    def test_probe_rows_round_trip_in_sample_shape(self):
        with tempfile.TemporaryDirectory() as root:
            store = ControlStore(Path(root) / "control.sqlite3")
            self.addCleanup(store.close)
            store.record_rate_probe("intl-work", "promo-f", {"credit": 0.06, "input_tokens": 8000,
                                                             "cache_read_tokens": 0, "output_tokens": 2}, rate=0.9)
            rows = store.rate_probes(0)
            self.assertEqual(rows[0]["model"], "promo-f")
            self.assertEqual((rows[0]["credit"], rows[0]["rate"]), (0.06, 0.9))
            self.assertEqual(summarize(rows)[("intl-work", "promo-f")]["rate"], 0.9)
            with self.assertRaises(ValueError):
                store.record_rate_probe("intl-work", "promo-f", rows[0], rate=-1)
            self.assertTrue(summarize(rows)[("intl-work", "promo-f")]["billed"])
            with self.assertRaises(ValueError):
                store.record_rate_probe("moon", "promo-f", rows[0])
            store.prune_rate_probes(keep_days=0)
            self.assertEqual(store.rate_probes(0), [])


class ProbeOnceTests(unittest.TestCase):
    def test_reads_credit_from_the_streamed_usage(self):
        lines = ['data: {"choices":[{"delta":{"content":"OK"}}]}',
                 'data: {"choices":[],"usage":{"prompt_tokens":8100,"completion_tokens":2,"credit":0.06}}',
                 "data: [DONE]"]
        response = Mock(status_code=200)
        response.iter_lines.return_value = lines
        client = Mock()
        client.stream.return_value.__enter__ = Mock(return_value=response)
        client.stream.return_value.__exit__ = Mock(return_value=False)
        with patch("httpx.Client") as factory:
            factory.return_value.__enter__ = Mock(return_value=client)
            factory.return_value.__exit__ = Mock(return_value=False)
            result = rate_measurement.probe_once(
                "https://upstream/chat",
                {"Authorization": "Bearer t", "X-Domain": "www.workbuddy.cn", "X-IDE-Type": "WorkBuddy"},
                "promo-f")
        self.assertEqual(result, {"credit": 0.06, "input_tokens": 8100, "cache_read_tokens": 0, "output_tokens": 2})
        kwargs = client.stream.call_args.kwargs
        body = json.loads(gzip.decompress(kwargs["content"])) if "content" in kwargs else kwargs["json"]
        self.assertEqual(client.stream.call_args.kwargs["headers"]["X-Agent-Type"], "auxiliary")
        self.assertEqual(client.stream.call_args.kwargs["headers"]["X-Agent-Runtime"], "local-agent")
        self.assertEqual(body["model"], "promo-f")
        self.assertGreater(len(body["messages"][1]["content"]), 20000)

    def test_missing_credit_is_an_error_not_a_free_verdict(self):
        response = Mock(status_code=200)
        response.iter_lines.return_value = ['data: {"usage":{"prompt_tokens":10}}']
        client = Mock()
        client.stream.return_value.__enter__ = Mock(return_value=response)
        client.stream.return_value.__exit__ = Mock(return_value=False)
        with patch("httpx.Client") as factory:
            factory.return_value.__enter__ = Mock(return_value=client)
            factory.return_value.__exit__ = Mock(return_value=False)
            with self.assertRaises(RuntimeError):
                rate_measurement.probe_once("https://upstream/chat", {}, "promo-f")


class ProbeFlowTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = ControlStore(root / "control.sqlite3")
        self.addCleanup(self.store.close)
        catalog = [{"id": "promo-f", "credits": "x0.00", "supportsToolCall": True},
                   {"id": "ref", "credits": "x0.06", "supportsToolCall": True}]
        self.gateway = Mock(CONFIG={"control_store": self.store,
                                    "account_catalogs": {"acct": {"profile": "intl-work", "serves": catalog}}})
        self.gateway._usable_models.side_effect = converter._usable_models
        self.gateway._catalog_account_scope.side_effect = lambda account, scope: account["serves"]
        self.gateway._multiplier_value.side_effect = converter._multiplier_value
        self.enterContext(patch.object(model_rates, "_accounts_serving",
                                       return_value=[{"account_key": "acct", "cm": Mock()}]))

    def test_free_target_spends_no_reference_request(self):
        with patch.object(model_rates, "_send", return_value={"credit": 0, "input_tokens": 8200,
                                                              "cache_read_tokens": 0, "output_tokens": 4}) as send:
            result = model_rates.probe(self.gateway, "promo-f", "intl-work")
        self.assertEqual(send.call_count, 1)
        self.assertEqual((result["billed"], result["rate"]), (False, None))
        self.assertIn("确认为 0 倍率", result["message"])

    def test_charged_target_is_calibrated_against_the_reference(self):
        replies = {"promo-f": {"credit": 0.59, "input_tokens": 8239, "cache_read_tokens": 0, "output_tokens": 16},
                   "ref": {"credit": 0.04, "input_tokens": 8821, "cache_read_tokens": 0, "output_tokens": 4}}
        with patch.object(model_rates, "_send", side_effect=lambda _g, _entry, model, _p: replies[model]):
            result = model_rates.probe(self.gateway, "promo-f", "intl-work")
        self.assertAlmostEqual(result["rate"], 0.9476, places=3)
        self.assertEqual(result["reference"]["model"], "ref")
        self.assertEqual(self.store.rate_probes(0)[-1]["rate"], result["rate"])

    def test_a_reference_the_account_tier_refuses_falls_through_to_the_next(self):
        self.gateway.CONFIG["account_catalogs"]["acct"]["serves"].append(
            {"id": "tier-locked", "credits": "x0.1", "supportsToolCall": True})
        replies = {"promo-f": {"credit": 0.59, "input_tokens": 8239, "cache_read_tokens": 0, "output_tokens": 16},
                   "ref": {"credit": 0.04, "input_tokens": 8821, "cache_read_tokens": 0, "output_tokens": 4}}

        def send(_g, _entry, model, _p):
            if model == "tier-locked":
                raise RuntimeError("上游 HTTP 400 code=11158 not allowed")
            return replies[model]
        with patch.object(model_rates, "_send", side_effect=send):
            result = model_rates.probe(self.gateway, "promo-f", "intl-work")
        self.assertEqual(result["reference"]["model"], "ref")
        self.assertIsNotNone(result["rate"])


class ProbeJobTests(unittest.TestCase):
    """Probes run as one shared background job whose banner names the model being probed."""

    def wait(self):
        for _ in range(200):
            view = model_rates.job_view(Mock())
            if view["status"] != "running":
                return view
            time.sleep(0.01)
        self.fail("probe job did not finish")

    def test_job_reports_progress_counts_and_refreshes_rates(self):
        gate = threading.Event()
        outcomes = {"promo-f": {"ok": True, "billed": True}, "free-m": {"ok": True, "billed": False},
                    "broken": {"ok": False, "billed": None}}

        def probe(_gateway, model, profile, progress):
            progress(f"对照 ref（{model}）")
            gate.wait(2)
            return {"model": model, "profile": profile, "message": "", **outcomes[model]}
        gateway = Mock()
        pairs = [("broken", "cn-cli"), ("free-m", "cn-cli"), ("promo-f", "intl-work")]
        with patch.object(model_rates, "targets", return_value=pairs), \
                patch.object(model_rates, "probe", side_effect=probe), \
                patch.object(model_rates, "refresh") as refresh:
            started = model_rates.start_probe(gateway)
            for _ in range(200):
                running = model_rates.job_view(gateway)
                if len(running["current"]) == 3:
                    break
                time.sleep(0.01)
            again = model_rates.start_probe(gateway, "promo-f", scope="promo-f")
            gate.set()
            done = self.wait()
        self.assertEqual((started["status"], started["scope"], started["total"]), ("running", "all", 3))
        self.assertEqual(again["started_at"], started["started_at"])   # No second job while one runs.
        self.assertIn({"item": {"model": "promo-f", "profile": "intl-work"}, "step": "对照 ref（promo-f）"},
                      running["current"])
        self.assertEqual((done["billed"], done["free"], done["failed"], done["current"]), (1, 1, 1, []))
        refresh.assert_called_once_with(gateway)

    def test_model_probe_without_any_serving_account_is_rejected(self):
        with patch.object(model_rates, "targets", return_value=[]), self.assertRaises(HTTPException):
            model_rates.start_probe(Mock(), "nowhere")


class ProbeRequestLogTests(unittest.TestCase):
    """Probes are visible in the request log with their cost, under a fixed conversation prefix."""

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.audit = AuditStore(root / "audit.sqlite3")
        self.addCleanup(self.audit.close)
        self.gateway = Mock(CONFIG={"audit_store": self.audit})
        self.gateway._dynamic_request_headers.return_value = {"X-Request-ID": "req-1", "X-Conversation-ID": "x"}
        self.gateway._upstream_model.side_effect = lambda model, _profile: model
        self.gateway.chat_url_for_headers.return_value = "https://upstream/chat"
        cm = Mock()
        cm._lock = threading.Lock()
        cm.get_headers.return_value = {"Authorization": "Bearer t"}
        self.entry = {"account_key": "acct", "cm": cm}

    def record(self):
        items = self.audit.list_records(kind="request")["items"]
        self.assertEqual(len(items), 1)
        return self.audit.get_request(items[0]["id"])

    def test_successful_probe_is_logged_with_its_charge(self):
        sample_row = {"credit": 0.59, "input_tokens": 8239, "cache_read_tokens": 0, "output_tokens": 16}
        with patch.object(rate_measurement, "probe_once", return_value=sample_row) as send:
            model_rates._send(self.gateway, self.entry, "promo-f", "intl-work")
        conversation = send.call_args.args[1]["X-Conversation-ID"]
        self.assertTrue(conversation.startswith(model_rates.PROBE_CONVERSATION_PREFIX))
        row = self.record()
        self.assertEqual((row["model"], row["profile"], row["protocol"], row["credit"], row["input_tokens"]),
                         ("promo-f", "intl-work", "rate_probe", 0.59, 8239))
        self.assertEqual(row["attempts"][0]["conversation_id"], conversation)
        # The calibrated probe table already counts this sample; the log copy must not.
        self.assertEqual(self.audit.credit_samples(0), [])

    def test_refused_probe_is_logged_as_an_error(self):
        refused = rate_measurement.ProbeError("上游 HTTP 400 code=11158", 400, "11158")
        with patch.object(rate_measurement, "probe_once", side_effect=refused), self.assertRaises(
                rate_measurement.ProbeError):
            model_rates._send(self.gateway, self.entry, "tier-locked", "intl-work")
        row = self.record()
        self.assertEqual((row["outcome"], row["status_code"], row["error_code"]), ("error", 400, "11158"))


class OutputProbeFileTests(unittest.TestCase):
    def test_reads_valid_records_and_drops_malformed_ones(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / output_probes.FILENAME
            self.assertEqual(output_probes.read(path)["exists"], False)
            path.write_text(json.dumps({"version": 1, "probes": {
                "glm-5.2": {"claimed": 1048576, "measured": 32000, "verdict": "clamped", "note": "钳制",
                            "tested_at": "2026-10-19 12:00:00", "source": "probe_max_tokens.py"},
                "bad": {"verdict": "huh"}, "": {"verdict": "clamped"}}}), encoding="utf-8")
            view = output_probes.read(path)
            self.assertTrue(view["exists"])
            self.assertEqual(set(view["probes"]), {"glm-5.2"})
            self.assertEqual(view["probes"]["glm-5.2"]["measured"], 32000)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(output_probes.read(path)["probes"], {})

    def test_file_sits_beside_the_control_database(self):
        self.assertEqual(output_probes.default_path(Path("/state/control.sqlite3")),
                         Path("/state") / output_probes.FILENAME)
        self.assertIsNone(output_probes.default_path(None))


class ZeroBalanceRoutingTests(unittest.TestCase):
    """A zero-balance account may route a declared-free model only until billing is measured."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": str(self.root)}))
        self.enterContext(patch.object(converter, "_sync_usage", Mock()))
        self.enterContext(patch.object(converter.model_policy, "credential_auto_checkin", return_value=False))
        self.enterContext(patch.object(converter.model_policy, "credential_auto_travel", return_value=False))
        value = {"account": {"uid": "intl-user"}, "auth": {
            "accessToken": "token-intl", "domain": "www.codebuddy.ai", "expiresAt": (time.time() + 3600) * 1000}}
        path = self.root / "intl-user.info"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.pool = converter.CredentialPool([path])
        self.ledger = CreditLedger(self.root / "ledger.json")
        self.pool.set_ledger(self.ledger)
        self.entry = self.pool.entries()[0]
        self.ledger.bind_identity(self.entry["id"], self.entry["account_key"])
        self.book = RateBook()
        self.enterContext(patch.dict(converter.CONFIG, {
            "cred_pool": self.pool, "ledger": self.ledger, "model_cache": ModelCatalogCache(self.root / "m.json"),
            "aggregate_catalog": None, "catalog_probe_mode": "account", "control_store": None,
            "usage_snapshots": None, "housekeep_concurrency": 2, "log_path": None, "account_catalogs": None,
            "model_catalogs": {}, "catalog_index": None, "model_guard": True, "rate_book": self.book,
            "audit_store": None}))
        models = [{"id": "promo-f", "supportsToolCall": True, "credits": "x0.00"}]
        with patch.object(converter.credits_mod, "fetch_credits",
                          return_value={"credits": 0, "intl": True, "segments": []}), \
             patch.object(converter.credits_mod, "fetch_model_scopes",
                          return_value={"picker": models, "account": models}):
            self.pool.entry_by_id(self.entry["id"])["synced_at"] = 0.0
            converter._housekeep_once(self.pool, self.ledger)
        converter.invalidate_model_table()

    def test_measured_billing_removes_the_free_route_for_empty_accounts(self):
        self.assertTrue(self.pool._eligible(self.entry, "promo-f"))
        audit = Mock()
        audit.credit_samples.return_value = [sample(0.2, at=time.time(), profile=self.entry["profile"])]
        converter.CONFIG["audit_store"] = audit   # setUp's patch.dict restores the whole CONFIG.
        model_rates.refresh(converter)
        self.assertFalse(self.pool._eligible(self.entry, "promo-f"))
        self.assertNotIn("promo-f", converter.current_models())

    def test_public_models_flag_the_measured_rate_as_estimated(self):
        store = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(store.close)
        converter.CONFIG["control_store"] = store
        self.ledger.update_credits(self.entry["id"], {"credits": 5, "intl": True, "segments": []})
        audit = Mock()
        audit.credit_samples.return_value = [sample(0.59, at=time.time(), profile=self.entry["profile"], rate=0.95)]
        converter.CONFIG["audit_store"] = audit
        model_rates.refresh(converter)
        row = next(item for item in converter.model_policy.public_details(converter) if item["id"] == "promo-f")
        self.assertTrue(row["credits_estimated"])
        self.assertEqual(row["credits"], 0.95)


if __name__ == "__main__":
    unittest.main(verbosity=2)
