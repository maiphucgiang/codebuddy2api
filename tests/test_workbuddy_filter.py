"""WorkBuddy 请求适配、审核识别和重试边界；仅使用 MockTransport。"""
import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient

import converter
from app import upstream_io
from app.content_filter import ContentFilterDetector, is_filter_error


ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude"
BRANCH = "Main branch (you will usually use this for PRs)"
REFUSAL = "抱歉，系统检测到您当前输入的信息存在敏感内容，我无法响应您的请求，请检查后重新输入。"
TOOL = {"index": 0, "id": "call-test", "type": "function",
        "function": {"name": "inspect", "arguments": "{}"}}


def event(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def sse(*chunks, done=True):
    text = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
    return (text + ("data: [DONE]\n\n" if done else "")).encode()


def reply(text=REFUSAL, *, field="content", finish="stop"):
    return httpx.Response(200, content=sse(event({field: text}, finish)))


def payload(route, *, stream=False, tools=False, identity=IDENTITY.lower()):
    # 小写身份走 Responses 的保守投影，保留可供兜底压缩的模板。
    system = (identity + ".\n" + BRANCH + ": main\n## Planning\n"
              + "Read relevant source files, preserve project conventions, and verify changes with tests.\n" * 5)
    user = {"role": "user", "content": "List the repository files."}
    body = {"model": "auto", "stream": stream}
    if route == "/v1/messages":
        body.update(system=[{"type": "text", "text": system}], messages=[user], max_tokens=64)
        if tools:
            body.update(tools=[{"name": "inspect", "input_schema": {"type": "object"}}],
                        tool_choice={"type": "tool", "name": "inspect"})
    elif route == "/v1/responses":
        body.update(instructions=system, input=[user])
        if tools:
            body.update(tools=[{"type": "function", "name": "inspect", "parameters": {"type": "object"}}],
                        tool_choice="required")
    else:
        body["messages"] = [{"role": "system", "content": system}, user]
        if tools:
            body.update(tools=[{"type": "function", "function": {
                "name": "inspect", "parameters": {"type": "object"}}}], tool_choice="required")
    return body


class DetectionTests(unittest.TestCase):
    def test_split_unicode_and_escaped_sse_are_detected_in_both_collection_modes(self):
        for collect in (False, True):
            for field in ("content", "refusal"):
                tracker = upstream_io.ChatSSEAccumulator(collect=collect)
                for char in REFUSAL:
                    tracker.feed_line("data: " + json.dumps(event({field: char})))
                tracker.feed_line("data: " + json.dumps(event(finish="stop")))
                tracker.result()
                self.assertTrue(tracker.filter_detector.detected)
                self.assertTrue(tracker.filter_detector.retry_safe)

    def test_normal_mentions_and_generic_refusals_are_not_content_filters(self):
        for text in ("内容审核的处理方法", "Cannot comply", "content_filter is a finish reason",
                     "Example: " + REFUSAL, REFUSAL + " This is an example."):
            detector = ContentFilterDetector()
            detector.feed({"content": text}, "stop")
            self.assertFalse(detector.detected, text)

    def test_explicit_filter_with_partial_output_is_not_retry_safe(self):
        for delta in ({"content": "partial"}, {"reasoning_content": "reason"}, {"tool_calls": [TOOL]}):
            detector = ContentFilterDetector()
            detector.feed(delta, "content_filter")
            self.assertTrue(detector.detected)
            self.assertFalse(detector.retry_safe)

    def test_detection_buffer_is_bounded_and_overflow_never_retries(self):
        detector = ContentFilterDetector()
        detector.feed({"content": "x" * 1000000, "refusal": REFUSAL}, None)
        self.assertLessEqual(sum(map(len, detector.text.values())), 2048)
        self.assertFalse(detector.detected)
        detector.feed({}, "content-filter")
        self.assertTrue(detector.detected)
        self.assertFalse(detector.retry_safe)

    def test_only_structured_errors_are_classified(self):
        for error in ({"code": "content_filter"}, {"type": "content-filter"}, {"message": REFUSAL}):
            self.assertTrue(is_filter_error(json.dumps({"error": error}).encode()))
        for value in (None, [], {"error": "content_filter"}, {"error": {"code": []}},
                      {"error": {"message": "documentation about content_filter"}},
                      {"choices": [{"message": {"content": REFUSAL}}]}):
            self.assertFalse(is_filter_error(json.dumps(value).encode()))
        self.assertFalse(is_filter_error(b"not-json"))
        self.assertFalse(is_filter_error(b"[" * 10000 + b"]" * 10000))


class EndpointFilterTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(converter.CONFIG, {
            "api_key": "", "cred": None, "cred_pool": None, "model_guard": False,
            "control_store": None, "max_images": 16, "image_policy": "truncate",
            "max_request_bytes": 32 * 1024 * 1024, "log_body_limit": 0, "log_path": None,
            "desensitize": True, "no_compact": True}))
        self.credentials = self.enterContext(patch.object(converter, "_cred_for", return_value=(None, {})))
        self.credential_status = self.enterContext(patch.object(converter, "_note_cred_status"))
        self.logs = self.enterContext(patch.object(converter, "_log"))
        self.attempts = self.enterContext(patch.object(converter, "observe_attempt"))
        self.failures = self.enterContext(patch.object(converter, "observe_failure"))
        self.requests = []
        self.respond = lambda request: reply("ok")
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.handle)
        self.enterContext(patch.object(upstream_io.httpx, "AsyncClient",
                                       side_effect=lambda **kw: real_client(transport=transport, **kw)))
        self.client = self.enterContext(TestClient(converter.app))

    def handle(self, request):
        self.requests.append(request)
        return self.respond(request)

    def reset(self):
        self.requests.clear()
        self.credentials.reset_mock()
        self.credential_status.reset_mock()
        self.attempts.reset_mock()
        self.failures.reset_mock()
        self.logs.reset_mock()

    def post(self, route, **kwargs):
        return self.client.post(route, json=payload(route, **kwargs))

    def assert_one_request(self):
        self.assertEqual(len(self.requests), 1)
        self.credentials.assert_called_once()

    def test_templates_removed_before_upstream_on_all_protocols_and_modes(self):
        for route in ROUTES:
            for stream in (False, True):
                for no_compact in (False, True):
                    with self.subTest(route=route, stream=stream, no_compact=no_compact):
                        self.reset()
                        converter.CONFIG["no_compact"] = no_compact
                        original = payload(route, stream=stream)
                        before = copy.deepcopy(original)
                        response = self.client.post(route, json=original)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assert_one_request()
                        sent = self.requests[0].content.decode()
                        self.assertNotIn(IDENTITY.lower(), sent.lower())
                        self.assertNotIn(BRANCH, sent)
                        self.assertEqual(original, before)
                        if no_compact:
                            self.assertIn("You are CodeBuddy, Tencent's official CLI", sent)
                            self.assertIn("Main branch (you will usually use this for PR): main", sent)
                            self.assertIn("## Planning", sent)

    def test_disabled_desensitization_leaves_template_text_for_chat_and_messages(self):
        converter.CONFIG["desensitize"] = False
        for route in (ROUTES[0], ROUTES[2]):
            self.reset()
            self.assertEqual(self.post(route).status_code, 200)
            text = self.requests[0].content.decode()
            self.assertIn(IDENTITY.lower(), text)
            self.assertIn(BRANCH, text)

    def test_nonstream_pure_refusal_retries_once_without_rerouting(self):
        for route in ROUTES:
            for field in ("content", "refusal"):
                with self.subTest(route=route, field=field):
                    self.reset()
                    self.respond = lambda req: reply(field=field) if len(self.requests) == 1 else reply("recovered")
                    response = self.post(route)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("recovered", response.text)
                    self.assertEqual(len(self.requests), 2)
                    first, second = self.requests
                    self.assertEqual(first.url, second.url)
                    self.assertEqual({k: v for k, v in first.headers.items() if k != "content-length"},
                                     {k: v for k, v in second.headers.items() if k != "content-length"})
                    self.assertLess(len(second.content), len(first.content))
                    first_body, second_body = json.loads(first.content), json.loads(second.content)
                    self.assertNotEqual(first_body["messages"][0], second_body["messages"][0])
                    self.assertEqual(first_body["messages"][1:], second_body["messages"][1:])
                    self.credentials.assert_called_once()
                    self.credential_status.assert_not_called()
                    self.failures.assert_not_called()
                    self.attempts.assert_any_call("content_filter_retry", error_code="content_filter")
                    self.assertFalse(any(REFUSAL in call.args[0] for call in self.logs.call_args_list))

    def test_all_streams_detect_refusal_without_replay_even_with_required_tools(self):
        for route in ROUTES:
            for tools in (False, True):
                with self.subTest(route=route, tools=tools):
                    self.reset()
                    self.respond = lambda req: httpx.Response(200, content=sse(
                        *(event({"content": char}) for char in REFUSAL), event(finish="stop")))
                    response = self.post(route, stream=True, tools=tools)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assert_one_request()
                    self.failures.assert_called_once_with("content_filter")
                    self.credential_status.assert_not_called()
                    self.attempts.assert_any_call("content_filter", error_code="content_filter")

    def test_retry_exhaustion_preserves_refusal_without_tool_regeneration(self):
        self.respond = lambda req: reply()
        for route in ROUTES:
            self.reset()
            response = self.post(route, tools=True)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(REFUSAL, json.dumps(response.json(), ensure_ascii=False))
            self.assertEqual(len(self.requests), 2)
            self.failures.assert_called_once_with("content_filter")
            self.credential_status.assert_not_called()

    def test_disabled_or_compacted_or_unchanged_body_never_retries(self):
        self.respond = lambda req: reply()
        for desensitize, no_compact, identity in ((False, True, IDENTITY), (True, False, IDENTITY),
                                                 (True, True, "You are a helpful assistant")):
            for route in ROUTES:
                with self.subTest(route=route, desensitize=desensitize, no_compact=no_compact, identity=identity):
                    self.reset()
                    converter.CONFIG.update(desensitize=desensitize, no_compact=no_compact)
                    self.assertEqual(self.post(route, identity=identity).status_code, 200)
                    self.assert_one_request()
                    self.failures.assert_called_once_with("content_filter")

    def test_oversized_retry_body_preserves_original_refusal(self):
        guard = converter._guard_request_size
        def reject_compact(body):
            if body["messages"][0]["content"].startswith("You are a coding assistant."):
                raise converter.HTTPException(status_code=413, detail="synthetic retry budget")
            return guard(body)
        self.respond = lambda req: reply()
        with patch.object(converter, "_guard_request_size", side_effect=reject_compact):
            for route in ROUTES:
                self.reset()
                response = self.post(route)
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_one_request()
                self.assertIn(REFUSAL, json.dumps(response.json(), ensure_ascii=False))

    def test_second_http_failure_is_not_hidden_by_first_refusal(self):
        for route in ROUTES:
            for status in (401, 403, 429, 503):
                with self.subTest(route=route, status=status):
                    self.reset()
                    self.respond = lambda req: reply() if len(self.requests) == 1 else httpx.Response(
                        status, json={"error": {"message": "synthetic retry failure"}})
                    response = self.post(route)
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(len(self.requests), 2)
                    self.credential_status.assert_called_once()
                    self.assertEqual(self.credential_status.call_args.args[1], status)

    def test_http_and_sse_filter_errors_are_preserved_without_auth_cooldown_or_retry(self):
        for route in ROUTES:
            for stream in (False, True):
                for status in (200, 400, 403):
                    with self.subTest(route=route, stream=stream, status=status):
                        self.reset()
                        error = {"error": {"code": "content_filter", "message": REFUSAL}}
                        self.respond = lambda req: (httpx.Response(200, content=sse(error)) if status == 200
                                                    else httpx.Response(status, json=error))
                        response = self.post(route, stream=stream)
                        self.assert_one_request()
                        self.credential_status.assert_not_called()
                        self.failures.assert_called_once_with("content_filter")
                        self.assertEqual(response.status_code, 200 if stream else 502 if status == 200 else status)
                        self.assertIn("content_filter", response.text)

    def test_explicit_empty_filter_can_retry_nonstream_but_never_stream(self):
        for route in ROUTES:
            for stream in (False, True):
                self.reset()
                self.respond = lambda req: (httpx.Response(200, content=sse(event(finish="content_filter")))
                                            if len(self.requests) == 1 else reply("recovered"))
                response = self.post(route, stream=stream)
                self.assertEqual(len(self.requests), 1 if stream else 2)
                if stream:
                    self.assertIn("content_filter", response.text)
                    self.failures.assert_called_once_with("content_filter")
                else:
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("recovered", response.text)
                    self.failures.assert_not_called()

    def test_partial_text_reasoning_and_tools_never_trigger_filter_replay(self):
        for route in ROUTES:
            for delta, finish in (({"content": REFUSAL, "reasoning_content": "reason"}, "stop"),
                                  ({"content": "partial text"}, "content_filter"),
                                  ({"content": REFUSAL, "tool_calls": [TOOL]}, "content_filter")):
                with self.subTest(route=route, delta=delta):
                    self.reset()
                    self.respond = lambda req: httpx.Response(200, content=sse(event(delta, finish)))
                    self.assertEqual(self.post(route).status_code, 200)
                    self.assert_one_request()
                    self.failures.assert_called_once_with("content_filter")

    def test_keyword_mentions_in_content_reasoning_or_tool_arguments_do_not_retry(self):
        deltas = ({"content": "Explain content_filter and 内容审核"},
                  {"content": "Example: " + REFUSAL}, {"content": "ok", "reasoning_content": REFUSAL},
                  {"tool_calls": [{**TOOL, "function": {"name": "inspect", "arguments": json.dumps({"text": REFUSAL})}}]})
        for route in ROUTES:
            for delta in deltas:
                with self.subTest(route=route, delta=delta):
                    self.reset()
                    self.respond = lambda req: httpx.Response(200, content=sse(event(delta, "stop")))
                    response = self.post(route)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assert_one_request()
                    self.failures.assert_not_called()

    def test_refusal_followed_by_transport_error_or_malformed_sse_never_retries(self):
        class Broken(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse(event({"content": REFUSAL}), done=False)
                raise httpx.ReadError("synthetic disconnect")
        for route in ROUTES:
            for stream in (False, True):
                for kind in ("disconnect", "invalid", "no_ending"):
                    with self.subTest(route=route, stream=stream, kind=kind):
                        self.reset()
                        def respond(req):
                            if kind == "disconnect":
                                return httpx.Response(200, stream=Broken())
                            raw = sse(event({"content": REFUSAL}), done=False)
                            if kind == "invalid":
                                raw += b"data: not-json\n\n"
                            return httpx.Response(200, content=raw)
                        self.respond = respond
                        response = self.post(route, stream=stream)
                        self.assert_one_request()
                        self.assertEqual(response.status_code, 200 if stream else 502)
                        self.assertNotIn("response.completed", response.text)
                        self.assertNotIn("message_stop", response.text)

    def test_retry_transport_and_protocol_failures_are_not_hidden_or_replayed(self):
        for route in ROUTES:
            for kind in ("read", "write", "invalid_sse", "unterminated"):
                with self.subTest(route=route, kind=kind):
                    self.reset()
                    def respond(req):
                        if len(self.requests) == 1:
                            return reply()
                        if kind in ("read", "write"):
                            error = httpx.ReadTimeout if kind == "read" else httpx.WriteError
                            raise error("synthetic retry transport failure", request=req)
                        raw = (b"data: bad-json\n\n" if kind == "invalid_sse" else
                               sse(event({"content": REFUSAL}), done=False))
                        return httpx.Response(200, content=raw)
                    self.respond = respond
                    response = self.post(route)
                    self.assertEqual(response.status_code, 502, response.text)
                    self.assertEqual(len(self.requests), 2)
                    self.credential_status.assert_not_called()

    def test_empty_filter_followed_by_error_frame_never_retries(self):
        for route in ROUTES:
            self.reset()
            self.respond = lambda req: httpx.Response(200, content=sse(
                event(finish="content_filter"), {"error": {"code": "content_filter"}}))
            self.assertEqual(self.post(route).status_code, 502)
            self.assert_one_request()

    def test_cancellation_during_filter_retry_is_propagated(self):
        def respond(req):
            if len(self.requests) == 1:
                return reply()
            raise asyncio.CancelledError()
        self.respond = respond
        body = converter._chat_body_desensitize({
            "model": "auto", "messages": [{"role": "system", "content": IDENTITY + ".\n" + "Follow project guidance. " * 20},
                                            {"role": "user", "content": "hi"}], "stream": True})
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(converter._fetch_checked_chat(
                "https://synthetic.invalid/v2/chat/completions", {}, body, "auto", "test", filter_retry=True))
        self.assertEqual(len(self.requests), 2)

    def test_audit_hooks_mark_only_unrecovered_filter_as_failed(self):
        import time
        from app import observability
        for recover in (False, True):
            self.reset()
            observation = observability._Observation({}, time.monotonic())
            token = observability._current.set(observation)
            try:
                self.respond = lambda req: reply("recovered") if recover and len(self.requests) == 2 else reply()
                with patch.object(converter, "observe_attempt", observability.observe_attempt), \
                     patch.object(converter, "observe_failure", observability.observe_failure):
                    self.assertEqual(self.post(ROUTES[0]).status_code, 200)
                self.assertEqual(observation.failed, not recover)
                self.assertTrue(any(a["stage"] == "content_filter_retry" for a in observation.attempts))
                self.assertEqual(observation.record.get("error_code"), None if recover else "content_filter")
                self.assertNotIn(REFUSAL, json.dumps(observation.record, ensure_ascii=False))
            finally:
                observability._current.reset(token)


    def test_short_template_is_not_expanded_for_filter_retry(self):
        self.respond = lambda req: reply()
        for route in ROUTES:
            self.reset()
            body = payload(route)
            if route == ROUTES[0]:
                body["messages"][0]["content"] = IDENTITY.lower()
            elif route == ROUTES[1]:
                body["instructions"] = IDENTITY.lower()
            else:
                body["system"] = IDENTITY.lower()
            self.assertEqual(self.client.post(route, json=body).status_code, 200)
            self.assert_one_request()
            self.failures.assert_called_once_with("content_filter")

    def test_filter_errors_never_write_echoed_body_to_any_log(self):
        sentinel = "SYNTHETIC_PRIVATE_BODY_SENTINEL"
        for route in ROUTES:
            for stream in (False, True):
                for status in (200, 403):
                    with self.subTest(route=route, stream=stream, status=status):
                        self.reset()
                        error = {"error": {"code": "content_filter", "message": sentinel}}
                        self.respond = lambda req: (httpx.Response(200, content=sse(error)) if status == 200
                                                    else httpx.Response(status, json=error))
                        with patch.object(converter, "_log_text_body") as previews:
                            response = self.post(route, stream=stream)
                        self.assert_one_request()
                        previews.assert_not_called()
                        self.assertNotIn(sentinel, str(self.logs.call_args_list))
                        self.assertIn(sentinel, response.text)

    def test_filtered_response_never_logs_reasoning_or_tool_body_previews(self):
        sentinel = "SYNTHETIC_PRIVATE_RESPONSE_SENTINEL"
        for route in ROUTES:
            for stream in (False, True):
                self.reset()
                delta = {"content": REFUSAL, "reasoning_content": sentinel}
                self.respond = lambda req: httpx.Response(200, content=sse(event(delta, "content_filter")))
                with patch.object(converter, "_log_text_body") as text_logs, \
                     patch.object(converter, "_log_json") as json_logs:
                    self.assertEqual(self.post(route, stream=stream).status_code, 200)
                self.assert_one_request()
                text_logs.assert_not_called()
                self.assertNotIn(sentinel, str(json_logs.call_args_list))
                self.assertNotIn(sentinel, str(self.logs.call_args_list))
                self.failures.assert_called_once_with("content_filter")


    def test_filter_retry_budget_is_shared_across_tool_regenerations(self):
        for route in ROUTES:
            self.reset()
            def respond(req):
                if len(self.requests) == 1:
                    return reply("tool choice ignored")
                if len(self.requests) == 2:
                    return reply()
                return httpx.Response(200, content=sse(event({"tool_calls": [TOOL]}, "tool_calls")))
            self.respond = respond
            response = self.post(route, tools=True)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(self.requests), 3)
            self.assertEqual(sum(c.args[0] == "content_filter_retry" for c in self.attempts.call_args_list), 1)
            self.failures.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
