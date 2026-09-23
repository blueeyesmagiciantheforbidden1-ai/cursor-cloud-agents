"""Offline commissioning tests: fixed HTTP surfaces, no native or provider calls."""
import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

import heartbeat as hb
import package_status


class Response:
    def __init__(self, body, status=200, metadata=False):
        self.body, self.status, self.metadata = body, status, metadata
    def read(self, limit):
        return self.body[:limit]
    def getheader(self, name):
        return 'Google' if name == 'Metadata-Flavor' and self.metadata else None


class FakeWire:
    def __init__(self):
        self.calls = []
        self.closed = 0
        self.identity = Response(b'synthetic.google.identity', metadata=True)
        self.hub = Response(b'{"accepted":true}')
    def factory(self, tls):
        wire = self
        class Connection:
            def __init__(self, host, timeout):
                self.host, self.timeout = host, timeout
            def request(self, method, path, body=None, headers=None):
                wire.calls.append({'tls': tls, 'host': self.host, 'timeout': self.timeout,
                                   'method': method, 'path': path, 'body': body, 'headers': headers})
            def getresponse(self):
                return wire.hub if tls else wire.identity
            def close(self):
                wire.closed += 1
        return Connection


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.config = hb.worker_config({'worker_id': 'codex-cloud', 'expected_account_ref': 'a' * 64})
        self.token = 'synthetic-hub-role-credential'

    def transport(self, wire):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(hb.http.client, 'HTTPConnection', wire.factory(False)))
        stack.enter_context(patch.object(hb.http.client, 'HTTPSConnection', wire.factory(True)))
        return stack

    def test_exact_metadata_audience_and_only_fixed_report(self):
        wire = FakeWire()
        with self.transport(wire):
            hb.HeartbeatTransport().report(self.config, self.token)
        self.assertEqual(len(wire.calls), 2)
        metadata, hub = wire.calls
        self.assertEqual((metadata['tls'], metadata['host'], metadata['method']),
                         (False, 'metadata.google.internal', 'GET'))
        self.assertEqual(metadata['headers'], {'Metadata-Flavor': 'Google'})
        self.assertEqual(metadata['path'], '/computeMetadata/v1/instance/service-accounts/default/identity'
                         '?audience=https%3A%2F%2Fruncrew-hub-kdhodumsza-uc.a.run.app&format=full')
        self.assertEqual((hub['tls'], hub['host'], hub['path'], hub['method']),
                         (True, hb.HUB_HOST, '/v1/workers/report', 'POST'))
        self.assertEqual(hub['headers']['X-Hub-Agent'], 'codex')
        self.assertEqual(hub['headers']['X-Hub-Token'], self.token)
        self.assertEqual(hub['headers']['Authorization'], 'Bearer synthetic.google.identity')
        self.assertEqual(json.loads(hub['body']), {'worker_id': 'codex-cloud', 'status': 'offline',
            'auth_status': 'unknown', 'current_room_id': None, 'last_exit_code': None, 'usage': []})
        self.assertEqual(wire.closed, 2)

    def test_success_is_exact_core_worker_ack_format_and_scrubs_environment(self):
        wire = FakeWire()
        with self.transport(wire), patch.object(hb.sys, 'platform', 'linux'), \
                patch.object(hb.os, 'geteuid', return_value=10001, create=True), \
                patch.object(hb.os, 'umask'), patch.object(hb, 'read_config', return_value=self.config), \
                patch.dict(os.environ, {'HUB_AGENT_TOKEN': self.token, 'OPENAI_API_KEY': 'synthetic-key',
                                        'HTTP_PROXY': 'synthetic-proxy', 'CODEX_HOME': '/existing'}, clear=True), \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('native forbidden')), \
                patch.object(subprocess, 'run', side_effect=AssertionError('native forbidden')), \
                patch.object(os, 'system', side_effect=AssertionError('shell forbidden')):
            result = hb.run()
            self.assertEqual(set(os.environ), {'HOME', 'PATH', 'LANG', 'TMPDIR'})
        self.assertEqual(result, {'mode': 'heartbeat-only', 'telemetry_delivered': True,
            'worker_status': 'offline', 'provider_login_verified': False, 'inference_performed': False,
            'claims_or_completes_tasks': False})
        self.assertNotIn(self.token, json.dumps(result))

    def test_redirect_and_unacknowledged_report_never_retry(self):
        for response in (Response(b'synthetic-secret', status=302), Response(b'{"accepted":false}'),
                         Response(b'{"accepted":1}'), Response(b'{"accepted":true,"accepted":false}')):
            wire = FakeWire()
            wire.hub = response
            with self.transport(wire), self.assertRaises(hb.HeartbeatError) as caught:
                hb.HeartbeatTransport().report(self.config, self.token)
            self.assertEqual(len(wire.calls), 2)
            self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_metadata_failure_blocks_hub_and_never_follows_redirect(self):
        for response in (Response(b'identity', status=302), Response(b'identity'),
                         Response(b'x' * 16_385, metadata=True), Response(b'bad\nidentity', metadata=True)):
            wire = FakeWire()
            wire.identity = response
            with self.transport(wire), self.assertRaises(hb.HeartbeatError):
                hb.HeartbeatTransport().report(self.config, self.token)
            self.assertEqual(len(wire.calls), 1)

    def test_hub_response_is_bounded(self):
        wire = FakeWire()
        wire.hub = Response(b'x' * (hb.MAX_RESPONSE + 1))
        with self.transport(wire), self.assertRaisesRegex(hb.HeartbeatError, 'hub_response_limit'):
            hb.HeartbeatTransport().report(self.config, self.token)

    def test_configuration_cannot_change_project_target_provider_or_token_source(self):
        for field, value in (('project_id', 'other'), ('hub_url', 'https://attacker.example'),
                             ('agent_id', 'claude'), ('token_env', 'OPENAI_API_KEY'),
                             ('cloud_run_auth_mode', 'gcloud'), ('executable', '/bin/sh'),
                             ('unknown', 'ignored'), ('workspaces', {'default': '/'})):
            with self.subTest(field=field), self.assertRaises(hb.HeartbeatError):
                hb.worker_config({'expected_account_ref': 'a' * 64, field: value})
        for values in ({}, {'expected_account_ref': 'not-an-account-ref'},
                       {'expected_account_ref': 'a' * 64, 'model_policy_agents': ['codex', 'codex']},
                       {'expected_account_ref': 'a' * 64, 'timeout_seconds': True}):
            with self.assertRaises(hb.HeartbeatError):
                hb.worker_config(values)

    def test_root_and_nonlinux_rejected_before_configuration_or_token_reads(self):
        for platform, uid in (('linux', 0), ('linux', 1000), ('win32', 10001)):
            with patch.object(hb.sys, 'platform', platform), \
                    patch.object(hb.os, 'geteuid', return_value=uid, create=True), \
                    patch.object(hb, 'read_config', side_effect=AssertionError('must not read')), \
                    self.assertRaisesRegex(hb.HeartbeatError, 'rootless_linux_worker_required'):
                hb.run()

    def test_tokens_and_errors_are_not_printed(self):
        for value in (None, 'short', self.token + '\n', 'x' * 16_385):
            with self.assertRaises(hb.HeartbeatError):
                hb.valid_token(value)
        output = io.StringIO()
        with patch.object(hb, 'run', side_effect=OSError('synthetic-secret-value')), contextlib.redirect_stderr(output):
            self.assertEqual(hb.main(), 1)
        self.assertNotIn('synthetic-secret-value', output.getvalue())
        self.assertFalse(json.loads(output.getvalue())['telemetry_delivered'])

    def test_default_and_once_never_touch_credentials_or_network(self):
        for args, code in (([], 0), (['--once'], 2)):
            with patch.object(hb, 'main', side_effect=AssertionError('heartbeat forbidden')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(package_status.main(args), code)
            self.assertFalse(json.loads(output.getvalue())['dispatch_enabled'])

    def test_explicit_heartbeat_dispatches_only_heartbeat_module(self):
        with patch.object(hb, 'main', return_value=0) as call:
            self.assertEqual(package_status.main(['--heartbeat-only']), 0)
        call.assert_called_once_with()

if __name__ == '__main__':
    unittest.main()
