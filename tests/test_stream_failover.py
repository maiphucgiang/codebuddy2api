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
# 上游没收下请求体：换连接/换账号重放安全
REPLAYABLE_TRANSPORT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.WriteTimeout)
# 请求体已经发出去了（甚至响应已经开始）：上游可能已处理并计费，禁止重放
AMBIGUOUS_TRANSPORT = (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                       httpx.RemoteProtocolError)
FAILOVER_LOG = "换凭证重放"


def error_body(message="synthetic rejection", code="rate_limit"):
    return {"error": {"message": message, "type": "upstream_error", "code": code}}


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
    def audited_client(self):
        store = AuditStore(self.root / "failover-audit.sqlite3")
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
