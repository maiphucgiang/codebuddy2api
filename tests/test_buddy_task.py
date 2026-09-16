"""Exercise real-request onboarding contracts without external HTTP calls or credentials."""
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch

import httpx

from app import buddy, buddy_task, travel
from app.audit_store import AuditStore
from app.control_store import ControlStore
from tests.test_buddy import ACTIVE, EMPTY, LIST, TASK, AGREEMENT, IDLE, CONFIG, TRIP


def tasks(state):
    return {"tasks": [{**TASK, "task_type": "beginner", "accept_status": state}]}


def sse():
    chunks = [{"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13}}]
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                          text="".join("data: " + json.dumps(row) + "\n\n" for row in chunks) + "data: [DONE]\n\n")


class BuddyTaskTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = ControlStore(self.root / "control.sqlite3")
        self.audit = AuditStore(self.root / "logs.sqlite3")
        self.addCleanup(self.store.close)
        self.addCleanup(self.audit.close)
        self.headers = {"Authorization": "Bearer synthetic-token", "X-Domain": "www.workbuddy.cn",
                        "X-User-Id": "fixture", "X-IDE-Type": "WorkBuddy"}
        self.model = {"id": "fast-model", "name": "Fast"}
        self.context = {"identity": "account", "profile": "cn-work", "store": self.store, "audit": self.audit,
                        "headers": self.headers, "task_model": lambda requested=None: self.model,
                        "auto_accept": True}
        self.calls = []

    def run_flow(self, responses, *, can_write=lambda: True, full=False, on_request=None, context=None):
        pending = iter(responses)
        def handle(request):
            self.calls.append(request)
            self.assertNotEqual(request.url.path, "/activity/growth/tasks/accept")
            if on_request:
                on_request(request)
            value = next(pending)
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, httpx.Response) else httpx.Response(200, json={"code": 0, "data": value})
        client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
        if full:
            with patch.object(travel.httpx, "Client", return_value=client):
                return travel.perform("synthetic-token", "cn-work", buddy_context=context or self.context, can_write=can_write)
        with client:
            return buddy.prepare(client, "synthetic-token", context=context or self.context, can_write=can_write)

    def writes(self):
        return [call for call in self.calls if call.method == "POST"]

    def test_one_checkbox_completes_unaccepted_task_without_acceptance_endpoint(self):
        context = {**self.context, "auto_accept": False, "consent_revision": buddy.AGREEMENT_REVISION}
        result = self.run_flow([IDLE, EMPTY, LIST, tasks("not_accepted"), sse(),
                                tasks("completed"), AGREEMENT, None, None, ACTIVE, IDLE, CONFIG, {}, TRIP], full=True, context=context)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["departed"])
        self.assertTrue(result["buddy_task_completed"])
        self.assertTrue(result["buddy_consent_accepted"])
        self.assertEqual([call.url.path for call in self.writes()], [
            "/v2/chat/completions", "/activity/growth/buddy/agreement",
            "/activity/growth/buddy/first", "/activity/growth/buddy/travel/depart"])
        chat = self.writes()[0]
        body = json.loads(chat.content)
        self.assertEqual(body["max_tokens"], 32)
        self.assertNotIn("tools", body)
        self.assertEqual(body["messages"][-1]["content"], buddy_task.PROMPT)
        growth = json.loads(body["extra_vars"]["growthEvent"])
        self.assertEqual(len(growth), 1)
        self.assertEqual(growth[0]["eventCode"], "chat_request_send")
        self.assertEqual(growth[0]["id"], chat.headers["X-Conversation-ID"])
        self.assertEqual(growth[0]["extra"]["requestModelId"], body["model"])
        self.assertEqual(growth[0]["extra"]["inputLength"], len(buddy_task.PROMPT))
        self.assertTrue(all(call.url.host == "www.workbuddy.cn" for call in self.calls))
        record = self.store.buddy_task_record("account")
        self.assertEqual((record["accept_started"], record["chat_started"], record["completed"], record["total_tokens"]), (0, 1, 1, 13))
        self.assertEqual(record["request_id"], chat.headers["X-Request-ID"])
        rows = self.audit.list_records("admin")["items"]
        self.assertIn("buddy.task_completed", {row["action"] for row in rows})
        receipt = next(row for row in rows if row["action"] == "buddy.task_chat" and row["details"]["outcome"] == "success")
        self.assertEqual(receipt["details"]["total_tokens"], 13)
        self.assertEqual(receipt["details"]["request_id"], record["request_id"])
        self.assertNotIn("synthetic-token", json.dumps(rows))
        self.assertNotIn(buddy_task.PROMPT, json.dumps(rows))

    def test_no_consent_only_reads_and_offers_full_authorization_at_false_eligibility(self):
        result = self.run_flow([EMPTY, LIST, tasks("not_accepted")], context={**self.context, "auto_accept": False})
        self.assertEqual(result["reason"], "buddy_confirmation_required")
        self.assertFalse(result["buddy_confirmation"]["can_claim"])
        self.assertIn("32", result["buddy_confirmation"]["authorization"])
        self.assertIn("积分", result["buddy_confirmation"]["authorization"])
        self.assertEqual(self.writes(), [])
        self.assertIsNone(self.store.buddy_task_record("account"))

    def test_old_adoption_only_consent_cannot_authorize_billable_onboarding(self):
        old = hashlib.sha256("\n".join(buddy.AGREEMENT_TERMS).encode()).hexdigest()
        self.store.save_buddy_consent("account", old)
        self.assertNotEqual(old, buddy.AGREEMENT_REVISION)
        result = self.run_flow([EMPTY, LIST, tasks("accepted")], context={**self.context, "auto_accept": False})
        self.assertEqual(result["reason"], "buddy_confirmation_required")
        self.assertEqual(self.writes(), [])

    def test_pending_state_reconciles_after_restart_without_repeating_accept_or_chat(self):
        result = self.run_flow([EMPTY, LIST, tasks("accepted"), sse(), tasks("in_progress")])
        self.assertEqual(result["reason"], "buddy_task_pending")
        self.assertEqual(len(self.writes()), 1)
        other = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(other.close)
        self.context["store"] = other
        result = self.run_flow([EMPTY, LIST, tasks("in_progress")])
        self.assertEqual(result["reason"], "buddy_task_pending")
        self.assertEqual(len(self.writes()), 1)
        result = self.run_flow([EMPTY, LIST, tasks("completed"), {"agreed": True}, None, ACTIVE])
        self.assertTrue(result["buddy_ready"], result)
        self.assertEqual([r.url.path for r in self.writes()].count("/v2/chat/completions"), 1)
        self.assertEqual(other.buddy_task_record("account")["completed"], 1)

    def test_uncertain_chat_is_never_replayed_and_can_finish_via_official_readback(self):
        for error in (httpx.ReadTimeout("synthetic-secret"), httpx.WriteTimeout("synthetic-secret"),
                      httpx.ConnectError("synthetic-secret"), httpx.Response(429, text="synthetic-secret"),
                      httpx.Response(302, headers={"Location": "https://evil.invalid"}),
                      httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text='data: {"choices": []}\n\n')):
            with self.subTest(error=type(error).__name__):
                self.context["identity"] = "case-" + str(len(self.calls))
                before = len(self.writes())
                result = self.run_flow([EMPTY, LIST, tasks("accepted"), error, tasks("in_progress")])
                self.assertEqual(result["reason"], "buddy_task_unconfirmed", result)
                self.assertNotIn("synthetic-secret", json.dumps(result))
                result = self.run_flow([EMPTY, LIST, tasks("accepted")])
                self.assertEqual(result["reason"], "buddy_task_unconfirmed")
                self.assertEqual(len(self.writes()), before + 1)
        self.context["identity"] = "confirmed-after-timeout"
        result = self.run_flow([EMPTY, LIST, tasks("accepted"), httpx.ReadTimeout("x"), tasks("completed"),
                                {"agreed": True}, None, ACTIVE])
        self.assertTrue(result["buddy_ready"], result)

    def test_historical_acceptance_record_resumes_once_with_preserved_consent(self):
        reserved = self.store.reserve_buddy_task("account", "accept")
        self.store.save_buddy_consent("account", buddy.AGREEMENT_REVISION)
        context = {**self.context, "auto_accept": False}
        result = self.run_flow([EMPTY, LIST, tasks("not_accepted"), sse(), tasks("not_accepted")], context=context)
        self.assertEqual(result["reason"], "buddy_task_pending")
        self.assertTrue(result["buddy_consent_accepted"])
        self.assertNotIn("buddy_confirmation", result)
        self.assertEqual(len(self.writes()), 1)
        checkpoint = self.store.buddy_task_record("account")
        self.assertEqual(checkpoint["conversation_id"], reserved["conversation_id"])
        self.assertEqual((checkpoint["accept_started"], checkpoint["chat_started"]), (1, 1))
        reopened = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(reopened.close)
        result = self.run_flow([EMPTY, LIST, tasks("not_accepted")], context={**context, "store": reopened})
        self.assertEqual(result["reason"], "buddy_task_pending")
        self.assertEqual(len(self.writes()), 1)

    def test_existing_completed_task_skips_chat_without_acceptance_request(self):
        result = self.run_flow([EMPTY, LIST, tasks("completed"), {"agreed": True}, None, ACTIVE])
        self.assertTrue(result["buddy_ready"], result)
        self.assertNotIn("/v2/chat/completions", [r.url.path for r in self.writes()])

    def test_final_preflight_rejection_releases_unsent_chat_and_allows_resume(self):
        for gate in ("setting", "model", "exception"):
            with self.subTest(gate=gate):
                identity = "resume-" + gate
                self.context["identity"] = identity
                before = len(self.writes())
                def can_write():
                    if self.store.buddy_task_record(identity) is not None:
                        if gate == "exception":
                            raise OSError("synthetic-secret")
                        return False
                    return True
                context = self.context
                if gate == "model":
                    context = {**context, "task_model": lambda requested=None: self.model if self.store.buddy_task_record(identity) is None else None}
                result = self.run_flow([EMPTY, LIST, tasks("not_accepted")], context=context,
                                       can_write=(lambda: True) if gate == "model" else can_write)
                reason = {"setting": "buddy_task_changed", "model": "buddy_task_no_model", "exception": "buddy_task_storage_error"}[gate]
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["buddy_task_chat_sent"])
                self.assertEqual(len(self.writes()), before)
                self.assertNotIn("synthetic-secret", json.dumps(result))
                unsent = self.store.buddy_task_record(identity)
                self.assertEqual(unsent["chat_started"], 0)
                reopened = ControlStore(self.root / "control.sqlite3")
                self.addCleanup(reopened.close)
                result = self.run_flow([EMPTY, LIST, tasks("not_accepted"), sse(), tasks("completed"),
                                        {"agreed": True}, None, ACTIVE], context={**self.context, "store": reopened})
                self.assertTrue(result["buddy_ready"], result)
                self.assertEqual([r.url.path for r in self.writes()[before:]], ["/v2/chat/completions", "/activity/growth/buddy/first"])
                saved = self.store.buddy_task_record(identity)
                self.assertNotEqual(saved["request_id"], unsent["request_id"])
                self.assertEqual((saved["chat_started"], saved["completed"]), (1, 1))
                events = self.audit.list_records("admin")["items"]
                skipped = [row for row in events if row["action"] == "buddy.task_chat"
                           and row["details"].get("credential") == identity and row["details"].get("outcome") == "skipped"]
                self.assertEqual(len(skipped), 1)
                self.assertEqual(skipped[0]["details"]["request_id"], unsent["request_id"])

    def test_stale_release_cannot_clear_a_new_owner_reservation(self):
        first = self.store.reserve_buddy_task("account", "chat", model="fast-model")
        other = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(other.close)
        self.assertTrue(other.release_buddy_task("account", first["request_id"]))
        second = other.reserve_buddy_task("account", "chat", model="fast-model")
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertFalse(self.store.release_buddy_task("account", first["request_id"]))
        self.assertFalse(self.store.release_buddy_task("another-account", second["request_id"]))
        self.assertEqual(other.buddy_task_record("account"), second)

    def test_recorded_chat_outcomes_cannot_be_released(self):
        for state in ("success", "uncertain", "completed", "usage"):
            with self.subTest(state=state):
                row = self.store.reserve_buddy_task(state, "chat", model="fast-model")
                if state == "completed":
                    self.store.buddy_task_checkpoint(state, completed=True)
                elif state == "usage":
                    self.store.buddy_task_checkpoint(state, total_tokens=0)
                else:
                    self.store.buddy_task_checkpoint(state, chat_state=state)
                saved = self.store.buddy_task_record(state)
                self.assertFalse(self.store.release_buddy_task(state, row["request_id"]))
                self.assertEqual(self.store.buddy_task_record(state), saved)

    def test_release_failure_retains_reservation_and_never_sends_chat(self):
        with patch.object(self.store, "release_buddy_task", side_effect=OSError("secret")):
            result = self.run_flow([EMPTY, LIST, tasks("not_accepted")],
                                   can_write=lambda: self.store.buddy_task_record("account") is None)
        self.assertEqual(result["reason"], "buddy_task_storage_error")
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.store.buddy_task_record("account")["chat_started"], 1)
        result = self.run_flow([EMPTY, LIST, tasks("not_accepted")])
        self.assertEqual(result["reason"], "buddy_task_unconfirmed")
        self.assertEqual(self.writes(), [])


    def test_reserved_chat_survives_crash_without_any_replay(self):
        self.store.reserve_buddy_task("account", "chat", model="fast-model")
        result = self.run_flow([EMPTY, LIST, tasks("accepted")])
        self.assertEqual(result["reason"], "buddy_task_unconfirmed")
        self.assertEqual(self.writes(), [])

    def test_no_model_wrong_product_or_changed_credentials_do_not_send_writes(self):
        for context, reason in (({**self.context, "task_model": lambda *args: None}, "buddy_task_no_model"),
                                ({**self.context, "profile": "cn-cli"}, "buddy_task_unsupported"),
                                ({**self.context, "headers": {**self.headers, "Authorization": "Bearer other"}}, "buddy_task_unsupported")):
            result = self.run_flow([EMPTY, LIST, tasks("not_accepted")], context=context)
            self.assertEqual(result["reason"], reason)
        result = self.run_flow([EMPTY, LIST, tasks("not_accepted")], can_write=lambda: False)
        self.assertEqual(result["reason"], "buddy_task_changed")
        self.assertEqual(self.writes(), [])

    def test_disabling_during_task_query_prevents_chat_and_claim(self):
        enabled = [True]
        def disable(request):
            if request.url.path.endswith("/growth/tasks"):
                enabled[0] = False
        result = self.run_flow([EMPTY, LIST, tasks("not_accepted")],
                               can_write=lambda: enabled[0], on_request=disable)
        self.assertEqual(result["reason"], "buddy_task_changed")
        self.assertEqual(self.writes(), [])
        self.assertIsNone(self.store.buddy_task_record("account"))

    def test_audit_or_reservation_failure_blocks_network_writes(self):
        for target, method in ((self.audit, "event"), (self.store, "reserve_buddy_task")):
            with patch.object(target, method, side_effect=OSError("synthetic-secret")):
                result = self.run_flow([EMPTY, LIST, tasks("accepted")])
            self.assertEqual(result["reason"], "buddy_task_storage_error")
            self.assertEqual(self.writes(), [])

    def test_bounded_response_and_no_success_event_fabrication(self):
        response = httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b"x" * (buddy_task.MAX_CHAT_BYTES + 1))
        result = self.run_flow([EMPTY, LIST, tasks("accepted"), response, tasks("in_progress")])
        self.assertEqual(result["reason"], "buddy_task_unconfirmed")
        self.assertEqual(result["error_kind"], "protocol")
        self.assertFalse(self.store.buddy_task_record("account")["completed"])
        self.assertFalse(any(row["action"] == "buddy.task_completed" for row in self.audit.list_records("admin")["items"]))

    def test_expired_chat_deadline_stops_and_never_replays(self):
        with patch.object(buddy_task, "CHAT_SECONDS", -1):
            result = self.run_flow([EMPTY, LIST, tasks("accepted"), sse(), tasks("in_progress")])
        self.assertEqual(result["error_kind"], "timeout")
        self.assertEqual(result["reason"], "buddy_task_unconfirmed")
        self.run_flow([EMPTY, LIST, tasks("accepted")])
        self.assertEqual(len(self.writes()), 1)

    def test_model_disappearing_before_dispatch_stops_before_chat(self):
        context = {**self.context, "task_model": lambda requested=None: self.model if requested is None else None}
        result = self.run_flow([EMPTY, LIST, tasks("not_accepted")], context=context)
        self.assertEqual(result["reason"], "buddy_task_no_model")
        self.assertEqual(self.writes(), [])

    def test_verification_failure_after_real_chat_does_not_resend_on_resume(self):
        result = self.run_flow([EMPTY, LIST, tasks("accepted"), sse(), httpx.ReadTimeout("secret")])
        self.assertEqual(result["reason"], "buddy_task_unconfirmed")
        self.assertEqual(self.store.buddy_task_record("account")["chat_state"], "success")
        result = self.run_flow([EMPTY, LIST, tasks("accepted")])
        self.assertEqual(result["reason"], "buddy_task_pending")
        self.assertEqual(len(self.writes()), 1)

    def test_locked_first_task_does_not_start_model_request(self):
        result = self.run_flow([EMPTY, LIST, {"tasks": [{**TASK, "locked": True}]}])
        self.assertEqual(result["reason"], "buddy_not_eligible")
        self.assertEqual(self.writes(), [])


    def test_all_official_pending_states_allow_one_conversation_and_require_completion(self):
        for state in ("not_accepted", "accepted", "in_progress"):
            with self.subTest(state=state):
                self.context["identity"] = "account-" + state
                count = len(self.writes())
                result = self.run_flow([EMPTY, LIST, tasks(state), sse(), tasks(state)])
                self.assertEqual(result["reason"], "buddy_task_pending")
                self.assertFalse(result["buddy_claimed"])
                self.assertEqual(len(self.writes()), count + 1)
                self.assertFalse(self.store.buddy_task_record(self.context["identity"])["completed"])


    def test_task_reservation_is_atomic_between_store_connections(self):
        other = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(other.close)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda store: store.reserve_buddy_task("account", "chat", model="fast-model"), [self.store, other]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertIsNone(self.store.reserve_buddy_task("account", "accept"))
        self.assertIsNotNone(self.store.reserve_buddy_task("another", "chat", model="fast-model"))


if __name__ == "__main__":
    unittest.main()
