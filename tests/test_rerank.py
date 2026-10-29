#!/usr/bin/env python3
"""Codebase reranker contracts, routing and no-score response behavior."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import unittest
from unittest.mock import patch

import httpx

import converter
from app import model_policy, rerank
from app.control_store import ControlStore
from app.gateway_management import Management
from tests import test_region_routing as fixtures


def rerank_payload(count=2):
    indices = list(range(count - 1, -1, -1))
    return {"output": {"results": [{"index": index} for index in indices]},
            "request_id": "0" * 32, "usage": {"total_tokens": 0}}


class RerankContractTests(unittest.TestCase):
    def test_request_and_no_score_reply(self):
        body, documents, top_n, return_documents = rerank.request_body({
            "query": "capital of France", "documents": ["Berlin", "Paris", "London"],
            "top_n": 2, "return_documents": True})
        self.assertEqual(body["model"], "reranker")
        self.assertEqual(top_n, 2)
        result = rerank.parse_reply(rerank_payload(3), documents, top_n, return_documents)
        self.assertEqual(result["model"], "reranker")
        self.assertEqual([item["index"] for item in result["results"]], [2, 1])
        self.assertEqual(result["results"][0]["document"], {"text": "London"})
        self.assertNotIn("relevance_score", json.dumps(result))
        self.assertEqual(result["usage"], {"total_tokens": 0})

    def test_request_validation(self):
        for payload in ({}, {"query": " ", "documents": ["x"]}, {"query": "x", "documents": []},
                        {"query": "x", "documents": ["x"] * 101},
                        {"query": "x", "documents": [1]}, {"query": "x", "documents": ["x"], "top_n": 0},
                        {"query": "x", "documents": ["x"], "top_n": 2},
                        {"query": "x", "documents": ["x"], "return_documents": 1}):
            with self.subTest(payload=payload), self.assertRaises(rerank.RerankRequestError):
                rerank.request_body(payload)

    def test_bad_replies_are_rejected(self):
        for payload in ({}, {"output": {"results": []}},
                        {"output": {"results": [{"index": 4}]}},
                        {"output": {"results": [{"index": 0}, {"index": 0}]}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                rerank.parse_reply(payload, ["a", "b"], 2, False)
        with self.assertRaises(rerank.RerankUpstreamError) as caught:
            rerank.parse_reply({"code": 10001, "msg": "empty documents"}, ["a"], 1, False)
        self.assertEqual(caught.exception.upstream_code, 10001)


class RerankRoutingTests(fixtures.RegionRoutingTests):
    def setUp(self):
        super().setUp()
        self.allowed_profiles = {"cn-cli", "cn-work"}
        self.rerank_requests = []
        self.rerank_replies = []
        self.enterContext(patch.object(converter, "_log", side_effect=lambda *args, **kwargs: None))

    def handle_upstream(self, request):
        if request.url.path != "/codebase/openai/v1/rerank":
            return super().handle_upstream(request)
        uid = request.headers.get("x-user-id")
        self.assertIn(uid, self.allowed_profiles)
        profile = self.account_profiles[uid]
        self.assertIn(profile, ("cn-cli", "cn-work"))
        self.assertEqual(request.url.host, httpx.URL(converter.PROFILE_ENDPOINTS[profile]).host)
        self.assertEqual(request.headers["authorization"], "Bearer synthetic-access-" + uid)
        body = json.loads(request.content)
        self.rerank_requests.append((request, body))
        if self.rerank_replies:
            status, payload = self.rerank_replies.pop(0)
        else:
            status, payload = 200, rerank_payload(len(body["documents"]))
        return httpx.Response(status, json=payload)

    def post(self, documents, **extra):
        return self.client.post("/v1/rerank", json={
            "model": "reranker", "query": "capital of France", "documents": documents, **extra})

    def install_policy(self):
        store = ControlStore(self.root / f"rerank-policy-{self.fixture_sequence}.sqlite3")
        self.addCleanup(store.close)
        self.enterContext(patch.dict(converter.CONFIG, {"control_store": store}))
        converter.invalidate_model_table()
        return store

    def test_public_catalog_and_roundtrip(self):
        models = self.client.get("/v1/models")
        item = next(item for item in models.json()["data"] if item["id"] == "reranker")
        self.assertTrue(item["rerank"])
        response = self.post(["Berlin", "Paris", "London"], top_n=2, return_documents=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "reranker")
        self.assertEqual([item["index"] for item in response.json()["results"]], [2, 1])
        self.assertEqual(response.json()["results"][0]["document"], {"text": "London"})
        self.assertNotIn("relevance_score", response.text)
        request, body = self.rerank_requests[-1]
        self.assertEqual(request.url.path, "/codebase/openai/v1/rerank")
        self.assertEqual(body["model"], "reranker")
        self.assertEqual(body["top_n"], 2)

    def test_default_model_and_validation(self):
        response = self.client.post("/v1/rerank", json={"query": "x", "documents": ["doc"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "reranker")
        for documents, extra in (([], {}), (["x"] * 101, {}), (["x"], {"top_n": 0})):
            result = self.post(documents, **extra)
            self.assertEqual(result.status_code, 400)
        wrong = self.post(["x"], model="baai/bge-m3")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json()["error"]["code"], "model_not_rerank")
        chat = self.client.post("/v1/chat/completions", json={
            "model": "reranker", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(chat.status_code, 400)
        self.assertEqual(chat.json()["error"]["code"], "rerank_model_not_chat")
        self.assertEqual(len(self.rerank_requests), 1)

    def test_alias_binding_disable_and_rate_probe_guard(self):
        store = self.install_policy()
        rule = {**model_policy.default_rule("reranker"), "public_id": "rerank-alias",
                "credential_ids": [self.entries["cn-work"]["account_key"]]}
        store.update_model("reranker", rule, 0, known_models=["reranker"])
        response = self.post(["a", "b"], model="rerank-alias")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "rerank-alias")
        self.assertEqual(self.rerank_requests[-1][0].headers["x-user-id"], "cn-work")
        self.assertEqual(self.rerank_requests[-1][1]["model"], "reranker")
        store.update_model("reranker", {**rule, "enabled": False}, 1, known_models=["reranker"])
        disabled = self.post(["a"], model="rerank-alias")
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(len(self.rerank_requests), 1)
        with self.assertRaisesRegex(ValueError, "不支持聊天倍率探测"):
            Management(converter).admin_rate_probe_model("reranker")

    def test_server_and_business_failures_are_not_replayed(self):
        self.rerank_replies = [(500, {"error": {"message": "unavailable"}}), (200, rerank_payload(1))]
        response = self.post(["doc"])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(self.rerank_requests), 1)

        self.rerank_replies = [(200, {"code": 10001, "msg": "bad request"}), (200, rerank_payload(1))]
        response = self.post(["doc"])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["upstream_code"], 10001)
        self.assertEqual(len(self.rerank_requests), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
