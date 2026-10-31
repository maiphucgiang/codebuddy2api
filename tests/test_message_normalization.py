"""Verify international image-run normalization without changing domestic requests or replay policy."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi import HTTPException
import converter as gateway
from app.message_normalization import merge_intl_user_images
from app.request_context import RequestContext
import test_api_flow as fixtures
from test_model_capabilities import IMAGE, image_payload, model


def user(content, **extras):
    return {"role": "user", "content": content, **extras}


def images(body):
    return [part for message in body["messages"] if isinstance(message.get("content"), list)
            for part in message["content"] if part.get("type") == "image_url"]


class NormalizationTests(unittest.TestCase):
    def test_first_middle_last_and_multiple_images_keep_order_and_original_bytes(self):
        for position in range(3):
            messages = [user("first"), user("second"), user("third")]
            messages[position]["content"] = [{"type": "text", "text": "before"}, deepcopy(IMAGE),
                                              {"type": "text", "text": "after"}]
            body = {"messages": messages}
            original = deepcopy(body)
            for profile in ("intl-cli", "intl-work"):
                result, runs, removed = merge_intl_user_images(body, profile)
                self.assertEqual((runs, removed), (1, 2))
                self.assertEqual(images(result), images(original))
                self.assertEqual(len(result["messages"]), 1)
                self.assertEqual(body, original)
                blocks = result["messages"][0]["content"]
                self.assertEqual(sum(part.get("text") == "\n\n" for part in blocks), 2)
                next(part for part in blocks if part["type"] == "image_url")["image_url"]["detail"] = "high"
                self.assertEqual(body, original)
        body = {"messages": [user([deepcopy(IMAGE)]), user([deepcopy(IMAGE)])]}
        self.assertEqual(len(images(merge_intl_user_images(body, "intl-work")[0])), 2)

    def test_domestic_and_text_only_runs_are_unchanged(self):
        body = {"messages": [user([deepcopy(IMAGE)]), user("question")]}
        for profile in ("cn-cli", "cn-work", None):
            self.assertIs(merge_intl_user_images(body, profile)[0], body)
        text = {"messages": [user("a"), user("b")]}
        self.assertIs(merge_intl_user_images(text, "intl-cli")[0], text)

    def test_system_assistant_and_tool_boundaries_are_not_crossed(self):
        for role in ("system", "assistant", "tool"):
            body = {"messages": [user([deepcopy(IMAGE)]), {"role": role, "content": "boundary"}, user("question")]}
            self.assertIs(merge_intl_user_images(body, "intl-work")[0], body)

    def test_conflicting_message_attributes_and_unrepresentable_content_are_rejected(self):
        for second in (user("text", name="different"), user("text"), user(None, name="same"), user([3], name="same")):
            body = {"messages": [user([deepcopy(IMAGE)], name="same"), second]}
            with self.assertRaises(HTTPException) as error:
                merge_intl_user_images(body, "intl-work")
            self.assertEqual(error.exception.status_code, 400)
            self.assertNotIn("different", str(error.exception.detail))
        body = {"messages": [user([deepcopy(IMAGE)], name="same"), user("text", name="same")]}
        self.assertEqual(merge_intl_user_images(body, "intl-work")[0]["messages"][0]["name"], "same")

    def test_session_fingerprint_remains_bound_to_original_input(self):
        body = {"messages": [user([deepcopy(IMAGE)]), user("question")]}
        context = RequestContext("chat", "scoped")
        context.bind_session(body, body["messages"])
        key = context.session_key
        routed = merge_intl_user_images(body, "intl-work")[0]
        context.bind_session(body, routed["messages"])
        self.assertEqual(key, context.session_key)


class EndpointTests(fixtures.GatewayFixture, unittest.TestCase):
    def test_three_protocols_both_modes_only_international_bodies_are_merged(self):
        for profile in fixtures.fixtures.PROFILES:
            self.fx.configure(profiles=(profile,))
            self.fx.account_catalogs({profile: [model()]})
            for protocol in fixtures.fixtures.GENERATIONS:
                for stream in (False, True):
                    with self.subTest(profile=profile, protocol=protocol, stream=stream):
                        body = image_payload(self.fx, protocol, stream=stream)
                        key = "input" if protocol == "responses" else "messages"
                        body[key].append(user("question"))
                        original = deepcopy(body)
                        self.fx.post_ok(protocol, body, {profile})
                        sent = fixtures.request_json(self.fx.requests[-1])
                        self.assertEqual(sum(m["role"] == "user" for m in sent["messages"]), 1 if profile.startswith("intl") else 2)
                        self.assertEqual(len(images(sent)), 1)
                        self.assertEqual(body, original)
                        self.assertEqual(self.fx.pool._capacity._counts, {})

    def test_guard_off_does_not_disable_image_normalization(self):
        self.fx.configure(profiles=("intl-work",))
        body = image_payload(self.fx, "chat/completions")
        body["messages"].append(user("question"))
        with patch.dict(gateway.CONFIG, model_capability_guard=False):
            self.fx.post_ok("chat/completions", body, {"intl-work"})
        self.assertEqual(sum(m["role"] == "user" for m in fixtures.request_json(self.fx.requests[-1])["messages"]), 1)

    def test_unmergeable_runs_release_capacity_without_calling_upstream(self):
        self.fx.configure(profiles=("intl-work",))
        body = image_payload(self.fx, "chat/completions")
        body["messages"][0]["name"] = "one"
        body["messages"].append(user("question", name="two"))
        with patch.dict(gateway.CONFIG, max_inflight_per_account=1):
            response = self.fx.client.post("/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fx.requests, [])
        self.assertEqual(self.fx.pool._capacity._counts, {})

    def test_size_is_rechecked_after_merge_and_failed_lease_is_released(self):
        self.fx.configure(profiles=("intl-work",))
        body = image_payload(self.fx, "chat/completions")
        body["messages"].append(user("question"))
        prepared = gateway._prepare_chat_body(deepcopy(body))
        limit = gateway._guard_request_size(prepared)
        self.assertGreater(gateway._guard_request_size(merge_intl_user_images(prepared, "intl-work")[0]), limit)
        with patch.dict(gateway.CONFIG, max_request_bytes=limit):
            response = self.fx.client.post("/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self.fx.requests, [])
        self.assertEqual(self.fx.pool._capacity._counts, {})

    def test_international_to_domestic_failover_uses_the_unmerged_canonical_body(self):
        for protocol in fixtures.fixtures.GENERATIONS:
            for stream in (False, True):
                self.fx.configure(profiles=("intl-work", "cn-cli"))
                self.fx.account_catalogs({"intl-work": [model()], "cn-cli": [model()]})
                seen = []
                def reply(request):
                    seen.append((request.headers["x-domain"], fixtures.request_json(request)))
                    if len(seen) == 1:
                        return httpx.Response(429, json={"error": {"message": "synthetic quota", "code": "quota"}})
                    return httpx.Response(200, content=fixtures.fixtures.success_sse())
                body = image_payload(self.fx, protocol, stream=stream)
                body["input" if protocol == "responses" else "messages"].append(user("question"))
                with patch.dict(gateway.CONFIG, failover_max=1), self.responder(reply):
                    response = self.fx.client.post("/v1/" + protocol, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([item[0] for item in seen], ["www.workbuddy.ai", "www.codebuddy.cn"])
                self.assertEqual([sum(m["role"] == "user" for m in item[1]["messages"]) for item in seen], [1, 2])
                self.assertEqual(self.fx.pool._capacity._counts, {})


if __name__ == "__main__":
    unittest.main()
