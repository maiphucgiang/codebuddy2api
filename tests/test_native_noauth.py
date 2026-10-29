"""Exercise the shipped image's no-TTY startup contract with disposable state."""
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]


class NativeNoAuthTests(unittest.TestCase):
    def test_image_default_starts_without_key_and_creates_admin_account(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        command = json.loads(next(line[4:] for line in dockerfile.splitlines() if line.startswith('CMD ')))
        self.assertIn('ENV CODEBUDDY2API_ALLOW_OPEN_NOAUTH=true', dockerfile)
        with tempfile.TemporaryDirectory() as directory, socket.socket() as probe:
            root = Path(directory)
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
            probe.close()
            env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT') if name in os.environ}
            env.update(HOME=directory, USERPROFILE=directory, TMPDIR=directory, TEMP=directory,
                       CODEBUDDY_AUTH_DIR=str(root / 'auth'), CODEBUDDY2API_ALLOW_OPEN_NOAUTH='true',
                       PYTHONDONTWRITEBYTECODE='1')
            process = subprocess.Popen([sys.executable, '-B', str(ROOT / command[1]), *command[2:], '--port', str(port)],
                                       cwd=root, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}', transport=httpx.HTTPTransport(retries=5),
                                  timeout=5, trust_env=False) as client:
                    self.assertEqual(client.get('/health').json(), {'status': 'ok'})
                    session = client.get('/admin/session')
                    self.assertEqual(session.status_code, 200)
                    self.assertEqual(session.json(), {'authenticated': False, 'csrf_token': None})
                    self.assertEqual(client.get('/v1/models').status_code, 200)
                with contextlib.closing(sqlite3.connect(root / 'auth/control.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM gateway_secrets').fetchone()[0], 1)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT) if os.name == 'posix' else process.terminate()
                try:
                    output, error = process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
                    self.fail('Owned no-auth fixture did not shut down')
            self.assertNotIn(b'cb-', output + error)


if __name__ == '__main__':
    unittest.main()
