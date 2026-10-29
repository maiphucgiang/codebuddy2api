"""Manual-only trial operations, safe feedback and retired-setting compatibility."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import threading
import unittest
from unittest.mock import patch

import httpx
import converter
from app import trial_rewards, trial_management, settings
from app.control_store import ControlStore
from tests import test_credential_actions as fixtures
from tests.admin_session import credentials
from tests.test_trial_rewards import headers, mock_http


class TrialIntegrationTests(unittest.TestCase):
    add_account = fixtures.CredentialActionTests.add_account
    configure = fixtures.CredentialActionTests.configure
    handle_upstream = fixtures.CredentialActionTests.handle_upstream
    post = fixtures.CredentialActionTests.post

    def setUp(self):
        fixtures.CredentialActionTests.setUp(self)
        self.entry = self.entries['intl-work']
        self.url = '/admin/credentials/' + self.entry['account_key']
        self.trials = trial_rewards.TrialLedger(self.root / 'trial-ledger.json')
        converter.CONFIG['trial_ledger'] = self.trials
        self.claim = self.enterContext(patch.object(trial_rewards, 'claim_trial',
            return_value={'ok': True, 'already': False, 'code': 0, 'status': 200}))

    def status(self):
        response = self.client.get('/admin/credentials')
        self.assertEqual(response.status_code, 200)
        return next(r for r in response.json()['credentials'] if r['id'] == self.entry['account_key'])['trial']

    def test_inventory_is_read_only_and_support_is_product_specific(self):
        response = self.client.get('/admin/credentials')
        self.assertEqual(response.status_code, 200)
        for row in response.json()['credentials']:
            self.assertEqual(row['trial_supported'], row['profile'] == 'intl-work')
        self.assertTrue(self.status()['can_claim'])
        self.assertFalse(self.trials.path.exists())
        self.claim.assert_not_called()

    def test_manual_claim_is_scoped_idempotent_and_does_not_sync(self):
        with patch.object(converter, '_sync_credits') as sync:
            first = self.post('trial')
            self.assertTrue(first['ok'])
            self.assertEqual(first['state'], 'claimed')
            self.assertFalse(first['can_claim'])
            self.assertTrue(self.post('trial')['skipped'])
            sync.assert_not_called()
        self.claim.assert_called_once()
        self.travel_mock.assert_not_called()
        self.status_query.assert_not_called()
        self.assertEqual(set(self.trials.snapshot()), {self.entry['account_key']})
        self.assertTrue(self.status()['ok'])

    def test_wrong_products_never_refresh_or_send(self):
        for profile in ('cn-cli', 'cn-work', 'intl-cli'):
            self.entry = self.entries[profile]
            self.url = '/admin/credentials/' + self.entry['account_key']
            with patch.object(self.entry['cm'], 'get_headers') as token:
                self.assertEqual(self.post('trial')['state'], 'not_applicable')
                token.assert_not_called()
        self.claim.assert_not_called()

    def test_disabled_and_missing_accounts_cannot_claim(self):
        self.control.set_credential(self.entry['account_key'], False)
        self.assertTrue(self.post('trial')['skipped'])
        self.assertEqual(self.client.post('/admin/credentials/missing/trial').status_code, 404)
        self.claim.assert_not_called()

    def test_old_auto_flag_does_not_trigger_claims_during_sync(self):
        converter.CONFIG['auto_trial'] = True
        with patch.object(converter.credits_mod, 'fetch_credits', return_value={'credits': 12, 'intl': True}):
            failed = set()
            converter._sync_credits(self.pool, self.ledger, self.entry, checkin=False, failed=failed)
            self.assertFalse(failed)
        self.claim.assert_not_called()
        self.assertFalse(self.trials.path.exists())

    def test_duplicate_files_still_produce_one_manual_result(self):
        with patch.object(self.pool, 'entries', return_value=[self.entry, self.entry]):
            response = self.client.post(self.url + '/trial')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 1)
        self.claim.assert_called_once()

    def test_legacy_startup_options_warn_without_background_claims(self):
        source = '''
import converter, json, sys
from unittest.mock import patch
sys.argv = ['converter.py', '--skip-check', '--auto-trial', 'true']
with patch.object(converter.threading.Thread, 'start'), patch.object(converter, 'seed_credentials'), \\
     patch.object(converter, 'run_server'), patch.object(converter.trial_rewards, 'claim_trial') as claim:
    converter.main()
    assert not converter.CONFIG.get('auto_trial')
    assert converter.CONFIG.get('trial_ledger') is not None
    claim.assert_not_called()
print(json.dumps({'manual_only': True}))
'''
        env = {'PATH': os.defpath, 'PYTHONPATH': str(Path(converter.__file__).parent),
               'HOME': str(self.root), 'CODEBUDDY_AUTH_DIR': str(self.root / 'startup-auth'),
               'CODEBUDDY2API_KEY': 'synthetic', 'CODEBUDDY2API_AUTO_TRIAL': 'true'}
        process = subprocess.run([sys.executable, '-B', '-c', source], cwd=self.root,
                                 env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('AUTO_TRIAL / --auto-trial 已停用', process.stderr)
        self.assertTrue(json.loads(process.stdout)['manual_only'])
        self.assertFalse((self.root / 'startup-auth/trial-ledger.json').exists())


    def test_timeout_is_safe_visible_and_does_not_repeat_after_refresh(self):
        self.claim.return_value = {'ok': False, 'already': False, 'code': None, 'status': None, 'error': 'timeout'}
        first = self.post('trial')
        self.assertEqual(first['state'], 'timeout')
        self.assertIn('超时', first['message'])
        self.assertFalse(first['can_claim'])
        self.assertGreater(first['retry_at'], first['attempted_at'])
        converter.CONFIG['trial_ledger'] = trial_rewards.TrialLedger(self.trials.path)
        self.assertFalse(self.status()['can_claim'])
        self.assertTrue(self.post('trial')['skipped'])
        self.claim.assert_called_once()
        self.assertNotIn('error', next(iter(self.trials.snapshot().values())))

    def test_backoff_expiry_enables_only_a_manual_attempt_and_success_is_permanent(self):
        key = self.entry['account_key']
        self.trials.begin(key, now=100)
        self.trials.finish(key, {'ok': False}, now=101)
        record = self.trials.snapshot()[key]
        self.assertFalse(trial_management.view(record, now=100 + 86400 - 1)['can_claim'])
        self.assertTrue(trial_management.view(record, now=100 + 86400)['can_claim'])
        self.claim.assert_not_called()
        self.trials.finish(key, {'ok': True, 'already': False, 'status': 200, 'code': 0}, now=102)
        self.assertFalse(trial_management.view(self.trials.snapshot()[key], now=1e10)['can_claim'])

    def test_oversized_legacy_timestamp_is_a_visible_storage_error(self):
        key = self.entry['account_key']
        self.trials.begin(key)
        document = json.loads(self.trials.path.read_text())
        document['accounts'][key]['attempted_at'] = 10**500
        self.trials.path.write_text(json.dumps(document))
        self.assertEqual(self.status()['state'], 'storage_error')
        self.assertEqual(self.post('trial')['state'], 'storage_error')
        self.claim.assert_not_called()


    def test_official_denial_and_already_are_distinct(self):
        self.claim.return_value = {'ok': False, 'already': False, 'code': 9876, 'status': 403}
        result = self.post('trial')
        self.assertEqual(result['state'], 'auth_error')
        self.assertEqual(result['status'], 403)
        self.assertEqual(result['code'], 9876)
        event = self.audit.list_records(kind='admin')['items'][0]['details']
        self.assertEqual(event['stage'], 'auth_error')
        self.assertEqual(event['status_code'], 403)
        self.assertEqual(event['code'], '9876')
        self.assertEqual(event['outcome'], 'error')
        self.assertGreaterEqual(event['duration_ms'], 0)
        self.assertFalse(result['ok'])
        converter.CONFIG['trial_ledger'] = trial_rewards.TrialLedger(self.root / 'already.json')
        self.claim.return_value = {'ok': False, 'already': True, 'code': 14051, 'status': 409}
        result = self.post('trial')
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'already')

    def test_persist_before_send_and_partial_success_on_finish_failure(self):
        with patch.object(self.trials, 'begin', side_effect=OSError('synthetic-secret')):
            result = self.post('trial')
            self.assertEqual(result['state'], 'storage_error')
            self.assertNotIn('synthetic-secret', json.dumps(result))
        self.claim.assert_not_called()
        with patch.object(self.trials, 'finish', side_effect=OSError('synthetic-secret')):
            result = self.post('trial')
            self.assertFalse(result['ok'])
            self.assertTrue(result['claimed'])
            self.assertEqual(result['state'], 'storage_error')
            self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertEqual(self.status()['state'], 'unconfirmed')
        self.assertFalse(self.status()['can_claim'])
        self.assertTrue(self.post('trial')['skipped'])
        self.claim.assert_called_once()

    def test_corrupt_ledger_preserved_and_does_not_break_inventory(self):
        self.trials.path.write_text('invalid synthetic contents')
        self.assertEqual(self.status()['state'], 'storage_error')
        self.assertEqual(self.post('trial')['state'], 'storage_error')
        self.assertEqual(self.trials.path.read_text(), 'invalid synthetic contents')
        self.claim.assert_not_called()

    def test_token_failure_is_not_misreported_as_storage_or_claim_failure(self):
        with patch.object(self.entry['cm'], 'get_headers', side_effect=httpx.ReadTimeout('synthetic-secret')):
            result = self.post('trial')
        self.assertEqual(result['state'], 'credential_error')
        self.assertIn('未发送', result['message'])
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertFalse(self.trials.path.exists())
        self.claim.assert_not_called()


    def test_relogin_between_reservation_and_send_stops_claim(self):
        begin = self.trials.begin
        def reserve(key):
            value = begin(key)
            self.entry['cm'].invalidate()
            return value
        with patch.object(self.trials, 'begin', side_effect=reserve):
            self.assertEqual(self.post('trial')['state'], 'changed')
        self.claim.assert_not_called()

    def test_success_stays_bound_to_original_account_if_token_changes(self):
        def finish(headers):
            self.entry['cm'].invalidate()
            return {'ok': True, 'already': False, 'code': 0, 'status': 200, 'raw': 'synthetic-secret'}
        self.claim.side_effect = finish
        result = self.post('trial')
        self.assertTrue(result['ok'])
        self.assertIn('凭证已变化', result['message'])
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertEqual(set(self.trials.snapshot()), {self.entry['account_key']})

    def test_concurrent_manual_clicks_do_not_queue_more_posts(self):
        entered, release = threading.Event(), threading.Event()
        def wait(headers):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test release missing')
            return {'ok': True, 'already': False, 'code': 0, 'status': 200}
        self.claim.side_effect = wait
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(self.client.post, self.url + '/trial')
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.client.post(self.url + '/trial').status_code, 409)
            finally:
                release.set()
            self.assertEqual(task.result(timeout=3).status_code, 200)
        self.claim.assert_called_once()

    def test_manual_post_requires_auth_and_csrf(self):
        self.client.headers.pop('Authorization', None)
        self.client.cookies.clear()
        self.assertEqual(self.client.post(self.url + '/trial').status_code, 401)
        login = self.client.post('/admin/session', headers={'Origin': 'https://testserver'},
                                 json=credentials('synthetic-management-key'))
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.post(self.url + '/trial').status_code, 403)
        self.claim.assert_not_called()
        response = self.client.post(self.url + '/trial', headers={'Origin': 'https://testserver',
            'X-CSRF-Token': login.json()['csrf_token']})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['results'][0]['ok'])

    def test_legacy_saved_boolean_loads_without_exposing_a_toggle(self):
        payload = {'settings': {'auto_trial': True}, 'models': {}, 'credentials': {}}
        self.control._db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(payload),))
        loaded = ControlStore(self.root / 'control.sqlite3')
        self.addCleanup(loaded.close)
        self.assertNotIn('auto_trial', loaded.snapshot()['settings'])
        self.assertNotIn('auto_trial', settings.SCHEMA)
        with self.assertRaises(ValueError):
            loaded.update_settings({'auto_trial': True}, loaded.snapshot()['revision'])
        with self.assertRaises(ValueError):
            settings.validate_settings({'auto_trial': 'true'}, legacy=True)


class ManualTrialTransportTests(unittest.TestCase):
    def test_response_limit_stops_reading_and_closes_connection(self):
        closed = []
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                yield b'x' * (trial_rewards.MAX_RESPONSE_BYTES + 1)
                raise AssertionError('must stop at limit')
            def close(self):
                closed.append(True)
        with mock_http(lambda r: httpx.Response(200, stream=Body())):
            result = trial_rewards.claim_trial(headers())
        self.assertEqual(result['error'], 'response_too_large')
        self.assertFalse(result['ok'])
        self.assertTrue(closed)

    def test_response_budget_does_not_accept_endless_trickle(self):
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                yield b' '
                raise AssertionError('must stop after deadline')
        with mock_http(lambda r: httpx.Response(200, stream=Body())), \
             patch.object(trial_rewards.time, 'monotonic', side_effect=[0, 31]):
            self.assertEqual(trial_rewards.claim_trial(headers())['error'], 'timeout')

    def test_compressed_or_malformed_response_is_not_read_unbounded(self):
        with mock_http(lambda r: httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=httpx.ByteStream(b'bad'))):
            self.assertEqual(trial_rewards.claim_trial(headers())['error'], 'invalid_response')


if __name__ == '__main__':
    unittest.main()
