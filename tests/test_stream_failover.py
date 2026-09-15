#!/usr/bin/env python3
"""流式换凭证重放回归（`--failover-max`）：本地换账号重试，而不是把 429/502 甩给下游。

背景：流式请求在「一个字节都还没发给下游」时失败，已经被 `_preflight_stream` 还原成真实
状态码（见 tests/test_stream_status_contract.py）。但还原成 429 只是诚实，不是解决问题 ——
限流/认证/网关抖动这类失败换一个账号大概率就能成，下游（尤其 Codex CLI）不该看到 502。

这里钉住重放的边界：
  - 默认关闭（`failover_max=0`）时行为与上游完全一致：一次都不多重放，如实回真实状态码；
  - 开启后只在确定「上游没收下请求体 / 上游用 HTTP 状态码拒绝」时重放，且必须换凭证；
  - 审核拒绝、聚合器合成的 502（上游已回 200，可能已计费）永不重放；
  - 重放次数有上界，换不出别的凭证时如实回第一次的状态码，绝不死循环。
运行：.venv/bin/python -B -m unittest -v tests/test_stream_failover.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import converter
from app.audit_store import AuditStore
from app.observability import AuditMiddleware
from tests import test_region_routing as fixtures

REPLAYABLE_STATUS = (401, 403, 429, 502, 503, 504)
DETERMINISTIC_STATUS = (400, 404, 405, 413, 422)
# 上游手里没有任何正文：换连接/换账号重放安全
REPLAYABLE_TRANSPORT = (httpx.ConnectError, httpx.ConnectTimeout)
# 写超时：正文没写完是确定的，是否已按半截正文计费看不到，只有显式 opt-in 才参与重放
WRITE_TIMEOUT_TRANSPORT = (httpx.WriteTimeout,)
# 请求体已经发出去了（甚至响应已经开始）：上游可能已处理并计费，禁止重放
AMBIGUOUS_TRANSPORT = (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                       httpx.RemoteProtocolError)
FAILOVER_LOG = "换凭证重放"


def error_body(message="synthetic rejection", code="rate_limit"):
    return {"error": {"message": message, "type": "upstream_error", "code": code}}


def filtered_sse():
    """上游正常回 200、结果却是审核拒绝：聚合路径会在 `fetch` 返回**之前**就记上这次失败。

    只用 `finish_reason` 触发检测（`ContentFilterDetector.feed` 认 `content_filter`），
    不依赖任何拒绝文案，免得上游改措辞就把测试带崩。
    """
    chunks = [
        {"id": "synthetic-completion", "choices": [{"index": 0,
         "delta": {"role": "assistant", "content": "blocked"}, "finish_reason": None}]},
        {"id": "synthetic-completion", "choices": [{"index": 0,
         "delta": {}, "finish_reason": "content_filter"}],
         "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
    ]
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode()


@contextmanager
def allow_failover(times: int):
    """打开换凭证重放开关（等价于 --failover-max N）。"""
    with patch.dict(converter.CONFIG, {"failover_max": times}):
        yield


class StreamFailoverTests(fixtures.RegionRoutingTests):
    """复用四档合成凭证 + MockTransport，只把「被点名的那一个凭证」改成会失败。"""

    def setUp(self):
        super().setUp()
        self.arm_next = False
        self.stream = True
        self.poison_uid = None
        self.poison_once = False
        self.poison = lambda request: httpx.Response(429, json=error_body())
        self.logs = []
        self.enterContext(patch.object(converter, "_log", side_effect=self.capture_log))
        self.allowed_profiles = set(fixtures.PROFILES)

    def fresh_pool(self):
        """重建凭证池：401/403 熔断与 429 冷却都是池内状态，子例之间必须清干净。"""
        self.configure()
        self.allowed_profiles = set(fixtures.PROFILES)

    def capture_log(self, message, *args, **kwargs):
        self.logs.append(str(message))

    def handle_upstream(self, request):
        uid = request.headers.get("x-user-id")
        if self.arm_next:
            self.arm_next = False
            self.poison_uid = uid          # 不依赖轮询顺序：下一个被选中的凭证开始失败
        if self.poison_uid is not None and uid == self.poison_uid:
            self.requests.append(request)
            if self.poison_once:
                self.poison_uid = None     # 只掐第一枪：后面那一枪是同一凭证上的重放
            return self.poison(request)    # 可以返回错误状态，也可以直接抛传输层异常
        return super().handle_upstream(request)

    def poison_with_status(self, status, body=None):
        self.poison_uid = None
        self.arm_next = True
        payload = json.dumps(body or error_body()).encode()
        self.poison = lambda request: httpx.Response(status, content=payload,
                                                    headers={"content-type": "application/json"})

    def poison_with_transport(self, error_type):
        self.poison_uid = None
        self.arm_next = True
        def raise_transport(request):
            raise error_type("synthetic transport failure")
        self.poison = raise_transport

    def poison_transport_once(self, error_type):
        """只让第一枪失败：测「同一连接/同一凭证」的底层重放，换凭证那条日志压根到不了。"""
        self.poison_with_transport(error_type)
        self.poison_once = True

    def stream_post(self, endpoint="chat/completions"):
        self.allowed_profiles = set(fixtures.PROFILES)
        before = len(self.requests)
        response = self.client.post("/v1/" + endpoint, json=self.payload(endpoint, stream=self.stream))
        return response, self.requests[before:]

    def uids(self, requests):
        return [request.headers.get("x-user-id") for request in requests]

    def failover_lines(self):
        return [line for line in self.logs if FAILOVER_LOG in line]

    # --- 开启后：下游只看到一次正常成功 ---
    def test_429_is_replayed_on_another_credential(self):
        with allow_failover(1):
            self.poison_with_status(429)
            response, sent = self.stream_post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("ok", response.text)
        self.assertIn("data: [DONE]", response.text)
        self.assertEqual(len(sent), 2, "应当恰好重放一次")
        self.assertEqual(len(set(self.uids(sent))), 2, "重放必须换凭证")
        self.assertEqual(len(self.failover_lines()), 1, self.failover_lines())

    def test_replay_log_separates_the_maybe_billed_class(self):
        """受理期拒绝不标风险；502/504 可能已被后端处理并计费，必须在日志里单独标出来。

        重放的取舍不是「省钱 vs 花钱」：这类失败连响应头都没有，那次结果对下游永远拿不到，
        不重放也退不回额度，只是把一次已付费的请求换成一段断掉的会话。所以保留重放，但要
        如实标注，便于事后按官方用量明细核对。
        """
        for status, marked in ((429, False), (401, False), (403, False), (503, False),
                               (502, True), (504, True)):
            with self.subTest(status=status):
                self.fresh_pool()
                self.logs.clear()
                with allow_failover(1):
                    self.poison_with_status(status)
                    response, sent = self.stream_post()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(sent), 2)
                lines = self.failover_lines()
                self.assertEqual(len(lines), 1, lines)
                self.assertEqual(("上游可能已处理该请求" in lines[0]), marked, lines[0])

    def test_every_stream_endpoint_can_fail_over(self):
        for endpoint in fixtures.GENERATIONS:
            with self.subTest(endpoint=endpoint):
                self.fresh_pool()
                with allow_failover(1):
                    self.poison_with_status(429)
                    response, sent = self.stream_post(endpoint)
                terminal = {"chat/completions": "data: [DONE]",
                            "responses": '"response.completed"',
                            "messages": "event: message_stop"}[endpoint]
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("ok", response.text)
                self.assertIn(terminal, response.text)
                self.assertEqual(len(sent), 2)
                self.assertEqual(len(set(self.uids(sent))), 2)

    def test_failover_limit_bounds_upstream_attempts(self):
        self.response_status = 429          # 所有凭证都失败
        for maximum, expected in ((1, 2), (2, 3)):
            with self.subTest(failover_max=maximum):
                self.fresh_pool()      # 上一轮的 429 冷却会让这一轮少打一次上游
                self.requests.clear()
                self.logs.clear()
                with allow_failover(maximum):
                    response = self.client.post("/v1/chat/completions",
                                                json=self.payload(stream=True))
                self.assertEqual(response.status_code, 429, response.text)
                self.assertEqual(len(self.requests), expected,
                                 f"重放次数必须恰好等于 failover_max={maximum}")
                self.assertEqual(len(self.failover_lines()), maximum, self.failover_lines())
                self.assertEqual(len(set(self.uids(self.requests))), expected, "每轮都必须是新凭证")

    def test_last_resort_surfaces_the_real_status(self):
        """换不出别的凭证（只剩一个账号）时，如实回第一次的状态码，且不再打上游。"""
        self.configure(profiles=("cn-cli",))
        self.allowed_profiles = {"cn-cli"}
        with allow_failover(3):
            self.poison_with_status(429)
            response, sent = self.stream_post()
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(len(sent), 1, "无凭证可换时不得重复打上游")

    def test_reroute_cannot_loop_back_to_the_same_credential(self):
        """重放选回同一个凭证时（单凭证池/黏绑）必须立刻收敛，不能死循环。"""
        self.configure(profiles=("cn-cli", "cn-work"))
        self.allowed_profiles = set(fixtures.PROFILES)
        with allow_failover(5):
            self.response_status = 401
            response = self.client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 401, response.text)
        self.assertLessEqual(len(self.requests), 3, "每轮都要换新凭证，池子耗尽即停")
        self.assertEqual(len(set(self.uids(self.requests))), len(self.requests),
                         "同一凭证不得被打两次")

    # --- 重路由必须沿用客户端请求的模型，不得被上游改写名绕过 ---
    AUTO_PROFILES = ("intl-work", "intl-cli")

    def arm_auto(self, profiles=None):
        """账号目录都含 default-model：国际站会把 `auto` 改写成它，国内站不会 —— 所以放宽只有
        在「改写后的名字」上查规则才会发生，站点绑定那条用例要靠国内站账号当靶子。"""
        self.auto_profiles = tuple(profiles or self.AUTO_PROFILES)
        self.configure(profiles=self.auto_profiles,
                       tables={profile: [fixtures.model("default-model")] for profile in self.auto_profiles})

    def bind_auto(self, name, **rule):
        """建一个管理库并给 `auto` 下一条路由策略。"""
        from app import model_policy
        from app.control_store import ControlStore

        store = ControlStore(self.root / name)
        self.addCleanup(store.close)
        converter.CONFIG["control_store"] = store
        store.update_model("auto", dict(model_policy.default_rule("auto"), **rule), 0)
        return store

    def auto_post(self):
        """发一次 model=auto 的流式请求，返回下游响应与真正打出去的上游请求。"""
        self.allowed_profiles = set(self.auto_profiles)
        before = len(self.requests)
        response = self.client.post("/v1/chat/completions",
                                    json=self.payload(stream=True, selected_model="auto"))
        return response, self.requests[before:]

    def test_auto_is_rewritten_on_the_international_site(self):
        """夹具自检：国际站确实把 auto 发成了 default-model，否则下面几条等于没测。"""
        self.arm_auto()
        response, sent = self.auto_post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(sent), 1)
        import json as _json
        self.assertEqual(_json.loads(sent[0].content)["model"], "default-model")

    def test_failover_cannot_widen_the_credential_binding(self):
        """把 `auto` 只绑到 A：A 失败后不许放宽给 B。

        `_route_chat` 会把 auto 改写成 default-model 再发出去；若拿改写后的正文重新选
        凭证，策略查的是 default-model（没有规则），绑定在 auto 上的限制整个失效。
        """
        self.arm_auto()
        self.bind_auto("bind.sqlite3", credential_ids=[self.entries["intl-work"]["account_key"]])
        with allow_failover(1):
            self.poison_with_status(503)
            response, sent = self.auto_post()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(len(sent), 1,
                         f"绑定 auto 的账号失败后不得放宽给别的账号：{self.uids(sent)}")

    def test_failover_cannot_widen_the_site_binding(self):
        """同上，按站点绑定：`auto` 限定 intl 时，重放不许跑到国内站。"""
        self.arm_auto(("intl-work", "cn-work"))
        self.bind_auto("region.sqlite3", region="intl")
        with allow_failover(3):
            self.poison_with_status(503)
            response, sent = self.auto_post()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(len(sent), 1, f"站点绑定被放宽：{self.uids(sent)}")

    def test_reroute_uses_the_pristine_body_model(self):
        """直接钉住重路由入参：每一轮选凭证看的都必须是客户端请求的模型名。"""
        self.arm_auto()
        self.bind_auto("spy.sqlite3", region="intl")
        seen = []
        real = converter._route_chat

        def spy(payload, body, rid, **kwargs):
            seen.append(body.get("model"))
            return real(payload, body, rid, **kwargs)

        with allow_failover(1), patch.object(converter, "_route_chat", side_effect=spy):
            self.poison_with_status(503)
            response, sent = self.auto_post()
        self.assertEqual(len(seen), 2, f"应当恰好发生一次重路由：{seen}")
        self.assertEqual([name for name in seen if name != "auto"], [],
                         f"重路由拿到了被改写的模型名：{seen}")

    def test_same_credential_write_timeout_replay_carries_the_risk_note(self):
        """评审 P2：底层连接上的写超时重放也必须带代价标记，两层同一口径。

        第一次写超时、同一凭证第二次就成 —— 这条路径到不了换凭证那行日志，`failover_max=0`
        时更是完全不经过它，所以标注只能由 `open_backend_stream` 的重试回调自己带上。
        """
        store, client = self.audited_client()
        with patch.dict(converter.CONFIG, {"retry_write_timeout": True, "failover_max": 0}):
            self.poison_transport_once(httpx.WriteTimeout)
            response = client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.requests), 2, self.uids(self.requests))
        self.assertEqual(len(set(self.uids(self.requests))), 1,
                         f"底层重放不许换凭证：{self.uids(self.requests)}")
        self.assertEqual(self.failover_lines(), [], "不该出现换凭证重放的日志")
        lines = [line for line in self.logs if "写超时重放" in line]
        self.assertEqual(len(lines), 1, self.logs)
        self.assertIn("上游可能已处理该请求", lines[0])
        stages = [attempt.get("stage") for attempt in self.only_record(store)["attempts"]]
        self.assertIn("write_timeout_retry", stages, stages)

    def test_connect_retry_stays_untagged(self):
        """建连失败不标记风险：上游手里没有正文，重放确定不重复计费。"""
        self.audited_client()
        with patch.dict(converter.CONFIG, {"failover_max": 0}):
            self.poison_transport_once(httpx.ConnectError)
            response = self.client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 200, response.text)
        lines = [line for line in self.logs if "建连失败" in line]
        self.assertEqual(len(lines), 1, self.logs)
        self.assertNotIn("上游可能已处理该请求", lines[0])

    # --- 默认关闭：与上游一致，一次都不多重放 ---
    def test_disabled_by_default_replays_nothing(self):
        self.assertEqual(converter.CONFIG["failover_max"], 0, "默认必须关闭，行为与上游一致")
        for status in REPLAYABLE_STATUS:
            for stream in (True, False):
                with self.subTest(status=status, stream=stream):
                    self.fresh_pool()
                    self.stream = stream
                    self.requests.clear()
                    self.logs.clear()
                    self.poison_with_status(status, error_body(code="quota"))
                    response, sent = self.stream_post()
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(len(sent), 1, "关闭时禁止任何重放")
                    self.assertEqual(self.failover_lines(), [])
        self.stream = True

    # --- 审计口径：重放救回来的请求不得留在 error ---
    def audited_client(self, name="failover-audit"):
        store = AuditStore(self.root / f"{name}.sqlite3")   # 一条用例要对比两行审计时分开落盘
        self.addCleanup(store.close)
        application = FastAPI()
        application.router.routes = list(converter.app.router.routes)
        application.add_middleware(AuditMiddleware, store)
        return store, self.enterContext(TestClient(application))

    def only_record(self, store):
        records = store.list_records()["items"]
        self.assertEqual(len(records), 1, records)
        return records[0]

    def test_replayed_request_is_audited_as_success(self):
        """重放成功的请求审计必须是 success，不能又退回「error + 200」这个骗人的签名。"""
        store, client = self.audited_client()
        with allow_failover(1):
            self.poison_with_status(429)
            response = client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 200, response.text)
        record = self.only_record(store)
        self.assertEqual(record["outcome"], "success", record)
        self.assertFalse(record.get("error_code"), record)
        statuses = [attempt.get("status_code") for attempt in record["attempts"]]
        self.assertEqual(statuses[:2], [429, 200], record["attempts"])
        self.assertIn("failover_recovered", json.dumps(record["attempts"], ensure_ascii=False))
        markers = [attempt for attempt in record["attempts"]
                   if attempt.get("stage") == "failover_recovered"]
        self.assertEqual([attempt.get("code") for attempt in markers], ["upstream_429"],
                         "恢复标记必须点名「被撤销的那一次失败」本身")

    def test_content_filter_after_replay_is_still_audited_as_filtered(self):
        """换到的账号回了审核拒绝：那是这一枪的真实结果，不能被上一枪 429 的重放抹掉。

        没有修复前的样子：`outcome=success` + `error_code` 为空 + 恢复标记写着
        `content_filter` —— 等于把一次被拦截的请求记成了一次干净的成功。
        """
        store, client = self.audited_client()
        with allow_failover(1), patch.object(fixtures, "success_sse", filtered_sse):
            self.poison_once = True
            self.poison_with_status(429)
            response = client.post("/v1/chat/completions", json=self.payload(stream=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.failover_lines()), 1, self.logs)
        record = self.only_record(store)
        self.assertEqual(record["outcome"], "error", record)
        self.assertEqual(record["error_code"], "content_filter", record)
        self.assertNotIn("failover_recovered",
                         json.dumps(record["attempts"], ensure_ascii=False), record["attempts"])

    def test_content_filter_audit_matches_the_unreplayed_request(self):
        """同一次审核拒绝，走没走过重放必须是同一行审计：重放不该改变账单口径。"""
        with patch.object(fixtures, "success_sse", filtered_sse):
            store, client = self.audited_client("filter-without-replay")
            plain = client.post("/v1/chat/completions", json=self.payload(stream=False))
            self.assertEqual(plain.status_code, 200, plain.text)
            baseline = self.only_record(store)
        self.assertEqual(baseline["outcome"], "error", baseline)
        self.assertEqual(baseline["error_code"], "content_filter", baseline)
        store, client = self.audited_client("filter-with-replay")
        with allow_failover(1), patch.object(fixtures, "success_sse", filtered_sse):
            self.poison_once = True
            self.poison_with_status(429)
            replayed = client.post("/v1/chat/completions", json=self.payload(stream=False))
        self.assertEqual(replayed.status_code, 200, replayed.text)
        record = self.only_record(store)
        for key in ("outcome", "error_code", "status_code"):
            self.assertEqual(record[key], baseline[key], (key, record, baseline))

    def test_content_filter_after_replay_survives_the_aggregated_stream(self):
        """带 tools 的流式在预取里就跑完整聚合，`_stream_plan` 那条顺序陷阱一模一样。"""
        store, client = self.audited_client()
        payload = self.payload(stream=True)
        payload["tools"] = [{"type": "function", "function": {"name": "synthetic_tool",
                                                             "parameters": {"type": "object"}}}]
        with allow_failover(1), patch.object(fixtures, "success_sse", filtered_sse):
            self.poison_once = True
            self.poison_with_status(429)
            response = client.post("/v1/chat/completions", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.failover_lines()), 1, self.logs)
        record = self.only_record(store)
        self.assertEqual(record["outcome"], "error", record)
        self.assertEqual(record["error_code"], "content_filter", record)

    def test_unreplayed_failure_is_still_audited_as_error(self):
        store, client = self.audited_client()
        self.poison_with_status(429)
        response = client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 429, response.text)
        record = self.only_record(store)
        self.assertEqual(record["outcome"], "error", record)
        self.assertEqual(record["error_code"], "upstream_429", record)

    # --- 重放判定矩阵：只重放「上游确定没收下/没处理」的失败 ---
    def test_replayable_http_statuses(self):
        for status in REPLAYABLE_STATUS:
            with self.subTest(status=status):
                self.assertTrue(converter._failover_safe(
                    converter.UpstreamHTTPError(status, b'{"error":{"code":"quota"}}')))

    def test_deterministic_http_statuses_are_not_replayed(self):
        for status in DETERMINISTIC_STATUS:
            with self.subTest(status=status):
                self.assertFalse(converter._failover_safe(
                    converter.UpstreamHTTPError(status, b'{"error":{"code":"bad_request"}}')))

    def test_aggregator_synthesized_502_is_not_replayed(self):
        """上游已经回了 200，聚合器合成的 502 可能对应已计费的请求：不换账号重放。"""
        self.assertFalse(converter._failover_safe(
            converter.UpstreamResponseError(502, json.dumps(error_body(code="empty_response")).encode())))
        with allow_failover(2):
            self.poison_uid = None
            self.arm_next = True
            self.poison = lambda request: httpx.Response(
                200, content=b"", headers={"content-type": "text/event-stream"})
            response, sent = self.stream_post()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(len(sent), 1, "空流不得重放")
        self.assertEqual(self.failover_lines(), [])

    def test_filter_rejection_is_never_replayed(self):
        """内容审核是模型的真实答复，换账号只会再撞同一堵墙，还会白烧一次额度。"""
        refusal = error_body("请求包含违规内容，已被拦截", code="content_filter")
        for status in (403, 429):
            with self.subTest(status=status):
                self.fresh_pool()
                with allow_failover(2):
                    self.poison_with_status(status, refusal)
                    response, sent = self.stream_post()
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(len(sent), 1)
                self.assertEqual(self.failover_lines(), [])
        self.assertFalse(converter._failover_safe(
            converter.UpstreamHTTPError(403, json.dumps(refusal).encode()),
            json.dumps(refusal).encode()))

    def test_transport_before_request_body_fails_over(self):
        for error_type in REPLAYABLE_TRANSPORT:
            with self.subTest(error=error_type.__name__):
                self.requests.clear()
                self.logs.clear()
                with allow_failover(1):
                    self.poison_with_transport(error_type)
                    response, sent = self.stream_post()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("data: [DONE]", response.text)
                # 建连失败/写超时在 open_backend_stream 内部已换连接重试一次，再换凭证重放一次
                self.assertGreaterEqual(len(sent), 2)
                self.assertNotEqual(len(set(self.uids(sent))), 1, "必须换过凭证")
                self.assertEqual(len(self.failover_lines()), 1, self.failover_lines())

    def test_write_timeout_needs_an_explicit_opt_in(self):
        """默认：写超时按歧义处理——如实回 502，一次都不多重放。"""
        self.assertEqual(converter.CONFIG["retry_write_timeout"], False, "默认必须关闭")
        for error_type in WRITE_TIMEOUT_TRANSPORT:
            with self.subTest(error=error_type.__name__):
                self.fresh_pool()
                self.requests.clear()
                self.logs.clear()
                with allow_failover(2):
                    self.poison_with_transport(error_type)
                    response, sent = self.stream_post()
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(len(sent), 1, "未开启 opt-in 时禁止重放")
                self.assertEqual(self.failover_lines(), [])

    def test_write_timeout_opt_in_replays_and_flags_the_billing_risk(self):
        """开启 `--retry-write-timeout`：重放救回会话，但必须在日志里标出计费歧义。"""
        for error_type in WRITE_TIMEOUT_TRANSPORT:
            with self.subTest(error=error_type.__name__):
                self.fresh_pool()
                self.requests.clear()
                self.logs.clear()
                with allow_failover(1), patch.dict(converter.CONFIG, {"retry_write_timeout": True}):
                    self.poison_with_transport(error_type)
                    response, sent = self.stream_post()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("data: [DONE]", response.text)
                self.assertGreaterEqual(len(sent), 2, "应当重放过")
                lines = self.failover_lines()
                self.assertTrue(lines, "重放必须留日志")
                self.assertIn("上游可能已处理该请求", lines[0], lines[0])

    def test_write_timeout_opt_in_does_not_replay_ambiguous_transport(self):
        """开关只管写超时：读超时/中途 reset 这些歧义失败照旧禁止重放。"""
        with patch.dict(converter.CONFIG, {"retry_write_timeout": True}):
            for error_type in AMBIGUOUS_TRANSPORT:
                with self.subTest(error=error_type.__name__):
                    self.fresh_pool()
                    self.requests.clear()
                    with allow_failover(2):
                        self.poison_with_transport(error_type)
                        response, sent = self.stream_post()
                    self.assertEqual(response.status_code, 502, response.text)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(self.failover_lines(), [])

    def test_failover_switches_are_hot_public_settings(self):
        """两个开关都必须能在大控制台「系统设置」里改，且不需要重启进程。"""
        from app import settings

        self.assertEqual(converter.CONFIG["failover_max"], 0)
        self.assertEqual(converter.CONFIG["retry_write_timeout"], False)
        for key, value in (("failover_max", 2), ("retry_write_timeout", True)):
            with self.subTest(key=key):
                spec = settings.SCHEMA[key]
                self.assertEqual(spec["mode"], "hot", f"{key} 改了要能立即生效，不能要求重启")
                self.assertFalse(spec["sensitive"], "运维开关必须对管理台可见")
                self.assertEqual(settings.validate_settings({key: value}), {key: value})
                listed = {item["key"]: item for item in
                          settings.resolve_settings({"failover_max": 2, "retry_write_timeout": True})}
                self.assertIn(key, listed)
                self.assertFalse(listed[key]["locked"], listed[key])
        with self.assertRaises(ValueError):
            settings.validate_settings({"failover_max": 99})       # 上界 10
        with self.assertRaises(ValueError):
            settings.validate_settings({"retry_write_timeout": "yes"})

    def test_ambiguous_transport_never_fails_over(self):
        for error_type in AMBIGUOUS_TRANSPORT:
            with self.subTest(error=error_type.__name__):
                self.fresh_pool()
                self.requests.clear()
                self.logs.clear()
                with allow_failover(2):
                    self.poison_with_transport(error_type)
                    response, sent = self.stream_post()
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(len(sent), 1, "请求体可能已被上游处理，禁止换账号重放")
                self.assertEqual(self.failover_lines(), [])

    def test_non_streaming_request_is_replayed_too(self):
        """非流式一个字节都没回下游，判定同一流式口径：换凭证重放，下游只看到一次成功。"""
        with allow_failover(1):
            self.stream = False
            self.poison_with_status(429)
            response, sent = self.stream_post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(sent), 2, "应当恰好重放一次")
        self.assertEqual(len(set(self.uids(sent))), 2, "重放必须换凭证")
        self.assertEqual(len(self.failover_lines()), 1, self.failover_lines())
        self.stream = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
