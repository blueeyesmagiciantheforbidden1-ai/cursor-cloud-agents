"""Broker-image auth library gate. google-auth is a test install, not a fleet dependency."""
import builtins
import io
import json
import re
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'live-worker-runtime'))
sys.path.insert(0, str(ROOT / 'agent-hub'))

import broker_auth_check
import dynamic_broker
import serve

PINS = (
    ('google-auth', '2.58.0', 2),
    ('cryptography', '50.0.1', 46),
    ('cffi', '2.1.1', 100),
    ('pyasn1', '0.6.4', 2),
    ('pyasn1-modules', '0.4.2', 2),
    ('pycparser', '3.0', 2),
)
HASH = re.compile(r'^sha256:[0-9a-f]{64}$')
CONTROLLER = ROOT / 'live-image-controller'


def _block_google(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'google' or name.startswith('google.'):
        raise ImportError('blocked')
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


_REAL_IMPORT = builtins.__import__


class BrokerAuthCheckTests(unittest.TestCase):
    def test_check_passes_with_google_auth_installed(self):
        broker_auth_check.check()

    def test_tampered_token_fails_with_fixed_code(self):
        signature = broker_auth_check.TOKEN.rsplit('.', 1)[1]
        flipped = ('A' if signature[:1] != 'A' else 'B') + signature[1:]
        tampered = broker_auth_check.TOKEN.rsplit('.', 1)[0] + '.' + flipped
        with patch.object(broker_auth_check, 'TOKEN', tampered):
            self._assert_fixed(broker_auth_check.check)

    def test_wrong_key_fails_with_fixed_code(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = other.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        del other
        with patch.object(broker_auth_check, 'PUBLIC_KEY_PEM', public):
            self._assert_fixed(broker_auth_check.check)

    def test_blocked_google_import_fails_with_fixed_code(self):
        with patch('builtins.__import__', _block_google):
            self._assert_fixed(broker_auth_check.check)

    def test_dynamic_broker_main_refuses_to_serve_when_check_fails(self):
        with patch.object(broker_auth_check, 'check', side_effect=broker_auth_check.BrokerAuthLibraryUnavailable()) as check:
            with patch.object(dynamic_broker.base, 'BrokerServer') as server:
                self._assert_fixed(dynamic_broker.main)
        check.assert_called_once()
        server.assert_not_called()

    def test_plain_check_stays_green_without_google(self):
        buffer = io.StringIO()
        with patch('builtins.__import__', _block_google), patch.object(sys, 'argv', ['serve.py', '--check']):
            with redirect_stdout(buffer):
                self.assertEqual(serve.main(), 0)
        self.assertEqual(json.loads(buffer.getvalue()), {
            'status': 'image_ok', 'roles': ['broker', 'fleet'], 'credentials_included': False})

    def test_check_broker_prints_ok(self):
        buffer = io.StringIO()
        with patch.object(sys, 'argv', ['serve.py', '--check-broker']), redirect_stdout(buffer):
            self.assertEqual(serve.main(), 0)
        self.assertEqual(json.loads(buffer.getvalue()), {'status': 'broker_auth_ok'})

    def test_requirements_names_exactly_the_six_packages_with_hashes(self):
        text = (CONTROLLER / 'requirements-broker.txt').read_text(encoding='utf-8')
        self.assertIn('runcrew-live-broker', text)
        self.assertIn('standard-library only', text)
        names, hashes, current = [], {}, None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.endswith('\\'):
                line = line[:-1].strip()
            if line.startswith('--hash='):
                digest = line.split('=', 1)[1]
                self.assertRegex(digest, HASH)
                hashes.setdefault(current, []).append(digest)
                continue
            name, version = line.split('==')
            names.append((name, version))
            current = name
        self.assertEqual([(name, version) for name, version, _count in PINS], names)
        self.assertEqual([name for name, _version, _count in PINS], list(hashes))
        for name, _version, count in PINS:
            self.assertEqual(len(hashes[name]), count)
            self.assertEqual(len(set(hashes[name])), count)

    def test_dockerfile_broker_requires_hashes_and_check_broker(self):
        broker = (CONTROLLER / 'Dockerfile.broker').read_text(encoding='utf-8')
        fleet = (CONTROLLER / 'Dockerfile').read_text(encoding='utf-8')
        install = 'RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements-broker.txt'
        probe = 'RUN python -I -B /opt/runcrew/app/serve.py --check-broker'
        self.assertIn(install, broker)
        self.assertLess(broker.index(install), broker.index('USER 10001:10001'))
        self.assertIn('COPY live-image-controller/requirements-broker.txt /tmp/requirements-broker.txt', broker)
        self.assertIn('rm -f /tmp/requirements-broker.txt', broker)
        self.assertLess(broker.index('USER 10001:10001'), broker.index(probe))
        self.assertIn('broker_auth_check.py', broker)
        self.assertIn('broker_auth_check.py', fleet)
        self.assertEqual(_from_line(broker), _from_line(fleet))
        build = json.loads((CONTROLLER / 'cloudbuild.broker.json').read_text(encoding='utf-8'))
        fleet_build = json.loads((CONTROLLER / 'cloudbuild.json').read_text(encoding='utf-8'))
        self.assertEqual(build['serviceAccount'], fleet_build['serviceAccount'])
        args = build['steps'][0]['args']
        self.assertIn('live-image-controller/Dockerfile.broker', args)
        self.assertIn('controller-worker:live-20260924a-broker', build['images'][0])
        self.assertTrue(any(arg.endswith('controller-worker:live-20260924a-broker') for arg in args))

    def test_fleet_dockerfile_stays_pip_free(self):
        fleet = (CONTROLLER / 'Dockerfile').read_text(encoding='utf-8')
        self.assertNotRegex(fleet, r'(?m)^\s*(?:RUN|COPY).*pip')
        self.assertNotIn('requirements-broker', fleet)
        self.assertNotIn('--require-hashes', fleet)
        self.assertNotIn('--check-broker', fleet)
        self.assertIn('serve.py --check\n', fleet)
        source = (ROOT / 'live-worker-runtime' / 'broker_auth_check.py').read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE KEY', source)
        self.assertIn('BEGIN PUBLIC KEY', source)

    def _assert_fixed(self, call):
        with self.assertRaises(broker_auth_check.BrokerAuthLibraryUnavailable) as caught:
            call()
        error = caught.exception
        self.assertEqual(str(error), 'broker_auth_library_unavailable')
        self.assertEqual(error.args, ('broker_auth_library_unavailable',))
        self.assertIsNone(error.__cause__)
        self.assertNotIn('blocked', str(error))
        self.assertNotIn('google', str(error).lower())
        self.assertNotIn('Malformed', str(error))
        self.assertNotIn('Token', str(error))


def _from_line(text):
    return next(line for line in text.splitlines() if line.startswith('FROM '))


if __name__ == '__main__':
    unittest.main()
