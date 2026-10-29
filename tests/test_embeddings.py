#!/usr/bin/env python3
"""Codebase embedding contracts, public aliases, routing and fallback."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import base64
import json
import struct
import unittest
from unittest.mock import patch

import httpx

import converter
from app import embeddings, model_policy
from app.control_store import ControlStore
from app.gateway_management import Management
from tests import test_region_routing as fixtures


def vector(value=0.0):
    return [float(value)] + [0.0] * (embeddings.DIMENSIONS - 1)


def embedding_payload(count=2, *, codebase=False):
    usage = {"prompt_tokens": 0, "total_tokens": 0, "credit": 0} if codebase else {
        "prompt_tokens": 7, "total_tokens": 7, "credit": 0.001}
    return {"object": "list", "model": "model2" if codebase else "bge-m3",
            "data": [{"object": "embedding", "index": index, "embedding": vector(index)}
                     for index in range(count)], "usage": usage}


class EmbeddingContractTests(unittest.TestCase):
    def test_builtin_public_alias_and_codebase_names(self):
        for source in (embeddings.MODEL_ID, embeddings.PUBLIC_MODEL_ID):
            rule = model_policy.default_rule(source)
            self.assertEqual(rule["public_id"], "baai/bge-m3")
            self.assertEqual(rule["upstream_id"], "bge-m3")
        self.assertEqual(model_policy.source_for({}, "baai/bge-m3"), "bge-m3")
        self.assertEqual(embeddings.codebase_model("bge-m3"), "model2")
        self.assertEqual(embeddings.codebase_model(embeddings.QWEN_MODEL_ID), embeddings.QWEN_MODEL_ID)
        self.assertEqual(embeddings.is_embedding_model(embeddings.QWEN_MODEL_ID), True)

    def test_openai_input_shapes(self):
        cases = (("hello", 1), (["hello", "world"], 2), ([1, 2, 3], 1), ([[1], [2, 3]], 2))
        for value, count in cases:
            with self.subTest(value=value):
                body, encoding, actual = embeddings.request_body(
                    {"input": value, "dimensions": 1024}, "bge-m3")
                self.assertEqual(body, {"model": "bge-m3", "input": value, "encoding_format": "float"})
                self.assertEqual(encoding, "float")
                self.assertEqual(actual, count)
        for payload in ({}, {"input": ""}, {"input": []}, {"input": ["ok", 1]},
                        {"input": [True]}, {"input": [[]]}, {"input": "x", "encoding_format": "hex"},
                        {"input": "x", "dimensions": 512}, {"input": "x", "user": 1}):
            with self.subTest(payload=payload), self.assertRaises(embeddings.EmbeddingRequestError):
                embeddings.request_body(payload, "bge-m3")

    def test_reply_order_base64_and_usage(self):
        payload = embedding_payload()
        payload["data"].reverse()
        result = embeddings.parse_reply(payload, "baai/bge-m3", "float", 2)
        self.assertEqual([item["index"] for item in result["data"]], [0, 1])
        self.assertEqual(result["model"], "baai/bge-m3")
        self.assertEqual(result["usage"], {"prompt_tokens": 7, "total_tokens": 7, "credit": 0.001})
        zero = embeddings.parse_reply(embedding_payload(codebase=True), "baai/bge-m3", "float", 2)
        self.assertEqual(zero["usage"], {"prompt_tokens": 0, "total_tokens": 0, "credit": 0})

        encoded = embeddings.parse_reply(payload, "baai/bge-m3", "base64", 2)
        raw = base64.b64decode(encoded["data"][1]["embedding"], validate=True)
        self.assertEqual(len(raw), embeddings.DIMENSIONS * 4)
        self.assertEqual(struct.unpack("<2f", raw[:8]), (1.0, 0.0))

        upstream_base64 = embedding_payload(1)
        upstream_base64["data"][0]["embedding"] = base64.b64encode(
            struct.pack(f"<{embeddings.DIMENSIONS}f", *vector(0.25))).decode()
        decoded = embeddings.parse_reply(upstream_base64, "baai/bge-m3", "float", 1)
        self.assertEqual(decoded["data"][0]["embedding"][0], 0.25)

    def test_bad_replies_are_rejected(self):
        for mutate in (
                lambda body: body.update(data=[]),
                lambda body: body["data"][0].update(index=9),
                lambda body: body["data"][0].update(embedding=[0.0]),
                lambda body: body["data"][0].update(embedding="not-base64"),
                lambda body: body["data"][0].update(embedding=[float("nan")] * 1024)):
            payload = embedding_payload()
            mutate(payload)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                embeddings.parse_reply(payload, "baai/bge-m3", "float", 2)
        with self.assertRaises(embeddings.EmbeddingUpstreamError) as caught:
            embeddings.parse_reply({"code": 10040, "msg": "unavailable"}, "baai/bge-m3", "float", 1)
        self.assertEqual(caught.exception.upstream_code, 10040)


class EmbeddingRoutingTests(fixtures.RegionRoutingTests):
    def setUp(self):
        super().setUp()
        self.allowed_profiles = {"cn-cli", "cn-work"}
        self.embedding_requests = []
        self.embedding_replies = []
        self.enterContext(patch.object(converter, "_log", side_effect=lambda *args, **kwargs: None))

    @staticmethod
    def input_items(raw):
        if isinstance(raw, str) or (isinstance(raw, list) and all(type(value) is int for value in raw)):
            return [raw]
        return raw

    def handle_upstream(self, request):
        if request.url.path not in ("/codebase/openai/v1/embeddings", "/v2/embeddings"):
            return super().handle_upstream(request)
        uid = request.headers.get("x-user-id")
        self.assertIn(uid, self.allowed_profiles)
        profile = self.account_profiles[uid]
        self.assertIn(profile, embeddings.PROFILES)
        codebase = request.url.path.startswith("/codebase/")
        expected_host = httpx.URL(converter.PROFILE_ENDPOINTS[profile]).host if codebase else "www.workbuddy.cn"
        self.assertEqual(request.url.host, expected_host)
        self.assertEqual(request.headers["authorization"], "Bearer synthetic-access-" + uid)
        body = json.loads(request.content)
        self.embedding_requests.append((request, body))
        if self.embedding_replies:
            status, payload = self.embedding_replies.pop(0)
        else:
            payload = embedding_payload(len(self.input_items(body["input"])), codebase=codebase)
            status = 200
        return httpx.Response(status, json=payload)

    def post(self, value, **extra):
        return self.client.post("/v1/embeddings", json={"model": "baai/bge-m3", "input": value, **extra})

    def install_policy(self):
        store = ControlStore(self.root / f"embedding-policy-{self.fixture_sequence}.sqlite3")
        self.addCleanup(store.close)
        self.enterContext(patch.dict(converter.CONFIG, {"control_store": store}))
        converter.invalidate_model_table()
        return store

    def test_public_catalog_and_codebase_batch_roundtrip(self):
        models = self.client.get("/v1/models")
        self.assertEqual(models.status_code, 200)
        ids = {item["id"] for item in models.json()["data"]}
        self.assertIn("baai/bge-m3", ids)
        self.assertIn(embeddings.QWEN_MODEL_ID, ids)
        self.assertNotIn("bge-m3", ids)
        item = next(item for item in models.json()["data"] if item["id"] == "baai/bge-m3")
        self.assertTrue(item["embedding"])

        response = self.post(["first", "second"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "baai/bge-m3")
        self.assertEqual(len(response.json()["data"]), 2)
        self.assertEqual(response.json()["usage"], {"prompt_tokens": 0, "total_tokens": 0, "credit": 0})
        request, body = self.embedding_requests[-1]
        self.assertEqual(body, {"model": "model2", "input": ["first", "second"], "encoding_format": "float"})
        self.assertEqual(request.url.path, "/codebase/openai/v1/embeddings")

    def test_qwen_uses_its_direct_codebase_name(self):
        response = self.post("hello", model=embeddings.QWEN_MODEL_ID)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], embeddings.QWEN_MODEL_ID)
        request, body = self.embedding_requests[-1]
        self.assertEqual(request.url.path, "/codebase/openai/v1/embeddings")
        self.assertEqual(body["model"], embeddings.QWEN_MODEL_ID)

    def test_token_input_and_base64_response(self):
        response = self.post([[1, 2], [3]], encoding_format="base64", dimensions=1024)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["data"]), 2)
        raw = base64.b64decode(response.json()["data"][0]["embedding"], validate=True)
        self.assertEqual(len(raw), embeddings.DIMENSIONS * 4)
        self.assertEqual(self.embedding_requests[-1][1]["input"], [[1, 2], [3]])
        self.assertEqual(self.embedding_requests[-1][1]["encoding_format"], "float")

    def test_old_name_wrong_endpoint_and_invalid_input_are_rejected(self):
        old = self.post("hello", model="bge-m3")
        self.assertEqual(old.status_code, 404)
        self.assertEqual(old.json()["error"]["code"], "model_renamed")
        chat = self.client.post("/v1/chat/completions", json={
            "model": "baai/bge-m3", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(chat.status_code, 400)
        self.assertEqual(chat.json()["error"]["code"], "embedding_model_not_chat")
        wrong = self.post("hello", model="shared-model")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json()["error"]["code"], "model_not_embedding")
        invalid = self.post([])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["param"], "input")
        self.assertEqual(self.embedding_requests, [])

    def test_binding_disable_and_rate_probe_guard(self):
        store = self.install_policy()
        rule = {**model_policy.default_rule("bge-m3"),
                "credential_ids": [self.entries["cn-work"]["account_key"]]}
        store.update_model("bge-m3", rule, 0, known_models=["bge-m3"])
        response = self.post("hello")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.embedding_requests[-1][0].headers["x-user-id"], "cn-work")
        store.update_model("bge-m3", {**rule, "enabled": False}, 1, known_models=["bge-m3"])
        disabled = self.post("hello")
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(len(self.embedding_requests), 1)
        with self.assertRaisesRegex(ValueError, "不支持聊天倍率探测"):
            Management(converter).admin_rate_probe_model("bge-m3")

    def test_missing_codebase_route_falls_back_to_paid_bge(self):
        self.embedding_replies = [(404, {"error": {"message": "route not found"}}),
                                  (200, embedding_payload(1))]
        response = self.post("hello")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([request.url.path for request, _body in self.embedding_requests],
                         ["/codebase/openai/v1/embeddings", "/v2/embeddings"])
        self.assertEqual(self.embedding_requests[0][1]["model"], "model2")
        self.assertEqual(self.embedding_requests[1][1]["model"], "bge-m3")
        self.assertEqual(response.json()["usage"]["total_tokens"], 7)

    def test_qwen_has_no_paid_fallback(self):
        self.embedding_replies = [(404, {"error": {"message": "route not found"}}),
                                  (200, embedding_payload(1))]
        response = self.post("hello", model=embeddings.QWEN_MODEL_ID)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.embedding_requests), 1)

    def test_authentication_failure_replays_on_another_account(self):
        self.embedding_replies = [(401, {"error": {"message": "expired"}}),
                                  (200, embedding_payload(1, codebase=True))]
        with patch.dict(converter.CONFIG, {"failover_max": 1}):
            response = self.post("hello")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.embedding_requests), 2)
        self.assertNotEqual(self.embedding_requests[0][0].headers["x-user-id"],
                            self.embedding_requests[1][0].headers["x-user-id"])

    def test_server_and_business_failures_are_not_replayed(self):
        self.embedding_replies = [(500, {"error": {"message": "unavailable"}}), (200, embedding_payload(1))]
        response = self.post("hello")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(self.embedding_requests), 1)

        self.embedding_replies = [(200, {"code": 10040, "msg": "unavailable"}), (200, embedding_payload(1))]
        response = self.post("hello")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["upstream_code"], 10040)
        self.assertEqual(len(self.embedding_requests), 2)

    def test_embedding_has_independent_sixteen_way_capacity(self):
        with patch.dict(converter.CONFIG, {"max_inflight_per_account": 1}), \
                patch.object(self.pool._capacity, "acquire", wraps=self.pool._capacity.acquire) as acquire:
            cred, _headers = converter._embedding_cred_for("bge-m3")
            converter.release_credential(cred)
        self.assertEqual(acquire.call_args.args[1], 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
