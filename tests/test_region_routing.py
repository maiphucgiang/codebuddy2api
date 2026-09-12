"""原 /v1 接口自动地域/产品路由回归：合成凭据，httpx 全部由 MockTransport 接管。

运行：.venv/bin/python -B -m unittest -v tests/test_region_routing.py
不启动维护线程，不读取本机 auth/.env，不依赖在线目录或真实账号。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

from copy import deepcopy
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import client_profiles
import converter
from app import credits


PROFILES = ("cn-cli", "cn-work", "intl-cli", "intl-work")
DOMAINS = {
    "cn-cli": "www.codebuddy.cn", "cn-work": "www.workbuddy.cn",
    "intl-cli": "www.codebuddy.ai", "intl-work": "www.workbuddy.ai",
}
HOSTS = dict(DOMAINS, **{"cn-cli": "copilot.tencent.com"})
GENERATIONS = ("chat/completions", "responses", "messages")


def model(identifier, credits=None):
    value = {"id": identifier, "name": identifier, "supportsToolCall": True,
             "supportsImages": True, "credits": {"input": 1, "output": 2}}
    if credits is not None:
        value["credits"] = credits
    return value


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
            "model_catalogs": {}, "account_catalogs": None, "model_cache": None, "model_guard": True,
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
        self.account_profiles = {}
        for profile in PROFILES:
            self.add_account(profile, profile)
        transport = httpx.MockTransport(self.handle_upstream)
        real_sync, real_async = httpx.Client, httpx.AsyncClient

        def sync_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_sync(*args, **kwargs)

        def async_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async(*args, **kwargs)

        # Cover chat, token refresh and catalog requests, not just the generation client.
        self.enterContext(patch.object(httpx, "Client", side_effect=sync_client))
        self.enterContext(patch.object(httpx, "AsyncClient", side_effect=async_client))
        self.configure()
        self.client = self.enterContext(TestClient(converter.app))

    def add_account(self, uid, profile):
        now = time.time()
        value = {"account": {"uid": uid, "enterpriseId": "synthetic-enterprise"},
                 "auth": {"domain": DOMAINS[profile], "accessToken": "synthetic-access-" + uid,
                          "refreshToken": "synthetic-refresh-" + uid,
                          "expiresAt": (now + 86400) * 1000, "lastRefreshTime": now * 1000}}
        self.account_profiles[uid] = profile
        self.credentials[uid] = value
        (self.root / (uid + ".info")).write_text(json.dumps(value), encoding="utf-8")

    def configure(self, profiles=PROFILES, tables=None, balances=None, guard=True):
        self.fixture_sequence += 1
        self.pool = converter.CredentialPool([self.root / (p + ".info") for p in profiles])
        self.entries = {entry["cm"].summary()["uid"]: entry for entry in self.pool.entries()}
        self.ledger = credits.CreditLedger(self.root / f"ledger-{self.fixture_sequence}.json")
        self.pool.set_ledger(self.ledger)
        for uid, entry in self.entries.items():
            balance = (balances or {}).get(uid, 100)
            if balance is not None:
                self.ledger.update_credits(entry["id"], {
                    "credits": balance, "intl": self.account_profiles[uid].startswith("intl-"),
                    "segments": [], "soonest_expiry": None,
                })
        converter.CONFIG.update(cred_pool=self.pool, ledger=self.ledger,
                                model_catalogs=deepcopy(catalogs() if tables is None else tables),
                                model_guard=guard, model_cache=None, account_catalogs=None)
        converter.invalidate_model_table()

    def account_catalogs(self, tables):
        converter.CONFIG["account_catalogs"] = {
            self.entries[uid]["account_key"]: {"profile": self.account_profiles[uid], "models": items}
            for uid, items in tables.items()
        }
        converter.invalidate_model_table()

    def handle_upstream(self, request):
        self.requests.append(request)
        uid = request.headers.get("x-user-id")
        self.assertIn(uid, self.allowed_profiles, "unexpected selected UID")
        profile = self.account_profiles[uid]
        self.assertEqual(request.url.scheme, "https")
        self.assertEqual(request.url.host, HOSTS[profile])
        self.assertEqual(request.headers["x-domain"], self.credentials[uid]["auth"]["domain"])
        self.assertEqual(request.headers["authorization"], "Bearer synthetic-access-" + uid)
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
            self.assertEqual(request.headers["x-refresh-token"], "synthetic-refresh-" + uid)
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "synthetic-access-" + uid, "expiresIn": 86400}})
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

    def post_ok(self, endpoint, payload, allowed):
        self.allowed_profiles = set(allowed)
        before = len(self.requests)
        response = self.client.post("/v1/" + endpoint, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("ok", response.text)
        if payload.get("stream"):
            self.assertIn({"chat/completions": "[DONE]", "responses": "response.completed",
                           "messages": "message_stop"}[endpoint], response.text)
        else:
            data = response.json()
            if endpoint == "chat/completions":
                self.assertEqual(data["object"], "chat.completion")
                self.assertEqual(data["choices"][0]["message"]["content"], "ok")
            elif endpoint == "responses":
                self.assertEqual(data["object"], "response")
                self.assertEqual(data["output"][0]["content"][0]["text"], "ok")
            else:
                self.assertEqual(data["type"], "message")
                self.assertEqual(data["role"], "assistant")
                self.assertEqual(data["content"][0]["text"], "ok")
        self.assertEqual(len(self.requests), before + 1, response.text)
        request = self.requests[-1]
        return request, json.loads(request.content)

    def post_rejected(self, endpoint, payload, statuses=(404, 503)):
        self.allowed_profiles = set()
        before = len(self.requests)
        response = self.client.post("/v1/" + endpoint, json=payload)
        self.assertIn(response.status_code, statuses, response.text)
        self.assertEqual(len(self.requests), before, "local rejection must not call any upstream")
        return response

    def test_original_generation_apis_preserve_shapes_for_all_four_profiles(self):
        for profile in PROFILES:
            for endpoint in GENERATIONS:
                for stream in (False, True):
                    with self.subTest(profile=profile, endpoint=endpoint, stream=stream):
                        selected = profile + "-only"
                        _, body = self.post_ok(endpoint, self.payload(endpoint, selected, stream=stream),
                                               {profile})
                        self.assertEqual(body["model"], selected)
                        self.assertTrue(body["stream"])

    def test_regional_prefixes_are_not_public_routes(self):
        for prefix in ("/cn", "/intl"):
            for endpoint in (*GENERATIONS, "messages/count_tokens", "models"):
                with self.subTest(prefix=prefix, endpoint=endpoint):
                    path = prefix + "/v1/" + endpoint
                    response = (self.client.get(path) if endpoint == "models" else
                                self.client.post(path, json=self.payload()))
                    self.assertEqual(response.status_code, 404, response.text)
        self.assertFalse(self.requests)

    def test_shared_model_rotates_across_regions_and_products(self):
        for endpoint in GENERATIONS:
            seen = set()
            for _ in range(8):
                request, body = self.post_ok(endpoint, self.payload(endpoint), PROFILES)
                self.assertEqual(body["model"], "shared-model")
                seen.add(request.headers["x-user-id"])
            self.assertEqual(seen, set(PROFILES))

    def test_zero_multiplier_model_prefers_free_accounts(self):
        free = "intl-cli"
        tables = catalogs()
        tables[free] = [dict(model("shared-model"), credits="x0.00"), model(free + "-exclusive")]
        for profile in PROFILES:
            if profile != free:
                tables[profile] = [dict(model("shared-model"), credits="x0.03"), model(profile + "-exclusive")]
        self.configure(tables=tables)
        for endpoint in GENERATIONS:
            for _ in range(6):
                request, _ = self.post_ok(endpoint, self.payload(endpoint), {free})
                self.assertEqual(request.headers["x-user-id"], free)
            # 计费账号只在免费账号不可用时兜底。
            self.pool.note_status(self.entries[free]["cm"], 429, model="shared-model")
            request, _ = self.post_ok(endpoint, self.payload(endpoint), set(PROFILES) - {free})
            self.assertNotEqual(request.headers["x-user-id"], free)
            # 冷却换绑后该会话黏在计费账号上；解除冷却应重绑回免费账号。
            self.pool.note_status(self.entries[free]["cm"], 429, model="other-model")
            with self.pool._lock:
                self.pool._model_fail.clear()
            request, _ = self.post_ok(endpoint, self.payload(endpoint), {free})
            self.assertEqual(request.headers["x-user-id"], free)

    def test_zero_multiplier_model_requires_declared_credits_field(self):
        tables = catalogs()
        # 只给 intl-cli 声明零倍率，其余账号目录仍是不带 credits 字段的同名模型。
        tables["intl-cli"] = [dict(model("shared-model"), credits="x0.00"), model("intl-cli-exclusive")]
        tables["intl-work"] = [model("shared-model"), model("intl-work-exclusive")]
        self.configure(tables=tables)
        self.assertTrue(self.pool._model_free(self.entries["intl-cli"], "shared-model"))
        self.assertFalse(self.pool._model_free(self.entries["intl-work"], "shared-model"))
        for _ in range(4):
            request, _ = self.post_ok("chat/completions", self.payload(), {"intl-cli"})
            self.assertEqual(request.headers["x-user-id"], "intl-cli")
        # 免费账号不可用后，未声明 credits 的账号按普通轮询调度，不会被误判为免费。
        self.pool.note_status(self.entries["intl-cli"]["cm"], 429, model="shared-model")
        seen = set()
        for _ in range(6):
            request, _ = self.post_ok("chat/completions", self.payload(), set(PROFILES) - {"intl-cli"})
            seen.add(request.headers["x-user-id"])
        self.assertEqual(seen, {"cn-cli", "cn-work", "intl-work"})

    def test_free_account_sticky_rebinds_only_while_it_stays_best(self):
        free = "intl-cli"
        tables = catalogs()
        for profile in PROFILES:
            tables[profile] = [dict(model("shared-model"), credits="x0.00" if profile == free else "x0.03"),
                               model(profile + "-exclusive")]
        self.configure(tables=tables)
        payload = self.payload()
        request, _ = self.post_ok("chat/completions", payload, {free})
        self.assertEqual(request.headers["x-user-id"], free)
        self.post_ok("chat/completions", payload, {free})  # 黏绑保持
        # 免费账号改为计费后，旧黏绑必须重绑到仍然免费的账号。
        tables[free] = [dict(model("shared-model"), credits="x0.03"), model(free + "-exclusive")]
        tables["cn-cli"] = [dict(model("shared-model"), credits="x0.00"), model("cn-cli-exclusive")]
        self.account_catalogs(tables)
        request, _ = self.post_ok("chat/completions", payload, {"cn-cli"})
        self.assertEqual(request.headers["x-user-id"], "cn-cli")

    def test_credits_multiplier_parser_accepts_official_forms(self):
        for value in ("x0.00", "x0.00 credits", "x0.0", "X0.00 CREDITS", " x0.00 ", "x0"):
            self.assertTrue(converter._free_multiplier(value), value)
        for value in ("x0.03", "x0.03 credits", "x0.34 credits", "", "credits", None, 0, {}, "x0.00x"):
            self.assertFalse(converter._free_multiplier(value), repr(value))

    def test_product_exclusive_models_only_rotate_between_supporting_regions(self):
        for endpoint in GENERATIONS:
            for product in ("cli", "work"):
                expected = {"cn-" + product, "intl-" + product}
                seen = set()
                for _ in range(4):
                    request, body = self.post_ok(endpoint,
                        self.payload(endpoint, product + "-exclusive"), expected)
                    self.assertEqual(body["model"], product + "-exclusive")
                    seen.add(request.headers["x-user-id"])
                self.assertEqual(seen, expected)

    def test_original_root_automatically_selects_international_only_models(self):
        for endpoint in GENERATIONS:
            for profile in ("intl-cli", "intl-work"):
                self.post_ok(endpoint, self.payload(endpoint, profile + "-only"), {profile})

    def test_wrong_region_or_product_sticky_is_automatically_rebound(self):
        for right in PROFILES:
            for wrong in set(PROFILES) - {right}:
                with self.subTest(right=right, wrong=wrong):
                    payload = self.payload(selected_model=right + "-only")
                    old_keys = set(self.pool._sticky)
                    self.post_ok("chat/completions", payload, {right})
                    keys = set(self.pool._sticky) - old_keys
                    self.assertEqual(len(keys), 1)
                    key = keys.pop()
                    self.pool._sticky[key] = (self.entries[wrong]["id"], time.time())
                    self.post_ok("chat/completions", payload, {right})
                    self.assertEqual(self.pool._sticky[key][0], self.entries[right]["id"])

    def test_model_change_rechecks_sticky_region_and_product_availability(self):
        payload = self.payload(selected_model="cn-cli-only")
        self.post_ok("chat/completions", payload, {"cn-cli"})
        payload["model"] = "intl-work-only"
        self.post_ok("chat/completions", payload, {"intl-work"})

    def test_repeated_payload_preserves_account_sticky_and_conversation_id(self):
        for endpoint in GENERATIONS:
            for profile in PROFILES:
                payload = self.payload(endpoint, profile + "-only", system="Keep this instruction.")
                first, _ = self.post_ok(endpoint, payload, {profile})
                repeat, _ = self.post_ok(endpoint, payload, {profile})
                self.assertTrue(first.headers["x-conversation-id"])
                self.assertEqual(first.headers["x-conversation-id"], repeat.headers["x-conversation-id"])
                self.assertEqual(first.headers["x-user-id"], repeat.headers["x-user-id"])

    def test_single_region_pool_uses_original_root_and_rejects_absent_sources(self):
        for present, absent in (("cn", "intl"), ("intl", "cn")):
            expected = {present + "-cli", present + "-work"}
            self.configure(profiles=tuple(expected))
            for endpoint in GENERATIONS:
                self.post_ok(endpoint, self.payload(endpoint), expected)
                self.post_rejected(endpoint, self.payload(endpoint, absent + "-cli-only"))

    def test_intl_requires_known_positive_balance_not_domestic_fallback(self):
        for balance in (None, 0, -1):
            with self.subTest(balance=balance):
                self.configure(balances={"intl-cli": balance, "intl-work": balance})
                for endpoint in GENERATIONS:
                    self.post_rejected(endpoint, self.payload(endpoint, "intl-cli-only"))
                    self.post_ok(endpoint, self.payload(endpoint), {"cn-cli", "cn-work"})

    def test_unknown_or_zero_balance_is_not_a_rotation_candidate(self):
        for unavailable in PROFILES:
            for balance in ((None, 0) if unavailable.startswith("intl-") else (0,)):
                self.configure(balances={unavailable: balance})
                expected = set(PROFILES) - {unavailable}
                for _ in range(6):
                    self.post_ok("chat/completions", self.payload(), expected)

    def test_unknown_or_empty_catalog_never_borrows_other_profile_models(self):
        for unavailable in PROFILES:
            for missing in (None, []):
                with self.subTest(profile=unavailable, catalog=missing):
                    tables = catalogs()
                    tables[unavailable] = missing
                    self.configure(tables=tables)
                    for endpoint in GENERATIONS:
                        self.post_rejected(endpoint, self.payload(endpoint, unavailable + "-only"))
                        self.post_ok(endpoint, self.payload(endpoint), set(PROFILES) - {unavailable})
        for missing in (None, []):
            self.configure(tables={profile: missing for profile in PROFILES})
            for endpoint in GENERATIONS:
                response = self.post_rejected(endpoint, self.payload(endpoint))
                if response.status_code == 503:
                    self.assertIn("retry-after", response.headers)

    def test_model_cooldown_rebinds_across_regions_then_fails_locally(self):
        payload = self.payload()
        remaining = set(PROFILES)
        while remaining:
            request, _ = self.post_ok("chat/completions", payload, remaining)
            selected = request.headers["x-user-id"]
            remaining.remove(selected)
            self.pool.note_status(self.entries[selected]["cm"], 429, model="shared-model")
        for endpoint in GENERATIONS:
            self.post_rejected(endpoint, self.payload(endpoint), statuses=(429,))
        # Model cooldown is per account and model, not an account-wide ban.
        for profile in PROFILES:
            self.post_ok("chat/completions", self.payload(selected_model=profile + "-only"), {profile})

    def test_sent_post_is_not_replayed_even_when_another_source_is_available(self):
        for status in (401, 429):
            for endpoint in GENERATIONS:
                self.configure()
                payload = self.payload(endpoint)
                self.allowed_profiles = set(PROFILES)
                self.response_status = status
                before = len(self.requests)
                try:
                    response = self.client.post("/v1/" + endpoint, json=payload)
                finally:
                    self.response_status = 200
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(len(self.requests), before + 1, "a sent POST must not be replayed")
                failed = self.requests[-1].headers["x-user-id"]
                self.post_ok(endpoint, payload, set(PROFILES) - {failed})

    def test_exclusive_model_429_cannot_fallback_to_unsupported_source(self):
        for profile in PROFILES:
            self.configure()
            payload = self.payload(selected_model=profile + "-only")
            self.allowed_profiles = {profile}
            self.response_status = 429
            before = len(self.requests)
            try:
                response = self.client.post("/v1/chat/completions", json=payload)
            finally:
                self.response_status = 200
            self.assertEqual(response.status_code, 429, response.text)
            self.assertEqual(len(self.requests), before + 1)
            self.post_rejected("chat/completions", payload, statuses=(429,))
            self.post_ok("chat/completions", self.payload(), PROFILES)

    def test_account_cooldown_rebinds_to_supported_other_region(self):
        for region in ("cn", "intl"):
            self.configure()
            remote = "intl" if region == "cn" else "cn"
            for profile in (region + "-cli", region + "-work"):
                self.pool.cooldown(self.entries[profile]["cm"], reason="synthetic cooldown")
            for endpoint in GENERATIONS:
                self.post_ok(endpoint, self.payload(endpoint), {remote + "-cli", remote + "-work"})
            for profile in (remote + "-cli", remote + "-work"):
                self.pool.cooldown(self.entries[profile]["cm"], reason="synthetic cooldown")
            for endpoint in GENERATIONS:
                self.post_rejected(endpoint, self.payload(endpoint), statuses=(503,))

    def test_unknown_model_has_no_source_in_multi_product_pool_even_without_guard(self):
        for guard in (True, False):
            self.configure(guard=guard)
            for endpoint in GENERATIONS:
                self.post_rejected(endpoint, self.payload(endpoint, "unlisted-model"))

    def test_models_is_merged_and_count_tokens_remains_local(self):
        response = self.client.get("/v1/models")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["object"], "list")
        actual = {item["id"] for item in response.json()["data"]}
        expected = {item["id"] for items in catalogs().values() for item in items}
        self.assertEqual(actual, expected | {"auto"})

    def test_models_exposes_credits_multiplier_per_profile(self):
        tables = catalogs()
        # 默认目录的 credits 是 {input, output} 对象（官方新版形态），只有字符串倍率可解析。
        tables["cn-cli"] = [model("shared-model", credits="x0.00"), model("cn-cli-only", credits="x0.03")]
        tables["intl-cli"] = [model("shared-model", credits="x0.34"), model("intl-cli-only", credits="x0.03")]
        self.configure(tables=tables)
        data = {item["id"]: item for item in self.client.get("/v1/models").json()["data"]}
        shared = data["shared-model"]
        self.assertEqual(shared["credits"], 0.0)  # 取各来源最小值
        # 只有给出可解析字符串倍率的来源进入分组；cn-work / intl-work 仍是对象形态，故不列出。
        self.assertEqual(shared["credits_by_profile"], {"cn-cli": 0.0, "intl-cli": 0.34})
        self.assertEqual(data["cn-cli-only"]["credits"], 0.03)
        self.assertEqual(data["cn-cli-only"]["credits_by_profile"], {"cn-cli": 0.03})
        # 标准 OpenAI 字段必须保留。
        for item in data.values():
            self.assertEqual(item["object"], "model")
            self.assertIsInstance(item["created"], int)
            self.assertEqual(item["owned_by"], "codebuddy")

    def test_models_omits_unknown_and_unusable_multipliers(self):
        tables = catalogs()
        tables["cn-cli"] = [model("shared-model", credits="x0.03"),
                            dict(model("cn-cli-only"), credits=""),
                            dict(model("cn-cli-only-2"), credits="not-a-multiplier")]
        self.configure(tables=tables)
        data = {item["id"]: item for item in self.client.get("/v1/models").json()["data"]}
        self.assertEqual(data["shared-model"]["credits"], 0.03)  # 只有 cn-cli 给出可解析倍率
        self.assertEqual(data["shared-model"]["credits_by_profile"], {"cn-cli": 0.03})
        self.assertIsNone(data["cn-cli-only"]["credits"])
        self.assertEqual(data["cn-cli-only"]["credits_by_profile"], {})
        for selected in ("shared-model", "intl-cli-only"):
            response = self.client.post("/v1/messages/count_tokens",
                                        json=self.payload("messages", selected))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsInstance(response.json()["input_tokens"], int)
            self.assertGreaterEqual(response.json()["input_tokens"], 0)
        self.assertFalse(self.requests)

    def test_models_does_not_publish_missing_or_unfunded_product_catalog(self):
        for profiles, balances, eligible in (
                (("cn-cli", "cn-work", "intl-cli"), {}, ("cn-cli", "cn-work", "intl-cli")),
                (PROFILES, {"intl-cli": None, "intl-work": 0}, ("cn-cli", "cn-work")),
                (("intl-cli", "intl-work"), {"intl-cli": None, "intl-work": 0}, ())):
            self.configure(profiles=profiles, balances=balances)
            response = self.client.get("/v1/models")
            self.assertIn(response.status_code, (200, 503), response.text)
            if eligible:
                self.assertEqual(response.status_code, 200, response.text)
            if response.status_code == 200:
                expected = {m["id"] for profile in eligible for m in catalogs()[profile]}
                self.assertEqual({m["id"] for m in response.json()["data"]} - {"auto"}, expected)
                if not eligible:
                    self.assertEqual(response.json()["data"], [])
        self.assertFalse(self.requests)

    def test_same_profile_accounts_cannot_borrow_catalog_or_balance(self):
        second = "intl-cli-second"
        self.add_account(second, "intl-cli")
        for balance in (None, 0, 100):
            for missing in (None, [], [model("second-only")]):
                with self.subTest(balance=balance, catalog=missing):
                    self.configure(profiles=("intl-cli", second), balances={"intl-cli": balance})
                    self.account_catalogs({"intl-cli": [model("first-only")], second: missing})
                    if balance == 100:
                        self.post_ok("chat/completions", self.payload(selected_model="first-only"), {"intl-cli"})
                    else:
                        self.post_rejected("chat/completions", self.payload(selected_model="first-only"))
                    if missing:
                        self.post_ok("chat/completions", self.payload(selected_model="second-only"), {second})
                    else:
                        self.post_rejected("chat/completions", self.payload(selected_model="second-only"))
                    response = self.client.get("/v1/models")
                    self.assertIn(response.status_code, (200, 503), response.text)
                    if response.status_code == 200:
                        expected = ({"first-only"} if balance == 100 else set()) | ({"second-only"} if missing else set())
                        self.assertEqual({m["id"] for m in response.json()["data"]}, expected)

    def test_auto_maps_to_each_accounts_declared_default_and_rotates(self):
        tables = catalogs()
        tables["cn-work"].append(model("auto"))
        for profile in ("intl-cli", "intl-work"):
            tables[profile].append(model("default-model"))
        self.configure(tables=tables)
        for endpoint in GENERATIONS:
            seen = set()
            for _ in range(8):
                request, body = self.post_ok(endpoint, self.payload(endpoint, "auto"), PROFILES)
                selected = request.headers["x-user-id"]
                seen.add(selected)
                self.assertEqual(body["model"], "default-model" if selected.startswith("intl-") else "auto")
            self.assertEqual(seen, set(PROFILES))

    def test_international_auto_requires_declared_default_and_mapped_cooldown(self):
        tables = catalogs()
        tables["intl-cli"].append(model("default-model"))
        self.configure(profiles=("intl-cli", "intl-work"), tables=tables)
        for endpoint in GENERATIONS:
            _, body = self.post_ok(endpoint, self.payload(endpoint, "auto"), {"intl-cli"})
            self.assertEqual(body["model"], "default-model")
        self.pool.note_status(self.entries["intl-cli"]["cm"], 429, model="default-model")
        for endpoint in GENERATIONS:
            self.post_rejected(endpoint, self.payload(endpoint, "auto"), statuses=(429,))

    def test_auto_never_borrows_other_accounts_default(self):
        for profile in PROFILES:
            second = profile + "-second"
            self.add_account(second, profile)
            self.configure(profiles=(profile, second))
            declared = "default-model" if profile.startswith("intl-") else "auto"
            self.account_catalogs({profile: [model(declared)], second: []})
            for endpoint in GENERATIONS:
                _, body = self.post_ok(endpoint, self.payload(endpoint, "auto"), {profile})
                self.assertEqual(body["model"], declared)

    def test_auto_rejects_unknown_empty_and_unrelated_work_or_intl_catalogs(self):
        for profile in PROFILES:
            for items in (None, [], [model("unrelated")]):
                if profile == "cn-cli" and items:
                    continue  # Legacy CLI auto is valid only with a known nonempty catalog.
                self.configure(profiles=(profile,))
                self.account_catalogs({profile: items})
                for endpoint in GENERATIONS:
                    self.post_rejected(endpoint, self.payload(endpoint, "auto"))

    def test_domestic_cli_retains_legacy_auto_without_borrowing_work_default(self):
        tables = catalogs()
        tables["cn-work"].append(model("default"))
        self.configure(tables=tables)
        for endpoint in GENERATIONS:
            _, body = self.post_ok(endpoint, self.payload(endpoint, "auto"), {"cn-cli"})
            self.assertEqual(body["model"], "auto")

    def test_system_is_only_added_when_needed_and_preserves_payload_and_parameters(self):
        for profile in PROFILES:
            for endpoint in GENERATIONS:
                for system in (None, "Original caller system; preserve verbatim."):
                    with self.subTest(profile=profile, endpoint=endpoint, system=system):
                        payload = self.payload(endpoint, profile + "-only", system=system)
                        original = deepcopy(payload)

                        async def original_json(request):
                            return payload

                        # Use the actual caller object to catch in-place mutation.
                        with patch.object(converter.Request, "json", new=original_json):
                            _, body = self.post_ok(endpoint, payload, {profile})
                        self.assertEqual(payload, original)
                        self.assertEqual(body["temperature"], 0.25)
                        self.assertEqual(body["top_p"], 0.75)
                        self.assertEqual(body["max_tokens"], 37)
                        self.assertEqual(body["model"], profile + "-only")
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
        payload = self.payload(selected_model="intl-cli-only")
        payload["messages"].append({"role": "system", "content": "Keep this late system intact."})
        original = deepcopy(payload)

        async def original_json(request):
            return payload

        with patch.object(converter.Request, "json", new=original_json):
            _, body = self.post_ok("chat/completions", payload, {"intl-cli"})
        self.assertEqual(payload, original)
        self.assertEqual(body["messages"][0], original["messages"][-1])
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
        self.post_ok("chat/completions", self.payload(selected_model="cn-cli-only"), {"cn-cli"})
        value["auth"]["expiresAt"] = 1
        path.write_text(json.dumps(value), encoding="utf-8")
        before = len(self.requests)
        self.entries["cn-cli"]["cm"].get_headers()
        self.assertEqual(len(self.requests), before + 1)
        self.assertEqual(self.requests[-1].url.path, "/v2/plugin/auth/token/refresh")


if __name__ == "__main__":
    unittest.main()
