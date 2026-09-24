"""Broker-image auth library gate. google-auth is a test install, not a fleet dependency."""
import builtins
import io
import json
import os
import re
from pathlib import Path
import subprocess
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

    def test_wrong_audience_fails_with_fixed_code(self):
        with patch.object(broker_auth_check, 'AUDIENCE', 'https://other.invalid/broker'):
            self._assert_fixed(broker_auth_check.check)

    def test_wrong_issuer_fails_with_fixed_code(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from google.auth import jwt
        from google.auth.crypt import RSASigner
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = _self_signed_certificate(key)
        signer = RSASigner.from_string(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode(), key_id='k')
        token = jwt.encode(signer, {
            'aud': broker_auth_check.AUDIENCE, 'iss': 'https://attacker.invalid',
            'iat': broker_auth_check.ISSUED, 'exp': broker_auth_check.EXPIRES,
            'probe': broker_auth_check.CLAIM_VALUE}).decode()
        del key, signer
        with patch.object(broker_auth_check, 'CERT_PEM', certificate), patch.object(
                broker_auth_check, 'TOKEN', token):
            self._assert_fixed(broker_auth_check.check)

    def test_certificate_for_another_key_fails_with_fixed_code(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = _self_signed_certificate(other)
        del other
        with patch.object(broker_auth_check, 'CERT_PEM', certificate):
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

    def test_dunder_main_exits_nonzero_without_constructing_broker_server(self):
        main_source = Path(dynamic_broker.__file__).read_text(encoding='utf-8').split('def main():', 1)[1]
        main_source = main_source.split("if __name__ == '__main__':", 1)[0]
        self.assertLess(main_source.index('broker_auth_check.check()'), main_source.index('BrokerServer('))
        proc = _run_module_as_main(dynamic_broker.__file__)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('live_broker_startup_rejected', proc.stdout)
        self.assertNotIn('authentication_required', proc.stdout)

    def test_serve_broker_role_runs_dynamic_broker_main_and_exits_nonzero(self):
        self.assertEqual(serve.ROLES['broker'][0], 'dynamic_broker')
        proc = _run_module_as_main(serve.__file__, broker_role=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('"status": "stopped"', proc.stdout)
        self.assertIn('broker_auth_library_unavailable', proc.stdout)
        self.assertNotIn('authentication_required', proc.stdout)

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
        self.assertIn('BEGIN CERTIFICATE', source)

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


def _self_signed_certificate(key):
    from datetime import datetime, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'other-key')])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2023, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=timezone.utc))
        .sign(key, hashes.SHA256()))
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def _from_line(text):
    return next(line for line in text.splitlines() if line.startswith('FROM '))


def _run_module_as_main(path, *, broker_role=False):
    """Execute path as __main__ with check() failing and BrokerServer patched.

    Exit 1 means the module guard turned the failure into a non-zero status
    and never constructed BrokerServer. 0 means the guard swallowed it. 2 means
    the server was constructed. 3 means check() never ran.
    """
    script = r'''
import os, sys
from unittest.mock import patch
import runpy
runtime, hub, target = sys.argv[1:]
sys.path[:0] = [runtime, hub]
sys.argv = ["serve.py"]
if os.environ.get("BROKER_ROLE") == "1":
    os.environ["RUNCREW_ROLE"] = "broker"
    from pathlib import Path
    real_is_file = Path.is_file
    def is_file(self):
        if self.as_posix() == "/run/config/live-broker.json":
            return True
        return real_is_file(self)
    Path.is_file = is_file
import broker_auth_check
from agent_hub import credential_broker_service as base
with patch.object(broker_auth_check, "check", side_effect=broker_auth_check.BrokerAuthLibraryUnavailable()) as check, \
     patch.object(base, "BrokerServer") as server:
    try:
        runpy.run_path(target, run_name="__main__")
    except SystemExit as error:
        code = error.code
    except Exception:
        code = 1
    else:
        code = 0
    if server.called:
        raise SystemExit(2)
    if not check.called:
        raise SystemExit(3)
    if code in (0, None):
        raise SystemExit(0)
    raise SystemExit(code if isinstance(code, int) else 1)
'''
    env = os.environ.copy()
    if broker_role:
        env['BROKER_ROLE'] = '1'
    else:
        env.pop('RUNCREW_ROLE', None)
        env.pop('BROKER_ROLE', None)
    return subprocess.run(
        [sys.executable, '-c', script, str(ROOT / 'live-worker-runtime'), str(ROOT / 'agent-hub'), path],
        capture_output=True, text=True, env=env, check=False)


if __name__ == '__main__':
    unittest.main()
