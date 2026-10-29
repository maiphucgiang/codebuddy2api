#!/usr/bin/env python3
"""Test growth automation chains: payload shapes, orchestration and guard rails."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import json
import unittest
from datetime import datetime
from unittest.mock import patch

import httpx

from app import growth_chains, growth_events, growth_tasks

def envelope(data, code=0):
    return {"code": code, "msg": "ok" if code == 0 else "synthetic rejection", "data": data}


def raw_task(code, *, current=0, target=1, accept="accepted", locked=False):
    return {"task_code": code, "title": code, "task_desc": "", "locked": locked,
            "accept_status": accept, "current": current, "target": target,
            "reward_credit": 100, "reward_energy": 5}


def default_tasks():
    return [raw_task("chat_5", current=3, target=5),
            raw_task("RichMeow_Chat", current=0, target=1),
            raw_task("Library_read", current=0, target=1),
            raw_task("black_cat", current=0, target=2),
            raw_task("Model_chat_GLM5.2", current=0, target=1, accept="not_accepted")]


class ChainHarness(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.tasks_table = default_tasks()
        self.claims = {}
        self.real = httpx.Client

    def transport(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.posts.append({"method": method, "path": path, "host": request.url.host,
                           "body": json.loads(request.read().decode("utf-8")) if request.content else None,
                           "headers": {key.lower(): value for key, value in request.headers.items()}})
        if method == "GET" and path == growth_tasks.TASKS_PATH:
            return httpx.Response(200, json=envelope({"tasks": self.tasks_table}))
        if method == "POST" and path == growth_tasks.ACCEPT_PATH:
            # 上游 accept 成功即登记：后续回读的 accept_status 已变化。
            codes = (json.loads(request.content.decode()).get("task_codes") or []) if request.content else []
            for task in self.tasks_table:
                if task["task_code"] in codes:
                    task["accept_status"] = "accepted"
            return httpx.Response(200, json=envelope({}))
        if method == "POST" and path.endswith("/claim"):
            code = request.url.path.split("/")[-2]
            payload = self.claims.get(code, {"already_claimed": False, "credit": 100, "energy": 5})
            return httpx.Response(200, json=envelope(payload))
        if method == "POST" and path == "/v2/report":
            return httpx.Response(200, json=envelope({}))
        return httpx.Response(404, json=envelope({}, code=404))

    def run_chain(self, code, *, base="https://www.workbuddy.cn", task_table=None,
                  claims=None, real_chat=None):
        self.tasks_table = task_table if task_table is not None else self.tasks_table
        self.claims = claims or {}
        transport = httpx.MockTransport(self.transport)

        def make_client(**kwargs):
            kwargs["transport"] = transport
            return self.real(**kwargs)

        ctx = growth_chains.Context(base=base, token="tok", uid="u1", domain="workbuddy.cn",
                                    nickname="tester", headers={"X-User-Id": "u1"}, lock_key="acct")
        patches = [patch.object(growth_tasks.httpx, "Client", make_client),
                   patch.object(growth_events.httpx, "Client", make_client)]
        if real_chat is not None:
            patches.append(patch.object(growth_events, "desktop_chat", side_effect=real_chat))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return growth_chains.run(code, ctx)


class BehaviorChainTests(ChainHarness):
    def setUp(self):
        super().setUp()
        # 全部 runner 的节奏间隔在测试中不真实等待。
        self.enterContext(patch.object(growth_events, "REPORT_GAP_S", 0))
        self.enterContext(patch.object(growth_chains, "POLL_ATTEMPTS", 1))
        self.enterContext(patch.object(growth_chains, "POLL_GAP_S", 0))

    def test_rich_meow_posts_six_event_desktop_chain(self):
        result = self.run_chain("RichMeow_Chat")
        self.assertTrue(result["ok"], result)
        reports = [post for post in self.posts if post["path"] == "/v2/report"]
        self.assertEqual(len(reports), 1)
        events = reports[0]["body"]
        codes = [event["eventCode"] for event in events]
        self.assertEqual(codes, ["agent_task_created", "chat_message_send", "chat_request_send",
                                 "chat_message_response", "chat_message_status", "chat_request_response"])
        for event in events:
            self.assertEqual(event["extName"], "workbuddy-desktop")
            self.assertEqual(event["userId"], "u1")
        # machineId 稳定：同一 uid 两次派生一致（面板幂等口径）。
        self.assertEqual(events[0]["machineId"], growth_events.derive_id("u1", "machine"))

    def test_business_field_wins_over_fingerprint(self):
        seen = {}

        def capture(base, token, uid, nickname="", domain="", events=()):
            seen.update(events[0])
            return None

        with patch.object(growth_events, "report_desktop", capture):
            growth_chains.run_rich_meow(growth_chains.Context(base="b", token="t", uid="u1"), None)
        self.assertEqual(seen["conversationId"].startswith("wb2api-rm-"), True)

    def test_chat_5_reports_only_the_gap(self):
        result = self.run_chain("chat_5")
        self.assertTrue(result["ok"], result)
        reports = [post for post in self.posts if post["path"] == "/v2/report"]
        self.assertEqual(len(reports), 2)      # 3/5 → 补 2 条
        self.assertEqual(reports[0]["body"][0]["eventCode"], "chat_request_send")
        self.assertEqual(reports[0]["body"][0]["userId"], "u1")
        event = reports[0]["body"][0]
        self.assertEqual(event["ideVersion"], "5.9.3")
        self.assertEqual(event["sdk_version"], "5.9.3")
        self.assertEqual(event["platform"], "windows")
        self.assertEqual(event["chat_session_uuid"], event["conversationId"])
        self.assertEqual(event["title"], "对话")
        self.assertEqual(event["temperature"], 0.7)
        self.assertEqual(event["maxRetries"], 2)
        self.assertEqual(event["cpuCores"], 24)
        self.assertIn("WorkBuddyDesktop/5.9.3", reports[0]["headers"]["user-agent"])

    def test_library_read_uses_web_shape_on_account_domain(self):
        result = self.run_chain("Library_read")
        self.assertTrue(result["ok"], result)
        report = [post for post in self.posts if post["path"] == "/v2/report"][0]
        self.assertEqual(report["host"], "www.workbuddy.cn")
        event = report["body"][0]
        self.assertEqual(event["eventCode"], "web_element_click")
        self.assertEqual(event["elementId"], "library_doc_intro_click")
        self.assertEqual(event["machineId"], growth_events.derive_id("u1", "webmachine"))

    def test_desktop_report_falls_back_to_cli_domain(self):
        result = self.run_chain("RichMeow_Chat")
        fallback = [post for post in self.posts
                    if post["path"] == "/v2/report" and post["host"] == "copilot.tencent.com"]
        # 账号域 200 时不产生兜底请求。
        self.assertEqual(fallback, [])
        # 账号域 404 时兜底到 CLI 域。
        self.run_chain("RichMeow_Chat")
        original_transport = self.transport

        def reject_account_domain(request):
            if request.url.host == "www.workbuddy.cn" and request.url.path == "/v2/report":
                return httpx.Response(404)
            return original_transport(request)

        self.transport = reject_account_domain
        result = self.run_chain("RichMeow_Chat")
        self.assertTrue(result["ok"], result)
        fallback = [post for post in self.posts
                    if post["path"] == "/v2/report" and post["host"] == "copilot.tencent.com"]
        self.assertGreaterEqual(len(fallback), 1)


class OrchestrationTests(ChainHarness):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(growth_chains, "POLL_ATTEMPTS", 1))
        self.enterContext(patch.object(growth_chains, "POLL_GAP_S", 0))

    def test_claimed_task_is_skipped_without_requests(self):
        self.tasks_table = [raw_task("RichMeow_Chat", accept="claimed")]
        result = self.run_chain("RichMeow_Chat")
        self.assertTrue(result["skipped"])
        self.assertEqual([post for post in self.posts if post["path"] == "/v2/report"], [])

    def test_unknown_task_reports_missing(self):
        self.tasks_table = []
        result = self.run_chain("RichMeow_Chat")
        self.assertTrue(result["skipped"])
        self.assertIn("没有此任务", result["message"])

    def test_claimable_after_chain_auto_claims(self):
        def real_chat(base, headers, expert_id="", model=growth_events.FAST_MODEL, prompt=""):
            return "wb2api-conv-x", "cmb-" + "a" * 32

        self.tasks_table = [raw_task("Model_chat_GLM5.2", current=1, target=1, accept="not_accepted")]
        # accept 之后回读即可领奖：claim mock 返回到账。
        self.claims["Model_chat_GLM5.2"] = {"already_claimed": False, "credit": 100, "energy": 5}
        result = self.run_chain("Model_chat_GLM5.2", real_chat=real_chat)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("claimed"))
        self.assertIn("已自动领奖 +100c +5e", result["message"])
        accept_posts = [post for post in self.posts if post["path"] == growth_tasks.ACCEPT_PATH]
        self.assertEqual(len(accept_posts), 1)
        self.assertEqual(accept_posts[0]["body"], {"task_codes": ["Model_chat_GLM5.2"]})
        claims = [post for post in self.posts if post["path"].endswith("/Model_chat_GLM5.2/claim")]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["headers"]["x-client-platform"], "web")

    def test_busy_account_is_skipped(self):
        self.assertTrue(growth_chains.try_lock("acct"))
        try:
            result = self.run_chain("RichMeow_Chat")
            self.assertTrue(result["skipped"])
            self.assertIn("正在执行", result["message"])
        finally:
            growth_chains.unlock("acct")

    def test_real_chat_request_id_shape(self):
        # SERVER_ID_RE 是 requestId 的硬校验（自造 id 不计数，须为服务端 id）。
        self.assertFalse(growth_events.SERVER_ID_RE.match("wb2api-conv-x"))
        self.assertTrue(growth_events.SERVER_ID_RE.match("cmb-" + "a" * 32))
        self.assertTrue(growth_events.SERVER_ID_RE.match("a" * 32))


class NamedExpertTests(ChainHarness):
    def exercise(self, code, expert):
        reports, seen = [], {}

        def real_chat(_ctx, expert_id="", model=growth_chains.DEFAULT_CHAT_MODEL):
            seen["expert_id"] = expert_id
            return "wb2api-conv-x", "cmb-" + "a" * 32

        # Same-name market entries must never replace the task-specific expert id.
        wrong = {"expert_id": "ex_wrong", "display_name_zh": expert["display_name_zh"]}
        ctx = growth_chains.Context(base="https://www.workbuddy.cn", token="tok", uid="u1")
        with patch.object(growth_events, "market_expert_list", return_value=[wrong]), \
                patch.object(growth_chains, "_real_chat", side_effect=real_chat), \
                patch.object(growth_chains, "_report_desktop",
                             side_effect=lambda _ctx, events: reports.append(events)):
            message = growth_chains.RUNNERS[code](ctx, raw_task(code))
        return message, reports, seen

    def test_cloud_expert_tasks_keep_their_verified_ids(self):
        for code, expert in growth_chains.NAMED_EXPERT_TASKS.items():
            with self.subTest(code=code):
                message, reports, seen = self.exercise(code, expert)
                self.assertEqual(seen["expert_id"], expert["expert_id"])
                self.assertEqual(growth_tasks.GROUPS[code], "chat")
                self.assertIn(expert["display_name_zh"], message)
                summoned = next(event for report in reports for event in report
                                if event["eventCode"] == "expert_summoned")
                actual = next(event for report in reports for event in report
                              if event["eventCode"] == "expert_actual_use")
                self.assertEqual(summoned["id"], expert["expert_id"])
                self.assertEqual(actual["id"], expert["expert_id"])
                self.assertEqual(actual["name"], expert["display_name_zh"])
                self.assertEqual(actual["version"], expert["version"])
                self.assertEqual(actual["type"], expert["categories"][0])
                self.assertEqual(actual["mode"], "craft")

    def test_lighthouse_keeps_its_local_event_shape(self):
        expert = {"expert_id": growth_chains.LIGHTHOUSE_EXPERT_ID,
                  "display_name_zh": "腾讯轻量云专家"}
        _message, reports, seen = self.exercise("Expert_lighthouse", expert)
        self.assertEqual(seen["expert_id"], growth_chains.LIGHTHOUSE_EXPERT_ID)
        actual = next(event for report in reports for event in report
                      if event["eventCode"] == "expert_actual_use")
        self.assertEqual(actual["mode"], "LOCAL")
        self.assertEqual(actual["type"], "")
        self.assertEqual(actual["cost"], 0)


class NightWindowTests(ChainHarness):
    def test_window_boundaries(self):
        for hour, expected in ((22, False), (23, True), (0, True), (7, True), (8, False)):
            with self.subTest(hour=hour):
                self.assertEqual(growth_chains.in_night_window(datetime(2026, 10, 19, hour)), expected)

    def test_black_cat_outside_window_is_reported_not_run(self):
        with patch.object(growth_chains, "in_night_window", return_value=False):
            result = self.run_chain("black_cat")
        self.assertTrue(result["ok"], result)
        self.assertIn("23:00–08:00", result["message"])
        self.assertEqual([post for post in self.posts if post["path"] == "/v2/report"], [])


class MiniProgramTests(ChainHarness):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(growth_chains, "POLL_ATTEMPTS", 1))
        self.enterContext(patch.object(growth_chains, "POLL_GAP_S", 0))
        self.enterContext(patch.object(growth_chains, "MP_CHAT_GAP_S", 0))
        self.enterContext(patch.object(growth_chains, "ACCEPT_VERIFY_GAP_S", 0))

    def mp_tasks(self):
        return [raw_task("school_season", current=0, target=1, accept="not_accepted")]

    def test_school_season_accepts_and_reports_activity_id(self):
        self.tasks_table = self.mp_tasks()
        self.claims["school_season"] = {"already_claimed": False, "credit": 100, "energy": 5}
        result = self.run_chain("school_season")
        self.assertTrue(result["ok"], result)
        accept = [post for post in self.posts if post["path"] == growth_tasks.ACCEPT_PATH]
        self.assertEqual(accept[-1]["headers"]["x-client-platform"], growth_tasks.MP_PLATFORM)
        report = [post for post in self.posts if post["path"] == "/v2/report"][-1]
        self.assertEqual(report["host"], "www.codebuddy.cn")     # mp 指纹走 CodeBuddy CN 域
        self.assertEqual(report["headers"]["x-client-platform"], "mp-weixin")
        event = report["body"][0]
        self.assertEqual(event["eventCode"], "chat_request_send")
        self.assertEqual(event["activityId"], growth_chains.SCHOOL_ACTIVITY_ID)
        self.assertEqual(event["platform"], "mini_program")

    def test_accept_not_registered_waits_for_next_round(self):
        self.tasks_table = self.mp_tasks()
        original = self.transport

        def stale_accept(request):
            response = original(request)
            if request.url.path == growth_tasks.ACCEPT_PATH:
                # accept 永不落账：回读仍为 not_accepted。
                self.tasks_table = [raw_task("school_season", current=0, target=1, accept="not_accepted")]
            return response

        self.transport = stale_accept
        result = self.run_chain("school_season")
        self.assertTrue(result["ok"], result)
        self.assertIn("accept 未登记生效", result["message"])
        self.assertEqual([post for post in self.posts if post["path"] == "/v2/report"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
