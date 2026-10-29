#!/usr/bin/env python3
"""Preview gateway: network isolation, the simulated upstream behind real gateway code, and seeding.

The sandbox routes every httpx transport into the simulation, so these tests exercise the
gateway's own request building and parsing without any socket leaving the process.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import asyncio
import gzip
import json
import os
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from app import auth_oauth, credits, growth_tasks, travel, trial_rewards, web_search
from app.audit_store import AuditStore
from app.site_routing import PROFILE_DOMAINS
from app.updates import Updater
import preview.__main__ as launcher
from preview import public, sandbox, seed
from preview.upstream import Upstream

ACCOUNTS = {account.key: account for account in seed.ACCOUNTS}


def login(key):
    account = ACCOUNTS[key]
    return seed.token(account), account.uid, PROFILE_DOMAINS[account.profile]


class Isolated(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.addCleanup(sandbox.install(self.upstream))


class SandboxTests(Isolated):
    def test_http_reaches_only_the_simulation(self):
        response = httpx.get("https://www.example.org/anything")
        self.assertEqual(response.status_code, 501)
        self.assertIn("未模拟", response.json()["msg"])

        async def fetch():
            async with httpx.AsyncClient() as client:
                return await client.get("https://www.example.org/anything")
        self.assertEqual(asyncio.run(fetch()).status_code, 501)

    def test_raw_sockets_and_dns_are_refused(self):
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("192.0.2.1", 443), timeout=1)
        with self.assertRaises(socket.gaierror):
            socket.getaddrinfo("example.org", 443)
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 80))

    def test_updates_cannot_install(self):
        self.assertEqual(Updater.self_update_block(None), "预览环境不支持安装更新")

    def test_restore_undoes_every_patch(self):
        original = (httpx.HTTPTransport.handle_request, socket.socket.connect, socket.getaddrinfo)
        restore = sandbox.install(Upstream())
        restore()
        self.assertEqual((httpx.HTTPTransport.handle_request, socket.socket.connect, socket.getaddrinfo), original)


class UpstreamTests(Isolated):
    def test_personal_and_enterprise_balances(self):
        token, uid, domain = login("aurora")
        self.assertEqual(credits.fetch_credits(token, uid, domain)["credits"], 1524.5)
        token, uid, domain = login("fjord")
        self.assertTrue(credits.fetch_credits(token, uid, domain, enterprise_id="tenant-fjord")["unlimited"])
        token, uid, domain = login("grove")
        self.assertEqual(credits.fetch_credits(token, uid, domain, enterprise_id="tenant-grove")["credits"], 3170)

    def test_revoked_account_is_signed_out(self):
        token, uid, domain = login("harbor")
        with self.assertRaises(credits.AuthExpiredError):
            credits.fetch_credits(token, uid, domain)

    def test_catalog_and_usage(self):
        token, uid, domain = login("dune")
        picker = credits.fetch_model_scopes(token, domain=domain, uid=uid)["picker"]
        self.assertEqual([model["id"] for model in picker], list(seed.INTL_MODELS))
        usage = credits.fetch_request_usage(token, uid=uid, domain=domain)
        self.assertEqual(usage["requests"], 30 * ACCOUNTS["dune"].daily_requests)

    def test_checkin_once_per_day(self):
        token, uid, domain = login("cedar")
        self.assertEqual(credits.daily_checkin(token, uid, domain)["state"], "success")
        self.assertEqual(credits.daily_checkin(token, uid, domain)["state"], "already")
        self.assertEqual(credits.fetch_credits(token, uid, domain)["credits"], 47)

    def test_travel_states(self):
        states = {key: travel.perform(login(key)[0], ACCOUNTS[key].profile, read_only=True)["state"]
                  for key in ("aurora", "breeze", "cedar")}
        self.assertEqual(states, {"aurora": "arrived", "breeze": "idle", "cedar": "traveling"})

    def test_growth_sweep_accepts_and_claims(self):
        token, uid, domain = login("aurora")
        summary = growth_tasks.sweep_account(token, uid, domain)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["claimed"], ["template_5"])
        self.assertIn("school_season", summary["accepted"])
        self.assertNotIn("skill_1", summary["accepted"])

    def test_trial_claims_once(self):
        token, _, domain = login("ember")
        headers = {"Authorization": f"Bearer {token}", "X-Domain": domain}
        self.assertTrue(trial_rewards.claim_trial(headers)["ok"])
        self.assertTrue(trial_rewards.claim_trial(headers)["already"])

    def test_search_results_stay_on_example_domains(self):
        token, _, _ = login("aurora")
        response = httpx.post("https://copilot.tencent.com/agenttool/v1/search",
                              headers={"Authorization": f"Bearer {token}"},
                              json=web_search.request_body("codebuddy", 3))
        results = web_search.parse_results(response.json())
        self.assertEqual(len(results), 3)
        self.assertTrue(all(item["url"].startswith("https://example.com/") for item in results))

    def test_release_archive_serves_the_speed_test_only(self):
        response = httpx.get("https://ghfast.top/https://github.com/maiphucgiang/codebuddy2api/releases/download/"
                             "v1.0.0/codebuddy2api-1.0.0.tar.gz")
        self.assertTrue(response.content.startswith(b"\x1f\x8b"))
        release = httpx.get("https://api.github.com/repos/maiphucgiang/codebuddy2api/releases").json()[0]
        self.assertEqual({asset["name"] for asset in release["assets"]},
                         {release["tag_name"].replace("v", "codebuddy2api-", 1) + ".tar.gz", "SHA256SUMS"})

    def chat(self, content, **body):
        token, _, _ = login("aurora")
        response = httpx.post("https://copilot.tencent.com/v2/chat/completions",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"model": "glm-5.1", "messages": [{"role": "user", "content": content}], **body})
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: {")]
        return response, events

    def test_chat_streams_text_with_usage(self):
        response, events = self.chat("你好")
        self.assertEqual(response.status_code, 200)
        text = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
        self.assertIn("模拟回复", text)
        self.assertGreater(events[-1]["usage"]["credit"], 0)

    def test_gzip_chat_body_is_decoded_before_dispatch(self):
        token, _, _ = login("aurora")
        body = json.dumps({"model": "glm-5.1", "messages": [{"role": "user", "content": "你好"}]}).encode()
        response = httpx.post("https://copilot.tencent.com/v2/chat/completions",
                              headers={"Authorization": f"Bearer {token}", "Content-Encoding": "gzip"},
                              content=gzip.compress(body))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("data: [DONE]", response.text)


    def test_embeddings_return_vectors_and_billed_usage(self):
        token, _, _ = login("aurora")
        response = httpx.post("https://www.workbuddy.cn/v2/embeddings",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"model": "bge-m3", "input": ["hello", "world"], "encoding_format": "float"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["data"]), 2)
        self.assertEqual(len(response.json()["data"][0]["embedding"]), 1024)
        self.assertGreater(response.json()["usage"]["credit"], 0)

    def test_codebase_embeddings_and_rerank_have_no_simulated_charge(self):
        token, uid, domain = login("aurora")
        before = credits.fetch_credits(token, uid, domain)["credits"]
        embedded = httpx.post("https://copilot.tencent.com/codebase/openai/v1/embeddings",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"model": "model2", "input": ["hello"], "encoding_format": "float"})
        self.assertEqual(embedded.status_code, 200, embedded.text)
        self.assertEqual(embedded.json()["usage"]["total_tokens"], 0)
        self.assertEqual(credits.fetch_credits(token, uid, domain)["credits"], before)

        ranked = httpx.post("https://copilot.tencent.com/codebase/openai/v1/rerank",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"query": "capital France", "documents": ["Berlin", "Paris capital France"],
                                  "top_n": 1})
        self.assertEqual(ranked.status_code, 200, ranked.text)
        self.assertEqual(ranked.json()["output"]["results"], [{"index": 1}])
        self.assertNotIn("relevance_score", ranked.text)
    def test_console_chat_uses_the_same_simulated_upstream(self):
        token, _, _ = login("aurora")
        response = httpx.post("https://copilot.tencent.com/console/chat/completions",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"model": "glm-5.1", "messages": [{"role": "user", "content": "你好"}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("data: [DONE]", response.text)

    def test_chat_directives(self):
        self.assertEqual(self.chat("[preview:429]")[0].status_code, 429)
        self.assertEqual(self.chat("[preview:500]")[0].status_code, 500)
        tools = [{"type": "function", "function": {"name": "Read", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
        _, plain = self.chat("hello", tools=tools)
        self.assertNotIn("tool_calls", json.dumps(plain))
        _, called = self.chat("[preview:tool=Read]", tools=tools)
        self.assertEqual(called[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_oauth_signs_in_a_new_synthetic_account(self):
        manager = auth_oauth.OAuthManager()
        started = manager.start("cn")
        self.assertTrue(started["verification_uri"].startswith("https://www.codebuddy.cn/"))
        self.assertFalse(manager.poll(started["login_id"])["done"])
        with patch("preview.upstream.LOGIN_SECONDS", 0):
            result = manager.poll(started["login_id"])
        self.assertTrue(result["done"])
        self.assertEqual(auth_oauth.validate_cred_data(result["cred"])[0], result["uid"])


class SeedTests(unittest.TestCase):
    def test_credentials_are_valid_and_synthetic(self):
        with tempfile.TemporaryDirectory() as directory:
            seed.write_credentials(Path(directory))
            files = sorted(Path(directory).glob("*.info"))
            self.assertEqual(len(files), len(seed.ACCOUNTS))
            for path in files:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsNone(auth_oauth.validate_cred_data(data)[1])
                self.assertTrue(data["auth"]["accessToken"].startswith("preview-"))

    def test_history_fills_the_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            seed.write_history(Path(directory) / "logs.sqlite3", days=3)
            store = AuditStore(Path(directory) / "logs.sqlite3")
            try:
                self.assertGreater(store.dashboard(days=7)["summary"]["requests"], 0)
            finally:
                store.close()

    def test_api_keys_history_and_media_ownership_are_seeded(self):
        from app.api_keys import KeyStore
        from app.control_store import ControlStore
        from app.media_store import MediaStore
        with tempfile.TemporaryDirectory() as directory:
            ids = seed.write_api_keys(Path(directory))
            self.assertEqual(set(ids), {"expired", "ip", "models"})
            control = ControlStore(Path(directory) / "control.sqlite3")
            keys = KeyStore(control)
            rows = {item["name"]: item for item in keys.list()}
            self.assertLess(rows["expired"]["expires_at"], __import__("time").time())
            self.assertEqual(list(rows["ip"]["ip_allow"]), ["127.0.0.1/32"])
            self.assertEqual(list(rows["models"]["models"]), ["auto"])
            seed.write_history(Path(directory) / "logs.sqlite3", days=1,
                               key_ids=list(ids.values()))
            audit = AuditStore(Path(directory) / "logs.sqlite3")
            self.assertGreater(sum(item["requests"] for item in audit.api_key_stats()["items"].values()), 0)
            audit.close()
            control.close()
            seed.write_media(Path(directory), api_key=ids["models"])
            media = MediaStore(Path(directory))
            tasks = {item["task_id"]: item["api_key"] for item in media.list_tasks()["items"]}
            self.assertEqual(tasks["seedvideotask-complete"], ids["models"])
            self.assertIsNone(tasks[seed.SEED_VIDEO_TASK_ID])
            media.close()


class PublicTests(unittest.TestCase):
    def test_guard_refuses_credential_transfer_and_shared_changes(self):
        app = FastAPI()
        app.add_api_route("/{path:path}", lambda: {"ok": True},
                          methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        app.add_middleware(public.Guard)
        client = TestClient(app)
        for method, path in (("POST", "/admin/credentials"), ("POST", "/admin/credentials/upload"),
                             ("POST", "/admin/credentials/refresh-import"), ("POST", "/admin/credentials/export"),
                             ("DELETE", "/admin/credentials/aurora.info"), ("PATCH", "/admin/settings"),
                             ("POST", "/admin/logs/clear"), ("POST", "/admin/update/install"),
                             ("DELETE", "/admin/media/assets/img_demo"),
                             ("POST", "/admin/media/tasks/task_demo/refresh"),
                             ("POST", "/admin/models"), ("PUT", "/admin/models/hy-image-3.5"),
                             ("DELETE", "/admin/models/custom:demo")):
            response = client.request(method, path)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.headers["x-codebuddy-preview"], "public")
            self.assertEqual(response.json()["error"]["message"], public.MESSAGE)
        for method, path in (("GET", "/admin/credentials"), ("PATCH", "/admin/credentials/abc"),
                             ("POST", "/admin/credentials/abc/checkin"), ("POST", "/v1/chat/completions")):
            response = client.request(method, path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers["x-codebuddy-preview"], "public")

    def test_reset_stops_the_server(self):
        class Immediate:
            def __init__(self, delay, callback):
                self.delay, self.callback, self.daemon = delay, callback, False

            def start(self):
                self.callback()
        server = Mock(should_exit=False)
        reset = public.schedule_reset({"server": server}, 6, timer=Immediate)
        self.assertEqual(reset.delay, 6 * 3600)
        self.assertTrue(reset.daemon)
        self.assertTrue(server.should_exit)


class LaunchTests(unittest.TestCase):
    def test_prepare_refuses_foreign_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "real.info").write_text("{}", encoding="utf-8")
            with self.assertRaises(SystemExit):
                launcher.prepare(Path(directory))
        with self.assertRaises(SystemExit):
            launcher.prepare(launcher.ROOT / "auth")

    def test_prepare_reuses_its_own_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data"
            launcher.prepare(target)
            (target / "aurora.info").unlink()
            launcher.prepare(target)
            self.assertFalse((target / "aurora.info").exists())

    def test_main_never_inherits_real_settings(self):
        seen = {}

        def fake_main():
            seen.update(env=dict(os.environ), cwd=os.getcwd(), argv=list(sys.argv))
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        environment = {"CODEBUDDY2API_KEY": "real-key", "CODEBUDDY_AUTH_DIR": "/real/auth"}
        with patch.dict(os.environ, environment), patch.object(sys, "argv", ["preview", "--port", "8799"]), \
                patch.object(sandbox, "install", lambda upstream: lambda: None), patch("converter.main", fake_main):
            launcher.main()
        self.assertNotIn("CODEBUDDY2API_KEY", seen["env"])
        self.assertEqual(seen["env"]["CODEBUDDY_AUTH_DIR"], seen["cwd"])
        self.assertIn("codebuddy2api-preview-", seen["cwd"])
        self.assertEqual(seen["argv"][-6:], ["--host", "127.0.0.1", "--port", "8799", "--api-key", "preview"])
        self.assertFalse(Path(seen["cwd"]).exists())

    def test_public_mode_and_gateway_arguments(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        argv = ["preview", "--public", "--reset-hours", "6", "--admin-allowed-origins", "https://demo.example"]
        seen = {}
        with patch.object(sys, "argv", argv), patch.object(sandbox, "install", lambda upstream: lambda: None), \
                patch("converter.main", lambda: seen.update(argv=list(sys.argv))), \
                patch("converter.app.add_middleware") as middleware, patch.object(public, "schedule_reset") as reset:
            launcher.main()
        self.assertEqual(seen["argv"][-2:], ["--admin-allowed-origins", "https://demo.example"])
        middleware.assert_called_once_with(public.Guard)
        self.assertEqual(reset.call_args.args[1], 6.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
