#!/usr/bin/env python3
"""Tests for the richer personal model table and its compatibility fallback."""

import base64
import json
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import credits


def jwt(issuer):
    payload = base64.urlsafe_b64encode(json.dumps({"iss": issuer}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def personal_payload():
    return {"code": 0, "data": {
        "models": [
            {"id": "auto", "name": "Auto", "supportsToolCall": True, "isDefault": True},
            {"id": "hy4-preview", "name": "Hy4 preview", "credits": "x0.29",
             "maxInputTokens": 960000, "contextWindow": {"defaultLength": 300000},
             "supportsToolCall": True},
            {"id": "hunyuan-image-alpha", "name": "Hunyuan Image Alpha",
             "tags": ["text-to-image"]},
        ],
        "agents": [{"name": "cli", "tags": ["cli", "default"],
                     "models": ["auto", "hy4-preview"]}],
        "endpoint": "https://copilot.tencent.com",
        "mergeStrategy": "merge",
    }}


class PersonalModelTableTests(TestCase):
    def _client(self, *responses):
        client = MagicMock()
        client.get.side_effect = list(responses)
        factory = patch("app.credits.httpx.Client")
        mocked = factory.start()
        self.addCleanup(factory.stop)
        mocked.return_value.__enter__.return_value = client
        return client

    @staticmethod
    def response(status, payload):
        return httpx.Response(status, json=payload)

    def test_personal_endpoint_preserves_rich_fields_and_scopes(self):
        client = self._client(self.response(200, personal_payload()))
        scopes = credits.fetch_personal_model_scopes(
            jwt("https://www.codebuddy.cn/auth/realms/copilot"),
            user_agent="CLI/test", uid="user-1")
        self.assertEqual([item["id"] for item in scopes["picker"]], ["auto", "hy4-preview"])
        self.assertEqual([item["id"] for item in scopes["account"]],
                         ["auto", "hy4-preview", "hunyuan-image-alpha"])
        rich = scopes["account"][1]
        self.assertEqual(rich["credits"], "x0.29")
        self.assertEqual(rich["contextWindow"]["defaultLength"], 300000)
        url = client.get.call_args.args[0]
        self.assertEqual(url, "https://www.codebuddy.cn/v2/enterprises/personal/models")
        headers = client.get.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(len(headers["X-Request-ID"]), 32)
        self.assertNotIn("user-1", headers["X-Request-ID"])

    def test_preferred_personal_table_falls_back_to_v3(self):
        client = self._client(self.response(404, {"code": 404}),
                              self.response(200, personal_payload()))
        scopes = credits.fetch_model_scopes(
            jwt("https://www.codebuddy.cn/auth/realms/copilot"),
            prefer_personal=True)
        self.assertEqual([item["id"] for item in scopes["picker"]], ["auto", "hy4-preview"])
        self.assertEqual(client.get.call_args_list[0].args[0],
                         "https://www.codebuddy.cn/v2/enterprises/personal/models")
        self.assertEqual(client.get.call_args_list[1].args[0],
                         "https://copilot.tencent.com/v3/config")
    def test_preferred_personal_table_is_supplemented_by_v3(self):
        v3 = {"code": 0, "data": {
            "models": [
                {"id": "hy4-preview", "name": "Legacy Hy4", "credits": "x9.9", "supportsToolCall": True},
                {"id": "v3-only", "name": "V3 only", "supportsToolCall": True},
            ],
            "agents": [{"name": "cli", "models": ["hy4-preview", "v3-only"]}],
        }}
        client = self._client(self.response(200, personal_payload()), self.response(200, v3))
        scopes = credits.fetch_model_scopes(
            jwt("https://www.codebuddy.cn/auth/realms/copilot"), prefer_personal=True)
        self.assertEqual([item["id"] for item in scopes["picker"]], ["auto", "hy4-preview", "v3-only"])
        self.assertEqual([item["id"] for item in scopes["account"]],
                         ["auto", "hy4-preview", "hunyuan-image-alpha", "v3-only"])
        self.assertEqual(scopes["picker"][1]["name"], "Hy4 preview")
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(client.get.call_args_list[1].args[0], "https://copilot.tencent.com/v3/config")

    def test_personal_table_survives_supplemental_v3_failure(self):
        client = self._client(self.response(200, personal_payload()), self.response(503, {"code": 503}))
        scopes = credits.fetch_model_scopes(
            jwt("https://www.codebuddy.cn/auth/realms/copilot"), prefer_personal=True)
        self.assertEqual([item["id"] for item in scopes["picker"]], ["auto", "hy4-preview"])
        self.assertEqual(client.get.call_count, 2)


    def test_enterprise_preferred_fetch_uses_v3_without_personal_path(self):
        client = self._client(self.response(200, personal_payload()))
        scopes = credits.fetch_model_scopes(
            jwt("https://www.codebuddy.cn/auth/realms/copilot"),
            enterprise_id="tenant-a", prefer_personal=True)
        self.assertEqual([item["id"] for item in scopes["picker"]], ["auto", "hy4-preview"])
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(client.get.call_args.args[0], "https://copilot.tencent.com/v3/config")


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
