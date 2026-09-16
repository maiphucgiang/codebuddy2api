"""Test consent, first-claim reservations and travel prerequisites using offline transports."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from app import buddy, travel
from app.audit_store import AuditStore
from app.control_store import ControlStore

ACTIVE = {"buddy": {"instance_id": 42}}
EMPTY = {"buddy": None}
LIST = {"buddies": [], "count": 0}
TASK = {"task_code": "first_buddy", "accept_status": "completed", "locked": False, "reward_buddy": True}
TASKS = {"tasks": [TASK]}
AGREEMENT = {"agreed": False}
IDLE = {"state": "idle", "daily_limit_reached": False}
CONFIG = {"locations": [{"id": 1, "name": "咖啡馆"}]}
TRIP = {"state": "traveling", "daily_limit_reached": False, "location": {"id": 1, "name": "咖啡馆"}}


class BuddyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = ControlStore(self.root / "control.sqlite3")
        self.audit = AuditStore(self.root / "logs.sqlite3")
        self.addCleanup(self.store.close)
        self.addCleanup(self.audit.close)
        self.context = {"identity": "account", "profile": "cn-work", "store": self.store, "audit": self.audit}
        self.context["headers"] = {"Authorization": "Bearer synthetic-token", "X-Domain": "www.workbuddy.cn"}
        self.calls = []

    def client(self, responses, on_request=None):
        pending = iter(responses)
        def handle(request):
            self.calls.append(request)
            if on_request:
                on_request(request)
            value = next(pending)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, json={"code": 0, "data": value})
        return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)

    def prepare(self, responses, *, consent=None, automatic=False, can_write=lambda: True, on_request=None):
        context = {**self.context, "consent_revision": consent, "auto_accept": automatic}
        with self.client(responses, on_request) as client:
            return buddy.prepare(client, "synthetic-token", context=context, can_write=can_write)

    def posts(self):
        return [call for call in self.calls if call.method == "POST"]

    def test_existing_buddy_needs_no_consent_or_reservation(self):
        result = self.prepare([ACTIVE])
        self.assertTrue(result["buddy_ready"])
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.store.buddy_record("account"))

    def test_missing_buddy_returns_account_scoped_confirmation_without_writes(self):
        result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT])
        self.assertEqual(result["reason"], "buddy_confirmation_required")
        self.assertTrue(result["buddy_confirmation"]["can_claim"])
        self.assertEqual(result["buddy_confirmation"]["revision"], buddy.AGREEMENT_REVISION)
        self.assertEqual(self.posts(), [])
        self.assertIsNone(self.store.buddy_record("account"))

    def test_qualification_and_existing_collection_cannot_be_bypassed_by_environment(self):
        for state in ("not_accepted", "accepted", "in_progress", "claimed", None):
            with self.subTest(state=state):
                result = self.prepare([EMPTY, LIST, {"tasks": [{**TASK, "accept_status": state}]}], automatic=True)
                self.assertEqual(result["reason"], "buddy_task_no_model" if state in {"not_accepted", "accepted", "in_progress"} else "buddy_not_eligible")
        result = self.prepare([EMPTY, {"buddies": [{"instance_id": 9}], "count": 1}], automatic=True)
        self.assertEqual(result["reason"], "buddy_selection_required")
        result = self.prepare([EMPTY, LIST, {"tasks": [{**TASK, "locked": True}]}], automatic=True)
        self.assertEqual(result["reason"], "buddy_not_eligible")
        self.assertEqual(self.posts(), [])

    def test_manual_consent_uses_only_agreement_and_first_endpoints(self):
        result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT, None, {"buddy": {}}, ACTIVE], consent=buddy.AGREEMENT_REVISION)
        self.assertTrue(result["buddy_ready"], result)
        self.assertTrue(result["buddy_claimed"])
        self.assertTrue(result["agreement_accepted"])
        self.assertEqual([r.url.path for r in self.posts()], ["/activity/growth/buddy/agreement", "/activity/growth/buddy/first"])
        self.assertEqual(json.loads(self.posts()[0].content), {"agree": True})
        self.assertEqual(self.posts()[1].content, b"")
        for call in self.calls:
            self.assertEqual(call.url.host, "www.workbuddy.cn")
            self.assertEqual(call.headers["authorization"], "Bearer synthetic-token")
        saved = self.store.buddy_record("account")
        self.assertEqual(saved["outcome"], "success")
        self.assertEqual(saved["claimed"], 1)
        events = self.audit.list_records("admin")["items"]
        self.assertEqual({e["action"] for e in events}, {"buddy.authorization_used", "buddy.agreement", "buddy.first"})
        self.assertTrue(all(e["details"]["consent_source"] == "manual" for e in events))
        self.assertNotIn("synthetic-token", json.dumps(events))
        self.assertEqual(self.store.snapshot()["revision"], 0)

    def test_environment_authorization_skips_previously_accepted_agreement(self):
        result = self.prepare([EMPTY, LIST, TASKS, {"agreed": True}, None, ACTIVE], automatic=True)
        self.assertTrue(result["buddy_ready"], result)
        self.assertEqual(result["consent_source"], "environment")
        self.assertEqual([r.url.path for r in self.posts()], ["/activity/growth/buddy/first"])

    def test_stale_revision_is_not_consent(self):
        result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT], consent="old-version")
        self.assertEqual(result["reason"], "buddy_confirmation_required")
        self.assertEqual(self.posts(), [])

    def test_consent_is_saved_before_eligibility_and_automatically_resumed_after_restart(self):
        client = self.client([IDLE, EMPTY, LIST, {"tasks": [{**TASK, "accept_status": "not_accepted"}]}])
        with patch.object(travel.httpx, "Client", return_value=client):
            result = travel.perform("synthetic-token", "cn-work", buddy_context={
                **self.context, "consent_revision": buddy.AGREEMENT_REVISION})
        self.assertTrue(result["buddy_consent_accepted"])
        self.assertEqual(result["reason"], "buddy_task_no_model")
        self.assertNotIn("buddy_confirmation", result)
        self.assertEqual(self.posts(), [])
        self.assertIsNone(self.store.buddy_record("account"))
        self.assertTrue(self.store.has_buddy_consent("account", buddy.AGREEMENT_REVISION))
        self.assertEqual([r["action"] for r in self.audit.list_records("admin")["items"]], ["buddy.consent"])
        reopened = ControlStore(self.root / "control.sqlite3")
        try:
            self.context["store"] = reopened
            result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT, None, None, ACTIVE])
        finally:
            reopened.close()
        self.assertTrue(result["buddy_ready"], result)
        self.assertEqual(result["consent_source"], "manual")
        self.assertTrue(result["buddy_consent_accepted"])
        self.assertNotIn("buddy_confirmation", result)
        self.assertEqual(len(self.posts()), 2)

    def test_consent_is_scoped_to_account_and_agreement_revision(self):
        self.store.save_buddy_consent("other-account", buddy.AGREEMENT_REVISION)
        self.store.save_buddy_consent("account", "old-revision")
        result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT])
        self.assertEqual(result["reason"], "buddy_confirmation_required")
        self.assertEqual(self.posts(), [])

    def test_consent_storage_failure_stops_before_network_and_does_not_authorize_later(self):
        for field in ("audit", "store"):
            with self.subTest(field=field):
                target = self.audit if field == "audit" else self.store
                method = "event" if field == "audit" else "save_buddy_consent"
                with patch.object(target, method, side_effect=OSError("secret")), patch.object(travel.httpx, "Client") as factory:
                    result = travel.perform("synthetic-token", "cn-work", buddy_context={
                        **self.context, "consent_revision": buddy.AGREEMENT_REVISION})
                self.assertFalse(result["buddy_consent_accepted"])
                self.assertEqual(result["reason"], "buddy_storage_error")
                self.assertFalse(self.store.has_buddy_consent("account", buddy.AGREEMENT_REVISION))
                factory.assert_not_called()

    def test_read_only_query_cannot_save_consent(self):
        client = self.client([IDLE])
        with patch.object(travel.httpx, "Client", return_value=client):
            travel.perform("synthetic-token", "cn-work", read_only=True, buddy_context={
                **self.context, "consent_revision": buddy.AGREEMENT_REVISION})
        self.assertFalse(self.store.has_buddy_consent("account", buddy.AGREEMENT_REVISION))

    def test_unknown_prerequisite_and_invalid_envelopes_stop_writes(self):
        cases = [[{}], [{"buddy": {}}], [{"buddy": {"instance_id": True}}],
                 [EMPTY, {}], [EMPTY, {"buddies": [], "count": False}],
                 [EMPTY, LIST, {"tasks": None}], [EMPTY, LIST, TASKS, {"agreed": "true"}],
                 [httpx.Response(200, json={"code": False, "data": ACTIVE})],
                 [httpx.Response(302, headers={"location": "https://untrusted.invalid"})],
                 [httpx.Response(200, content=b"x" * (buddy.MAX_RESPONSE_BYTES + 1))]]
        for values in cases:
            with self.subTest(values=str(values)[:80]):
                result = self.prepare(values, automatic=True)
                self.assertFalse(result["buddy_ready"])
                self.assertEqual(result["reason"], "buddy_unknown")
        self.assertEqual(self.posts(), [])

    def test_write_failure_keeps_reservation_and_never_replays(self):
        result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT, None, httpx.ReadTimeout("synthetic-secret"), EMPTY], automatic=True)
        self.assertFalse(result["buddy_ready"])
        self.assertTrue(result["agreement_accepted"])
        self.assertEqual(result["phase"], "buddy_first")
        self.assertEqual(len(self.posts()), 2)
        saved = self.store.buddy_record("account")
        self.assertEqual(saved["outcome"], "uncertain")
        self.assertGreater(saved["retry_at"], time.time())
        self.assertNotIn("synthetic-secret", str(result))
        again = self.prepare([EMPTY, LIST], automatic=True)
        self.assertEqual(again["reason"], "buddy_retry_later")
        self.assertEqual(len(self.posts()), 2)
        reconciled = self.prepare([ACTIVE])
        self.assertTrue(reconciled["buddy_ready"])
        self.assertEqual(self.store.buddy_record("account")["outcome"], "success")

    def test_timeout_can_be_reconciled_without_replaying_or_dispatching(self):
        result = self.prepare([EMPTY, LIST, TASKS, {"agreed": True}, httpx.ReadTimeout("secret"), ACTIVE], automatic=True)
        self.assertEqual(result["reason"], "buddy_reconciled")
        self.assertTrue(result["buddy_claimed"])
        self.assertFalse(result["buddy_ready"])
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.store.buddy_record("account")["outcome"], "success")

    def test_receipt_persistence_failure_stops_followup_but_preserves_known_claim(self):
        checkpoint = self.store.buddy_checkpoint
        def fail_after_receipt(*args, **kwargs):
            if kwargs.get("claimed"):
                raise OSError("synthetic storage failure")
            return checkpoint(*args, **kwargs)
        with patch.object(self.store, "buddy_checkpoint", side_effect=fail_after_receipt):
            result = self.prepare([EMPTY, LIST, TASKS, {"agreed": True}, None], automatic=True)
        self.assertTrue(result["buddy_claimed"])
        self.assertEqual(result["reason"], "buddy_storage_error")
        self.assertEqual(len(self.posts()), 1)
        result = self.prepare([ACTIVE])
        self.assertTrue(result["buddy_ready"])
        self.assertEqual(len(self.posts()), 1)

    def test_agreement_audit_failure_prevents_first_claim(self):
        event = self.audit.event
        def failing_event(kind, action, details):
            return {"ok": False} if action == "buddy.agreement" else event(kind, action, details)
        with patch.object(self.audit, "event", side_effect=failing_event):
            result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT, None], automatic=True)
        self.assertTrue(result["agreement_accepted"])
        self.assertEqual(result["reason"], "buddy_storage_error")
        self.assertEqual([r.url.path for r in self.posts()], ["/activity/growth/buddy/agreement"])

    def test_new_travel_or_daily_limit_after_adoption_prevents_dispatch(self):
        for status in (TRIP, {**IDLE, "daily_limit_reached": True}, {"state": "idle"}):
            with self.subTest(status=status):
                identity = "account-" + str(len(self.calls))
                client = self.client([IDLE, EMPTY, LIST, TASKS, {"agreed": True}, None, ACTIVE, status])
                with patch.object(travel.httpx, "Client", return_value=client):
                    result = travel.perform("synthetic-token", "cn-work", buddy_context={
                        **self.context, "identity": identity, "auto_accept": True})
                self.assertTrue(result["buddy_claimed"])
                self.assertFalse(result["departed"])
        self.assertFalse(any(r.url.path.endswith("/depart") for r in self.calls))

    def test_revocation_during_departure_audit_prevents_dispatch(self):
        allowed = [True]
        event = self.audit.event
        def revoke(kind, action, details):
            if action == "buddy.departure_requested":
                allowed[0] = False
            return event(kind, action, details)
        client = self.client([IDLE, EMPTY, LIST, TASKS, {"agreed": True}, None, ACTIVE, IDLE, CONFIG])
        with patch.object(travel.httpx, "Client", return_value=client), patch.object(self.audit, "event", side_effect=revoke):
            result = travel.perform("synthetic-token", "cn-work", can_write=lambda: allowed[0],
                                    buddy_context={**self.context, "auto_accept": True})
        self.assertTrue(result["buddy_claimed"])
        self.assertFalse(result["departed"])
        self.assertEqual(len(self.posts()), 1)

    def test_storage_and_audit_failure_block_first_write(self):
        with patch.object(self.store, "reserve_buddy", side_effect=OSError("secret")):
            result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT], automatic=True)
        self.assertEqual(result["reason"], "buddy_storage_error")
        with patch.object(self.audit, "event", return_value={"ok": False}):
            result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT], automatic=True)
        self.assertEqual(result["reason"], "buddy_storage_error")
        self.assertEqual(self.posts(), [])

    def test_disabled_or_replaced_credential_stops_between_writes(self):
        enabled = [True]
        def change(request):
            if request.method == "POST":
                enabled[0] = False
        result = self.prepare([EMPTY, LIST, TASKS, AGREEMENT, None], automatic=True,
                              can_write=lambda: enabled[0], on_request=change)
        self.assertEqual(result["reason"], "buddy_changed")
        self.assertTrue(result["agreement_accepted"])
        self.assertEqual(len(self.posts()), 1)

    def test_post_claim_read_failure_keeps_confirmed_claim(self):
        result = self.prepare([EMPTY, LIST, TASKS, {"agreed": True}, None, httpx.ConnectError("secret")], automatic=True)
        self.assertTrue(result["buddy_claimed"])
        self.assertFalse(result["buddy_ready"])
        self.assertEqual(self.store.buddy_record("account")["claimed"], 1)
        self.assertEqual(len(self.posts()), 1)

    def test_travel_checks_buddy_and_rechecks_status_after_adoption(self):
        context = {**self.context, "consent_revision": buddy.AGREEMENT_REVISION}
        client = self.client([IDLE, EMPTY, LIST, TASKS, AGREEMENT, None, None, ACTIVE, IDLE, CONFIG, None, TRIP])
        with patch.object(travel.httpx, "Client", return_value=client):
            result = travel.perform("synthetic-token", "cn-work", buddy_context=context)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["buddy_claimed"])
        self.assertTrue(result["departed"])
        self.assertEqual([call.url.path.rsplit("/", 1)[-1] for call in self.calls],
                         ["status", "info", "list", "tasks", "agreement", "agreement", "first", "info", "status", "config", "depart", "status"])

    def test_unavailable_task_model_never_dispatches(self):
        client = self.client([IDLE, EMPTY, LIST, {"tasks": [{**TASK, "accept_status": "not_accepted"}]}])
        with patch.object(travel.httpx, "Client", return_value=client):
            result = travel.perform("synthetic-token", "cn-work", buddy_context={**self.context, "auto_accept": True})
        self.assertEqual(result["reason"], "buddy_task_no_model")
        self.assertFalse(result["departed"])
        self.assertEqual(self.posts(), [])

    def test_read_only_and_international_never_bootstrap(self):
        client = self.client([IDLE])
        with patch.object(travel.httpx, "Client", return_value=client):
            result = travel.perform("synthetic-token", "cn-work", read_only=True, buddy_context={**self.context, "auto_accept": True})
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.calls), 1)
        with patch.object(travel.httpx, "Client") as factory:
            travel.perform("synthetic-token", "intl-work", buddy_context={**self.context, "auto_accept": True})
        factory.assert_not_called()

    def test_warning_is_once_per_account_day_across_restarts(self):
        config = {"audit_store": self.audit}
        result = {"buddy_blocked": True, "phase": "buddy_tasks", "reason": "buddy_not_eligible"}
        with patch.object(buddy.time, "strftime", return_value="2026-09-16"):
            for _ in range(3):
                buddy.daily_warning(config, "account", "cn-work", result)
            reopened = AuditStore(self.root / "logs.sqlite3")
            try:
                buddy.daily_warning({"audit_store": reopened}, "account", "cn-work", result)
            finally:
                reopened.close()
            buddy.daily_warning(config, "other-account", "cn-work", result)
        with patch.object(buddy.time, "strftime", return_value="2026-09-17"):
            buddy.daily_warning(config, "account", "cn-work", result)
        events = self.audit.list_records("runtime")["items"]
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["details"]["outcome"] == "warning" for e in events))

    def test_reservation_is_cross_connection_atomic_and_survives_restart(self):
        other = ControlStore(self.root / "control.sqlite3")
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                attempts = list(executor.map(lambda store: store.reserve_buddy("account", "manual", buddy.AGREEMENT_REVISION,
                                                                              retry_seconds=86400), [self.store, other]))
            self.assertEqual(sum(value is not None for value in attempts), 1)
            self.assertIsNotNone(other.buddy_record("account"))
            self.assertIsNone(other.reserve_buddy("account", "manual", buddy.AGREEMENT_REVISION, retry_seconds=86400))
        finally:
            other.close()

    def test_environment_boolean_is_strict_and_off_by_default(self):
        self.assertFalse(buddy.auto_accept_from_env({}))
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(buddy.auto_accept_from_env({"CODEBUDDY2API_AUTO_ACCEPT_BUDDY": value}))
        for value in ("0", "false", "FALSE", "no", "off"):
            self.assertFalse(buddy.auto_accept_from_env({"CODEBUDDY2API_AUTO_ACCEPT_BUDDY": value}))
        for value in ("", "enabled", "2"):
            with self.assertRaises(ValueError):
                buddy.auto_accept_from_env({"CODEBUDDY2API_AUTO_ACCEPT_BUDDY": value})


if __name__ == "__main__":
    unittest.main()
