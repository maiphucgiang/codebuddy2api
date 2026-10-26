#!/usr/bin/env python3
"""Test growth-center task listing, claiming and the automation switch surface."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import unittest
from unittest.mock import patch

import httpx

from app import growth_tasks, model_policy
from app.control_store import ControlStore


def envelope(data, code=0):
    return {"code": code, "msg": "ok" if code == 0 else "synthetic rejection", "data": data}


def raw_task(code, *, current=0, target=1, accept="accepted", locked=False, reward=(100, 5)):
    return {"task_code": code, "title": code, "task_desc": "", "locked": locked,
            "accept_status": accept, "current": current, "target": target,
            "reward_credit": reward[0], "reward_energy": reward[1]}


class FakeGrowth:
    """Record growth-domain requests and answer from a scripted table."""

    def __init__(self, tasks_default, tasks_mp, *, claims=None, claim_status=200):
        self.requests = []
        self.claims = dict(claims or {})
        self.claim_status = claim_status
        self.tasks_default, self.tasks_mp = tasks_default, tasks_mp

    def transport(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        platform = request.headers.get("x-client-platform")
        if method == "GET" and path == growth_tasks.TASKS_PATH:
            tasks = self.tasks_mp if platform == growth_tasks.MP_PLATFORM else self.tasks_default
            return httpx.Response(200, json=envelope({"tasks": tasks}))
        if method == "POST" and path == growth_tasks.ACCEPT_PATH:
            return httpx.Response(200, json=envelope({}))
        if method == "POST" and path.startswith("/activity/growth/tasks/") and path.endswith("/claim"):
            code = request.url.path.split("/")[-2]
            if self.claim_status != 200:
                return httpx.Response(self.claim_status, json=envelope({}, code=15003))
            payload = self.claims.get(code, {"already_claimed": False, "credit": 100, "energy": 5})
            return httpx.Response(200, json=envelope(payload))
        return httpx.Response(404, json=envelope({}, code=404))

    def requests_to(self, path, *, platform=None, method="POST"):
        return [request for request in self.requests
                if request.url.path == path and request.method == method
                and (platform is None or request.headers.get("x-client-platform") == platform)]


def with_fake(fake, function, *args, **kwargs):
    transport = httpx.MockTransport(fake.transport)
    real_client = httpx.Client

    def make_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    with patch.object(growth_tasks.httpx, "Client", make_client):
        return function(*args, **kwargs)


class ListMergeTests(unittest.TestCase):
    def test_mp_superset_merge_dedupes_with_progress_object(self):
        default_tasks = [raw_task("chat_5", current=3, target=5),
                         raw_task("first_buddy", accept="claimed")]
        mp_tasks = [raw_task("chat_5", current=5, target=5),
                    raw_task("Sequential_Tasks_1", current=0, target=1, accept="not_accepted"),
                    raw_task("wb_wechat_oa_subscribe_task", current=1, target=1)]
        fake = FakeGrowth(default_tasks, mp_tasks)
        tasks = with_fake(fake, growth_tasks.merged_tasks, "tok")
        by_code = {task["task_code"]: task for task in tasks}
        self.assertEqual(len(tasks), 4)
        self.assertEqual(by_code["chat_5"]["current"], 5)     # mp 口径覆盖同名任务
        self.assertTrue(by_code["chat_5"]["claimable"])
        self.assertFalse(by_code["first_buddy"]["claimable"])
        self.assertTrue(by_code["first_buddy"]["claimed"])
        self.assertEqual(by_code["Sequential_Tasks_1"]["group"], "miniprogram")

    def test_claimable_requires_progress_not_accept_status(self):
        task = growth_tasks._parse_task({"task_code": "expert_5", "accept_status": "completed",
                                         "target": 5, "current": 2})
        self.assertFalse(task["claimable"])   # completed 会回滚：只有 progress 达标才算

    def test_claim_headers_carry_web_shape(self):
        fake = FakeGrowth([], [], claims={"chat_5": {"already_claimed": True, "credit": 0, "energy": 0}})
        credit, energy, already = with_fake(fake, growth_tasks.claim_reward, "tok", "chat_5",
                                            "u1", "workbuddy.cn")
        self.assertEqual((credit, energy, already), (0, 0, True))
        request = fake.requests_to("/activity/growth/tasks/chat_5/claim", platform="web")[0]
        self.assertEqual(request.headers["origin"], growth_tasks.HOST)
        self.assertEqual(request.headers["referer"], growth_tasks.HOST + "/profile/growth-center")
        self.assertEqual(request.headers["x-user-id"], "u1")
        self.assertEqual(request.headers["x-domain"], "workbuddy.cn")
        self.assertEqual(request.headers["content-type"], "application/json")   # 面板同款形状


class ClaimFallbackTests(unittest.TestCase):
    def test_mp_claim_falls_back_to_web_on_400(self):
        fake = FakeGrowth([], [])

        original = fake.transport
        def mp_rejects(request):
            response = original(request)
            if request.headers.get("x-client-platform") == growth_tasks.MP_PLATFORM \
                    and request.url.path.endswith("/claim"):
                return httpx.Response(400, json=envelope({}, code=15003))
            return response
        fake.transport = mp_rejects
        credit, _energy, already = with_fake(fake, growth_tasks.claim_reward, "tok",
                                             "Sequential_Tasks_1", platform=growth_tasks.MP_PLATFORM)
        self.assertEqual((credit, already), (100, False))
        self.assertEqual(len(fake.requests_to("/activity/growth/tasks/Sequential_Tasks_1/claim",
                                              platform=growth_tasks.MP_PLATFORM)), 1)
        self.assertEqual(len(fake.requests_to("/activity/growth/tasks/Sequential_Tasks_1/claim",
                                              platform="web")), 1)

    def test_web_claim_failure_stays_web(self):
        fake = FakeGrowth([], [], claim_status=400)
        with self.assertRaises(growth_tasks.Failure):
            with_fake(fake, growth_tasks.claim_reward, "tok", "chat_5")
        self.assertEqual(len(fake.requests_to("/activity/growth/tasks/chat_5/claim")), 1)


class SweepTests(unittest.TestCase):
    def test_sweep_accepts_and_claims_across_platforms(self):
        default_tasks = [raw_task("chat_5", current=5, target=5, accept="accepted"),
                         raw_task("RichMeow_Chat", accept="not_accepted", locked=False)]
        mp_tasks = [raw_task("Sequential_Tasks_1", current=1, target=1, accept="not_accepted"),
                    raw_task("first_buddy", accept="claimed"),
                    raw_task("Library_read", locked=True)]
        fake = FakeGrowth(default_tasks, mp_tasks)
        summary = with_fake(fake, growth_tasks.sweep_account, "tok")
        self.assertEqual(summary["ok"], True)
        self.assertEqual(sorted(summary["claimed"]), ["Sequential_Tasks_1", "chat_5"])
        self.assertEqual(summary["credit"], 200)
        self.assertEqual(summary["accepted"], ["RichMeow_Chat", "Sequential_Tasks_1"])
        self.assertEqual(len(fake.requests_to(growth_tasks.ACCEPT_PATH, platform="web")), 1)
        self.assertEqual(len(fake.requests_to(growth_tasks.ACCEPT_PATH,
                                              platform=growth_tasks.MP_PLATFORM)), 1)
        self.assertEqual(len(fake.requests_to("/activity/growth/tasks/chat_5/claim")), 1)
        self.assertEqual(fake.requests_to("/activity/growth/tasks/first_buddy/claim"), [])
        self.assertEqual(fake.requests_to("/activity/growth/tasks/Library_read/claim"), [])

    def test_single_failure_does_not_abort(self):
        default_tasks = [raw_task("chat_5", current=5, target=5),
                         raw_task("create_canvas", current=1, target=1)]
        fake = FakeGrowth(default_tasks, [])
        fake.claims["chat_5"] = None
        original_transport = fake.transport

        def flaky(request):
            if request.url.path.endswith("/chat_5/claim"):
                return httpx.Response(500)
            return original_transport(request)

        fake.transport = flaky
        summary = with_fake(fake, growth_tasks.sweep_account, "tok")
        self.assertEqual(summary["claimed"], ["create_canvas"])
        self.assertEqual(summary["errors"], 1)
        self.assertFalse(summary["ok"])

    def test_account_restriction_aborts_remaining_claims(self):
        default_tasks = [raw_task("chat_5", current=5, target=5),
                         raw_task("create_canvas", current=1, target=1)]
        fake = FakeGrowth(default_tasks, [])
        original_transport = fake.transport

        def restricted(request):
            if request.url.path.endswith("/chat_5/claim"):
                return httpx.Response(403, json=envelope({}, code=11140))
            return original_transport(request)

        fake.transport = restricted
        summary = with_fake(fake, growth_tasks.sweep_account, "tok")
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["error"]["code"], 11140)
        self.assertEqual(summary["claimed"], [])
        self.assertEqual(fake.requests_to("/activity/growth/tasks/create_canvas/claim"), [])

    def test_idempotent_second_pass_makes_no_write(self):
        tasks = [raw_task("chat_5", current=5, target=5, accept="claimed")]
        first = FakeGrowth([raw_task("chat_5", current=5, target=5, accept="accepted")], [])
        with_fake(first, growth_tasks.sweep_account, "tok")
        second = FakeGrowth(tasks, [])
        summary = with_fake(second, growth_tasks.sweep_account, "tok")
        self.assertEqual((summary["accepted"], summary["claimed"]), ([], []))
        self.assertEqual(second.requests_to(growth_tasks.ACCEPT_PATH), [])
        self.assertEqual(second.requests_to("/activity/growth/tasks/chat_5/claim"), [])


class EligibilityTests(unittest.TestCase):
    def test_supported_matches_travel_personal_cn_matrix(self):
        # 面板/旅行同款口径：cn 区域个人账号（cn-cli 与 cn-work）均可参与。
        self.assertTrue(growth_tasks.supported("cn-work"))
        self.assertTrue(growth_tasks.supported("cn-cli"))
        for profile, enterprise in (("cn-work", True), ("cn-cli", True), ("intl-work", False),
                                    ("intl-cli", True)):
            self.assertFalse(growth_tasks.supported(profile, enterprise))

    def test_policy_gates(self):
        import tempfile
        config = {"control_store": None}
        personal = {"profile": "cn-work", "enterprise": False, "account_key": "acct"}
        enterprise = {"profile": "cn-work", "enterprise": True}
        self.assertTrue(model_policy.credential_auto_growth(config, personal))
        self.assertFalse(model_policy.credential_auto_growth(config, enterprise))
        self.assertFalse(model_policy.credential_auto_growth(config, {"profile": "intl-work"}))
        self.assertTrue(model_policy.credential_auto_growth(config, {"profile": "cn-cli"}))
        self.assertEqual(model_policy.growth_auto_tasks(config, personal), frozenset())
        store = ControlStore(Path(tempfile.mkdtemp()) / "control.sqlite3")
        store.set_growth_auto("acct", {"chat_5": True, "expert_5": False})
        config = {"control_store": store}
        self.assertEqual(model_policy.growth_auto_tasks(config, personal), frozenset({"chat_5"}))
        store.set_auto_growth("acct", False)
        self.assertEqual(model_policy.growth_auto_tasks(config, personal), frozenset())


class StoreSwitchTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.path = Path(tempfile.mkdtemp()) / "control.sqlite3"
        self.store = ControlStore(self.path)

    def test_growth_switch_roundtrip_and_validation(self):
        self.store.set_auto_growth("acct", False)
        self.store.set_growth_auto("acct", {"chat_5": True})
        reopened = ControlStore(self.path)
        metadata = reopened.snapshot()["credentials"]["acct"]
        self.assertEqual((metadata["auto_growth"], metadata["growth_auto"]), (False, {"chat_5": True}))
        with self.assertRaises(ValueError):
            self.store.set_growth_auto("acct", {"unknown_code": True})
        with self.assertRaises(ValueError):
            self.store.set_auto_growth("acct", "yes")

    def test_rejects_unknown_metadata_keys(self):
        # 写路径不做元数据校验（与既有字段一致），损坏在下次打开时被拒绝。
        self.store._update(None, lambda state: state["credentials"].setdefault(
            "acct", {"enabled": True}).update(growth_auto={"nope": True}))
        with self.assertRaises(ValueError):
            ControlStore(self.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
