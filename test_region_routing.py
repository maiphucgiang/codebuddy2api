"""地域/产品 HTTP 路由回归：临时合成凭据，所有 httpx 上游均由 MockTransport 接管。

运行：.venv/bin/python -B -m unittest -v test_region_routing
不启动维护线程，不读取本机 auth/.env，不依赖在线目录或真实账号。
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import client_profiles
import converter
import credits


PROFILES = ("cn-cli", "cn-work", "intl-cli", "intl-work")
DOMAINS = {
    "cn-cli": "www.codebuddy.cn", "cn-work": "www.workbuddy.cn",
    "intl-cli": "www.codebuddy.ai", "intl-work": "www.workbuddy.ai",
}
HOSTS = dict(DOMAINS, **{"cn-cli": "copilot.tencent.com"})
BASES = (("/v1", "cn"), ("/cn/v1", "cn"), ("/intl/v1", "intl"))
GENERATIONS = ("chat/completions", "responses", "messages")


def model(identifier):
    return {"id": identifier, "name": identifier, "supportsToolCall": True,
            "supportsImages": True, "credits": {"input": 1, "output": 2}}


def catalogs():
    return {profile: [model("shared-model"), model(profile.split("-")[1] + "-exclusive"),
                      model(profile + "-only")]
            for profile in PROFILES}


def success_sse():
    chunks = [
        {"id": "synthetic-completion", "choices": [{"index": 0,
         "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
        {"id": "synthetic-completion", "choices": [{"index": 0,
         "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
    ]
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode()


class RegionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": str(self.root)}))
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "ledger": None,
            "model_catalogs": {}, "model_cache": None, "model_guard": True,
            "models_remote": None, "models_intl": None,
            "max_images": 16, "image_policy": "truncate",
            "max_request_bytes": 32 * 1024 * 1024, "log_body_limit": 65536,
            "log_path": None, "desensitize": False, "no_compact": False,
        }))
        self.addCleanup(converter.invalidate_model_table)
        self.requests = []
        self.allowed_profiles = set()
        self.response_status = 200
        self.sequence = 0
        self.fixture_sequence = 0
        self.credentials = {}
        now = time.time()
        for profile in PROFILES:
            value = {"account": {"uid": profile, "enterpriseId": "synthetic-enterprise"},
                     "auth": {"domain": DOMAINS[profile],
                              "accessToken": "synthetic-access-" + profile,
                              "refreshToken": "synthetic-refresh-" + profile,
                              "expiresAt": (now + 86400) * 1000,
                              "lastRefreshTime": now * 1000}}
            self.credentials[profile] = value
            (self.root / (profile + ".info")).write_text(json.dumps(value), encoding="utf-8")
        transport = httpx.MockTransport(self.handle_upstream)
        real_sync, real_async = httpx.Client, httpx.AsyncClient

        def sync_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_sync(*args, **kwargs)

        def async_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async(*args, **kwargs)

        # Patch the shared module, covering chat, token refresh and catalog requests.
        self.enterContext(patch.object(httpx, "Client", side_effect=sync_client))
        self.enterContext(patch.object(httpx, "AsyncClient", side_effect=async_client))
        self.configure()
        self.client = self.enterContext(TestClient(converter.app))

    def configure(self, profiles=PROFILES, tables=None, balances=None, guard=True):
        self.fixture_sequence += 1
        self.pool = converter.CredentialPool([self.root / (p + ".info") for p in profiles])
        self.entries = {entry["cm"].summary()["uid"]: entry for entry in self.pool.entries()}
        self.ledger = credits.CreditLedger(self.root / f"ledger-{self.fixture_sequence}.json")
        self.pool.set_ledger(self.ledger)
        for profile, entry in self.entries.items():
            balance = (balances or {}).get(profile, 100)
            if balance is not None:
                self.ledger.update_credits(entry["id"], {
                    "credits": balance, "intl": profile.startswith("intl-"),
                    "segments": [], "soonest_expiry": None,
                })
        converter.CONFIG.update(cred_pool=self.pool, ledger=self.ledger,
                                model_catalogs=deepcopy(catalogs() if tables is None else tables),
                                model_guard=guard, model_cache=None)
        converter.invalidate_model_table()

    def handle_upstream(self, request):
        self.requests.append(request)
        profile = request.headers.get("x-user-id")
        self.assertIn(profile, self.allowed_profiles, "unexpected selected UID")
        self.assertEqual(request.url.scheme, "https")
        self.assertEqual(request.url.host, HOSTS[profile])
        self.assertEqual(request.headers["x-domain"], self.credentials[profile]["auth"]["domain"])
        self.assertEqual(request.headers["authorization"], "Bearer synthetic-access-" + profile)
        for name, value in client_profiles.identity_headers(profile).items():
            self.assertEqual(request.headers[name], value, (profile, name))
        self.assertEqual(request.headers["x-ide-type"],
                         "CLI" if profile.endswith("-cli") else "WorkBuddy")
        if profile.endswith("-work"):
            self.assertNotIn("x-client-platform", request.headers)
        if request.url.path == "/v3/config":
            self.assertEqual(request.method, "GET")
            if profile.endswith("-cli"):
                self.assertEqual(request.headers["x-client-platform"], "cli")
            return httpx.Response(200, json={"code": 0, "data": {
                "models": [model(profile + "-only")],
                "agents": [{"name": "cli", "models": [profile + "-only"]}],
            }})
        if request.url.path == "/v2/plugin/auth/token/refresh":
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["x-refresh-token"], "synthetic-refresh-" + profile)
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "synthetic-access-" + profile, "expiresIn": 86400}})
        self.assertEqual(request.url.path, "/v2/chat/completions")
        self.assertEqual(request.method, "POST")
        if self.response_status != 200:
            return httpx.Response(self.response_status, json={"error": {"message": "synthetic quota"}})
        return httpx.Response(200, content=success_sse(), headers={"Content-Type": "text/event-stream"})

    def payload(self, endpoint="chat/completions", selected_model="shared-model", *,
                stream=False, text=None, system=None):
        self.sequence += 1
        user = {"role": "user", "content": text or f"synthetic request {self.sequence}"}
        result = {"model": selected_model, "stream": stream, "temperature": 0.25, "top_p": 0.75}
        if endpoint == "responses":
            result.update(input=[user], max_output_tokens=37)
            if system is not None:
                result["instructions"] = system
        else:
            result.update(messages=[user], max_tokens=37)
            if system is not None:
                if endpoint == "messages":
                    result["system"] = system
                else:
                    result["messages"].insert(0, {"role": "system", "content": system})
        return result

    def post_ok(self, base, endpoint, payload, allowed):
        self.allowed_profiles = set(allowed)
        before = len(self.requests)
        response = self.client.post(base + "/" + endpoint, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("ok", response.text)
        if payload.get("stream"):
            self.assertIn({"chat/completions": "[DONE]", "responses": "response.completed",
                           "messages": "message_stop"}[endpoint], response.text)
        self.assertEqual(len(self.requests), before + 1, response.text)
        request = self.requests[-1]
        return request, json.loads(request.content)

    def post_rejected(self, base, endpoint, payload, statuses=(404, 503)):
        self.allowed_profiles = set()
        before = len(self.requests)
        response = self.client.post(base + "/" + endpoint, json=payload)
        self.assertIn(response.status_code, statuses, response.text)
        self.assertEqual(len(self.requests), before, "local rejection must not call any upstream")
        return response

    def test_all_three_generation_apis_have_default_cn_and_intl_routes(self):
        for base, region in BASES:
            for endpoint in GENERATIONS:
                for stream in (False, True):
                    with self.subTest(base=base, endpoint=endpoint, stream=stream):
                        _, body = self.post_ok(base, endpoint, self.payload(endpoint, stream=stream),
                                               (region + "-cli", region + "-work"))
                        self.assertEqual(body["model"], "shared-model")
                        self.assertTrue(body["stream"])

    def test_shared_model_rotates_only_between_same_region_products(self):
        for base, region in BASES:
            for endpoint in GENERATIONS:
                with self.subTest(base=base, endpoint=endpoint):
                    expected = {region + "-cli", region + "-work"}
                    seen = set()
                    for _ in range(4):
                        request, _ = self.post_ok(base, endpoint, self.payload(endpoint), expected)
                        seen.add(request.headers["x-user-id"])
                    self.assertEqual(seen, expected)

    def test_product_exclusive_models_never_rotate_into_other_product(self):
        for base, region in BASES:
            for endpoint in GENERATIONS:
                for product in ("cli", "work"):
                    with self.subTest(base=base, endpoint=endpoint, product=product):
                        for _ in range(2):
                            _, body = self.post_ok(base, endpoint,
                                                   self.payload(endpoint, product + "-exclusive"),
                                                   {region + "-" + product})
                            self.assertEqual(body["model"], product + "-exclusive")

    def test_default_root_rejects_international_only_model(self):
        for endpoint in GENERATIONS:
            for base in ("/v1", "/cn/v1"):
                self.post_rejected(base, endpoint, self.payload(endpoint, "intl-cli-only"))
        for endpoint in GENERATIONS:
            self.post_rejected("/intl/v1", endpoint, self.payload(endpoint, "cn-cli-only"))

    def test_wrong_region_or_product_sticky_is_automatically_rebound(self):
        for region in ("cn", "intl"):
            for wrong in PROFILES:
                right = region + "-cli"
                if wrong == right:
                    continue
                with self.subTest(region=region, wrong=wrong):
                    payload = self.payload(selected_model="cli-exclusive")
                    old_keys = set(self.pool._sticky)
                    self.post_ok("/" + region + "/v1", "chat/completions", payload, {right})
                    keys = set(self.pool._sticky) - old_keys
                    self.assertEqual(len(keys), 1)
                    key = keys.pop()
                    self.pool._sticky[key] = (self.entries[wrong]["id"], time.time())
                    self.post_ok("/" + region + "/v1", "chat/completions", payload, {right})
                    self.assertEqual(self.pool._sticky[key][0], self.entries[right]["id"])

    def test_model_change_rechecks_sticky_product_availability(self):
        for region in ("cn", "intl"):
            payload = self.payload(selected_model="cli-exclusive")
            base = "/" + region + "/v1"
            self.post_ok(base, "chat/completions", payload, {region + "-cli"})
            payload["model"] = "work-exclusive"
            self.post_ok(base, "chat/completions", payload, {region + "-work"})

    def test_same_model_and_payload_do_not_share_cross_region_conversation_id(self):
        for endpoint in GENERATIONS:
            payload = self.payload(endpoint, text="identical conversation in both regions",
                                   system="Preserve this caller instruction.")
            ids = {}
            for base, region in BASES:
                request, _ = self.post_ok(base, endpoint, payload,
                                          {region + "-cli", region + "-work"})
                conversation = request.headers["x-conversation-id"]
                self.assertTrue(conversation)
                if region in ids:
                    self.assertEqual(conversation, ids[region])
                ids[region] = conversation
                repeat, _ = self.post_ok(base, endpoint, payload,
                                         {request.headers["x-user-id"]})
                self.assertEqual(repeat.headers["x-conversation-id"], conversation)
            self.assertNotEqual(ids["cn"], ids["intl"])

    def test_single_region_pool_never_services_other_region(self):
        for present, absent in (("cn", "intl"), ("intl", "cn")):
            self.configure(profiles=(present + "-cli", present + "-work"))
            bases = ("/v1", "/cn/v1") if absent == "cn" else ("/intl/v1",)
            for base in bases:
                for endpoint in GENERATIONS:
                    self.post_rejected(base, endpoint, self.payload(endpoint))
            self.post_ok("/" + present + "/v1", "chat/completions", self.payload(),
                         {present + "-cli", present + "-work"})

    def test_intl_requires_known_positive_balance_not_domestic_fallback(self):
        for balance in (None, 0, -1):
            with self.subTest(balance=balance):
                self.configure(balances={"intl-cli": balance, "intl-work": balance})
                for endpoint in GENERATIONS:
                    self.post_rejected("/intl/v1", endpoint, self.payload(endpoint))
                self.post_ok("/v1", "chat/completions", self.payload(), {"cn-cli", "cn-work"})

    def test_unknown_or_zero_balance_product_is_not_a_rotation_candidate(self):
        for unavailable in ("intl-cli", "intl-work"):
            for balance in (None, 0):
                self.configure(balances={unavailable: balance})
                expected = {"intl-cli", "intl-work"} - {unavailable}
                for _ in range(3):
                    self.post_ok("/intl/v1", "chat/completions", self.payload(), expected)

    def test_unknown_or_empty_catalog_never_borrows_other_profile_models(self):
        for region in ("cn", "intl"):
            for missing in (None, []):
                with self.subTest(region=region, catalog=missing):
                    tables = catalogs()
                    tables[region + "-cli"] = missing
                    self.configure(tables=tables)
                    for endpoint in GENERATIONS:
                        self.post_rejected("/" + region + "/v1", endpoint,
                                           self.payload(endpoint, "cli-exclusive"))
                        self.post_ok("/" + region + "/v1", endpoint,
                                     self.payload(endpoint), {region + "-work"})
                    tables[region + "-work"] = missing
                    self.configure(tables=tables)
                    for endpoint in GENERATIONS:
                        response = self.post_rejected("/" + region + "/v1", endpoint,
                                                       self.payload(endpoint))
                        if missing is None and response.status_code == 503:
                            self.assertIn("retry-after", response.headers)

    def test_model_cooldown_rebinds_only_within_region_then_fails_locally(self):
        for region in ("cn", "intl"):
            with self.subTest(region=region):
                self.configure()
                base = "/" + region + "/v1"
                payload = self.payload()
                request, _ = self.post_ok(base, "chat/completions", payload,
                                          {region + "-cli", region + "-work"})
                first = request.headers["x-user-id"]
                self.pool.note_status(self.entries[first]["cm"], 429, model="shared-model")
                other = ({region + "-cli", region + "-work"} - {first}).pop()
                self.post_ok(base, "chat/completions", payload, {other})
                self.pool.note_status(self.entries[other]["cm"], 429, model="shared-model")
                for endpoint in GENERATIONS:
                    self.post_rejected(base, endpoint, self.payload(endpoint), statuses=(429,))
                # Other models remain usable; the same ID in the other region was not cooled.
                self.post_ok(base, "chat/completions", self.payload(selected_model="cli-exclusive"),
                             {region + "-cli"})
                remote = "intl" if region == "cn" else "cn"
                self.post_ok("/" + remote + "/v1", "chat/completions", self.payload(),
                             {remote + "-cli", remote + "-work"})

    def test_upstream_exclusive_model_429_cannot_fallback_to_wrong_product(self):
        for region in ("cn", "intl"):
            self.configure()
            base = "/" + region + "/v1"
            payload = self.payload(selected_model="cli-exclusive")
            self.allowed_profiles = {region + "-cli"}
            self.response_status = 429
            before = len(self.requests)
            try:
                response = self.client.post(base + "/chat/completions", json=payload)
            finally:
                self.response_status = 200
            self.assertEqual(response.status_code, 429, response.text)
            self.assertEqual(len(self.requests), before + 1)
            self.post_rejected(base, "chat/completions", payload, statuses=(429,))
            self.post_ok(base, "chat/completions", self.payload(selected_model="work-exclusive"),
                         {region + "-work"})

    def test_account_cooldown_never_uses_another_region(self):
        for region in ("cn", "intl"):
            self.configure()
            for profile in (region + "-cli", region + "-work"):
                self.pool.cooldown(self.entries[profile]["cm"], reason="synthetic cooldown")
            for endpoint in GENERATIONS:
                self.post_rejected("/" + region + "/v1", endpoint, self.payload(endpoint), statuses=(503,))

    def test_model_guard_false_only_allows_unknown_model_in_local_single_product(self):
        for region in ("cn", "intl"):
            for product in ("cli", "work"):
                profile = region + "-" + product
                # Keep both products in the other region: the restriction is region-local.
                remote = "intl" if region == "cn" else "cn"
                self.configure(profiles=(profile, remote + "-cli", remote + "-work"), guard=False)
                for endpoint in GENERATIONS:
                    _, body = self.post_ok("/" + region + "/v1", endpoint,
                                           self.payload(endpoint, "unlisted-model"), {profile})
                    self.assertEqual(body["model"], "unlisted-model")
            self.configure(guard=False)
            for endpoint in GENERATIONS:
                self.post_rejected("/" + region + "/v1", endpoint,
                                   self.payload(endpoint, "unlisted-model"))

    def test_models_and_count_tokens_are_local_and_region_scoped(self):
        for base, region in BASES:
            with self.subTest(base=base):
                self.allowed_profiles = set()
                before = len(self.requests)
                response = self.client.get(base + "/models")
                self.assertEqual(response.status_code, 200, response.text)
                actual = {item["id"] for item in response.json()["data"]}
                expected = {item["id"] for profile, items in catalogs().items()
                            if profile.startswith(region + "-") for item in items}
                self.assertEqual(actual - {"auto"}, expected)
                response = self.client.post(base + "/messages/count_tokens", json=self.payload("messages"))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsInstance(response.json()["input_tokens"], int)
                self.assertGreaterEqual(response.json()["input_tokens"], 0)
                self.assertEqual(len(self.requests), before)

    def test_models_does_not_publish_missing_or_unfunded_product_catalog(self):
        self.configure(profiles=("cn-cli", "cn-work", "intl-cli"))
        response = self.client.get("/intl/v1/models")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({m["id"] for m in response.json()["data"]} - {"auto"},
                         {m["id"] for m in catalogs()["intl-cli"]})
        self.configure(balances={"intl-cli": None, "intl-work": 0})
        response = self.client.get("/intl/v1/models")
        self.assertIn(response.status_code, (200, 503), response.text)
        if response.status_code == 200:
            self.assertEqual(response.json()["data"], [])
        self.assertFalse(self.requests)

    def test_international_auto_maps_to_declared_default_model(self):
        tables = catalogs()
        tables["intl-cli"].append(model("default-model"))
        # Work has no equivalent default: shared unrelated IDs cannot make it eligible for auto.
        self.configure(tables=tables)
        for endpoint in GENERATIONS:
            for _ in range(3):
                _, body = self.post_ok("/intl/v1", endpoint, self.payload(endpoint, "auto"), {"intl-cli"})
                self.assertEqual(body["model"], "default-model")

    def test_international_shared_default_alias_rotates_and_obeys_mapped_cooldown(self):
        tables = catalogs()
        for profile in ("intl-cli", "intl-work"):
            tables[profile].append(model("default-model"))
        self.configure(tables=tables)
        seen = set()
        for _ in range(4):
            request, body = self.post_ok("/intl/v1", "chat/completions",
                                         self.payload(selected_model="auto"), {"intl-cli", "intl-work"})
            seen.add(request.headers["x-user-id"])
            self.assertEqual(body["model"], "default-model")
        self.assertEqual(seen, {"intl-cli", "intl-work"})
        for profile in seen:
            self.pool.note_status(self.entries[profile]["cm"], 429, model="default-model")
        for endpoint in GENERATIONS:
            self.post_rejected("/intl/v1", endpoint, self.payload(endpoint, "auto"), statuses=(429,))

    def test_domestic_auto_prefers_actual_product_declaration(self):
        tables = catalogs()
        tables["cn-work"].append(model("auto"))
        tables["cn-cli"].append(model("default-model"))
        self.configure(tables=tables)
        for base in ("/v1", "/cn/v1"):
            for endpoint in GENERATIONS:
                for _ in range(2):
                    _, body = self.post_ok(base, endpoint, self.payload(endpoint, "auto"), {"cn-work"})
                    self.assertEqual(body["model"], "auto")

    def test_domestic_cli_only_keeps_legacy_auto_without_cross_product_alias(self):
        self.configure(profiles=("cn-cli", "intl-cli", "intl-work"))
        for endpoint in GENERATIONS:
            _, body = self.post_ok("/v1", endpoint, self.payload(endpoint, "auto"), {"cn-cli"})
            self.assertEqual(body["model"], "auto")
        tables = catalogs()
        tables["cn-cli"].append(model("default-model"))
        tables["cn-work"].append(model("default"))
        self.configure(tables=tables)
        for endpoint in GENERATIONS:
            # WorkBuddy 未声明 Auto 时仍使用已有的 CLI 别名，不借用其 default。
            _, body = self.post_ok("/cn/v1", endpoint, self.payload(endpoint, "auto"), {"cn-cli"})
            self.assertEqual(body["model"], "auto")

    def test_system_is_only_added_when_needed_and_preserves_payload_and_parameters(self):
        for base, region in BASES:
            for endpoint in GENERATIONS:
                for system in (None, "Original caller system; preserve verbatim."):
                    with self.subTest(base=base, endpoint=endpoint, system=system):
                        payload = self.payload(endpoint, system=system)
                        original = deepcopy(payload)

                        async def original_json(request):
                            return payload

                        # Return the actual caller object, so in-place mutation cannot hide behind JSON encoding.
                        with patch.object(converter.Request, "json", new=original_json):
                            _, body = self.post_ok(base, endpoint, payload,
                                                   {region + "-cli", region + "-work"})
                        self.assertEqual(payload, original)
                        self.assertEqual(body["temperature"], 0.25)
                        self.assertEqual(body["top_p"], 0.75)
                        self.assertEqual(body["max_tokens"], 37)
                        self.assertEqual(body["model"], "shared-model")
                        messages = body["messages"]
                        users = [message for message in messages if message["role"] == "user"]
                        self.assertEqual(users, original.get("messages", original.get("input"))[-1:])
                        systems = [message for message in messages if message["role"] == "system"]
                        if system is not None:
                            self.assertEqual(systems, [{"role": "system", "content": system}])
                            self.assertEqual(messages[0], systems[0])
                        else:
                            self.assertEqual(messages[0]["role"], "system")
                            self.assertTrue(messages[0]["content"])
                            self.assertEqual(len(systems), 1)

    def test_late_system_is_not_overwritten_when_intl_requires_first_system(self):
        payload = self.payload()
        payload["messages"].append({"role": "system", "content": "Keep this late system intact."})
        original = deepcopy(payload)

        async def original_json(request):
            return payload

        with patch.object(converter.Request, "json", new=original_json):
            _, body = self.post_ok("/intl/v1", "chat/completions", payload, {"intl-cli", "intl-work"})
        self.assertEqual(payload, original)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertTrue(body["messages"][0]["content"])
        for message in original["messages"]:
            self.assertIn(message, body["messages"])

    def test_catalog_fetch_uses_selected_profile_host_and_product_identity(self):
        for domain, profile in tuple((domain, profile) for profile, domain in DOMAINS.items()) + (
                ("copilot.tencent.com", "cn-cli"),):
            with self.subTest(domain=domain, profile=profile):
                self.credentials[profile]["auth"]["domain"] = domain
                self.allowed_profiles = {profile}
                before = len(self.requests)
                result = credits.fetch_model_catalog(
                    "synthetic-access-" + profile, domain=domain, uid=profile,
                    enterprise_id="synthetic-enterprise")
                self.assertEqual(result, [model(profile + "-only")])
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1].url.path, "/v3/config")

    def test_refresh_uses_selected_profile_host_and_identity(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                value = self.credentials[profile]
                value["auth"]["expiresAt"] = 1
                path = self.root / (profile + ".info")
                path.write_text(json.dumps(value), encoding="utf-8")
                self.allowed_profiles = {profile}
                before = len(self.requests)
                headers = self.entries[profile]["cm"].get_headers()
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1].url.path, "/v2/plugin/auth/token/refresh")
                self.assertEqual(headers["X-User-Id"], profile)

    def test_copilot_domain_is_domestic_cli_for_chat_and_refresh(self):
        value = self.credentials["cn-cli"]
        value["auth"]["domain"] = "copilot.tencent.com"
        path = self.root / "cn-cli.info"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.configure()
        self.post_ok("/v1", "chat/completions", self.payload(selected_model="cn-cli-only"), {"cn-cli"})
        value["auth"]["expiresAt"] = 1
        path.write_text(json.dumps(value), encoding="utf-8")
        before = len(self.requests)
        self.entries["cn-cli"]["cm"].get_headers()
        self.assertEqual(len(self.requests), before + 1)
        self.assertEqual(self.requests[-1].url.path, "/v2/plugin/auth/token/refresh")


if __name__ == "__main__":
    unittest.main()
