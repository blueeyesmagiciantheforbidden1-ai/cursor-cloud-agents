"""Offline Copilot metadata projection, protocol and lease-boundary tests."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
def load(name):
    spec = importlib.util.spec_from_file_location('copilot_meta_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
m, entry = load('metadata'), load('entrypoint')
AUTH = {'isAuthenticated': True, 'authType': 'user', 'login': m.EXPECTED_LOGIN, 'host': 'https://github.com'}
MODELS = {'models': [{'id': 'fixture-model', 'capabilities': {'supports': {'reasoningEffort': True}},
    'supportedReasoningEfforts': ['max', 'low', 'high'], 'defaultReasoningEffort': 'high',
    'policy': {'state': 'enabled', 'terms': 'untrusted text'},
    'billing': {'multiplier': 2, 'tokenPrices': {'inputPrice': 1, 'outputPrice': 4, 'batchSize': 1000}}}]}
QUOTA = {'quotaSnapshots': {'premium_interactions': {'isUnlimitedEntitlement': False,
    'entitlementRequests': 20000, 'usedRequests': 164, 'remainingPercentage': 99.18,
    'usageAllowedWithExhaustedQuota': False, 'overage': 0, 'overageAllowedWithExhaustedQuota': False,
    'resetDate': '2026-10-01T00:00:00Z'}}}

class MetadataTests(unittest.TestCase):
    def session(self, home, events):
        (home / '.copilot').mkdir()
        lease = SimpleNamespace(account_ref=m.ACCOUNT_REF, canonical_account_ref='b' * 64)
        broker = SimpleNamespace(assert_current=lambda lease: events.append('assert'),
                                 renew=lambda lease: events.append('renew'),
                                 quarantine=lambda lease, reason: events.append('quarantine'))
        return SimpleNamespace(home=home, state='active', lease=lease, broker=broker,
                               finish=lambda **kw: events.append(('finish', kw)) or 'version-1')

    def rpc(self, events, fail=None):
        class RPC:
            clean_shutdown = False
            def __init__(self, home, renew): pass
            def __enter__(self): events.append('start'); return self
            def __exit__(self, *_): events.append('stopped')
            def request(self, method):
                events.append(method)
                if method == fail: raise m.MetadataError('fixture_failure')
                self.clean_shutdown = method == 'runtime.shutdown'
                return {'connect': {'protocolVersion': 3}, 'auth.getStatus': AUTH,
                        'models.list': MODELS, 'account.getQuota': QUOTA, 'runtime.shutdown': {}}[method]
        return RPC

    def test_one_rpc_process_stops_before_refresh_writeback(self):
        events = []
        with tempfile.TemporaryDirectory() as root:
            result = m.collect(self.session(Path(root), events), rpc_factory=self.rpc(events))
        self.assertEqual([e for e in events if e in m.METHODS], list(m.METHODS))
        self.assertLess(events.index('stopped'), events.index(('finish', {'native_stopped': True})))
        self.assertNotIn('quarantine', events)
        self.assertTrue(result['same_process_account_model_quota'])
        self.assertFalse(result['task_execution_enabled'])
        self.assertEqual(result['model_calls'], 0)

    def test_native_failure_quarantines_without_release(self):
        events = []
        with tempfile.TemporaryDirectory() as root, self.assertRaises(m.MetadataError):
            m.collect(self.session(Path(root), events), rpc_factory=self.rpc(events, 'models.list'))
        self.assertEqual(events[-2:], ['stopped', 'quarantine'])
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'finish' for e in events))

    def test_wrong_owner_or_api_route_is_rejected(self):
        for key, value in (('login', 'different-user'), ('authType', 'token'), ('host', 'evil.invalid'), ('isAuthenticated', False)):
            with self.assertRaises(m.MetadataError): m.auth_metadata({**AUTH, key: value})
        result = m.auth_metadata(AUTH)
        self.assertNotIn(m.EXPECTED_LOGIN, json.dumps(result))
        self.assertFalse(result['native_email_or_immutable_subject_returned'])

    def test_effort_choices_preserved_without_guessed_ranking(self):
        value = m.model_metadata(MODELS)[0]
        self.assertEqual(value['supported_reasoning_efforts'], ['max', 'low', 'high'])
        self.assertIsNone(value['maximum_effort'])
        self.assertNotIn('untrusted text', json.dumps(value))
        self.assertEqual(value['billing']['tokenPrices']['batchSize'], 1000)

    def test_model_catalog_missing_is_not_default_model(self):
        for value in ({}, {'models': []}, {'models': [MODELS['models'][0]] * 2}):
            with self.assertRaises(m.MetadataError): m.model_metadata(value)

    def test_quota_is_fraction_and_canonical_pool_identity(self):
        pool = m.quota_metadata(QUOTA, 'b' * 64)['pools'][0]
        other = m.quota_metadata(QUOTA, 'c' * 64)['pools'][0]
        self.assertAlmostEqual(pool['remaining_fraction'], .9918)
        self.assertNotEqual(pool['pool_ref'], other['pool_ref'])
        self.assertFalse(pool['billing_authorized'])

    def test_unlimited_is_unknown_fraction_and_never_free(self):
        quota = deepcopy(QUOTA)
        quota['quotaSnapshots']['premium_interactions'].update(isUnlimitedEntitlement=True, entitlementRequests=-1)
        result = m.quota_metadata(quota, 'b' * 64)
        self.assertIsNone(result['pools'][0]['remaining_fraction'])
        self.assertFalse(result['unlimited_is_free'])
        self.assertEqual(m.quota_metadata({}, 'b' * 64)['status'], 'unavailable')

    def test_malformed_quota_or_nonfinite_values_fail(self):
        for field, value in (('remainingPercentage', 101), ('overage', float('nan')), ('resetDate', 'not-time'), ('isUnlimitedEntitlement', 'yes')):
            quota = deepcopy(QUOTA); quota['quotaSnapshots']['premium_interactions'][field] = value
            with self.assertRaises(m.MetadataError): m.quota_metadata(quota, 'b' * 64)

    def test_environment_has_no_tokens_or_provider_override(self):
        with patch.dict('os.environ', {'GITHUB_TOKEN': 'bad', 'COPILOT_PROVIDER_BASE_URL': 'bad', 'NODE_OPTIONS': 'bad'}):
            environment = m.environment(Path('/private'))
        self.assertFalse(set(environment) & {'GITHUB_TOKEN', 'GH_TOKEN', 'COPILOT_GITHUB_TOKEN', 'NODE_OPTIONS', 'COPILOT_PROVIDER_BASE_URL'})
        self.assertEqual(environment['COPILOT_DISABLE_KEYTAR'], '1')

    def test_rpc_framing_and_forbidden_methods(self):
        native = object.__new__(m.NativeRPC)
        native.index = 0
        native.process = SimpleNamespace(stdin=io.BytesIO())
        native.frame = lambda: {'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': 3}}
        self.assertEqual(native.request('connect'), {'protocolVersion': 3})
        header, _, body = native.process.stdin.getvalue().partition(b'\r\n\r\n')
        self.assertEqual(header, f'Content-Length: {len(body)}'.encode())
        self.assertEqual(json.loads(body)['params'], {})
        for method in ('session.create', 'session.send', 'account.login', 'account.getCurrentAuth', 'tools.list'):
            with self.assertRaises(m.MetadataError): native.request(method)

    def test_rpc_rejects_server_requests(self):
        native = object.__new__(m.NativeRPC)
        native.index = 0; native.process = SimpleNamespace(stdin=io.BytesIO())
        native.frame = lambda: {'jsonrpc': '2.0', 'id': 1, 'method': 'permission.request', 'params': {}}
        with self.assertRaises(m.MetadataError): native.request('connect')

    def test_frame_parser_rejects_bad_or_oversized_lengths(self):
        for header in (b'Content-Length: 0', b'Content-Length: 9999999999', b'Content-Length: 2\r\nContent-Length: 2'):
            native = object.__new__(m.NativeRPC); native.buffer = bytearray(header + b'\r\n\r\n{}')
            with self.assertRaises(m.MetadataError): native.frame()

    def test_metadata_entrypoint_requires_broker_and_never_heartbeats(self):
        module = SimpleNamespace(cloud_main=Mock(side_effect=RuntimeError('fixture broker unavailable')))
        with patch.object(entry, 'image_check', return_value={}), patch.dict(sys.modules, {'metadata': module}), \
                patch.object(entry.os, 'umask'), patch.object(entry, 'heartbeat') as heartbeat, \
                patch.object(sys, 'argv', ['entrypoint', '--metadata-only']), redirect_stderr(io.StringIO()) as out:
            self.assertEqual(entry.main(), 1)
        heartbeat.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())['status'], 'rejected')

    def test_cloud_hook_uses_http_client_and_journals_before_acquiring(self):
        config = SimpleNamespace(provider=m.AGENT, account_ref=m.ACCOUNT_REF, profile='blueeyes', job_name='projects/p/locations/l/jobs/j')
        broker = Mock(config=config, execution=config.job_name + '/executions/j-abcde')
        session = Mock()
        with tempfile.TemporaryDirectory() as root:
            def acquire(execution, request):
                journal = Path(root) / ('metadata-' + request) / 'acquisition.json'
                self.assertEqual(json.loads(journal.read_text())['request_id'], request)
                self.assertEqual(execution, broker.execution)
                return 'fixture-lease'
            broker.acquire.side_effect = acquire
            service = SimpleNamespace(load_client_config=Mock(return_value=broker))
            credentials = SimpleNamespace(RefreshSession=Mock(return_value=session))
            with patch.dict(sys.modules, {'agent_hub.credential_broker_service': service, 'credential_state': credentials}), \
                    patch.dict(m.os.environ, {'CLOUD_RUN_EXECUTION': 'j-abcde'}), \
                    patch.object(m, 'ATTEMPT_ROOT', Path(root)), patch.object(m, 'collect', return_value={'fixture': True}):
                self.assertEqual(m.cloud_main(), {'fixture': True})
            session.restore.assert_called_once_with()
            broker.acquire.assert_called_once()
            broker.quarantine.assert_not_called()

    def test_cloud_hook_refuses_other_execution_before_acquire(self):
        config = SimpleNamespace(provider=m.AGENT, account_ref=m.ACCOUNT_REF, profile='blueeyes', job_name='projects/p/locations/l/jobs/j')
        broker = Mock(config=config, execution=config.job_name + '/executions/j-different')
        with patch.dict(sys.modules, {'agent_hub.credential_broker_service': SimpleNamespace(load_client_config=lambda _: broker),
                                     'credential_state': SimpleNamespace(RefreshSession=Mock())}), \
                patch.dict(m.os.environ, {'CLOUD_RUN_EXECUTION': 'j-abcde'}):
            with self.assertRaises(m.MetadataError): m.cloud_main()
        broker.acquire.assert_not_called()

if __name__ == '__main__': unittest.main()
