#!/usr/bin/env python3
"""Test account catalogs, selector compatibility, cache isolation and model rates offline."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import base64
import json
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from app.credits import (ModelCatalogCache, fetch_model_catalog, fetch_model_scopes,
                         select_product_models)
import converter


def jwt(issuer):
    payload = base64.urlsafe_b64encode(json.dumps({"iss": issuer}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def config_payload():
    """Model an official catalog with four root entries and two CLI selector entries."""
    def entry(identifier, credits, tools=True):
        item = {"id": identifier, "name": identifier, "credits": credits}
        if tools:
            item["supportsToolCall"] = True
        return item
    return {"models": [entry("hy4-preview", "x0.29 credits"),
                       entry("hy4-preview-f", "x0.00 credits"),
                       entry("glm-4.6", "x0.23 credits"),
                       entry("hunyuan-image-v3.0-art", "x5.00 credits", tools=False)],
            "agents": [{"name": "cli", "tags": ["cli"],
                        "models": ["hy4-preview-f", "glm-4.6"]}]}


class CatalogScopeSelectionTests(unittest.TestCase):
    def test_picker_is_agent_subset_and_account_is_root_table(self):
        data = config_payload()
        picker = select_product_models(data, "cli")
        account = select_product_models(data, "cli", scope="account")
        self.assertEqual([item["id"] for item in picker], ["hy4-preview-f", "glm-4.6"])
        self.assertNotIn("hy4-preview", [item["id"] for item in picker])
        self.assertEqual([item["id"] for item in account],
                         ["hy4-preview", "hy4-preview-f", "glm-4.6", "hunyuan-image-v3.0-art"])

    def test_account_scope_keeps_root_metadata(self):
        account = select_product_models(config_payload(), "cli", scope="account")
        by_id = {item["id"]: item for item in account}
        self.assertEqual(by_id["hy4-preview"]["credits"], "x0.29 credits")
        self.assertTrue(by_id["hy4-preview"]["supportsToolCall"])

    def test_workbuddy_account_scope_ignores_default_agent_narrowing(self):
        data = config_payload()
        data["agents"].append({"name": "craft", "tags": ["default"], "models": ["hy4-preview-f"]})
        self.assertEqual([item["id"] for item in select_product_models(data, "workbuddy")],
                         ["hy4-preview-f"])
        self.assertEqual(len(select_product_models(data, "workbuddy", scope="account")), 4)

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            select_product_models(config_payload(), "cli", scope="whatever")

    def test_available_models_still_filters_both_scopes(self):
        data = config_payload()
        data["availableModels"] = ["hy4-preview", "hy4-preview-f"]
        self.assertEqual([item["id"] for item in select_product_models(data, "cli")], ["hy4-preview-f"])
        self.assertEqual([item["id"] for item in
                          select_product_models(data, "cli", scope="account")],
                         ["hy4-preview", "hy4-preview-f"])


class FetchScopesTests(unittest.TestCase):
    def _client(self, payload):
        client = MagicMock()
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {"code": 0, "data": payload}
        return client

    def test_both_scopes_come_from_one_request(self):
        client = self._client(config_payload())
        with patch("app.credits.httpx.Client") as factory:
            factory.return_value.__enter__.return_value = client
            scopes = fetch_model_scopes(jwt("https://www.codebuddy.cn/auth/realms/copilot"),
                                        "CLI/test", uid="u", enterprise_id="e")
        self.assertEqual(sorted(scopes), ["account", "picker"])
        self.assertEqual([item["id"] for item in scopes["picker"]], ["hy4-preview-f", "glm-4.6"])
        self.assertEqual(len(scopes["account"]), 4)
        self.assertEqual(client.get.call_count, 1, "两个作用域必须共用一次 /v3/config")

    def test_fetch_model_catalog_still_returns_the_picker_subset(self):
        client = self._client(config_payload())
        with patch("app.credits.httpx.Client") as factory:
            factory.return_value.__enter__.return_value = client
            models = fetch_model_catalog(jwt("https://www.codebuddy.cn/auth/realms/copilot"))
        self.assertEqual([item["id"] for item in models], ["hy4-preview-f", "glm-4.6"])
        self.assertEqual(client.get.call_count, 1)


class CacheScopeTests(unittest.TestCase):
    def test_serves_round_trips_through_disk(self):
        data = config_payload()
        picker, account = (select_product_models(data, "cli", scope=scope)
                           for scope in ("picker", "account"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model-catalog.json"
            cache = ModelCatalogCache(path)
            cache.put("domestic", picker, serves=account)
            self.assertEqual([m["id"] for m in cache.models("domestic")], ["hy4-preview-f", "glm-4.6"])
            self.assertEqual(len(cache.serves("domestic")), 4)
            reloaded = ModelCatalogCache(path)
            self.assertEqual(len(reloaded.serves("domestic")), 4)
            self.assertTrue(reloaded.fresh("domestic"))
            self.assertEqual(json.loads(path.read_text())["groups"]["domestic"]["models"], picker)

    def test_older_entries_without_root_are_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model-catalog.json"
            cache = ModelCatalogCache(path)
            cache.put("domestic", [{"id": "legacy"}])          # Legacy cache entry
            self.assertEqual(cache.serves("domestic"), [])
            self.assertEqual(ModelCatalogCache(path).models("domestic"), [{"id": "legacy"}])

    def test_malformed_root_does_not_void_the_whole_group(self):
        for broken in ("nope", [{"id": "ok"}, "not-a-dict"], None):
            with self.subTest(serves=type(broken).__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "model-catalog.json"
                    ModelCatalogCache(path).put("domestic", [{"id": "kept"}], serves=None)
                    stored = json.loads(path.read_text())
                    if broken is not None:
                        stored["groups"]["domestic"]["serves"] = broken
                    path.write_text(json.dumps(stored), encoding="utf-8")
                    cache = ModelCatalogCache(path)
                    self.assertEqual(cache.serves("domestic"), [])
                    self.assertEqual(cache.models("domestic"), [{"id": "kept"}])

    def test_cached_serves_is_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp) / "model-catalog.json")
            root = [{"id": "hy4-preview"}]
            cache.put("domestic", [], serves=root)
            root[0]["id"] = "mutated"
            self.assertEqual(cache.serves("domestic"), [{"id": "hy4-preview"}])


class AccountScopeHelperTests(unittest.TestCase):
    """Preserve selector metadata, supplement root names and forbid borrowing missing catalogs."""

    def test_scope_models_is_the_subset(self):
        account = {"models": [{"id": "picker"}], "serves": [{"id": "root"}]}
        self.assertEqual(converter._account_scope(account), [{"id": "picker"}])
        self.assertEqual(converter._account_scope(account, "models"), [{"id": "picker"}])

    def test_scope_serves_merges_without_losing_picker_metadata(self):
        account = {"models": [{"id": "shared", "credits": "x0.00 credits"}],
                   "serves": [{"id": "shared", "credits": "x0.50 credits"},
                              {"id": "root-only", "credits": "x0.29 credits"}]}
        merged = converter._account_scope(account, "serves")
        self.assertEqual([item["id"] for item in merged], ["shared", "root-only"])
        self.assertEqual(merged[0]["credits"], "x0.00 credits", "同名条目必须保留选择器那份")

    def test_missing_catalogs_stay_missing(self):
        for account in ({}, {"models": None, "serves": [{"id": "root-only"}]}):
            with self.subTest(account=sorted(account)):
                self.assertIsNone(converter._account_scope(account, "serves"))

    def test_legacy_cache_without_root_falls_back_to_the_subset(self):
        account = {"models": [{"id": "picker"}]}
        self.assertEqual(converter._account_scope(account, "serves"), [{"id": "picker"}])


class FreeTierWithRootScopeTests(unittest.TestCase):
    """Use each root model's own rate without borrowing a free selector rate."""

    def setUp(self):
        data = config_payload()
        self.models = converter._account_scope(
            {"models": select_product_models(data, "cli"),
             "serves": select_product_models(data, "cli", scope="account")}, "serves")

    def test_paid_root_only_model_is_not_free(self):
        self.assertFalse(converter._model_free(self.models, "hy4-preview", "cn-cli"))

    def test_declared_free_model_is_free(self):
        self.assertTrue(converter._model_free(self.models, "hy4-preview-f", "cn-cli"))

    def test_unknown_model_is_not_free(self):
        self.assertFalse(converter._model_free(deepcopy(self.models), "no-such-model", "cn-cli"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
