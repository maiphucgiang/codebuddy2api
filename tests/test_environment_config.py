"""Verify native and Compose environment settings without credentials or upstream requests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import converter
from app import runtime_management
from app.control_store import ControlStore
from app.settings import resolve_settings

ROOT = Path(__file__).resolve().parents[1]


class EnvironmentConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.auth = self.root / 'auth'

    def start(self, environ=None, cli=(), saved=None):
        if saved:
            with contextlib.closing(ControlStore(self.auth / 'control.sqlite3')) as store:
                store.update_settings(saved, store.snapshot()['revision'])
        env = {'HOME': str(self.root), 'CODEBUDDY_AUTH_DIR': str(self.auth), 'CODEBUDDY2API_KEY': 'synthetic-key'}
        env.update(environ or {})
        with patch.dict(os.environ, env, clear=True), patch.dict(converter.CONFIG, dict(converter.CONFIG), clear=True), \
             patch.object(sys, 'argv', ['converter.py', '--skip-check', *cli]), \
             patch.object(converter, 'load_startup_env', return_value=set()), \
             patch.object(converter, 'seed_credentials') as seed, patch.object(converter, 'CredentialPool') as pool, \
             patch.object(converter, '_publish_model_cache'), patch.object(runtime_management, 'install'), \
             patch.object(converter.threading, 'Thread'), patch.object(converter, '_log'), \
             patch.object(converter, 'run_server') as server, contextlib.redirect_stderr(io.StringIO()):
            try:
                converter.main()
                snapshot = {item['key']: item for item in resolve_settings(converter.CONFIG)}
                return dict(server.call_args.kwargs), snapshot, dict(converter.CONFIG)
            except (ValueError, SystemExit):
                server.assert_not_called()
                seed.assert_not_called()
                pool.assert_not_called()
                raise
            finally:
                runtime_management.close(converter.CONFIG)

    def test_environment_bind_and_port_override_saved_values_and_are_locked(self):
        server, items, _ = self.start({'CODEBUDDY2API_BIND': '127.0.0.2', 'CODEBUDDY2API_PORT': '9081'},
                                      saved={'host': '127.0.0.3', 'port': 9082})
        self.assertEqual((server['host'], server['port']), ('127.0.0.2', 9081))
        for name in ('host', 'port'):
            self.assertEqual(items[name]['source'], 'environment')
            self.assertTrue(items[name]['locked'])
            self.assertEqual(items[name]['mode'], 'restart')

    def test_cli_binding_wins_even_over_invalid_lower_priority_environment(self):
        server, items, _ = self.start({'CODEBUDDY2API_BIND': '', 'CODEBUDDY2API_PORT': 'invalid'},
                                      cli=('--host=127.0.0.4', '--port', '9084'))
        self.assertEqual((server['host'], server['port']), ('127.0.0.4', 9084))
        self.assertTrue(all(items[name]['source'] == 'cli' and items[name]['locked'] for name in ('host', 'port')))

    def test_accepted_cli_abbreviations_keep_cli_precedence(self):
        server, items, _ = self.start({'CODEBUDDY2API_BIND': '127.0.0.2', 'CODEBUDDY2API_PORT': '9081'},
                                      cli=('--ho', '127.0.0.4', '--po=9084'))
        self.assertEqual((server['host'], server['port']), ('127.0.0.4', 9084))
        self.assertTrue(all(items[name]['source'] == 'cli' for name in ('host', 'port')))


    def test_saved_binding_and_default_loopback_still_work_without_environment(self):
        server, items, _ = self.start()
        self.assertEqual((server['host'], server['port']), ('127.0.0.1', 8787))
        self.assertFalse(items['host']['locked'])
        server, items, _ = self.start(saved={'host': '127.0.0.5', 'port': 9085})
        self.assertEqual((server['host'], server['port']), ('127.0.0.5', 9085))
        self.assertEqual(items['port']['source'], 'management')

    def test_invalid_binding_environment_fails_before_server_or_credential_scan(self):
        for env in ({'CODEBUDDY2API_BIND': ''}, {'CODEBUDDY2API_BIND': 'x\ny'},
                    {'CODEBUDDY2API_PORT': '0'}, {'CODEBUDDY2API_PORT': '65536'}, {'CODEBUDDY2API_PORT': 'bad'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                self.start(env)

    def test_environment_public_binding_without_key_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.start({'CODEBUDDY2API_BIND': '0.0.0.0', 'CODEBUDDY2API_KEY': ''})

    def test_existing_runtime_limit_and_retry_variables_reach_effective_config(self):
        values = {'MAX_INBOUND_BYTES': '4096', 'MAX_COLLECT_BYTES': '0', 'MAX_CONCURRENT': '2',
                  'TOOL_CALL_MAX_RETRY': '1', 'FAILOVER_MAX': '1', 'RETRY_WRITE_TIMEOUT': 'true',
                  'UPSTREAM_KEEPALIVE': 'true', 'MAX_INFLIGHT_PER_ACCOUNT': '2'}
        _, _, config = self.start({'CODEBUDDY2API_' + name: value for name, value in values.items()})
        for name, value in values.items():
            expected = value == 'true' if value in ('true', 'false') else int(value)
            self.assertEqual(config[name.lower()], expected)

    def test_pooling_cli_wins_over_environment_and_saved_settings(self):
        _, items, config = self.start(
            {'CODEBUDDY2API_UPSTREAM_KEEPALIVE': 'true', 'CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT': '3'},
            cli=('--upstream-keepalive=false', '--max-inflight-per-account', '1'),
            saved={'upstream_keepalive': True, 'max_inflight_per_account': 2})
        self.assertFalse(config['upstream_keepalive'])
        self.assertEqual(config['max_inflight_per_account'], 1)
        self.assertTrue(all(items[key]['source'] == 'cli' and items[key]['locked']
                            for key in ('upstream_keepalive', 'max_inflight_per_account')))

    def test_invalid_pooling_environment_fails_before_startup(self):
        for env in ({'CODEBUDDY2API_UPSTREAM_KEEPALIVE': 'invalid'},
                    {'CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT': '-1'}):
            with self.subTest(env=env), self.assertRaises(SystemExit):
                self.start(env)

    def test_rollout_switches_default_to_conservative_modes(self):
        _, items, config = self.start()
        self.assertEqual(config['routing_scheduler'], 'shadow')
        self.assertEqual(config['catalog_probe_mode'], 'account')
        self.assertEqual(items['routing_scheduler']['source'], 'default')
        self.assertEqual(items['catalog_probe_mode']['source'], 'default')
        _, _, explicit = self.start(cli=('--routing-scheduler', 'indexed',
                                         '--catalog-probe-mode', 'representative'))
        self.assertEqual(explicit['routing_scheduler'], 'indexed')
        self.assertEqual(explicit['catalog_probe_mode'], 'representative')


    def test_capability_guard_defaults_precedence_and_validation(self):
        _, items, config = self.start()
        self.assertTrue(config['model_capability_guard'])
        self.assertFalse(items['model_capability_guard']['locked'])
        _, items, config = self.start(saved={'model_capability_guard': False})
        self.assertFalse(config['model_capability_guard'])
        self.assertEqual(items['model_capability_guard']['source'], 'management')
        _, items, config = self.start({'CODEBUDDY2API_MODEL_CAPABILITY_GUARD': 'true'})
        self.assertTrue(config['model_capability_guard'])
        self.assertTrue(items['model_capability_guard']['locked'])
        _, items, config = self.start({'CODEBUDDY2API_MODEL_CAPABILITY_GUARD': 'invalid'},
                                      cli=('--model-capability-guard=false',))
        self.assertFalse(config['model_capability_guard'])
        self.assertEqual(items['model_capability_guard']['source'], 'cli')
        with self.assertRaises(SystemExit):
            self.start({'CODEBUDDY2API_MODEL_CAPABILITY_GUARD': 'invalid'})

    def test_stream_mode_precedence_validation_and_hot_schema(self):
        _, items, config = self.start(saved={'stream_mode': 'realtime'})
        self.assertEqual(config['stream_mode'], 'realtime')
        self.assertEqual(items['stream_mode']['source'], 'management')
        self.assertEqual(items['stream_mode']['choices'], ['compatible', 'realtime'])
        self.assertFalse(items['stream_mode']['locked'])
        _, items, config = self.start({'CODEBUDDY2API_STREAM_MODE': 'compatible'})
        self.assertEqual(config['stream_mode'], 'compatible')
        self.assertEqual(items['stream_mode']['source'], 'environment')
        self.assertTrue(items['stream_mode']['locked'])
        _, items, config = self.start({'CODEBUDDY2API_STREAM_MODE': 'invalid'},
                                      cli=('--stream-mode=realtime',), saved={'stream_mode': 'compatible'})
        self.assertEqual(config['stream_mode'], 'realtime')
        self.assertEqual(items['stream_mode']['source'], 'cli')
        with self.assertRaises(ValueError):
            self.start({'CODEBUDDY2API_STREAM_MODE': 'invalid'})

    def test_admin_allowed_origins_precedence_normalization_and_locking(self):
        key = 'CODEBUDDY2API_ADMIN_ORIGINS'
        self.assertEqual(self.start()[2]['admin_allowed_origins'], '')
        _, items, config = self.start(saved={'admin_allowed_origins': 'saved.example.com'})
        self.assertEqual(config['admin_allowed_origins'], 'https://saved.example.com')
        self.assertEqual(items['admin_allowed_origins']['source'], 'management')
        self.assertFalse(items['admin_allowed_origins']['locked'])
        _, items, config = self.start({key: 'env.example.com, http://10.0.0.1:8787'},
                                      saved={'admin_allowed_origins': 'saved.example.com'})
        self.assertEqual(config['admin_allowed_origins'], 'https://env.example.com,http://10.0.0.1:8787')
        self.assertEqual(items['admin_allowed_origins']['source'], 'environment')
        self.assertTrue(items['admin_allowed_origins']['locked'])
        _, items, config = self.start({key: 'env.example.com'}, cli=('--admin-allowed-origins', 'cli.example.com'),
                                      saved={'admin_allowed_origins': 'saved.example.com'})
        self.assertEqual(config['admin_allowed_origins'], 'https://cli.example.com')
        self.assertEqual(items['admin_allowed_origins']['source'], 'cli')
        with self.assertRaises(SystemExit):
            self.start({key: 'ftp://example.com'})


    def test_responses_projection_settings_default_and_environment(self):
        _, items, config = self.start()
        self.assertEqual((config['responses_projection_mode'], config['responses_projection_max_bytes']),
                         ('balanced', 40000))
        self.assertEqual(items['responses_projection_mode']['choices'], ['balanced', 'passthrough'])
        self.assertEqual((items['responses_projection_max_bytes']['min'],
                          items['responses_projection_max_bytes']['max']), (0, 33554432))
        _, items, config = self.start({
            'CODEBUDDY2API_RESPONSES_PROJECTION_MODE': 'passthrough',
            'CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES': '0'})
        self.assertEqual((config['responses_projection_mode'], config['responses_projection_max_bytes']),
                         ('passthrough', 0))
        self.assertTrue(all(items[key]['locked'] for key in (
            'responses_projection_mode', 'responses_projection_max_bytes')))
        with self.assertRaises(ValueError):
            self.start({'CODEBUDDY2API_RESPONSES_PROJECTION_MODE': 'invalid'})
        for value in ('1', '128', '255', '33554433'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.start({'CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES': value})
        for value in ('1', '128', '255', '33554433'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.start(cli=(f'--responses-projection-max-bytes={value}',))
        with self.assertRaises(SystemExit):
            self.start(cli=('--responses-projection-mode=legacy',))

    def test_responses_projection_settings_management_precedence_and_schema(self):
        _, items, config = self.start(saved={
            'responses_projection_mode': 'passthrough', 'responses_projection_max_bytes': 0})
        self.assertEqual((config['responses_projection_mode'], config['responses_projection_max_bytes']),
                         ('passthrough', 0))
        self.assertEqual(items['responses_projection_mode'], {
            'key': 'responses_projection_mode', 'value': 'passthrough', 'stored': 'passthrough',
            'source': 'management', 'mode': 'hot', 'type': 'string',
            'label': 'Responses 投影模式', 'locked': False,
            'choices': ['balanced', 'passthrough']})
        self.assertEqual(items['responses_projection_max_bytes'], {
            'key': 'responses_projection_max_bytes', 'value': 0, 'stored': 0,
            'source': 'management', 'mode': 'hot', 'type': 'integer',
            'label': 'Responses 单项字节上限（0 或 ≥256）', 'locked': False,
            'min': 0, 'max': 33554432})
        _, items, config = self.start({
            'CODEBUDDY2API_RESPONSES_PROJECTION_MODE': 'balanced',
            'CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES': '33554432'}, saved={
            'responses_projection_mode': 'passthrough', 'responses_projection_max_bytes': 0})
        self.assertEqual((config['responses_projection_mode'], config['responses_projection_max_bytes']),
                         ('balanced', 33554432))
        self.assertTrue(all(items[key]['source'] == 'environment' and items[key]['locked']
                            for key in ('responses_projection_mode', 'responses_projection_max_bytes')))

        _, items, config = self.start({
            'CODEBUDDY2API_RESPONSES_PROJECTION_MODE': 'balanced',
            'CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES': '33554432'},
            cli=('--responses-projection-mode=passthrough', '--responses-projection-max-bytes=0'),
            saved={'responses_projection_mode': 'balanced', 'responses_projection_max_bytes': 256})
        self.assertEqual((config['responses_projection_mode'], config['responses_projection_max_bytes']),
                         ('passthrough', 0))
        self.assertTrue(all(items[key]['source'] == 'cli' and items[key]['locked']
                            for key in ('responses_projection_mode', 'responses_projection_max_bytes')))
    def test_prompt_mode_precedence_and_validation(self):
        _, items, config = self.start(saved={'prompt_mode': 'prepend'})
        self.assertEqual(config['prompt_mode'], 'prepend')
        self.assertEqual(items['prompt_mode']['source'], 'management')
        self.assertEqual(items['prompt_mode']['choices'], ['empty', 'prepend'])
        _, items, config = self.start({'CODEBUDDY2API_PROMPT_MODE': 'prepend'})
        self.assertEqual(config['prompt_mode'], 'prepend')
        self.assertTrue(items['prompt_mode']['locked'])
        _, items, config = self.start({'CODEBUDDY2API_PROMPT_MODE': 'invalid'},
                                      cli=('--prompt-mode=prepend',), saved={'prompt_mode': 'empty'})
        self.assertEqual(config['prompt_mode'], 'prepend')
        self.assertEqual(items['prompt_mode']['source'], 'cli')
        with self.assertRaises(ValueError):
            self.start({'CODEBUDDY2API_PROMPT_MODE': 'invalid'})
        _, _, config = self.start()
        self.assertEqual(config['prompt_mode'], 'empty')

    def test_request_context_mode_precedence_and_validation(self):
        _, items, config = self.start(saved={'request_context_mode': 'scoped'})
        self.assertEqual(config['request_context_mode'], 'scoped')
        self.assertEqual(items['request_context_mode']['source'], 'management')
        _, items, config = self.start({'CODEBUDDY2API_REQUEST_CONTEXT_MODE': 'scoped'})
        self.assertEqual(config['request_context_mode'], 'scoped')
        self.assertTrue(items['request_context_mode']['locked'])
        _, items, config = self.start({'CODEBUDDY2API_REQUEST_CONTEXT_MODE': 'invalid'},
                                      cli=('--request-context-mode=legacy',), saved={'request_context_mode': 'scoped'})
        self.assertEqual(config['request_context_mode'], 'legacy')
        self.assertEqual(items['request_context_mode']['source'], 'cli')
        with self.assertRaises(ValueError):
            self.start({'CODEBUDDY2API_REQUEST_CONTEXT_MODE': 'invalid'})


    def test_language_is_never_locked_and_the_notice_flag_stays_hidden(self):
        from app.settings import SCHEMA, resolve_settings
        config = {"settings_sources": {key: "dotenv" for key in SCHEMA}}
        config.update({key: spec["default"] for key, spec in SCHEMA.items()})
        items = {item["key"]: item for item in resolve_settings(config)}
        self.assertFalse(items["language"]["locked"])
        self.assertEqual(items["language"]["value"], "auto")
        self.assertNotIn("config_notice_muted", items)
        self.assertTrue(items["host"]["locked"])

    def test_example_covers_all_active_runtime_environment_names(self):
        example = (ROOT / '.env.example').read_text()
        documented = set(re.findall(r'(?m)^(?:# )?(CODEBUDDY[A-Z0-9_]+)=', example))
        referenced = set()
        for path in [ROOT / 'converter.py', *sorted((ROOT / 'app').rglob('*.py'))]:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch('CODEBUDDY[A-Z0-9_]+', node.value):
                    referenced.add(node.value)
        referenced.discard('CODEBUDDY2API_AUTO_TRIAL')
        self.assertLessEqual(referenced, documented)
        self.assertNotIn('CODEBUDDY2API_AUTO_TRIAL', documented)

    def compose(self, values):
        if shutil.which('docker') is None:
            self.skipTest('Docker CLI unavailable')
        env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(self.root)}
        version = subprocess.run(['docker', 'compose', 'version'], env=env, capture_output=True, text=True, timeout=15)
        if version.returncode:
            self.skipTest('Compose plugin unavailable')
        dotenv = self.root / 'compose.env'
        dotenv.write_text(''.join(name + '=' + value + '\n' for name, value in values.items()))
        # The service reads .env through env_file; point it at this batch instead of the checkout.
        env['CODEBUDDY2API_ENV_FILE'] = str(dotenv)
        command = ['docker', 'compose', '--project-directory', str(self.root), '--env-file', str(dotenv),
                   '-f', str(ROOT / 'docker-compose.yml'), 'config', '--format', 'json']
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['services']['codebuddy2api']

    def test_compose_forwards_dotenv_limits_and_retries_without_changing_internal_binding(self):
        values = {'CODEBUDDY2API_BIND': '127.0.0.2', 'CODEBUDDY2API_PORT': '9087',
                  'CODEBUDDY2API_MAX_INBOUND_BYTES': '4096', 'CODEBUDDY2API_MAX_COLLECT_BYTES': '0',
                  'CODEBUDDY2API_MAX_CONCURRENT': '2', 'CODEBUDDY2API_TOOL_CALL_MAX_RETRY': '1',
                  'CODEBUDDY2API_FAILOVER_MAX': '1', 'CODEBUDDY2API_RETRY_WRITE_TIMEOUT': 'true',
                  'CODEBUDDY2API_UPSTREAM_KEEPALIVE': 'true', 'CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT': '2',
                  'CODEBUDDY2API_REQUEST_CONTEXT_MODE': 'scoped',
                  'CODEBUDDY2API_RESPONSES_PROJECTION_MODE': 'passthrough',
                  'CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES': '0',
                  'CODEBUDDY2API_STREAM_MODE': 'realtime',
                  'CODEBUDDY2API_MODEL_CAPABILITY_GUARD': 'false',
                  'CODEBUDDY2API_ADMIN_ORIGINS': 'https://chat.example.com',
                  'CODEBUDDY2API_KEEP_TOOL_METADATA': 'false', 'CODEBUDDY_IMPORT_DIR': '/data/auth/incoming'}
        service = self.compose(values)
        port = service['ports'][0]
        self.assertEqual((port['host_ip'], str(port['published']), port['target']), ('127.0.0.2', '9087', 8787))
        # Compose inherits the image command instead of repeating the listener flags.
        self.assertIsNone(service.get('command'))
        self.assertEqual(service['environment']['CODEBUDDY_AUTH_DIR'], '/data/auth')
        for name, value in values.items():
            if name not in ('CODEBUDDY2API_BIND', 'CODEBUDDY2API_PORT'):
                self.assertEqual(service['environment'][name], value)

    def test_compose_unset_optional_settings_do_not_override_webui(self):
        service = self.compose({})
        for name in ('CODEBUDDY2API_KEEP_TOOL_METADATA', 'CODEBUDDY2API_FAILOVER_MAX', 'CODEBUDDY2API_RETRY_WRITE_TIMEOUT',
                     'CODEBUDDY2API_UPSTREAM_KEEPALIVE', 'CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT',
                     'CODEBUDDY2API_REQUEST_CONTEXT_MODE',
                     'CODEBUDDY2API_RESPONSES_PROJECTION_MODE',
                     'CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES', 'CODEBUDDY2API_STREAM_MODE',
                     'CODEBUDDY2API_MODEL_CAPABILITY_GUARD',
                     'CODEBUDDY2API_ADMIN_ORIGINS'):
            self.assertIsNone(service['environment'].get(name))
        self.assertEqual(service['ports'][0]['host_ip'], '127.0.0.1')
        # Container defaults to the data directory's imports/; .env may override it.
        self.assertIsNone(service['environment'].get('CODEBUDDY_IMPORT_DIR'))


if __name__ == '__main__':
    unittest.main()
