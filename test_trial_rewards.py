"""Offline trial tests: synthetic headers, MockTransport, disposable ledger directories.

Run: python -B -m unittest -v test_trial_rewards
No converter import, credentials, external scripts, or live HTTP requests.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import httpx

from client_profiles import credential_headers
import trial_rewards as trial

KEY = hashlib.sha256(b'["intl-work","synthetic-user","synthetic-tenant"]').hexdigest()
OTHER_KEY = hashlib.sha256(b"another synthetic account").hexdigest()
_HTTP_CLIENT = httpx.Client


def headers(domain="www.workbuddy.ai", token="synthetic-token-never-persist"):
    return credential_headers({"domain": domain, "accessToken": token},
                              {"uid": "synthetic-user", "enterpriseId": "synthetic-tenant"})


@contextmanager
def mock_http(handler):
    with patch.object(trial.httpx, "Client", side_effect=lambda **kwargs: _HTTP_CLIENT(
            transport=httpx.MockTransport(handler), **kwargs)) as factory:
        yield factory


def process_attempt(path, event, queue):
    """Each spawned process independently opens the real lock, but only mock HTTP."""
    def disconnected(request):
        queue.put("post")
        raise httpx.ConnectError("synthetic disconnect", request=request)

    if not event.wait(10):
        raise RuntimeError("test barrier timed out")
    with mock_http(disconnected):
        queue.put(trial.attempt_trial(trial.TrialLedger(path), KEY, headers()))


class ClaimTrialTests(unittest.TestCase):
    def claim(self, payload=None, *, status=200, raw=None):
        calls = []

        def respond(request):
            calls.append(request)
            return (httpx.Response(status, content=raw) if raw is not None
                    else httpx.Response(status, json=payload))

        with mock_http(respond):
            result = trial.claim_trial(headers())
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(result), {"ok", "already", "code", "status"})
        return result

    def test_four_profiles_and_invalid_routing_rejected_before_client(self):
        for domain in ("www.codebuddy.cn", "www.workbuddy.cn", "www.codebuddy.ai"):
            with self.subTest(domain=domain), patch.object(trial.httpx, "Client") as client:
                with self.assertRaises(ValueError):
                    trial.claim_trial(headers(domain))
                client.assert_not_called()
        for supplied in ({}, {"X-Domain": "unknown.invalid"},
                         {"X-Domain": "www.workbuddy.ai", "x-domain": "www.codebuddy.ai"}):
            with self.subTest(headers=supplied), patch.object(trial.httpx, "Client") as client:
                with self.assertRaises(ValueError):
                    trial.claim_trial(supplied)
                client.assert_not_called()
        self.assertTrue(self.claim({"code": 0})["ok"])

    def test_exact_endpoint_body_and_preserved_headers(self):
        supplied = {name.lower(): value for name, value in headers().items()}
        supplied["user-agent"] = "existing-product-user-agent"
        original = dict(supplied)
        calls = []

        def respond(request):
            calls.append(request)
            self.assertEqual(str(request.url), "https://www.workbuddy.ai/billing/ide/trial")
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.content), {})
            for name, value in supplied.items():
                self.assertEqual(request.headers[name], value)
            self.assertEqual(request.headers["content-type"], "application/json")
            return httpx.Response(200, json={"code": 0, "data": {"success": True}})

        with mock_http(respond) as factory:
            self.assertTrue(trial.claim_trial(supplied)["ok"])
        # 与网关其它 HTTP 调用一致，保留部署环境代理支持。
        factory.assert_called_once_with(timeout=12.0, follow_redirects=False)
        self.assertEqual(supplied, original)
        self.assertEqual(len(calls), 1)

    def test_only_missing_identity_headers_get_project_defaults(self):
        supplied = {"Authorization": "Bearer synthetic", "X-Domain": "www.workbuddy.ai"}

        def respond(request):
            self.assertEqual(request.headers["x-ide-type"], "WorkBuddy")
            self.assertEqual(request.headers["authorization"], supplied["Authorization"])
            self.assertNotIn("x-user-id", request.headers)
            return httpx.Response(200, json={"code": 0})

        with mock_http(respond):
            self.assertTrue(trial.claim_trial(supplied)["ok"])
        self.assertNotIn("X-IDE-Type", supplied)

    def test_explicit_success_and_already(self):
        self.assertEqual(self.claim({"code": 0, "data": {}}),
                         {"ok": True, "already": False, "code": 0, "status": 200})
        self.assertEqual(self.claim({"code": 14051, "success": False}),
                         {"ok": False, "already": True, "code": 14051, "status": 200})
        for status in (400, 409):
            self.assertEqual(self.claim({"code": 14051}, status=status),
                             {"ok": False, "already": True, "code": 14051, "status": status})
            self.assertFalse(self.claim({"code": 0}, status=status)["ok"])

    def test_http_failures_are_not_success_or_already_and_redirects_not_followed(self):
        for status in (301, 302, 307, 308, 401, 403, 404, 429, 500, 503):
            for code in (0, 14051):
                with self.subTest(status=status, code=code):
                    result = self.claim({"code": code}, status=status)
                    self.assertFalse(result["ok"] or result["already"])
                    self.assertEqual(result["status"], status)
        calls = []

        def redirect(request):
            calls.append(request)
            return httpx.Response(307, headers={"location": "https://www.codebuddy.ai/other"},
                                  json={"code": 0})

        with mock_http(redirect):
            self.assertFalse(trial.claim_trial(headers())["ok"])
        self.assertEqual(len(calls), 1)

    def test_unknown_malformed_and_semantic_false(self):
        cases = [None, [], True, 0, "ok", {}, {"success": True}, {"data": {"code": 0}},
                 {"code": None}, {"code": False}, {"code": True}, {"code": "0"},
                 {"code": 0.0}, {"code": "14051"}, {"code": 12345}, {"code": 2**100},
                 {"code": 0, "success": False}, {"code": 0, "ok": False},
                 {"code": 0, "success": "false"}, {"code": 0, "ok": 1},
                 {"code": 0, "data": False}, {"code": 0, "data": []},
                 {"code": 0, "data": "success"}, {"code": 0, "data": {"success": False}},
                 {"code": 0, "data": {"ok": False}}, {"code": 0, "data": {"success": 0}},
                 {"code": 14051, "data": []}]
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.claim(payload)
                self.assertFalse(result["ok"] or result["already"])
        for raw in (b"", b"not json", b"<html>error</html>", b"\xff",
                    b'{"code":0,"code":14051}', b'{"code":0,"data":NaN}',
                    b'{"code":0,"data":{"success":false,"success":true}}'):
            with self.subTest(raw=raw):
                result = self.claim(raw=raw)
                self.assertFalse(result["ok"] or result["already"])

    def test_disconnect_timeout_protocol_error_do_not_replay_or_leak(self):
        for exception in (httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            calls = []

            def fail(request):
                calls.append(request)
                raise exception("secret synthetic upstream error", request=request)

            with self.subTest(exception=exception), mock_http(fail):
                result = trial.claim_trial(headers())
                self.assertEqual(result, {"ok": False, "already": False, "code": None, "status": None})
                self.assertEqual(len(calls), 1)


class TrialLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="trial-rewards-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private" / "ledger.json"
        self.ledger = trial.TrialLedger(self.path)

    def test_construction_is_lazy_and_unknown_summary_is_safe(self):
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.ledger.summary(KEY), {"ok": False, "already": False,
                         "code": None, "status": None, "attempted_at": None, "finished_at": None})
        self.assertFalse(self.path.exists())

    def test_restart_backoff_crash_and_clock_rollback(self):
        self.assertTrue(self.ledger.begin(KEY, now=100))
        restarted = trial.TrialLedger(self.path)
        for now in (0, 100, 100 + trial.RETRY_INTERVAL - 0.01):
            self.assertFalse(restarted.begin(KEY, now=now))
        self.assertTrue(restarted.begin(KEY, now=100 + trial.RETRY_INTERVAL))
        restarted.finish(KEY, {"ok": False, "already": False, "code": 503, "status": 503},
                         now=101 + trial.RETRY_INTERVAL)
        self.assertFalse(trial.TrialLedger(self.path).begin(KEY, now=102 + trial.RETRY_INTERVAL))
        self.assertTrue(trial.TrialLedger(self.path).begin(KEY, now=100 + 2 * trial.RETRY_INTERVAL))
        self.assertTrue(restarted.begin(OTHER_KEY, now=100))

    def test_success_and_already_permanent_and_late_failure_cannot_undo(self):
        for key, code in ((KEY, 0), (OTHER_KEY, 14051)):
            self.assertTrue(self.ledger.begin(key, now=100))
            result = {"ok": code == 0, "already": code == 14051, "code": code, "status": 200}
            self.ledger.finish(key, result, now=101)
            restarted = trial.TrialLedger(self.path)
            self.assertFalse(restarted.begin(key, now=1e10))
            restarted.finish(key, {"ok": False}, now=102)
            self.assertEqual(restarted.summary(key), {**result, "attempted_at": 100, "finished_at": 101})

    def test_sanitized_bounded_result_snapshot_and_private_permissions(self):
        self.assertTrue(self.ledger.begin(KEY, now=100))
        self.ledger.finish(KEY, {"ok": True, "already": False, "code": 0, "status": 200,
                                "headers": headers(), "raw": "synthetic secret" * 100000}, now=101)
        record = self.ledger.summary(KEY)
        record["ok"] = False
        self.assertTrue(self.ledger.summary(KEY)["ok"])
        self.assertLess(self.path.stat().st_size, 1024)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        locks = list(self.path.parent.glob("*.lock"))
        self.assertEqual(len(locks), 1)
        self.assertEqual(stat.S_IMODE(locks[0].stat().st_mode), 0o600)
        for file in self.path.parent.iterdir():
            self.assertNotIn(b"synthetic", file.read_bytes())
            self.assertNotIn(b"Authorization", file.read_bytes())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_malformed_finish_cannot_create_terminal_state(self):
        self.assertTrue(self.ledger.begin(KEY, now=100))
        for result in ({"ok": True, "already": False, "code": False, "status": 200},
                       {"ok": True, "already": False, "code": 0, "status": 500},
                       {"ok": "true", "already": False, "code": 0, "status": 200},
                       {"ok": False, "already": True, "code": 14051, "status": 401}):
            self.ledger.finish(KEY, result, now=101)
            summary = trial.TrialLedger(self.path).summary(KEY)
            self.assertFalse(summary["ok"] or summary["already"])
        with self.assertRaises(ValueError):
            self.ledger.finish(OTHER_KEY, {}, now=101)

    def test_invalid_key_time_and_corrupt_file_fail_closed(self):
        for key in ("", "/credential/path.info", "Bearer synthetic", "a" * 65, 123):
            with self.assertRaises(ValueError):
                self.ledger.begin(key, now=100)
        for now in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                self.ledger.begin(KEY, now=now)
        self.assertTrue(self.ledger.begin(KEY, now=100))
        valid = self.path.read_bytes()
        for content in (b"", b"{}", b"[]", b"not JSON", b"x" * (trial._MAX_BYTES + 1),
                        valid.replace(b'"ok":false', b'"ok":true'),
                        valid.replace(b'"version":1', b'"version":true'),
                        valid.replace(b'"code":null', b'"code":false')):
            self.path.write_bytes(content)
            with patch.object(trial.httpx, "Client") as client:
                with self.assertRaises(ValueError):
                    trial.attempt_trial(self.ledger, KEY, headers())
                client.assert_not_called()
            self.assertEqual(self.path.read_bytes(), content)

    def test_symlink_target_rejected(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.path.parent / "untouched"
        target.write_bytes(b"synthetic sentinel")
        self.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            self.ledger.begin(KEY, now=100)
        self.assertEqual(target.read_bytes(), b"synthetic sentinel")

    def test_account_bound_never_evicts(self):
        self.assertTrue(self.ledger.begin(KEY, now=100))
        with patch.object(trial, "_MAX_ACCOUNTS", 1):
            with self.assertRaises(ValueError):
                self.ledger.begin(OTHER_KEY, now=100)
        self.assertFalse(self.ledger.begin(KEY, now=101))

    def test_begin_save_failures_prevent_http_and_atomic_replace_keeps_old_state(self):
        self.assertTrue(self.ledger.begin(OTHER_KEY, now=100))
        old = self.path.read_bytes()
        for target in ("tempfile.mkstemp", "os.fsync", "os.replace"):
            with self.subTest(target=target), patch("trial_rewards." + target, side_effect=OSError("mock save error")), \
                    patch.object(trial.httpx, "Client") as client:
                with self.assertRaises(OSError):
                    trial.attempt_trial(self.ledger, KEY, headers())
                client.assert_not_called()
            self.assertEqual(self.path.read_bytes(), old)
            self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_directory_fsync_failure_prevents_http_even_after_replace(self):
        original = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic directory sync failure")
            return original(fd)

        with patch.object(trial.os, "fsync", side_effect=fail_directory), \
                patch.object(trial.httpx, "Client") as client:
            with self.assertRaises(OSError):
                trial.attempt_trial(self.ledger, KEY, headers())
            client.assert_not_called()
        self.assertFalse(trial.TrialLedger(self.path).begin(KEY))

    def test_attempt_persists_before_post_and_token_rotation_does_not_reclaim(self):
        calls = []

        def respond(request):
            calls.append(request)
            summary = trial.TrialLedger(self.path).summary(KEY)
            self.assertIsNotNone(summary["attempted_at"])
            self.assertIsNone(summary["finished_at"])
            self.assertFalse(trial.TrialLedger(self.path).begin(KEY))
            return httpx.Response(200, json={"code": 0, "data": {"token": "synthetic-response-secret"}})

        with mock_http(respond):
            self.assertTrue(trial.attempt_trial(self.ledger, KEY, headers())["ok"])
            result = trial.attempt_trial(trial.TrialLedger(self.path), KEY, headers(token="synthetic-rotated"))
            self.assertFalse(result["ok"] or result["already"])
        self.assertEqual(len(calls), 1)
        for file in self.path.parent.iterdir():
            self.assertNotIn(b"synthetic", file.read_bytes())

    def test_finish_save_failure_propagates_and_attempt_still_blocks(self):
        save = self.ledger._save
        calls = 0

        def fail_second(accounts):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("mock finish failure")
            return save(accounts)

        with patch.object(self.ledger, "_save", side_effect=fail_second), \
                mock_http(lambda request: httpx.Response(200, json={"code": 0})) as client:
            with self.assertRaises(OSError):
                trial.attempt_trial(self.ledger, KEY, headers())
            client.assert_called_once()
        self.assertFalse(trial.TrialLedger(self.path).begin(KEY))
        self.assertIsNone(self.ledger.summary(KEY)["finished_at"])

    def test_unsupported_attempt_does_not_touch_ledger(self):
        with patch.object(self.ledger, "begin") as begin, patch.object(trial.httpx, "Client") as client:
            with self.assertRaises(ValueError):
                trial.attempt_trial(self.ledger, KEY, headers("www.workbuddy.cn"))
            begin.assert_not_called()
            client.assert_not_called()
        self.assertFalse(self.path.parent.exists())

    def test_thread_concurrency_separate_instances_at_most_one_post(self):
        with patch.object(trial, "claim_trial", return_value=trial._result()) as claim:
            with ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(lambda _: trial.attempt_trial(
                    trial.TrialLedger(self.path), KEY, headers()), range(30)))
            self.assertEqual(len(results), 30)
            claim.assert_called_once()

    def test_process_concurrency_at_most_one_post(self):
        context = multiprocessing.get_context("spawn")
        event, queue = context.Event(), context.Queue()
        workers = [context.Process(target=process_attempt, args=(str(self.path), event, queue))
                   for _ in range(6)]
        try:
            for worker in workers:
                worker.start()
            event.set()
            for worker in workers:
                worker.join(15)
                self.assertEqual(worker.exitcode, 0)
            messages = [queue.get(timeout=5) for _ in range(7)]
            self.assertEqual(messages.count("post"), 1)
            self.assertEqual(sum(isinstance(message, dict) for message in messages), 6)
            self.assertFalse(trial.TrialLedger(self.path).begin(KEY))
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
            queue.close()
            queue.join_thread()


if __name__ == "__main__":
    unittest.main()
