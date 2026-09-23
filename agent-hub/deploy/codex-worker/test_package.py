"""Offline synthetic contract tests; no credentials, subprocesses or network."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from build_native import check_schemas, extract_verified, safe_member, SCHEMA_FIELDS
from credential_state import CredentialError, Lease, RefreshSession
from protocol_gate import GateError, PrePromptGate, SANDBOX, WORKSPACE, account_ref, digest


class ArchiveTests(unittest.TestCase):
    def test_traversal_and_platform_paths_denied(self):
        for path in ('../evil', '/evil', 'a/../evil', 'a//b', 'a/./b', 'C:/evil', 'a\\b'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_member(path)

    def make_archive(self, root, *, link=False, duplicate=False):
        target = root / 'native.tar.gz'
        body = b'fixture only'
        item = tarfile.TarInfo('bin/codex')
        item.size, item.mode = len(body), 0o755
        if link:
            item.type, item.linkname, item.size = tarfile.SYMTYPE, '/outside', 0
        with tarfile.open(target, 'w:gz') as output:
            output.addfile(item, None if link else io.BytesIO(body))
            if duplicate:
                output.addfile(item, io.BytesIO(body))
        return target, {'members': [{'name': item.name, 'size': item.size,
                    'type': item.type.decode(), 'sha256': hashlib.sha256(body).hexdigest()}]}

    def test_exact_archive_extracts_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = self.make_archive(root)
            extract_verified(source, root / 'out', manifest)
            self.assertEqual((root / 'out/bin/codex').read_bytes(), b'fixture only')
            with self.assertRaises(ValueError):
                extract_verified(source, root / 'out', manifest)

    def test_mismatch_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = self.make_archive(root)
            manifest['members'][0]['sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                extract_verified(source, root / 'out', manifest)
            self.assertFalse((root / 'out').exists())

    def test_links_and_duplicates_rejected(self):
        for flags in ({'link': True}, {'duplicate': True}):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, manifest = self.make_archive(root, **flags)
                with self.assertRaises(ValueError):
                    extract_verified(source, root / 'out', manifest)

    def test_schema_surface_missing_field_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, fields in SCHEMA_FIELDS.items():
                (root / (name + '.json')).write_text(json.dumps({'properties': {x: {} for x in fields}}))
            self.assertEqual(set(check_schemas(root)), set(SCHEMA_FIELDS))
            (root / 'GetAccountRateLimitsResponse.json').write_text('{"properties":{}}')
            with self.assertRaises(ValueError):
                check_schemas(root)


class Broker:
    def __init__(self):
        self.current = True
        self.fail_commit = False
        self.committed, self.released, self.quarantined = [], [], []

    def assert_current(self, lease):
        if not self.current:
            raise CredentialError('stale_fence')

    def commit(self, lease, body):
        self.assert_current(lease)
        if self.fail_commit:
            raise TimeoutError()
        self.committed.append((lease.profile, body))
        return 'version-2'

    def release(self, lease, version):
        self.assert_current(lease)
        self.released.append(version)

    def quarantine(self, lease, reason):
        self.quarantined.append(reason)


class CredentialTests(unittest.TestCase):
    def lease(self, profile='ryan'):
        return Lease(profile, 'a' * 64, 1, 'version-1', b'OPAQUE-NONCREDENTIAL-FIXTURE')

    def test_opaque_bytes_not_in_repr(self):
        self.assertNotIn('OPAQUE', repr(self.lease()))

    def test_separate_profiles_refresh_and_write_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, broker = Path(temporary), Broker()
            first = RefreshSession(broker, self.lease(), root / 'ryan')
            second = RefreshSession(broker, self.lease('blueeyes'), root / 'blueeyes')
            first.restore()
            second.restore()
            (root / 'ryan/auth.json').write_bytes(b'NEW-OPAQUE-FIXTURE')
            first.finish(native_stopped=True)
            self.assertEqual(broker.committed, [('ryan', b'NEW-OPAQUE-FIXTURE')])
            self.assertEqual((root / 'blueeyes/auth.json').read_bytes(), self.lease().auth_bytes)

    def test_running_native_cannot_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = Broker()
            session = RefreshSession(broker, self.lease(), Path(temporary) / 'home')
            session.restore()
            with self.assertRaises(CredentialError):
                session.finish(native_stopped=False)
            self.assertEqual(broker.committed, [])

    def test_uncertain_commit_retains_file_and_holds_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = Broker()
            home = Path(temporary) / 'home'
            session = RefreshSession(broker, self.lease(), home)
            session.restore()
            broker.fail_commit = True
            with self.assertRaisesRegex(CredentialError, 'quarantined'):
                session.finish(native_stopped=True)
            self.assertTrue((home / 'auth.json').exists())
            self.assertEqual(broker.released, [])
            self.assertEqual(session.state, 'quarantined')
            with self.assertRaises(CredentialError):
                session.finish(native_stopped=True)

    def test_stale_fence_cannot_write_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = Broker()
            session = RefreshSession(broker, self.lease(), Path(temporary) / 'home')
            session.restore()
            broker.current = False
            with self.assertRaises(CredentialError):
                session.finish(native_stopped=True)
            self.assertEqual(broker.committed, [])
            self.assertEqual(broker.released, [])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.calls = []
        self.config = {'forced_login_method': 'chatgpt', 'cli_auth_credentials_store': 'file'}
        self.data = {
            'account/read': {'account': {'type': 'chatgpt', 'email': 'example@example.invalid', 'planType': 'pro'}},
            'model/list': {'data': [{'model': 'test-model', 'hidden': False,
                'supportedReasoningEfforts': [{'reasoningEffort': 'high'}, {'reasoningEffort': 'ultra'}]}], 'nextCursor': None},
            'account/rateLimits/read': {'accountId': 'synthetic-account', 'ordinaryUsageAllowed': True},
            'config/read': {'config': self.config},
            'configRequirements/read': {'requirements': None},
        }
        self.selection = {'account_ref': account_ref('example@example.invalid'), 'cli_model_id': 'test-model',
                          'effort': 'ultra', 'billing': 'subscription_included'}

    def gate(self):
        def rpc(method, params):
            self.calls.append((method, params))
            return self.data[method]
        return PrePromptGate(rpc, self.selection, config_sha256=digest(self.config),
            provider_account_ref=hashlib.sha256(b'openai-chatgpt:synthetic-account').hexdigest(), clock=lambda: self.now)

    def thread(self):
        return {'model': 'test-model', 'modelProvider': 'openai', 'reasoningEffort': 'ultra',
                'cwd': WORKSPACE, 'approvalPolicy': 'never', 'sandbox': dict(SANDBOX),
                'thread': {'id': 'synthetic-thread', 'ephemeral': True, 'turns': []}}

    def test_checks_same_transport_then_prepares_only_one_turn(self):
        gate = self.gate()
        self.assertFalse(gate.collect()['dispatch_enabled'])
        self.assertEqual(gate.thread_request()['sandbox'], 'workspace-write')
        gate.accept_thread(self.thread())
        self.assertEqual(gate.turn_request('synthetic prompt')['effort'], 'ultra')
        self.assertTrue(all(method not in ('thread/start', 'turn/start') for method, _ in self.calls))
        with self.assertRaises(GateError):
            gate.turn_request('duplicate')

    def test_wrong_account_blocks_before_model_discovery(self):
        self.data['account/read']['account']['email'] = 'other@example.invalid'
        with self.assertRaises(GateError):
            self.gate().collect()
        self.assertEqual(len(self.calls), 1)

    def test_unknown_or_exhausted_usage_never_infers_recovery(self):
        for value in (None, False, 'true'):
            self.data['account/rateLimits/read']['ordinaryUsageAllowed'] = value
            with self.subTest(value=value), self.assertRaises(GateError):
                self.gate().collect()

    def test_credit_route_not_fabricated_from_balance(self):
        self.selection['billing'] = 'existing_credits'
        with self.assertRaises(GateError):
            self.gate().collect()

    def test_downgrade_or_unknown_higher_effort_fails(self):
        self.selection['effort'] = 'high'
        with self.assertRaises(GateError):
            self.gate().collect()
        self.selection['effort'] = 'ultra'
        self.data['model/list']['data'][0]['supportedReasoningEfforts'].append({'reasoningEffort': 'future-unknown'})
        with self.assertRaises(GateError):
            self.gate().collect()

    def test_mutated_config_fails(self):
        gate = self.gate()
        self.config['mcp_servers'] = {'unexpected': {}}
        with self.assertRaises(GateError):
            gate.collect()

    def test_duplicate_or_infinite_catalog_fails(self):
        self.data['model/list']['nextCursor'] = 'repeated'
        with self.assertRaises(GateError):
            self.gate().collect()

    def test_applied_downgrade_or_sandbox_escape_fails(self):
        for mutation in ({'reasoningEffort': 'high'}, {'sandbox': {'type': 'dangerFullAccess'}},
                         {'approvalPolicy': 'on-request'}, {'modelProvider': 'other'}):
            gate = self.gate()
            gate.collect()
            response = self.thread()
            response.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(GateError):
                gate.accept_thread(response)
            with self.assertRaises(GateError):
                gate.turn_request('synthetic prompt')

    def test_expired_preflight_cannot_prepare_prompt(self):
        gate = self.gate()
        gate.collect()
        gate.accept_thread(self.thread())
        self.now += 31
        with self.assertRaises(GateError):
            gate.turn_request('synthetic prompt')


if __name__ == '__main__':
    unittest.main()
