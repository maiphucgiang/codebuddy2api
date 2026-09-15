"""Travel state transitions, fixed domestic origin, and uncertain write outcomes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import tempfile
import unittest
from unittest.mock import patch
import httpx
from app import travel
from app.credits import CreditLedger


class TravelTests(unittest.TestCase):
    def run_trip(self, responses, profile='cn-work', **kwargs):
        requests = []
        pending = iter(responses)
        def handle(request):
            requests.append(request)
            value = next(pending)
            if isinstance(value, Exception):
                raise value
            status, body = value if isinstance(value, tuple) else (200, {'code': 0, 'data': value})
            return httpx.Response(status, json=body)
        client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
        with patch('app.travel.httpx.Client', return_value=client), patch('app.travel.random.choice', return_value=2):
            result = travel.perform('synthetic-token', profile, **kwargs)
        return result, requests

    def test_arrival_claims_rechecks_then_departs(self):
        result, calls = self.run_trip([
            {'state': 'arrived', 'daily_limit_reached': False}, {'reward_credit': 8},
            {'state': 'idle', 'daily_limit_reached': False}, {'location': {'id': 2}, 'arrive_at': 300, 'server_now': 100}])
        self.assertTrue(result['ok'])
        self.assertTrue(result['claimed'])
        self.assertTrue(result['departed'])
        self.assertEqual(result['claimed_credit'], 8)
        self.assertEqual(result['location_name'], '商场店铺')
        self.assertEqual([(c.method, c.url.path) for c in calls], [
            ('GET', travel.PREFIX+'status'), ('POST', travel.PREFIX+'claim'),
            ('GET', travel.PREFIX+'status'), ('POST', travel.PREFIX+'depart')])
        for call in calls:
            self.assertEqual(call.url.host, 'www.workbuddy.cn')
            self.assertEqual(call.headers['authorization'], 'Bearer synthetic-token')
            self.assertNotIn('x-device-token', call.headers)
            self.assertNotIn('/v2', call.url.path)
        self.assertEqual(json.loads(calls[1].content), {})
        self.assertEqual(json.loads(calls[3].content), {'location_id': 2})

    def test_idle_can_depart_without_checkin(self):
        result, calls = self.run_trip([{'state': 'idle', 'daily_limit_reached': False}, {}])
        self.assertTrue(result['departed'])
        self.assertFalse(result['claimed'])
        self.assertEqual(len(calls), 2)

    def test_traveling_and_limit_do_not_write(self):
        for state, limit in [('traveling', False), ('idle', True)]:
            with self.subTest(state=state):
                result, calls = self.run_trip([{'state': state, 'daily_limit_reached': limit}])
                self.assertTrue(result['ok'])
                self.assertTrue(result['skipped'])
                self.assertEqual(len(calls), 1)

    def test_arrival_can_claim_even_when_dispatch_limit_reached(self):
        result, calls = self.run_trip([{'state': 'arrived', 'daily_limit_reached': True}, {'reward_credit': 8},
                                       {'state': 'idle', 'daily_limit_reached': True}])
        self.assertTrue(result['claimed'])
        self.assertTrue(result['ok'])
        self.assertFalse(result['departed'])
        self.assertEqual(len(calls), 3)

    def test_read_only_never_claims_even_on_arrival(self):
        result, calls = self.run_trip([{'state': 'arrived', 'reward_credit': 7}], read_only=True)
        self.assertTrue(result['ok'])
        self.assertFalse(result['claimed'])
        self.assertEqual(len(calls), 1)

    def test_missing_invalid_and_ambiguous_status_stop_writes(self):
        for data in ({}, {'state': None}, {'state': 'unknown'}, {'state': ['idle']},
                     {'state': 'idle'}, {'state': 'idle', 'daily_limit_reached': 0},
                     {'state': 'idle', 'daily_limit_reached': 'false'}):
            with self.subTest(data=data):
                result, calls = self.run_trip([data])
                self.assertFalse(result['ok'])
                self.assertEqual(len(calls), 1)

    def test_business_failure_is_not_a_success(self):
        for body in ({'code': 10001, 'msg': 'synthetic-secret'}, {'code': False, 'data': {'state': 'idle'}},
                     {'code': 0}, [], None):
            with self.subTest(body=body):
                result, calls = self.run_trip([(200, body)])
                self.assertFalse(result['ok'])
                self.assertNotIn('synthetic-secret', str(result))
                self.assertEqual(len(calls), 1)

    def test_failed_claim_and_failed_post_claim_query_do_not_depart(self):
        for response in [(500, {'code': -1}), httpx.ReadTimeout('synthetic-secret')]:
            result, calls = self.run_trip([{'state': 'arrived'}, response])
            self.assertFalse(result['ok'])
            self.assertFalse(result['claimed'])
            self.assertTrue(result['stale'])
            self.assertEqual(len(calls), 2)
            self.assertNotIn('synthetic-secret', str(result))
        result, calls = self.run_trip([{'state': 'arrived'}, {'reward_credit': 7}, (503, {})])
        self.assertTrue(result['claimed'])
        self.assertFalse(result['ok'])
        self.assertFalse(result['departed'])
        self.assertEqual(len(calls), 3)

    def test_failed_depart_is_not_replayed_and_preserves_confirmed_claim(self):
        result, calls = self.run_trip([{'state': 'arrived'}, {'reward_credit': 8},
            {'state': 'idle', 'daily_limit_reached': False}, httpx.ReadTimeout('uncertain write')])
        self.assertFalse(result['ok'])
        self.assertTrue(result['claimed'])
        self.assertFalse(result['departed'])
        self.assertTrue(result['stale'])
        self.assertEqual(len(calls), 4)

    def test_eventually_consistent_arrival_is_not_claimed_twice(self):
        result, calls = self.run_trip([{'state': 'arrived'}, {'reward_credit': 8}, {'state': 'arrived'}])
        self.assertFalse(result['ok'])
        self.assertTrue(result['claimed'])
        self.assertEqual(len(calls), 3)

    def test_setting_change_during_query_stops_claim_or_depart(self):
        for state in ('arrived', 'idle'):
            decisions = iter([True, False])
            result, calls = self.run_trip([{'state': state, 'daily_limit_reached': False}], can_write=lambda: next(decisions))
            self.assertFalse(result['ok'])
            self.assertTrue(result['skipped'])
            self.assertEqual(len(calls), 1)
        decisions = iter([True, True, False])
        result, calls = self.run_trip([{'state': 'arrived'}, {'reward_credit': 8}, {'state': 'idle', 'daily_limit_reached': False}],
                                     can_write=lambda: next(decisions))
        self.assertTrue(result['claimed'])
        self.assertFalse(result['departed'])
        self.assertEqual(len(calls), 3)

    def test_international_never_opens_a_network_client(self):
        with patch('app.travel.httpx.Client') as client:
            for profile in ('intl-work', 'intl-cli', None, 'unknown'):
                self.assertTrue(travel.perform('synthetic-token', profile)['skipped'])
            client.assert_not_called()

    def test_failure_keeps_last_success_without_recursive_growth(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = CreditLedger(Path(folder)/'ledger.json')
            travel.remember(ledger, 'one', {'ok': True, 'state': 'traveling', 'message': '旅行中'})
            for _ in range(3):
                travel.remember(ledger, 'one', {'ok': False, 'state': 'unknown', 'stale': True, 'message': '查询失败'})
            last = ledger.entry('one')['travel']['last_success']
            self.assertEqual(last['state'], 'traveling')
            self.assertNotIn('last_success', last)


if __name__ == '__main__':
    unittest.main()
