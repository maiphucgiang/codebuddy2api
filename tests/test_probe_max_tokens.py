#!/usr/bin/env python3
"""Test the output-limit probe's verdicts, tier capping and panel-file merging offline."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # Allow direct execution.
spec = importlib.util.spec_from_file_location("probe_max_tokens", ROOT / "scripts" / "probe_max_tokens.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

from app import output_probes  # noqa: E402 - the script path is prepared above


def run(replies, claimed=None, tiers=(40000, 100000), retries=2, budget=600):
    calls = []

    def fake(base, key, model, max_tokens, prompt, timeout):
        calls.append(max_tokens)
        return replies.pop(0)
    with patch.object(probe, "probe_once", side_effect=fake):
        row = probe.probe_model("http://gw/v1", "k", "m", claimed, list(tiers), retries, 900, budget, lambda _m: None)
    return row, calls


class VerdictTests(unittest.TestCase):
    def test_truncation_below_the_request_is_the_real_cap(self):
        row, _ = run([(32000, "length", None)], claimed=1048576)
        self.assertEqual((row["verdict"], row["measured"]), ("clamped", 32000))

    def test_full_output_climbs_and_ends_as_a_lower_bound(self):
        row, calls = run([(40000, "length", None), (99990, "length", None)])
        self.assertEqual(calls, [40000, 100000])
        self.assertEqual((row["verdict"], row["measured"]), ("at_least", 100000))

    def test_tiers_never_exceed_the_claimed_limit(self):
        row, calls = run([(40000, "length", None), (48000, "length", None)], claimed=48000)
        self.assertEqual(calls, [40000, 48000])
        self.assertEqual((row["verdict"], row["measured"]), ("at_least", 48000))

    def test_model_stopping_early_is_not_a_cap(self):
        row, calls = run([(3000, "stop", None), (2500, "stop", None)])
        self.assertEqual(calls, [40000, 40000])
        self.assertEqual((row["verdict"], row["measured"]), ("inconclusive", None))

    def test_stop_after_a_full_tier_keeps_the_known_floor(self):
        row, _ = run([(40000, "length", None), (5000, "stop", None), (6000, "stop", None)])
        self.assertEqual((row["verdict"], row["measured"]), ("at_least", 40000))

    def test_request_error_is_inconclusive(self):
        row, _ = run([(None, None, "HTTP 503: busy")])
        self.assertEqual(row["verdict"], "inconclusive")

    def test_exhausted_budget_stops_without_a_request(self):
        row, calls = run([], budget=0)
        self.assertEqual(calls, [])
        self.assertEqual(row["verdict"], "inconclusive")


class PanelFileTests(unittest.TestCase):
    def test_merge_keeps_unmeasured_models_and_reads_back_in_the_gateway(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / output_probes.FILENAME
            path.write_text(json.dumps({"version": 1, "probes": {
                "old": {"claimed": 8000, "measured": 8000, "verdict": "at_least", "note": "", "tested_at": ""}}}))
            probe.write_panel(path, [{"model": "glm", "claimed": 1048576, "measured": 32000,
                                      "verdict": "clamped", "note": "截断", "time": "2026-10-19 12:00:00"}])
            view = output_probes.read(path)
            self.assertEqual(set(view["probes"]), {"old", "glm"})
            self.assertEqual(view["probes"]["glm"]["measured"], 32000)


class SelectTests(unittest.TestCase):
    def test_exact_ids_win_over_substrings(self):
        models = [{"id": "glm-5.2"}, {"id": "glm-5.2-x"}, {"id": "kimi"}]
        self.assertEqual([m["id"] for m in probe.select(models, "glm-5.2")], ["glm-5.2"])
        self.assertEqual([m["id"] for m in probe.select(models, "glm")], ["glm-5.2", "glm-5.2-x"])
        self.assertEqual(len(probe.select(models, None)), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
