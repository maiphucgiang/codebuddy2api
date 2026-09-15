#!/usr/bin/env python3
"""Test bounded pre-response credential failover, routing restrictions and billing-risk auditing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

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
# Connection failures occur before the request body is sent.
REPLAYABLE_TRANSPORT = (httpx.ConnectError, httpx.ConnectTimeout)
# Incomplete writes may already be billed and require explicit replay opt-in.
WRITE_TIMEOUT_TRANSPORT = (httpx.WriteTimeout,)
# Other post-send transport failures must not replay potentially billed requests.
AMBIGUOUS_TRANSPORT = (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                       httpx.RemoteProtocolError)
FAILOVER_LOG = "换凭证重放"


def error_body(message="synthetic rejection", code="rate_limit"):
    return {"error": {"message": message, "type": "upstream_error", "code": code}}


def filtered_sse():
    """Return HTTP-success SSE with an explicit filter finish reason independent of refusal wording."""
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
    """Enable the requested credential failover budget."""
    with patch.dict(converter.CONFIG, {"failover_max": times}):
        yield


class StreamFailoverTests(fixtures.RegionRoutingTests):
    """Use four synthetic credential profiles and inject failures into a selected account."""

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
        """Reset pool authentication and quota cooldowns between subtests."""
        self.configure()
        self.allowed_profiles = set(fixtures.PROFILES)

    def capture_log(self, message, *args, **kwargs):
        self.logs.append(str(message))

    def handle_upstream(self, request):
        uid = request.headers.get("x-user-id")
        if self.arm_next:
            self.arm_next = False
            self.poison_uid = uid          # Select the failing credential independently of rotation order.
        if self.poison_uid is not None and uid == self.poison_uid:
            self.requests.append(request)
            if self.poison_once:
                self.poison_uid = None     # Fail only the initial transport attempt.
            return self.poison(request)    # Inject HTTP or transport failures.
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
        """Fail the first attempt to exercise same-credential transport replay."""
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

    # Successful failover produces one downstream response.
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
        """Flag possible billing for replayed gateway failures but not admission rejections."""
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
        self.response_status = 429          # Reject every credential.
        for maximum, expected in ((1, 2), (2, 3)):
            with self.subTest(failover_max=maximum):
                self.fresh_pool()      # Remove cooldown state from the preceding subtest.
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
        """Preserve the failure status when no alternate credential exists."""
        self.configure(profiles=("cn-cli",))
        self.allowed_profiles = {"cn-cli"}
        with allow_failover(3):
            self.poison_with_status(429)
            response, sent = self.stream_post()
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(len(sent), 1, "无凭证可换时不得重复打上游")

    def test_reroute_cannot_loop_back_to_the_same_credential(self):
        """Stop failover when routing selects an already tried credential."""
        self.configure(profiles=("cn-cli", "cn-work"))
        self.allowed_profiles = set(fixtures.PROFILES)
        with allow_failover(5):
            self.response_status = 401
            response = self.client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 401, response.text)
        self.assertLessEqual(len(self.requests), 3, "每轮都要换新凭证，池子耗尽即停")
        self.assertEqual(len(set(self.uids(self.requests))), len(self.requests),
                         "同一凭证不得被打两次")

    # Rerouting must preserve policy for the original client model ID.
    AUTO_PROFILES = ("intl-work", "intl-cli")

    def arm_auto(self, profiles=None):
        """Advertise default-model across regions to detect policy bypass after auto alias rewriting."""
        self.auto_profiles = tuple(profiles or self.AUTO_PROFILES)
        self.configure(profiles=self.auto_profiles,
                       tables={profile: [fixtures.model("default-model")] for profile in self.auto_profiles})

    def bind_auto(self, name, **rule):
        """Create an explicit routing policy for the auto model."""
        from app import model_policy
        from app.control_store import ControlStore

        store = ControlStore(self.root / name)
        self.addCleanup(store.close)
        converter.CONFIG["control_store"] = store
        store.update_model("auto", dict(model_policy.default_rule("auto"), **rule), 0)
        return store

    def auto_post(self):
        """Send an auto-model request and return downstream and captured upstream responses."""
        self.allowed_profiles = set(self.auto_profiles)
        before = len(self.requests)
        response = self.client.post("/v1/chat/completions",
                                    json=self.payload(stream=True, selected_model="auto"))
        return response, self.requests[before:]

    def test_auto_is_rewritten_on_the_international_site(self):
        """Verify the fixture rewrites international auto requests to default-model."""
        self.arm_auto()
        response, sent = self.auto_post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(sent), 1)
        import json as _json
        self.assertEqual(_json.loads(sent[0].content)["model"], "default-model")

    def test_failover_cannot_widen_the_credential_binding(self):
        """Preserve auto-model credential restrictions when failover reroutes an aliased request."""
        self.arm_auto()
        self.bind_auto("bind.sqlite3", credential_ids=[self.entries["intl-work"]["account_key"]])
        with allow_failover(1):
            self.poison_with_status(503)
            response, sent = self.auto_post()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(len(sent), 1,
                         f"绑定 auto 的账号失败后不得放宽给别的账号：{self.uids(sent)}")

    def test_failover_cannot_widen_the_site_binding(self):
        """Keep international auto routing within its region during failover."""
        self.arm_auto(("intl-work", "cn-work"))
        self.bind_auto("region.sqlite3", region="intl")
        with allow_failover(3):
            self.poison_with_status(503)
            response, sent = self.auto_post()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(len(sent), 1, f"站点绑定被放宽：{self.uids(sent)}")

    def test_reroute_uses_the_pristine_body_model(self):
        """Use the client's original model ID for every credential selection."""
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
        """Flag same-credential write-timeout billing risk independently of credential failover."""
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
        """Exclude pre-send connection failures from possible billing warnings."""
        self.audited_client()
        with patch.dict(converter.CONFIG, {"failover_max": 0}):
            self.poison_transport_once(httpx.ConnectError)
            response = self.client.post("/v1/chat/completions", json=self.payload(stream=True))
        self.assertEqual(response.status_code, 200, response.text)
        lines = [line for line in self.logs if "建连失败" in line]
        self.assertEqual(len(lines), 1, self.logs)
        self.assertNotIn("上游可能已处理该请求", lines[0])

    # Failover remains disabled by default.
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

    # Recovered requests have successful audit outcomes.
    def audited_client(self, name="failover-audit"):
        store = AuditStore(self.root / f"{name}.sqlite3")   # Isolate compared audit records.
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
        """Audit recovered requests as successful rather than error outcomes with HTTP 200."""
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
        """Retain a replacement account's filter failure when recovering an earlier quota rejection."""
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
        """Keep filter-refusal accounting consistent whether failover occurred or not."""
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
        """Preserve filter failures observed during tool-stream preflight aggregation."""
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

    # Retry classification and ambiguous failures
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
        """Never fail over a synthetic collection error after upstream HTTP 200."""
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
        """Treat content filtering as a terminal model response, not a failover trigger."""
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
                # Transport retry precedes the bounded credential failover attempt.
                self.assertGreaterEqual(len(sent), 2)
                self.assertNotEqual(len(set(self.uids(sent))), 1, "必须换过凭证")
                self.assertEqual(len(self.failover_lines()), 1, self.failover_lines())

    def test_write_timeout_needs_an_explicit_opt_in(self):
        """Return HTTP 502 without replaying ambiguous write timeouts by default."""
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
        """Flag possible billing when explicit write-timeout replay recovers a request."""
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
        """Keep read failures and midstream resets non-replayable despite write-timeout opt-in."""
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
        """Apply both replay settings through the WebUI without restarting."""
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
            settings.validate_settings({"failover_max": 99})       # Maximum budget is 10.
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
        """Apply the same pre-response credential failover policy to non-streaming requests."""
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
