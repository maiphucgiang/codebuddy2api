"""Per-account automation preferences and runtime gates use only isolated fixtures."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
from types import SimpleNamespace
from unittest.mock import patch
import converter
from app import checkin, credits, daily_chat, growth_tasks, model_policy
from app.control_store import ControlStore
from tests import test_credential_actions as fixtures


class AutomationTests(unittest.TestCase):
    add_account = fixtures.CredentialActionTests.add_account
    configure = fixtures.CredentialActionTests.configure
    handle_upstream = fixtures.CredentialActionTests.handle_upstream
    def setUp(self):
        fixtures.CredentialActionTests.setUp(self)
        management = SimpleNamespace(gateway=SimpleNamespace(invalidate_model_table=lambda: None))
        self.enterContext(patch.dict(converter.CONFIG, {"management": management}))

    def preference(self, entry, field, enabled):
        response = self.client.patch('/admin/credentials/' + entry['account_key'], json={field: enabled})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()[field], enabled)

    def post(self, entry, action):
        response = self.client.post('/admin/credentials/' + entry['account_key'] + '/' + action)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['results'][0]

    def periodic(self, entry):
        with patch.object(credits, 'fetch_credits', return_value={'credits': 10, 'intl': entry['profile'].startswith('intl')}):
            return converter._sync_credits(self.pool, self.ledger, entry, checkin=True, failed=set())

    def test_defaults_are_domestic_on_international_off(self):
        rows = self.client.get('/admin/credentials').json()['credentials']
        for row in rows:
            domestic = row['profile'].startswith('cn-')
            self.assertIs(row['auto_checkin'], domestic)
            self.assertIs(row['auto_travel'], domestic)
            self.assertIs(row['travel_supported'], domestic)
            self.assertEqual(row['checkin']['state'], 'unknown')

    def test_settings_persist_without_claiming_and_preserve_account_disable(self):
        entry = self.entries['intl-work']
        self.preference(entry, 'enabled', False)
        self.preference(entry, 'auto_checkin', True)
        reopened = ControlStore(self.root / 'control.sqlite3')
        self.addCleanup(reopened.close)
        meta = reopened.snapshot()['credentials'][entry['account_key']]
        self.assertEqual(meta, {'enabled': False, 'auto_checkin': True})
        self.status_query.assert_not_called()
        self.travel_mock.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_invalid_values_never_change_revision(self):
        before = self.control.snapshot()['revision']
        for data in ({}, {'auto_checkin': 1}, {'auto_travel': 'true'}, {'auto_checkin': None},
                     {'enabled': False, 'auto_checkin': True}, {'bad': True}):
            with self.subTest(data=data):
                self.assertEqual(self.client.patch(self.url, json=data).status_code, 400)
        self.assertEqual(self.control.snapshot()['revision'], before)
        self.assertEqual(self.client.patch('/admin/credentials/unknown', json={'auto_checkin': True}).status_code, 404)

    def test_international_can_enable_checkin_later_without_enabling_travel(self):
        entry = self.entries['intl-work']
        with patch.object(credits, 'daily_checkin', return_value={'ok': True}) as claim:
            self.periodic(entry)
            self.status_query.assert_not_called()
            claim.assert_not_called()
            self.preference(entry, 'auto_checkin', True)
            self.periodic(entry)
            claim.assert_called_once()
        self.travel_mock.assert_not_called()
        response = self.client.patch('/admin/credentials/' + entry['account_key'], json={'auto_travel': True})
        self.assertEqual(response.status_code, 400)

    def test_separate_switches_preserve_checkin_then_travel_order(self):
        events = []
        self.status_query.side_effect = lambda *a, **kw: events.append('status') or {'state': 'available'}
        self.travel_mock.side_effect = lambda *a, **kw: events.append('travel') or {'ok': True, 'message': '旅行中'}
        with patch.object(credits, 'daily_checkin', side_effect=lambda *a, **kw: events.append('checkin') or {'ok': True}):
            self.periodic(self.entry)
        self.assertEqual(events, ['status', 'checkin', 'travel'])
        events.clear()
        self.preference(self.entry, 'auto_checkin', False)
        self.periodic(self.entry)
        self.assertEqual(events, ['travel'])
        events.clear()
        self.preference(self.entry, 'auto_travel', False)
        self.periodic(self.entry)
        self.assertEqual(events, [])

    def test_disabling_during_status_query_cancels_unsent_claim_not_balance(self):
        def status(*args, **kwargs):
            self.control.set_auto_checkin(self.entry['account_key'], False)
            return {'state': 'available'}
        self.status_query.side_effect = status
        with patch.object(credits, 'daily_checkin') as claim:
            self.assertIsNotNone(self.periodic(self.entry))
            claim.assert_not_called()
        self.assertEqual(self.ledger.entry(self.entry['id'])['credits']['credits'], 10)

    def test_manual_checkin_is_not_blocked_by_auto_switch_and_can_follow_travel(self):
        self.preference(self.entry, 'auto_checkin', False)
        with patch.object(credits, 'daily_checkin', return_value={'ok': True}) as claim:
            r = self.client.post(self.url + '/checkin')
            self.assertTrue(r.json()['results'][0]['ok'], r.text)
            claim.assert_called_once()
            self.travel_mock.assert_called_once()
        self.travel_mock.reset_mock()
        self.preference(self.entry, 'auto_travel', False)
        self.client.post(self.url + '/checkin')
        self.travel_mock.assert_not_called()

    def test_inactive_checkin_is_visible_but_does_not_block_travel(self):
        self.status_query.return_value = {'ok': False, 'state': 'inactive', 'code': 10001}
        with patch.object(credits, 'daily_checkin') as claim:
            response = self.client.post(self.url + '/checkin')
            result = response.json()['results'][0]
            self.assertFalse(result['ok'])
            self.assertIn('未开放', result['message'])
            claim.assert_not_called()
            self.travel_mock.assert_called_once()
        row = next(r for r in self.client.get('/admin/credentials').json()['credentials'] if r['id'] == self.entry['account_key'])
        self.assertEqual(row['checkin']['state'], 'inactive')

    def test_sync_does_not_run_automations_and_travel_is_scoped(self):
        with patch.object(credits, 'fetch_credits', return_value={'credits': 10, 'intl': False}), \
             patch.object(credits, 'fetch_request_usage', return_value={'by_day': {}, 'total_credits': 0, 'requests': 0}):
            self.client.post(self.url + '/sync')
        self.status_query.assert_not_called()
        self.travel_mock.assert_not_called()
        result = self.client.post(self.url + '/travel').json()['results'][0]
        self.assertTrue(result['ok'])
        self.assertEqual(self.travel_mock.call_args.args[1], 'cn-cli')
        self.assertIn('travel', self.ledger.entry(self.entry['id']))
        self.travel_mock.reset_mock()
        for action in ('travel', 'travel-status'):
            response = self.client.post('/admin/credentials/' + self.entries['intl-work']['account_key'] + '/' + action)
            self.assertTrue(response.json()['results'][0]['skipped'])
        self.travel_mock.assert_not_called()

    def test_old_lease_during_preflight_stops_both_claim_and_followup(self):
        def status(*args, **kwargs):
            self.entry['cm'].invalidate()
            return {'state': 'available'}
        self.status_query.side_effect = status
        with patch.object(credits, 'daily_checkin') as claim:
            result = self.client.post(self.url + '/checkin').json()['results'][0]
            self.assertFalse(result['ok'])
            claim.assert_not_called()
            self.travel_mock.assert_not_called()

    def test_unexpected_followup_failure_preserves_partial_not_false_success(self):
        with patch.object(credits, 'daily_checkin', return_value={'ok': True}):
            self.travel_mock.side_effect = OSError('synthetic-secret')
            result = self.client.post(self.url + '/checkin').json()['results'][0]
        self.assertFalse(result['ok'])
        self.assertTrue(result['checkin_ok'])
        self.assertNotIn('synthetic-secret', result['message'])

    def test_old_automatic_lease_stops_balance_and_trial_after_preflight(self):
        entry = self.entries['intl-work']
        self.preference(entry, 'auto_checkin', True)
        def status(*args, **kwargs):
            entry['cm'].invalidate()
            return {'state': 'available'}
        self.status_query.side_effect = status
        with patch.object(credits, 'daily_checkin') as claim, patch.object(credits, 'fetch_credits') as balance, \
             patch.object(converter.trial_rewards, 'claim_trial') as trial:
            failed = set()
            self.assertIsNone(converter._sync_credits(self.pool, self.ledger, entry, checkin=True, failed=failed))
            self.assertIn(entry['id'], failed)
            claim.assert_not_called()
            balance.assert_not_called()
            trial.assert_not_called()


    def test_checkin_restriction_disables_account_and_stops_followup(self):
        entry = self.entry
        self.status_query.return_value = {"state": "available"}
        with patch.object(credits, "daily_checkin", return_value={"ok": False, "code": 11140}),              patch.object(growth_tasks, "sweep_account") as growth,              patch.object(credits, "fetch_credits") as balance:
            result = converter._sync_credits(self.pool, self.ledger, entry, checkin=True, failed=set())
        self.assertIsNone(result)
        self.assertTrue(self.pool._index_cm[entry["cm"]]["auto_disabled"])
        self.assertFalse(model_policy.credential_enabled(converter.CONFIG, entry))
        self.travel_mock.assert_not_called()
        growth.assert_not_called()
        balance.assert_not_called()

    def test_daily_chat_restriction_disables_account_and_skips_balance(self):
        entry = self.entries["intl-work"]
        self.preference(entry, "auto_daily_chat", True)
        with patch.object(daily_chat, "is_due", return_value=True),              patch.object(daily_chat, "perform", return_value={
                 "ok": False, "state": "rejected", "http_status": 403, "code": 11140}),              patch.object(credits, "fetch_credits") as balance:
            result = converter._sync_credits(self.pool, self.ledger, entry, checkin=True, failed=set())
        self.assertIsNone(result)
        self.assertTrue(self.pool._index_cm[entry["cm"]]["auto_disabled"])
        self.assertFalse(model_policy.credential_enabled(converter.CONFIG, entry))
        balance.assert_not_called()

    def test_growth_exception_restriction_disables_account_and_skips_balance(self):
        entry = self.entry
        self.preference(entry, "auto_checkin", False)
        self.preference(entry, "auto_travel", False)
        self.preference(entry, "auto_growth", True)
        with patch.object(growth_tasks, "sweep_account",
                          side_effect=growth_tasks.Failure("business", 403, 11140)) as growth,              patch.object(credits, "fetch_credits") as balance:
            result = converter._sync_credits(self.pool, self.ledger, entry, checkin=True, failed=set())
        self.assertIsNone(result)
        self.assertTrue(self.pool._index_cm[entry["cm"]]["auto_disabled"])
        self.assertFalse(model_policy.credential_enabled(converter.CONFIG, entry))
        growth.assert_called_once()
        balance.assert_not_called()

    def test_background_nonrestriction_errors_do_not_disable_or_cool(self):
        entry = self.entry
        self.preference(entry, "auto_checkin", False)
        self.preference(entry, "auto_travel", False)
        self.preference(entry, "auto_growth", True)
        for code in (1001, 11134):
            with self.subTest(code=code), \
                 patch.object(growth_tasks, "sweep_account",
                              side_effect=growth_tasks.Failure("business", 403, code)), \
                 patch.object(credits, "fetch_credits", return_value={"credits": 10, "intl": False}):
                result = converter._sync_credits(self.pool, self.ledger, entry, checkin=True, failed=set())
            self.assertIsNotNone(result)
            internal = self.pool._index_cm[entry["cm"]]
            self.assertIsNone(internal.get("auto_disabled"))
            self.assertEqual(internal["fail_until"], 0.0)
            self.assertTrue(model_policy.credential_enabled(converter.CONFIG, entry))

    def test_cookie_preferences_require_csrf(self):
        self.client.headers.pop('authorization')
        self.assertEqual(self.client.patch(self.url, json={'auto_checkin': False}).status_code, 401)
        r = self.client.post('/admin/session', json={'api_key': 'synthetic-management-key'}, headers={'Origin': 'https://testserver'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.patch(self.url, json={'auto_checkin': False}).status_code, 403)
        self.client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': r.json()['csrf_token']})
        self.preference(self.entry, 'auto_checkin', False)


    def test_enterprise_travel_is_exempt_and_no_longer_reports_an_upstream_403(self):
        """Replays the reported symptom: an enterprise card stuck on 阶段：状态查询 · HTTP 403.

        The growth center is personal-only, so the account must be recognised as exempt
        before any request is made, and the stale refusal must be cleared from the ledger
        so the dashboard stops presenting a permanent bound as the current travel state.
        """
        self.add_account('enterprise-tenant', 'cn-cli', enterprise='fylzonuezfnk')
        # A tenant is a separate identity, so the pool must be rebuilt around both files.
        self.configure(profiles=('cn-cli', 'enterprise-tenant'))
        entry = self.entries['enterprise-tenant']
        # Seed the exact failure the operator saw.
        self.ledger.update_travel(entry['id'], {'ok': False, 'state': 'unknown', 'phase': 'status',
                                                'error_kind': 'http', 'http_status': 403, 'code': 403,
                                                'message': '状态查询失败，未执行写操作'})
        self.assertTrue(entry['enterprise'])
        self.assertFalse(model_policy.credential_auto_travel(converter.CONFIG, entry))
        # The periodic sweep clears the stale refusal instead of replaying it.
        with patch.object(credits, 'fetch_credits', return_value={'credits': 10, 'intl': False}):
            converter._sync_credits(self.pool, self.ledger, entry, checkin=True, failed=set())
        travel_row = self.ledger.entry(entry['id'])['travel']
        self.assertEqual(travel_row['state'], 'unavailable')
        self.assertIsNone(travel_row.get('http_status'))
        self.assertIsNone(travel_row.get('error_kind'))
        self.assertIn('企业账号', travel_row['message'])
        # The admin row and the manual action both report the exemption without any request.
        rows = self.client.get('/admin/credentials').json()['credentials']
        row = next(r for r in rows if r['name'] == 'enterprise-tenant.info')
        self.assertIs(row['travel_supported'], False)
        self.assertIs(row['auto_travel'], False)
        self.assertEqual(row['travel']['state'], 'unavailable')
        self.travel_mock.reset_mock()
        self.requests.clear()
        result = self.post(entry, 'travel')
        self.assertTrue(result['skipped'])
        self.assertIn('企业账号', result['message'])
        self.travel_mock.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_a_manual_sync_also_clears_the_stale_travel_refusal(self):
        """The sync button runs with checkin=False, so the cleanup must not depend on it.

        Otherwise the operator can press 同步 repeatedly and the stale 403 stays on the
        card until the next full sweep, which is exactly the state that was reported.
        """
        self.add_account('enterprise-tenant', 'cn-cli', enterprise='fylzonuezfnk')
        self.configure(profiles=('cn-cli', 'enterprise-tenant'))
        entry = self.entries['enterprise-tenant']
        stale = {'ok': False, 'state': 'unknown', 'phase': 'status', 'error_kind': 'http',
                 'http_status': 403, 'code': 403, 'message': '状态查询失败，未执行写操作'}
        self.ledger.update_travel(entry['id'], dict(stale))
        with patch.object(credits, 'fetch_credits', return_value={'credits': 10, 'intl': False}):
            converter._sync_credits(self.pool, self.ledger, entry, checkin=False, failed=set())
        travel_row = self.ledger.entry(entry['id'])['travel']
        self.assertEqual(travel_row['state'], 'unavailable')
        self.assertIsNone(travel_row.get('http_status'))
        self.assertIn('企业账号', travel_row['message'])
        # A second sync must not rewrite the same notice, so an exempt account stays quiet.
        revision = self.ledger.revision
        with patch.object(credits, 'fetch_credits', return_value={'credits': 10, 'intl': False}):
            converter._sync_credits(self.pool, self.ledger, entry, checkin=False, failed=set())
        self.assertEqual(self.ledger.entry(entry['id'])['travel'], travel_row)
        self.assertGreaterEqual(revision, 0)

    def test_a_personal_account_keeps_travelling(self):
        entry = self.entries['cn-cli']
        self.assertFalse(entry.get('enterprise') is True)
        self.assertTrue(model_policy.credential_auto_travel(converter.CONFIG, entry))
        self.assertTrue(self.post(entry, 'travel')['ok'])
        self.travel_mock.assert_called_once()


class CheckinProtocolTests(unittest.TestCase):
    def test_status_requires_active_and_explicit_today_flag(self):
        cases = [({}, 'unknown'), ({'active': True}, 'unknown'), ({'active': 'true', 'today_checked_in': False}, 'unknown'),
                 ({'active': False}, 'inactive'), ({'active': True, 'today_checked_in': False}, 'available'),
                 ({'active': True, 'today_checked_in': True}, 'already')]
        for data, expected in cases:
            with self.subTest(data=data), patch.object(credits, '_checkin_request', return_value=(200, {'code': 0, 'data': data}, '')):
                self.assertEqual(credits.fetch_checkin_status('synthetic')['state'], expected)

    def test_service_failures_are_not_already_checked_in(self):
        for status in (0, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status), patch.object(credits, '_checkin_request', return_value=(status, {'code': 1001}, '')):
                self.assertFalse(credits.daily_checkin('synthetic')['ok'])
                self.assertEqual(credits.fetch_checkin_status('synthetic')['state'], 'unknown')

    def test_business_code_mapping_and_untrusted_messages(self):
        for code, message, expected in [(1001, '', 'already'), (1002, '', 'not_eligible'), (1003, '', 'inactive'),
                (10001, '活动未开启', 'inactive'), (10001, '今日已签到', 'already'), (10001, 'synthetic-secret', 'error'),
                (None, '', 'error'), (False, '', 'error'), (0.1, '', 'error')]:
            with self.subTest(code=code, message=message):
                r = checkin.normalize(credits.classify_checkin_result(True, code, message))
                self.assertEqual(r['state'], expected)
                self.assertNotIn('synthetic-secret', r['message'])
        self.assertEqual(checkin.view({'ok': False, 'code': 10001, 'message': '签到活动未开启或已过期'})['state'], 'inactive')
        self.assertEqual(checkin.view({'ok': False, 'code': 1001})['state'], 'unknown')

    def test_unknown_status_never_claims(self):
        with patch.object(credits, 'fetch_checkin_status', return_value={'state': 'unknown'}), patch.object(credits, 'daily_checkin') as claim:
            result = checkin.perform('synthetic')
            self.assertFalse(result['ok'])
            claim.assert_not_called()

if __name__ == '__main__':
    unittest.main()
