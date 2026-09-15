"""Travel protocol, dynamic locations, and uncertain write outcomes."""
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

CONFIG = {'locations': [{'id': 21, 'name': '海边书店'}, {'id': 37, 'name': '山间茶馆'}]}
IDLE = {'state': 'idle', 'daily_limit_reached': False}
TRAVELING = {'state': 'traveling', 'location': {'id': 21, 'name': '海边书店'},
             'daily_limit_reached': False, 'arrive_at': 300, 'server_now': 100}


class TravelTests(unittest.TestCase):
    def run_trip(self, responses, profile='cn-work', **kwargs):
        requests = []
        pending = iter(responses)
        def handle(request):
            requests.append(request)
            value = next(pending)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, httpx.Response):
                return value
            status, body = value if isinstance(value, tuple) else (200, {'code': 0, 'data': value})
            return httpx.Response(status, json=body)
        client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
        with patch('app.travel.httpx.Client', return_value=client), \
             patch('app.travel.random.choice', side_effect=lambda choices: choices[0]):
            result = travel.perform('synthetic-token', profile, **kwargs)
        return result, requests

    def test_arrival_claims_rechecks_then_configures_departs_and_rechecks(self):
        result, calls = self.run_trip([
            {'state': 'arrived', 'daily_limit_reached': False}, {'reward_credit': 8},
            IDLE, CONFIG, {}, TRAVELING])
        self.assertTrue(result['ok'])
        self.assertTrue(result['claimed'])
        self.assertTrue(result['departed'])
        self.assertEqual(result['claimed_credit'], 8)
        self.assertEqual(result['location_name'], '海边书店')
        self.assertEqual(result['remaining_seconds'], 200)
        self.assertEqual(result['phase'], 'after_depart')
        self.assertEqual([(c.method, c.url.path) for c in calls], [
            ('GET', travel.PREFIX+'status'), ('POST', travel.PREFIX+'claim'),
            ('GET', travel.PREFIX+'status'), ('GET', travel.PREFIX+'config'),
            ('POST', travel.PREFIX+'depart'), ('GET', travel.PREFIX+'status')])
        for call in calls:
            self.assertEqual(call.url.host, 'www.workbuddy.cn')
            self.assertEqual(call.headers['authorization'], 'Bearer synthetic-token')
            self.assertEqual(call.headers['x-product-code'], 'workbuddy')
            self.assertNotIn('x-device-token', call.headers)
            self.assertNotIn('/v2', call.url.path)
            if call.method == 'GET':
                self.assertEqual(call.content, b'')
            else:
                self.assertEqual(call.headers['content-type'], 'application/json')
        self.assertEqual(json.loads(calls[1].content), {})
        self.assertEqual(json.loads(calls[4].content), {'location_id': 21})

    def test_claim_success_accepts_missing_null_and_empty_data_without_inventing_credit(self):
        for receipt in ({'code': 0}, {'code': 0, 'data': None}, {'code': 0, 'data': {}}):
            with self.subTest(receipt=receipt):
                result, calls = self.run_trip([
                    {'state': 'arrived', 'reward_credit': 8}, (200, receipt),
                    {**IDLE, 'daily_limit_reached': True}])
                self.assertTrue(result['ok'])
                self.assertTrue(result['claimed'])
                self.assertIsNone(result['claimed_credit'])
                self.assertFalse(result['departed'])
                self.assertEqual(len(calls), 3)

    def test_idle_departs_without_checkin_and_uses_only_configured_locations(self):
        result, calls = self.run_trip([IDLE, {'locations': [{'id': 99, 'name': '新地点'}]}, {},
                                      {**TRAVELING, 'location': {'id': 99}}], profile='cn-cli')
        self.assertTrue(result['ok'])
        self.assertTrue(result['departed'])
        self.assertFalse(result['claimed'])
        self.assertEqual(result['location_name'], '新地点')
        self.assertEqual(json.loads(calls[2].content), {'location_id': 99})

    def test_empty_departure_receipt_requires_fresh_status(self):
        for receipt in ({'code': 0}, {'code': 0, 'data': None}):
            with self.subTest(receipt=receipt):
                result, calls = self.run_trip([IDLE, CONFIG, (200, receipt), TRAVELING])
                self.assertTrue(result['ok'])
                self.assertTrue(result['departed'])
                self.assertEqual(result['arrive_at'], 300)
                self.assertEqual(len(calls), 4)

    def test_traveling_and_limit_do_not_fetch_config_or_write(self):
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

    def test_read_only_never_claims_or_fetches_config(self):
        for state in ('idle', 'traveling', 'arrived'):
            with self.subTest(state=state):
                result, calls = self.run_trip([{'state': state, 'reward_credit': 7}], read_only=True,
                                              can_write=lambda: False)
                self.assertTrue(result['ok'])
                self.assertFalse(result['claimed'])
                self.assertFalse(result['departed'])
                self.assertEqual(len(calls), 1)

    def test_missing_invalid_and_ambiguous_status_stop_writes(self):
        for data in ({}, {'state': None}, {'state': 'unknown'}, {'state': ['idle']}, {'state': {}},
                     {'state': 'idle'}, {'state': 'idle', 'daily_limit_reached': 0},
                     {'state': 'idle', 'daily_limit_reached': 'false'}):
            with self.subTest(data=data):
                result, calls = self.run_trip([data])
                self.assertFalse(result['ok'])
                self.assertTrue(result['stale'])
                self.assertEqual(len(calls), 1)

    def test_business_failure_is_not_success_and_only_safe_diagnostics_are_returned(self):
        cases = [({'code': 10001, 'msg': 'synthetic-secret'}, 'business', 10001),
                 ({'code': False, 'data': IDLE}, 'protocol', None),
                 ({'code': '0', 'data': IDLE}, 'protocol', None),
                 ({'code': 0.0, 'data': IDLE}, 'protocol', None),
                 ({'code': 2**99, 'data': IDLE}, 'protocol', None),
                 ({'code': 0}, 'protocol', 0), ([], 'protocol', None), (None, 'protocol', None)]
        for body, kind, code in cases:
            with self.subTest(body=body):
                result, calls = self.run_trip([(200, body)])
                self.assertFalse(result['ok'])
                self.assertEqual(result['http_status'], 200)
                self.assertEqual(result['code'], code)
                self.assertEqual(result['error_kind'], kind)
                self.assertEqual(result['phase'], 'status')
                self.assertNotIn('synthetic-secret', str(result))
                self.assertEqual(len(calls), 1)

    def test_invalid_write_envelope_never_confirms_a_claim(self):
        for body in ({'code': 0, 'data': []}, {'code': False}, {'code': '0'}, {'data': {}}, None):
            with self.subTest(body=body):
                result, calls = self.run_trip([{'state': 'arrived'}, (200, body)])
                self.assertFalse(result['claimed'])
                self.assertEqual(result['phase'], 'claim')
                self.assertEqual(len(calls), 2)

    def test_http_errors_preserve_status_without_redirects_or_body_leaks(self):
        for status in (302, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                response = httpx.Response(status, text='synthetic-secret', headers={'Location': 'https://untrusted.invalid/'})
                result, calls = self.run_trip([response])
                self.assertFalse(result['ok'])
                self.assertEqual(result['http_status'], status)
                self.assertEqual(result['error_kind'], 'http')
                self.assertIsNone(result['code'])
                self.assertNotIn('synthetic-secret', str(result))
                self.assertEqual(len(calls), 1)
        result, _ = self.run_trip([(429, {'code': 123, 'msg': 'synthetic-secret'})])
        self.assertEqual(result['code'], 123)
        result, _ = self.run_trip([httpx.Response(200, text='synthetic-secret')])
        self.assertEqual(result['error_kind'], 'protocol')

    def test_invalid_config_never_falls_back_to_fixed_locations(self):
        invalid = [{}, {'locations': []}, {'locations': None}, {'locations': {}},
                   {'locations': [None]}, {'locations': [{'id': True, 'name': '地点'}]},
                   {'locations': [{'id': '1', 'name': '地点'}]}, {'locations': [{'id': 0, 'name': '地点'}]},
                   {'locations': [{'id': 1, 'name': ''}]}, {'locations': [{'id': 1, 'name': 'x'*81}]},
                   {'locations': [{'id': 1, 'name': 'a\nb'}]}, {'locations': [{'id': 1}]},
                   {'locations': [{'id': 1, 'name': 'A'}, {'id': 1, 'name': 'B'}]},
                   {'locations': [{'id': n, 'name': '地点'} for n in range(1, 102)]}]
        for config in invalid:
            with self.subTest(config=config):
                result, calls = self.run_trip([IDLE, config])
                self.assertFalse(result['departed'])
                self.assertFalse(result['ok'])
                self.assertEqual(result['phase'], 'config')
                self.assertEqual(result['error_kind'], 'protocol')
                self.assertEqual([c.method for c in calls], ['GET', 'GET'])

    def test_config_failure_preserves_confirmed_claim(self):
        result, calls = self.run_trip([{'state': 'arrived'}, (200, {'code': 0}), IDLE,
                                      (404, {'code': 12, 'msg': 'synthetic-secret'})])
        self.assertTrue(result['claimed'])
        self.assertFalse(result['departed'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['phase'], 'config')
        self.assertEqual(result['http_status'], 404)
        self.assertIn('已领取', result['message'])
        self.assertNotIn('synthetic-secret', str(result))
        self.assertEqual(len(calls), 4)

    def test_failed_claim_and_failed_post_claim_query_do_not_depart(self):
        for response in [(500, {'code': -1}), httpx.ReadTimeout('synthetic-secret')]:
            result, calls = self.run_trip([{'state': 'arrived'}, response])
            self.assertFalse(result['ok'])
            self.assertFalse(result['claimed'])
            self.assertTrue(result['stale'])
            self.assertEqual(len(calls), 2)
            self.assertNotIn('synthetic-secret', str(result))
        result, calls = self.run_trip([{'state': 'arrived'}, (200, {'code': 0}), (503, {})])
        self.assertTrue(result['claimed'])
        self.assertFalse(result['ok'])
        self.assertFalse(result['departed'])
        self.assertEqual(result['phase'], 'after_claim')
        self.assertEqual(len(calls), 3)

    def test_failed_depart_is_not_replayed_and_preserves_confirmed_claim(self):
        for error, kind in [(httpx.ReadTimeout('synthetic-secret'), 'timeout'),
                            (httpx.WriteTimeout('synthetic-secret'), 'timeout'),
                            (httpx.ConnectError('synthetic-secret'), 'network')]:
            result, calls = self.run_trip([{'state': 'arrived'}, {'reward_credit': 8}, IDLE, CONFIG, error])
            self.assertFalse(result['ok'])
            self.assertTrue(result['claimed'])
            self.assertFalse(result['departed'])
            self.assertTrue(result['stale'])
            self.assertEqual(result['error_kind'], kind)
            self.assertEqual(result['phase'], 'depart')
            self.assertEqual(len(calls), 5)
            self.assertNotIn('synthetic-secret', str(result))

    def test_post_depart_failure_retains_action_without_old_state_or_guessed_times(self):
        result, calls = self.run_trip([
            {**IDLE, 'reward_credit': 8, 'server_now': 50, 'arrive_at': 70}, CONFIG,
            (200, {'code': 0}), httpx.ReadTimeout('synthetic-secret')])
        self.assertFalse(result['ok'])
        self.assertTrue(result['departed'])
        self.assertTrue(result['stale'])
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['phase'], 'after_depart')
        for field in ('reward_credit', 'server_now', 'arrive_at', 'remaining_seconds', 'daily_limit_reached'):
            self.assertIsNone(result[field])
        self.assertEqual(len(calls), 4)
        self.assertIn('勿重复派出', result['message'])

    def test_idle_after_depart_is_uncertain_and_never_dispatches_again(self):
        result, calls = self.run_trip([IDLE, CONFIG, {}, IDLE])
        self.assertTrue(result['departed'])
        self.assertFalse(result['ok'])
        self.assertTrue(result['stale'])
        self.assertEqual(result['state'], 'idle')
        self.assertEqual(len(calls), 4)

    def test_arrival_after_depart_does_not_start_another_claim_loop(self):
        result, calls = self.run_trip([IDLE, CONFIG, {}, {'state': 'arrived'}])
        self.assertTrue(result['ok'])
        self.assertTrue(result['departed'])
        self.assertFalse(result['claimed'])
        self.assertEqual(result['state'], 'arrived')
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
        result, calls = self.run_trip([{'state': 'arrived'}, {'reward_credit': 8}, IDLE], can_write=lambda: next(decisions))
        self.assertTrue(result['claimed'])
        self.assertFalse(result['departed'])
        self.assertEqual(len(calls), 3)

    def test_setting_or_lease_change_during_config_stops_dispatch(self):
        decisions = iter([True, True, False])
        result, calls = self.run_trip([IDLE, CONFIG], can_write=lambda: next(decisions))
        self.assertFalse(result['departed'])
        self.assertTrue(result['skipped'])
        self.assertEqual(result['phase'], 'config')
        self.assertEqual(len(calls), 2)

    def test_initial_cancellation_never_queries(self):
        result, calls = self.run_trip([], can_write=lambda: False)
        self.assertTrue(result['skipped'])
        self.assertEqual(calls, [])

    def test_status_preserves_dynamic_name_and_only_computes_known_durations(self):
        result, _ = self.run_trip([TRAVELING], read_only=True)
        self.assertEqual(result['location_id'], 21)
        self.assertEqual(result['location_name'], '海边书店')
        self.assertEqual(result['remaining_seconds'], 200)
        for value in (None, True, '300', -1, float('inf'), float('nan')):
            with self.subTest(value=value):
                self.assertIsNone(travel._status({**TRAVELING, 'arrive_at': value})['remaining_seconds'])
        self.assertEqual(travel._status({**TRAVELING, 'arrive_at': 1})['remaining_seconds'], 0)
        self.assertIsNone(travel._status({**TRAVELING, 'state': 'arrived'})['remaining_seconds'])
        self.assertIsNone(travel._status({**TRAVELING, 'location': {'id': True, 'name': 'bad'}})['location_name'])

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
                travel.remember(ledger, 'one', {'ok': False, 'departed': True, 'state': 'unknown', 'stale': True,
                                                'phase': 'after_depart', 'http_status': 503, 'message': '查询失败'})
            saved = ledger.entry('one')['travel']
            self.assertTrue(saved['departed'])
            self.assertEqual(saved['phase'], 'after_depart')
            self.assertEqual(saved['last_success']['state'], 'traveling')
            self.assertNotIn('last_success', saved['last_success'])


if __name__ == '__main__':
    unittest.main()
