"""Verify scoped confirmation, read-only settings and daily automation warnings over the admin API."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time
import unittest
from unittest.mock import patch

import converter
from app import buddy, credits
from tests import test_credential_actions as fixtures


class BuddyActionTests(unittest.TestCase):
    add_account = fixtures.CredentialActionTests.add_account
    configure = fixtures.CredentialActionTests.configure
    handle_upstream = fixtures.CredentialActionTests.handle_upstream
    setUp = fixtures.CredentialActionTests.setUp

    def test_confirmation_is_current_account_and_current_agreement_only(self):
        response = self.client.post(self.url + '/travel', json={
            'confirm_buddy': True, 'agreement_revision': buddy.AGREEMENT_REVISION})
        self.assertEqual(response.status_code, 200)
        context = self.travel_mock.call_args.kwargs['buddy_context']
        self.assertEqual(context['identity'], self.entry['account_key'])
        self.assertEqual(context['consent_revision'], buddy.AGREEMENT_REVISION)
        self.assertEqual(self.travel_mock.call_count, 1)
        self.assertFalse(context['auto_accept'])

    def test_malformed_wrong_action_and_stale_confirmation_never_start_work(self):
        for payload in ({'confirm_buddy': 'true', 'agreement_revision': buddy.AGREEMENT_REVISION},
                        {'confirm_buddy': True}, {'agree': True},
                        {'confirm_buddy': True, 'agreement_revision': 'old'},
                        {'confirm_buddy': True, 'agreement_revision': 'x' * 5000}):
            with self.subTest(payload=str(payload)[:90]):
                self.assertEqual(self.client.post(self.url + '/travel', json=payload).status_code, 400)
        self.assertEqual(self.client.post(self.url + '/checkin', json={
            'confirm_buddy': True, 'agreement_revision': buddy.AGREEMENT_REVISION}).status_code, 400)
        self.travel_mock.assert_not_called()
        self.status_query.assert_not_called()

    def test_disabled_and_international_accounts_cannot_use_confirmation(self):
        payload = {'confirm_buddy': True, 'agreement_revision': buddy.AGREEMENT_REVISION}
        self.control.set_credential(self.entry['account_key'], False)
        self.assertFalse(self.client.post(self.url + '/travel', json=payload).json()['ok'])
        self.assertFalse(self.client.post('/admin/credentials/' + self.entries['intl-work']['account_key'] + '/travel',
                                         json=payload).json()['ok'])
        self.travel_mock.assert_not_called()

    def test_environment_authorization_is_visible_but_not_writable_in_webui(self):
        converter.CONFIG.update(auto_accept_buddy=True, auto_accept_buddy_source='environment')
        value = next(item for item in self.client.get('/admin/settings').json()['items'] if item['key'] == 'auto_accept_buddy')
        self.assertIs(value['value'], True)
        self.assertIs(value['locked'], True)
        self.assertEqual(value['mode'], 'startup')
        response = self.client.patch('/admin/settings', json={'revision': self.control.snapshot()['revision'],
                                                             'settings': {'auto_accept_buddy': False}})
        self.assertEqual(response.status_code, 400)
        self.client.post(self.url + '/travel')
        self.assertTrue(self.travel_mock.call_args.kwargs['buddy_context']['auto_accept'])
        self.assertIsNone(self.travel_mock.call_args.kwargs['buddy_context']['consent_revision'])

    def test_manual_checkin_preserves_success_and_emits_one_automation_warning(self):
        self.travel_mock.return_value = {'ok': False, 'buddy_blocked': True, 'phase': 'buddy_tasks',
                                         'reason': 'buddy_not_eligible', 'message': '需要完成官方任务'}
        with patch.object(credits, 'daily_checkin', return_value={'ok': True}):
            for _ in range(2):
                response = self.client.post(self.url + '/checkin').json()
                self.assertTrue(response['ok'])
                self.assertTrue(response['results'][0]['checkin_ok'])
                self.assertFalse(response['results'][0]['travel']['ok'])
        self.assertTrue(self.ledger.checkin_done(self.entry['id'], time.strftime('%Y-%m-%d')))
        warnings = self.audit.list_records('runtime')['items']
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]['details']['outcome'], 'warning')

    def test_periodic_warning_does_not_block_balance_sync_or_repeat_each_sweep(self):
        self.travel_mock.return_value = {'ok': False, 'buddy_blocked': True, 'phase': 'buddy_tasks',
                                         'reason': 'buddy_not_eligible', 'message': '需要完成官方任务'}
        with patch.object(credits, 'daily_checkin', return_value={'ok': True}), \
             patch.object(credits, 'fetch_credits', return_value={'credits': 12, 'intl': False}):
            for _ in range(2):
                self.assertIsNotNone(converter._sync_credits(self.pool, self.ledger, self.entry, checkin=True, failed=set()))
        self.assertEqual(self.ledger.entry(self.entry['id'])['credits']['credits'], 12)
        warnings = [item for item in self.audit.list_records('runtime')['items'] if item['action'] == 'buddy.attention_required']
        self.assertEqual(len(warnings), 1)

    def test_stale_credential_retains_confirmed_result_without_overwriting_ledger(self):
        def change(*args, **kwargs):
            self.entry['cm'].invalidate()
            return {'ok': True, 'buddy_claimed': True, 'agreement_accepted': True, 'message': '已领取'}
        self.travel_mock.side_effect = change
        result = self.client.post(self.url + '/travel').json()['results'][0]
        self.assertFalse(result['ok'])
        self.assertTrue(result['buddy_claimed'])
        self.assertTrue(result['agreement_accepted'])
        self.assertTrue(result['stale'])
        self.assertNotIn('travel', self.ledger.entry(self.entry['id']))

    def test_cookie_confirmation_requires_csrf(self):
        self.client.headers['Authorization'] = ''
        login = self.client.post('/admin/session', json={'api_key': 'synthetic-management-key'},
                                 headers={'Origin': 'https://testserver'})
        self.assertEqual(login.status_code, 200, login.text)
        payload = {'confirm_buddy': True, 'agreement_revision': buddy.AGREEMENT_REVISION}
        denied = self.client.post(self.url + '/travel', json=payload, headers={'Origin': 'https://testserver'})
        self.assertEqual(denied.status_code, 403)
        self.travel_mock.assert_not_called()
        allowed = self.client.post(self.url + '/travel', json=payload, headers={
            'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
        self.assertEqual(allowed.status_code, 200)
        self.travel_mock.assert_called_once()

    def test_onboarding_model_uses_only_current_account_catalog_and_policy(self):
        entry = self.entries['cn-work']
        other = self.entries['intl-work']
        key = entry['account_key']
        def model(name, rate):
            return {'id': name, 'name': name, 'credits': rate, 'supportsToolCall': True}
        converter.CONFIG['account_catalogs'] = {
            key: {'profile': 'cn-work', 'models': [model('paid', 'x2'), model('free', 'x0'), model('unknown', None)]},
            other['account_key']: {'profile': 'intl-work', 'models': [model('borrowed', 'x0')]}}
        selector = converter._buddy_context(entry, entry['cm'].get_headers())['task_model']
        self.assertEqual(selector()['id'], 'free')
        self.assertIsNone(selector('borrowed'))
        self.assertIsNone(selector('unknown'))
        self.control.update_model('free', {'enabled': False}, self.control.snapshot()['revision'])
        self.assertEqual(selector()['id'], 'paid')
        self.control.update_model('paid', {'credential_ids': [other['account_key']]}, self.control.snapshot()['revision'])
        self.assertIsNone(selector())
        self.assertIsNone(converter._buddy_context(self.entries['cn-cli'], {})['task_model']())

    def test_onboarding_model_rechecks_disable_and_cooldown_without_refreshing_credentials(self):
        entry = self.entries['cn-work']
        converter.CONFIG['account_catalogs'] = {entry['account_key']: {'profile': 'cn-work', 'models': [
            {'id': 'free', 'credits': 'x0', 'supportsToolCall': True}]}}
        selector = converter._buddy_context(entry, entry['cm'].get_headers())['task_model']
        self.assertIsNotNone(selector())
        self.pool._model_fail[(entry['id'], 'free')] = time.time() + 3600
        self.assertIsNone(selector('free'))
        self.pool._model_fail.clear()
        self.control.set_credential(entry['account_key'], False)
        self.assertIsNone(selector())


    def test_confirmation_requires_authentication(self):
        response = self.client.post(self.url + '/travel', headers={'Authorization': ''}, json={
            'confirm_buddy': True, 'agreement_revision': buddy.AGREEMENT_REVISION})
        self.assertEqual(response.status_code, 401)
        self.travel_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
