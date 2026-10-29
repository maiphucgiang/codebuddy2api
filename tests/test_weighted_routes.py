#!/usr/bin/env python3
"""Model route lists, weights and independent route enable switches."""

import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import model_policy
from app.control_store import validate_model
import converter


class Store:
    def __init__(self, models):
        self.data = {"models": models, "credentials": {}, "revision": 1}

    def snapshot(self):
        return self.data


def config(rule):
    return {"control_store": Store({"aggregate": rule})}


class WeightedRouteTests(TestCase):
    def test_legacy_rule_keeps_one_upstream_route(self):
        self.assertEqual(model_policy.route_candidates(
            {"control_store": Store({"public": {"public_id": "public", "upstream_id": "legacy"}})},
            "public"), ["legacy"])

    def test_ordered_routes_skip_disabled_entries(self):
        rule = {"public_id": "aggregate", "upstream_id": "first", "routes": [
            {"upstream_id": "first", "enabled": False, "source": "builtin"},
            {"upstream_id": "second", "enabled": True, "source": "cloud"},
            {"upstream_id": "third", "enabled": True, "source": "manual"},
        ]}
        self.assertEqual(model_policy.route_candidates(config(rule), "aggregate"), ["second", "third"])

    def test_weighted_routes_select_a_weighted_first_candidate(self):
        rule = {"public_id": "weighted-ratio", "upstream_id": "first", "routes": [
            {"upstream_id": "first", "weight": 3},
            {"upstream_id": "second", "weight": 1},
        ]}
        seen = [model_policy.route_candidates(config(rule), "weighted-ratio")[0] for _ in range(40)]
        self.assertEqual(seen.count("first"), 30)
        self.assertEqual(seen.count("second"), 10)
        self.assertEqual(set(seen), {"first", "second"})

    def test_all_disabled_routes_are_unavailable(self):
        rule = {"public_id": "aggregate", "upstream_id": "first", "routes": [
            {"upstream_id": "first", "enabled": False},
            {"upstream_id": "second", "enabled": False},
        ]}
        self.assertEqual(model_policy.route_candidates(config(rule), "aggregate"), [])

    def test_validation_preserves_route_order_and_source(self):
        rule = validate_model("aggregate", {
            "public_id": "aggregate", "upstream_id": "first", "routes": [
                {"upstream_id": "first", "source": "builtin", "enabled": False},
                {"upstream_id": "second", "source": "cloud", "weight": 2},
            ]}, known_models=["first", "second"])
        self.assertEqual([item["upstream_id"] for item in rule["routes"]], ["first", "second"])
        self.assertFalse(rule["routes"][0]["enabled"])
        self.assertEqual(rule["routes"][0]["source"], "builtin")
        self.assertEqual(rule["routes"][1]["source"], "cloud")
        self.assertEqual(rule["routes"][1]["weight"], 2.0)

    def test_validation_rejects_zero_weight_only_and_duplicate_routes(self):
        for routes in (
            [{"upstream_id": "first", "weight": 0}],
            [{"upstream_id": "first", "weight": 2, "enabled": False},
             {"upstream_id": "second", "weight": 0, "enabled": True}],
            [{"upstream_id": "first"}, {"upstream_id": "first"}],
        ):
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                validate_model("aggregate", {"upstream_id": "first", "routes": routes},
                               known_models=["first"])

    def test_catalog_variants_with_same_cost_and_capabilities_are_grouped(self):
        groups, aliases = converter._build_catalog_route_groups({"account": {
            "profile": "cn-cli", "serves": [
                {"id": "hy4-preview", "name": "Hy4 preview", "credits": "x0.29",
                 "maxInputTokens": 960000, "maxOutputTokens": 64000,
                 "supportsImages": True, "supportsReasoning": True, "supportsToolCall": True},
                {"id": "hy4-preview-x", "name": "Hy4 preview", "credits": "x0.29",
                 "maxInputTokens": 960000, "maxOutputTokens": 64000,
                 "supportsImages": True, "supportsReasoning": True, "supportsToolCall": True},
                {"id": "hy4-preview-paid", "name": "Hy4 preview", "credits": "x0.30",
                 "maxInputTokens": 960000, "maxOutputTokens": 64000,
                 "supportsImages": True, "supportsReasoning": True, "supportsToolCall": True},
            ]}})
        self.assertEqual([route["upstream_id"] for route in groups["hy4-preview"]],
                         ["hy4-preview", "hy4-preview-f", "hy4-preview-x"])
        self.assertEqual(aliases, {
            "hy4-preview-f": "hy4-preview", "hy4-preview-x": "hy4-preview", "kimi-k3-1": "kimi-k3"
        })


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
