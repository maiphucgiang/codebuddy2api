"""Verify secret disclosure uses an actual terminal instead of stdout/stderr."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contextlib
import io
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app.startup import terminal_stream


class TerminalOutputTests(unittest.TestCase):
    def test_regular_file_and_non_tty_device_are_rejected_and_closed(self):
        for mode, tty in ((stat.S_IFREG, True), (stat.S_IFCHR, False)):
            with self.subTest(mode=mode, tty=tty), \
                 patch('app.startup.os.open', return_value=123), \
                 patch('app.startup.os.fstat', return_value=type('Stat', (), {'st_mode': mode})()), \
                 patch('app.startup.os.isatty', return_value=tty), \
                 patch('app.startup.os.close') as close, \
                 patch('app.startup.os.fdopen') as fdopen:
                with self.assertRaises(OSError):
                    terminal_stream()
                close.assert_called_once_with(123)
                fdopen.assert_not_called()

    def test_platform_console_path_is_fixed(self):
        for platform, device in (('nt', 'CONOUT$'), ('posix', '/dev/tty')):
            with self.subTest(platform=platform), \
                 patch('app.startup.os.name', platform), \
                 patch('app.startup.os.open', return_value=123) as opened, \
                 patch('app.startup.os.fstat', return_value=type('Stat', (), {'st_mode': stat.S_IFCHR})()), \
                 patch('app.startup.os.isatty', return_value=True), \
                 patch('app.startup.os.fdopen', return_value=io.StringIO()):
                with terminal_stream():
                    pass
                self.assertEqual(opened.call_args.args[0], device)
                self.assertFalse(opened.call_args.args[1] & os.O_CREAT)

    @unittest.skipUnless(os.name == 'posix', 'Real controlling PTY requires POSIX')
    def test_real_pty_with_redirected_standard_streams_discloses_once(self):
        import pty
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'control.sqlite3'
            for attempt in range(2):
                master, slave = pty.openpty()
                try:
                    script = '''
import fcntl, os, sys, termios
from types import SimpleNamespace
from app.control_store import ControlStore
from app.startup import resolve_startup_auth
os.setsid()
fcntl.ioctl(int(sys.argv[1]), termios.TIOCSCTTY, 0)
control = ControlStore(sys.argv[2])
try:
    config = {"settings_sources": {}, "control_store": control, "state_store": control.state, "host": "127.0.0.1"}
    resolve_startup_auth(config, SimpleNamespace(api_key=""))
finally:
    control.close()
'''
                    result = subprocess.run([sys.executable, '-B', '-c', script, str(slave), str(database)],
                                            pass_fds=(slave,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            timeout=20, check=True)
                    with contextlib.closing(sqlite3.connect(database)) as db:
                        value, announced = db.execute(
                            "SELECT value,announced FROM gateway_secrets WHERE name='admin_account'").fetchone()
                    self.assertEqual(announced, 1)
                    self.assertIn('"password_source":"generated"', value)
                    os.set_blocking(master, False)
                    try:
                        output = os.read(master, 16384)
                    except BlockingIOError:
                        output = b''
                    self.assertEqual(output.count('初始管理密码：'.encode()), 1 if attempt == 0 else 0)
                    self.assertEqual((result.stdout, result.stderr), (b'', b''))
                finally:
                    os.close(slave)
                    os.close(master)

    @unittest.skipUnless(os.name == 'posix', 'Detached process requires POSIX')
    def test_no_controlling_terminal_discloses_password_to_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'control.sqlite3'
            script = '''
import sys
from types import SimpleNamespace
from app.control_store import ControlStore
from app.startup import resolve_startup_auth
control = ControlStore(sys.argv[1])
try:
    config = {"settings_sources": {}, "control_store": control, "state_store": control.state, "host": "127.0.0.1"}
    resolve_startup_auth(config, SimpleNamespace(api_key=""))
finally:
    control.close()
'''
            result = subprocess.run([sys.executable, '-B', '-c', script, str(database)],
                                    start_new_session=True, capture_output=True, timeout=20, check=True)
            self.assertIn('管理账户：admin'.encode(), result.stdout)
            self.assertIn('初始管理密码：'.encode(), result.stdout)
            self.assertEqual(result.stderr, b'')
            with contextlib.closing(sqlite3.connect(database)) as db:
                self.assertIn('"password_source":"generated"', db.execute(
                    "SELECT value FROM gateway_secrets WHERE name='admin_account'").fetchone()[0])
                self.assertEqual(db.execute('SELECT count(*) FROM api_keys').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
