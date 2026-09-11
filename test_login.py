#!/usr/bin/env python3
"""登录命令的扫码轮询、凭据保存和退出行为；运行 python3 test_login.py。"""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

import auth_oauth
import converter


class LoginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {"CODEBUDDY_AUTH_DIR": temporary.name}))
        self.enterContext(patch.dict(converter.CONFIG, {"cred_pool": None, "api_key": "", "log_path": None}))
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.browser = self.enterContext(patch("webbrowser.open", return_value=False))
        self.sleep = self.enterContext(patch("converter.time.sleep"))
        self.requests = []
        self.polls = 0

    def manager(self, domain="www.codebuddy.cn", uid="u1", timeout=600):
        def upstream(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, domain)
            path = request.url.path
            if path.endswith("/auth/state"):
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.url.params["platform"], "workbuddy")
                return httpx.Response(200, json={"code": 0, "data": {
                    "state": "test-state", "authUrl": f"https://{domain}/login?state=test-state"}})
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.params["state"], "test-state")
            if path.endswith("/auth/token"):
                self.polls += 1
                if self.polls == 1:
                    return httpx.Response(200, json={"code": 10001, "msg": "pending"})
                return httpx.Response(200, json={"code": 0, "data": {
                    "accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                    "domain": domain, "expiresIn": 7200}})
            self.assertEqual(path, "/v2/plugin/login/account")
            self.assertEqual(request.headers["Authorization"], "Bearer private-access-token")
            self.assertEqual(request.headers["X-Domain"], domain)
            return httpx.Response(200, json={"code": 0, "data": {"uid": uid, "nickname": "测试账号"}})

        return auth_oauth.OAuthManager(
            http_factory=lambda: httpx.Client(transport=httpx.MockTransport(upstream)), timeout_s=timeout)

    def assert_no_tokens_printed(self):
        output = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertNotIn("private-access-token", output)
        self.assertNotIn("private-refresh-token", output)

    def test_login_command_polls_saves_and_does_not_start_server(self):
        pool = converter.CredentialPool(scan=True)
        with patch.object(converter, "_OAUTH", self.manager("www.workbuddy.ai")), \
                patch("sys.argv", ["converter.py", "login", "--site", "intl", "--no-browser"]), \
                patch("converter.uvicorn.run") as server, patch("converter.threading.Thread") as thread:
            self.assertEqual(converter.main(), 0)
        server.assert_not_called()
        thread.assert_not_called()
        self.browser.assert_not_called()
        self.sleep.assert_called_once_with(1.5)
        saved = self.directory / "u1.info"
        credential = json.loads(saved.read_text())
        self.assertEqual(credential["auth"]["accessToken"], "private-access-token")
        self.assertEqual(auth_oauth.validate_cred_data(credential), ("u1", None))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertIsNone(pool.pick(None, region="cn"))  # 内部过滤不能把国际账号当作国内账号。
        self.assertEqual(pool.pick(None).summary()["uid"], "u1")
        self.assertIn("账号已保存", self.stdout.getvalue())
        self.assert_no_tokens_printed()

    def test_relogin_updates_existing_name_and_preserves_other_accounts(self):
        old = auth_oauth.build_auth_file(
            {"accessToken": "old", "domain": "www.codebuddy.cn"}, {"uid": "u1"})
        old["allAccounts"].append({"uid": "u2", "nickname": "其他账号"})
        named = self.directory / "named.info"
        named.write_text(json.dumps(old))
        with patch.object(converter, "_OAUTH", self.manager()):
            self.assertEqual(converter.login(), 0)
        self.browser.assert_called_once_with("https://www.codebuddy.cn/login?state=test-state")
        self.assertIn("请手动打开", self.stdout.getvalue())
        self.assertEqual(list(self.directory.glob("*.info")), [named])
        saved = json.loads(named.read_text())
        self.assertEqual(saved["auth"]["accessToken"], "private-access-token")
        self.assertEqual({a["uid"] for a in saved["allAccounts"]}, {"u1", "u2"})
        self.assert_no_tokens_printed()

    def test_expired_login_exits_without_saving(self):
        with patch.object(converter, "_OAUTH", self.manager(timeout=0)):
            self.assertEqual(converter.login(open_browser=False), 1)
        self.assertIn("登录超时", self.stderr.getvalue())
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.polls, 0)

    def test_cancel_during_poll_exits_without_saving(self):
        self.sleep.side_effect = KeyboardInterrupt
        with patch.object(converter, "_OAUTH", self.manager()):
            self.assertEqual(converter.login(open_browser=False), 130)
        self.assertIn("已取消登录", self.stderr.getvalue())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_upstream_exception_does_not_print_response_or_token(self):
        for error in (RuntimeError("private-access-token"), httpx.ConnectError("private-refresh-token")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(converter, "_OAUTH", Mock(start=Mock(side_effect=error))):
                self.assertEqual(converter.login(open_browser=False), 1)
        self.assert_no_tokens_printed()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_credential_is_not_saved(self):
        for uid, domain in (("u1", "untrusted.example"), ("../escape", "www.codebuddy.cn")):
            credential = auth_oauth.build_auth_file(
                {"accessToken": "private-access-token", "domain": domain}, {"uid": uid})
            manager = Mock()
            manager.start.return_value = {"verification_uri": "https://www.codebuddy.cn/login", "login_id": "test"}
            manager.poll.return_value = {"done": True, "cred": credential}
            with self.subTest(uid=uid), patch.object(converter, "_OAUTH", manager):
                self.assertEqual(converter.login(open_browser=False), 1)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_no_tokens_printed()

    def test_save_failure_does_not_claim_success_or_print_token(self):
        with patch.object(converter, "_OAUTH", self.manager()), \
                patch("converter.atomic_write_credential", side_effect=PermissionError("private-access-token")):
            self.assertEqual(converter.login(open_browser=False), 1)
        self.assertNotIn("账号已保存", self.stdout.getvalue())
        self.assertIn("无法保存凭据", self.stderr.getvalue())
        self.assertEqual(list(self.directory.glob("*.info")), [])
        for path in self.directory.iterdir():
            self.assertTrue(path.name.endswith(".lock"))
            self.assertIn(path.read_bytes(), (b"", b"\0"))
        self.assert_no_tokens_printed()

    def test_admin_poll_still_saves_and_loads_into_pool(self):
        pool = converter.CredentialPool(scan=False)
        with patch.object(converter, "_OAUTH", self.manager()), \
                patch.dict(converter.CONFIG, {"cred_pool": pool}):
            started = converter.admin_oauth_start(site="cn")
            self.assertEqual(converter.admin_oauth_poll(started["login_id"]), {"done": False})
            result = converter.admin_oauth_poll(started["login_id"])
        self.assertTrue(result["done"])
        self.assertEqual(result["uid"], "u1")
        self.assertTrue(Path(result["imported"]).is_file())
        self.assertEqual(pool.pick(None).summary()["uid"], "u1")
        self.assertNotIn("cred", result)


if __name__ == "__main__":
    unittest.main()
