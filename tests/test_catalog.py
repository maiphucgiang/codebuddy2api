#!/usr/bin/env python3
"""CLI/WorkBuddy 产品目录解析、请求隔离与 schema2 缓存回归；纯 mock/临时目录。

运行：.venv/bin/python -B tests/test_catalog.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx

from app.client_profiles import catalog_headers, identity_headers
from app.credits import (
    AuthExpiredError, ModelCatalogCache, fetch_model_catalog, select_cli_models,
    select_product_models,
)


def jwt(issuer):
    payload = base64.urlsafe_b64encode(json.dumps({"iss": issuer}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def catalog(references):
    return {"models": [
        {"id": "gpt-5.1-codex-mini", "name": "Codex Mini", "supportsToolCall": True},
        {"id": "fast-model", "name": "Fast", "aliases": ["fast", "quick"],
         "credits": {"input": 1}, "supportsToolCall": True,
         "relatedModels": {"lite": "fast-lite", "reasoning": "fast-reasoning"}},
        {"id": "fast-lite", "name": "Fast Lite"},
        {"id": "fast-reasoning", "name": "Fast Reasoning"},
        {"id": "other-model", "name": "Other", "aliases": ["other"]},
    ], "agents": [{"name": "web", "models": ["gpt-5.1-codex-mini"]},
                   {"name": "cli", "models": references}]}


class CatalogSelectionTests(unittest.TestCase):
    def test_cli_filters_by_id_name_alias_and_preserves_root_metadata(self):
        data = catalog(["Fast", {"name": "other"}, "fast-model", "quick", "unknown"])
        selected = select_cli_models(data)
        self.assertEqual(selected, [data["models"][1], data["models"][4]])
        self.assertEqual([m["id"] for m in selected], ["fast-model", "other-model"])
        self.assertEqual(select_cli_models(catalog([{"id": "fast"}])), [data["models"][1]])
        self.assertEqual(select_cli_models(catalog(["fast-model"])), [data["models"][1]])
        selected[0]["credits"]["input"] = 99
        self.assertEqual(data["models"][1]["credits"]["input"], 1)

    def test_work_default_agent_precedes_cli_and_first_agent(self):
        data = catalog(["fast"])
        data["agents"].append({"name": "chat", "tags": ["default"],
                               "models": ["other", "other-model"]})
        for agents in (data["agents"], list(reversed(data["agents"]))):
            with self.subTest(agents=agents):
                data["agents"] = agents
                self.assertEqual(select_product_models(data, "workbuddy"), [data["models"][4]])
                self.assertEqual(select_product_models(data, "cli"), [data["models"][1]])
                self.assertEqual(select_cli_models(data), select_product_models(data, "cli"))
        selected = select_product_models(data, "workbuddy")
        selected[0]["aliases"].append("mutation")
        self.assertEqual(data["models"][4]["aliases"], ["other"])

    def test_work_fallback_and_legacy_root_table(self):
        data = catalog(["fast"])
        self.assertEqual(select_product_models(data, "workbuddy"), [data["models"][1]])
        data["agents"] = [{"name": "unused"}, {"name": "chat", "models": ["other"]}]
        self.assertEqual(select_product_models(data, "workbuddy"), [data["models"][4]])
        for extra in ({}, {"agents": []}, {"agents": [{"name": "chat", "tags": ["default"]}]}):
            with self.subTest(extra=extra):
                legacy = dict(models=data["models"], **extra)
                self.assertEqual(select_product_models(legacy, "workbuddy"), data["models"])

    def test_work_explicit_empty_default_never_falls_back_to_cli_or_root(self):
        data = catalog(["fast"])
        data["agents"].append({"name": "chat", "tags": ["default"], "models": []})
        self.assertEqual(select_product_models(data, "workbuddy"), [])
        self.assertEqual(select_cli_models(data), [data["models"][1]])
        self.assertEqual(select_product_models(catalog([]), "workbuddy"), [])
        self.assertEqual(select_product_models({"models": []}, "workbuddy"), [])

    def test_available_models_and_disabled_filter_without_changing_order(self):
        for product in ("cli", "workbuddy"):
            data = catalog(["other", "fast", "fast-lite"])
            data["agents"].append({"name": "chat", "tags": ["default"],
                                   "models": ["other", "fast", "fast-lite"]})
            data["models"][2]["disabled"] = True
            data["availableModels"] = ["fast-model", "fast-lite", "other-model"]
            with self.subTest(product=product):
                self.assertEqual([m["id"] for m in select_product_models(data, product)],
                                 ["other-model", "fast-model"])
                data["availableModels"] = ["fast-model"]
                self.assertEqual(select_product_models(data, product), [data["models"][1]])
                data["availableModels"] = ["unknown"]
                self.assertEqual(select_product_models(data, product), [])
                data["availableModels"] = []
                self.assertEqual([m["id"] for m in select_product_models(data, product)],
                                 ["other-model", "fast-model"])
                del data["agents"]
                self.assertEqual(len(select_product_models(data, product)), 4)
                data["availableModels"] = ["fast-lite", "fast-model"]
                self.assertEqual(select_product_models(data, product), [data["models"][1]])
                data["availableModels"] = None
                self.assertEqual(len(select_product_models(data, product)), 4)

    def test_product_metadata_validation(self):
        for product in ("cli", "workbuddy"):
            for available in ({}, "secret", [None], [3], [""]):
                with self.subTest(product=product, available=available), self.assertRaises(ValueError):
                    select_product_models(dict(catalog(["fast"]), availableModels=available), product)
            for tags in ("default", [None], [3]):
                data = catalog(["fast"])
                data["agents"].append({"name": "chat", "tags": tags, "models": ["other"]})
                with self.subTest(product=product, tags=tags), self.assertRaises(ValueError):
                    select_product_models(data, product)
        for refs in (None, "secret", [None], ["unresolved"]):
            data = catalog(["fast"])
            data["agents"].append({"name": "chat", "tags": ["default"], "models": refs})
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                select_product_models(data, "workbuddy")
        with self.assertRaises(ValueError):
            select_product_models(catalog(["fast"]), "unknown")

    def test_variants_only_when_explicitly_selected(self):
        selected = select_cli_models(catalog(["fast", "Fast Reasoning"]))
        self.assertEqual([m["id"] for m in selected], ["fast-model", "fast-reasoning"])
        self.assertEqual(selected[0]["relatedModels"]["lite"], "fast-lite")

    def test_ids_take_precedence_over_names_and_aliases(self):
        data = {"models": [{"id": "a", "name": "b", "aliases": ["c"]},
                           {"id": "b"}, {"id": "c"}],
                "agents": [{"name": "cli", "models": ["b", "c"]}]}
        self.assertEqual([m["id"] for m in select_cli_models(data)], ["b", "c"])

    def test_legacy_without_agents_cli_or_models(self):
        roots = catalog([])["models"]
        for extra in ({}, {"agents": []}, {"agents": [{"name": "web", "models": []}]},
                      {"agents": [{"name": "cli"}]}):
            with self.subTest(extra=extra):
                self.assertEqual(select_cli_models(dict(models=roots, **extra)), roots)

    def test_explicit_empty_lists(self):
        for data in (catalog([]), {"models": []},
                     {"models": [], "agents": [{"name": "cli", "models": []}]}):
            with self.subTest(data=data):
                self.assertEqual(select_cli_models(data), [])

    def test_malformed_data_is_not_successful_empty_catalog(self):
        bad_data = [None, [], {}, {"models": None}, {"models": {}}, {"models": "secret"},
                    {"models": [None]}, {"models": [{}]}, {"models": [{"id": 3}]},
                    {"models": [{"id": " "}]}, {"models": [{"id": "a", "name": []}]},
                    {"models": [{"id": "a", "aliases": "secret"}]},
                    {"models": [{"id": "a", "aliases": [None]}]},
                    {"models": [{"id": "a"}, {"id": "a"}]}]
        for agents in (None, {}, "secret", [None], [{}], [{"name": []}],
                       [{"name": "cli"}, {"name": "cli"}]):
            bad_data.append({"models": [], "agents": agents})
        for refs in (None, {}, "secret", [None], [5], [{}], [""], [{"id": []}],
                     ["fast", None], [{"name": "fast", "id": None}]):
            bad_data.append(catalog(refs))
        for data in bad_data:
            with self.subTest(data=data):
                with self.assertRaises(ValueError) as raised:
                    select_cli_models(data)
                self.assertLess(len(str(raised.exception)), 100)
                self.assertNotIn("secret", str(raised.exception))

    def test_unknown_nonempty_list_must_resolve_at_least_one_model(self):
        for data in (catalog(["unknown-secret"]),
                     {"models": [], "agents": [{"name": "cli", "models": ["unknown-secret"]}]}):
            with self.subTest(data=data), self.assertRaises(ValueError) as raised:
                select_cli_models(data)
            self.assertNotIn("unknown-secret", str(raised.exception))
        self.assertEqual(len(select_cli_models(catalog(["unknown", "fast"]))), 1)

    def test_fetch_calls_selector_and_uses_international_host(self):
        client = MagicMock()
        client.get.return_value.status_code = 200
        data = catalog(["fast"])
        client.get.return_value.json.return_value = {"code": 0, "data": data}
        token = jwt("https://www.codebuddy.ai/auth/realms/copilot")
        with patch("app.credits.httpx.Client") as factory, patch(
                "app.credits.select_product_models", wraps=select_product_models) as select:
            factory.return_value.__enter__.return_value = client
            self.assertEqual(fetch_model_catalog(token, "CLI/test"), [data["models"][1]])
            select.assert_called_once_with(data, "cli")
        args, kwargs = client.get.call_args
        self.assertEqual(args[0], "https://www.codebuddy.ai/v3/config")
        self.assertEqual(kwargs["headers"]["x-client-platform"], "cli")
        self.assertEqual(httpx.Headers(kwargs["headers"])["user-agent"], "CLI/test")

    def test_fetch_profile_endpoints_and_headers(self):
        profiles = {
            "cn-cli": ("www.codebuddy.cn", "https://copilot.tencent.com"),
            "cn-work": ("www.workbuddy.cn", "https://www.workbuddy.cn"),
            "intl-cli": ("www.codebuddy.ai", "https://www.codebuddy.ai"),
            "intl-work": ("www.workbuddy.ai", "https://www.workbuddy.ai"),
        }
        data = catalog(["fast"])
        data["agents"].append({"name": "chat", "tags": ["default"], "models": ["other"]})
        for profile, (domain, host) in profiles.items():
            for token, hint in (("opaque", domain), (jwt(f"https://{domain}/x"), "")):
                with self.subTest(profile=profile, opaque=token == "opaque"):
                    client = MagicMock()
                    client.get.return_value.status_code = 200
                    client.get.return_value.json.return_value = {"code": 0, "data": data}
                    with patch("app.credits.httpx.Client") as factory:
                        factory.return_value.__enter__.return_value = client
                        result = fetch_model_catalog(token, domain=hint, uid="user", enterprise_id="enterprise")
                    self.assertEqual(result, [data["models"][4 if profile.endswith("work") else 1]])
                    client.get.assert_called_once()
                    args, kwargs = client.get.call_args
                    self.assertEqual(args, (host + "/v3/config",))
                    self.assertEqual(kwargs["headers"], catalog_headers(
                        {"accessToken": token, "domain": hint}, {"uid": "user", "enterpriseId": "enterprise"}))
                    headers = httpx.Headers(kwargs["headers"])
                    self.assertEqual(headers["authorization"], f"Bearer {token}")
                    self.assertEqual(headers["x-domain"], domain)
                    self.assertEqual(headers["x-user-id"], "user")
                    self.assertEqual(headers["x-enterprise-id"], "enterprise")
                    self.assertEqual(headers["x-tenant-id"], "enterprise")
                    for name, value in identity_headers(profile).items():
                        self.assertEqual(headers[name], value)
                    if profile.endswith("cli"):
                        self.assertEqual(headers["x-client-platform"], "cli")
                        self.assertEqual(headers["x-ide-type"], "CLI")
                    else:
                        self.assertNotIn("x-client-platform", headers)
                        self.assertEqual(headers["x-ide-type"], "WorkBuddy")
                    self.assertNotIn("origin", headers)
                    self.assertNotIn("referer", headers)

    def test_work_user_agent_cannot_be_overridden_by_cli_compat_argument(self):
        client = MagicMock()
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {"code": 0, "data": {"models": []}}
        with patch("app.credits.httpx.Client") as factory:
            factory.return_value.__enter__.return_value = client
            self.assertEqual(fetch_model_catalog("opaque", "CLI/test", domain="www.workbuddy.ai"), [])
        headers = httpx.Headers(client.get.call_args.kwargs["headers"])
        self.assertEqual(headers["user-agent"], identity_headers("intl-work")["User-Agent"])
        self.assertNotIn("x-client-platform", headers)

    def test_fetch_invalid_hints_fail_before_client_creation(self):
        cases = [("opaque", "unknown.example"),
                 (jwt("https://unknown.example/x"), "www.codebuddy.cn"),
                 (jwt("https://www.workbuddy.cn/x"), "www.codebuddy.cn"),
                 (jwt("https://www.workbuddy.ai/x"), "www.workbuddy.cn"),
                 (jwt("https://www.workbuddy.ai/x"), "www.codebuddy.ai")]
        with patch("app.credits.httpx.Client") as factory:
            for token, domain in cases:
                with self.subTest(domain=domain), self.assertRaises(ValueError):
                    fetch_model_catalog(token, domain=domain)
            factory.assert_not_called()

    def test_fetch_failure_never_falls_back_to_another_profile(self):
        for domain, host in (("", "https://copilot.tencent.com"),
                             ("www.codebuddy.cn", "https://copilot.tencent.com"),
                             ("www.workbuddy.cn", "https://www.workbuddy.cn"),
                             ("www.codebuddy.ai", "https://www.codebuddy.ai"),
                             ("www.workbuddy.ai", "https://www.workbuddy.ai")):
            for status in (401, 404, 500, "network"):
                with self.subTest(domain=domain, status=status):
                    client = MagicMock()
                    client.get.return_value.status_code = status
                    if status == "network":
                        client.get.side_effect = httpx.ConnectError("mock network failure")
                    with patch("app.credits.httpx.Client") as factory:
                        factory.return_value.__enter__.return_value = client
                        with self.assertRaises(AuthExpiredError if status == 401 else RuntimeError):
                            fetch_model_catalog("opaque", domain=domain)
                    client.get.assert_called_once()
                    self.assertEqual(client.get.call_args.args, (host + "/v3/config",))

    def test_fetch_malformed_and_error_responses_are_bounded_and_secret_free(self):
        client = MagicMock()
        token = jwt("https://www.workbuddy.ai/x")
        with patch("app.credits.httpx.Client") as factory:
            factory.return_value.__enter__.return_value = client
            client.get.return_value.status_code = 200
            for payload in (None, [], "secret" * 1000, {},
                            {"code": "secret", "msg": "secret" * 1000},
                            {"code": 0, "data": None}, {"code": 0, "data": catalog(["secret"])}):
                client.get.return_value.json.return_value = payload
                with self.subTest(payload=payload), self.assertRaises((ValueError, RuntimeError)) as raised:
                    fetch_model_catalog(token)
                self.assertLess(len(str(raised.exception)), 100)
                self.assertNotIn("secret", str(raised.exception))
            client.get.return_value.json.side_effect = ValueError("secret" * 1000)
            with self.assertRaises(ValueError) as raised:
                fetch_model_catalog(token)
            self.assertNotIn("secret", str(raised.exception))
            client.get.return_value.status_code = 401
            with self.assertRaises(AuthExpiredError):
                fetch_model_catalog(token)
            client.get.side_effect = httpx.ConnectError("secret" * 1000)
            with self.assertRaises(RuntimeError) as raised:
                fetch_model_catalog(token)
            self.assertNotIn("secret", str(raised.exception))


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "catalog.json"

    def test_v1_migration_keeps_other_group_stale_after_save_and_reload(self):
        roots = catalog([])["models"]
        self.path.write_text(json.dumps({"version": 1, "groups": {
            "domestic": {"models": [{"id": "domestic"}], "fetched_at": 1000},
            "international": {"models": roots, "fetched_at": 1000},
        }}), encoding="utf-8")
        with patch("app.credits.time.time", return_value=1001):
            cache = ModelCatalogCache(self.path)
            self.assertEqual(cache.models("international"), roots)
            self.assertFalse(cache.fresh("domestic"))
            self.assertFalse(cache.fresh("international"))
            self.assertEqual(cache.age("international"), 1)
            cache.put("domestic", [{"id": "new-domestic"}])
            persisted = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["version"], 2)
            reloaded = ModelCatalogCache(self.path)
            self.assertTrue(reloaded.fresh("domestic"))
            self.assertFalse(reloaded.fresh("international"))
            self.assertEqual(reloaded.models("international"), roots)
            reloaded.put("international", select_cli_models(catalog(["fast"])))
            final = ModelCatalogCache(self.path)
            self.assertTrue(final.fresh("international"))
            self.assertEqual([m["id"] for m in final.models("international")], ["fast-model"])
            self.assertEqual(final.models("domestic"), [{"id": "new-domestic"}])

    def test_empty_catalog_ttl_and_epoch_zero_age(self):
        with patch("app.credits.time.time", return_value=0):
            cache = ModelCatalogCache(self.path, ttl=60)
            self.assertIsNone(cache.age("international"))
            cache.put("international", [])
            self.assertTrue(cache.fresh("international"))
            self.assertEqual(cache.age("international"), 0)
        with patch("app.credits.time.time", return_value=59):
            cache = ModelCatalogCache(self.path, ttl=60)
            self.assertTrue(cache.fresh("international"))
            self.assertEqual(cache.models("international"), [])
            self.assertEqual(cache.age("international"), 59)
        with patch("app.credits.time.time", return_value=60):
            self.assertFalse(cache.fresh("international"))
            self.assertEqual(cache.age("international"), 60)

    def test_deep_copy_on_put_and_get(self):
        cache = ModelCatalogCache(self.path)
        models = select_cli_models(catalog(["fast"]))
        expected = deepcopy(models)
        cache.put("international", models)
        models[0]["credits"]["input"] = 999
        result = cache.models("international")
        result[0]["relatedModels"]["lite"] = "wrong"
        result[0]["aliases"].append("wrong")
        self.assertEqual(cache.models("international"), expected)
        self.assertEqual(ModelCatalogCache(self.path).models("international"), expected)

    def test_failed_fetch_keeps_old_models_and_age(self):
        cache = ModelCatalogCache(self.path)
        with patch("app.credits.time.time", return_value=1000):
            cache.put("international", [{"id": "old"}])
        before = self.path.read_bytes()
        client = MagicMock()
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {"code": 0, "data": catalog(["unknown"])}
        with patch("app.credits.httpx.Client") as factory:
            factory.return_value.__enter__.return_value = client
            with self.assertRaises(ValueError):
                cache.put("international", fetch_model_catalog(jwt("https://www.codebuddy.ai/x")))
        self.assertEqual(cache.models("international"), [{"id": "old"}])
        self.assertEqual(self.path.read_bytes(), before)
        with patch("app.credits.time.time", return_value=1010):
            self.assertEqual(cache.age("international"), 10)

    def test_site_groups_are_separate(self):
        cache = ModelCatalogCache(self.path)
        for origin in ("codebuddy", "workbuddy"):
            self.assertEqual(cache.group_for_token(jwt(f"https://www.{origin}.ai/x")), "international")
            self.assertEqual(cache.group_for_token(jwt(f"https://www.{origin}.cn/x")), "domestic")
        cache.put("domestic", [{"id": "cn-only"}])
        self.assertFalse(cache.fresh("international"))
        self.assertEqual(cache.models("international"), [])
        cache.put("international", [])
        self.assertTrue(cache.fresh("international"))
        self.assertEqual(cache.models("domestic"), [{"id": "cn-only"}])

    def test_threaded_reads_writes_and_persistence(self):
        cache = ModelCatalogCache(self.path)

        def roundtrip(group):
            for i in range(10):
                cache.put(group, [{"id": group, "metadata": {"iteration": i}}])
                value = cache.models(group)
                self.assertEqual(value[0]["id"], group)
                value[0]["metadata"]["iteration"] = -1
                self.assertGreaterEqual(cache.models(group)[0]["metadata"]["iteration"], 0)
                self.assertTrue(cache.fresh(group))
                self.assertIsNotNone(cache.age(group))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(roundtrip, ["domestic", "international"]))
        reloaded = ModelCatalogCache(self.path)
        for group in ("domestic", "international"):
            self.assertEqual(reloaded.models(group)[0]["metadata"]["iteration"], 9)
            self.assertTrue(reloaded.fresh(group))


if __name__ == "__main__":
    unittest.main()
