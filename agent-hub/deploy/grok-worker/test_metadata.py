"""Offline tests for documented Grok metadata commands and durable refresh."""
from contextlib import redirect_stderr
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
    spec = importlib.util.spec_from_file_location('grok_meta_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

m, entry = load('metadata'), load('entrypoint')

class MetadataTests(unittest.TestCase):
    def session(self, home, events):
        lease = SimpleNamespace(account_ref=m.ACCOUNT_REF, canonical_account_ref='b' * 64)
        broker = SimpleNamespace(assert_current=lambda lease: events.append('assert'),
                                 renew=lambda lease: events.append('renew'),
                                 quarantine=lambda lease, reason: events.append(('quarantine', reason)))
        return SimpleNamespace(home=home, state='active', lease=lease, broker=broker,
                               finish=lambda **kw: events.append(('finish', kw)) or 'version-1')

    def test_inspection_publishes_shape_without_private_values(self):
        raw = json.dumps({'config': {'token': 'private-value'}, 'rules': ['/private/path'],
                          'unknown': 'secret-name'}).encode()
        result = m.inspect_metadata(raw)
        self.assertEqual(result['discovery_fields']['config'], {'kind': 'object', 'entry_count': 1})
        self.assertEqual(result['discovery_fields']['rules']['entry_count'], 1)
        for private in ('private-value', '/private/path', 'secret-name', 'unknown'):
            self.assertNotIn(private, json.dumps(result))
        self.assertFalse(result['effective_auth_route_verified'])
        self.assertFalse(result['effective_model_verified'])

    def test_inspection_requires_json_object(self):
        for raw in (b'[]', b'null', b'not json', b'\xff'):
            with self.assertRaises(m.MetadataError): m.inspect_metadata(raw)

    def test_model_text_is_diagnostic_never_account_eligibility(self):
        result = m.model_metadata(b'\x1b[32mgrok-fixture-v1\x1b[0m\nAvailable grok-fixture-v2\n')
        self.assertEqual(result['observed_model_mentions'], ['grok-fixture-v1', 'grok-fixture-v2'])
        self.assertFalse(result['account_eligible_catalog'])
        self.assertFalse(result['maximum_model_verified'])
        self.assertIsNone(result['supported_efforts'])
        self.assertIsNone(result['maximum_effort'])

    def test_empty_models_do_not_become_default_or_fixed_effort(self):
        result = m.model_metadata(b'')
        self.assertEqual(result['observed_model_mentions'], [])
        self.assertEqual(result['effort_status'], 'unavailable')

    def test_exact_environment_omits_keys_and_custom_endpoints(self):
        inherited = {'XAI_API_KEY': 'unused', 'GROK_API_KEY': 'unused', 'GROK_MODELS_BASE_URL': 'unused',
                     'GROK_XAI_API_BASE_URL': 'unused', 'GROK_AUTH_MODE': 'unused'}
        with patch.dict(m.os.environ, inherited):
            env = m.environment(Path('/private'))
        self.assertFalse(set(env) & set(inherited))
        self.assertEqual(env['GROK_HOME'], str(Path('/private/.grok')))
        for family in ('CLAUDE', 'CURSOR'):
            for feature in ('SKILLS', 'RULES', 'AGENTS', 'MCPS', 'HOOKS'):
                self.assertEqual(env[f'GROK_{family}_{feature}_ENABLED'], '0')

    def test_only_documented_metadata_commands_can_spawn(self):
        with patch.object(m.subprocess, 'Popen') as spawn:
            for command in (('models', '--json'), ('status',), ('agent', 'stdio'), ('-p', 'prompt')):
                with self.assertRaises(m.MetadataError):
                    m.run_native(command, Path('/private'), lambda: None, 99999)
        spawn.assert_not_called()

    def test_collection_finishes_after_both_commands_and_keeps_raw_private(self):
        events = []
        def runner(command, home, renew, deadline):
            events.append(command)
            self.assertGreater(deadline, m.time.monotonic())
            renew()
            return b'{"config":{"private":"value"}}' if command == m.COMMANDS[0] else b'grok-fixture-v1'
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            result = m.collect(self.session(home, events), runner=runner)
            self.assertEqual((home / 'metadata-private' / 'models.txt').read_bytes(), b'grok-fixture-v1')
            self.assertIn(b'private', (home / 'metadata-private' / 'inspect.json').read_bytes())
        self.assertEqual([event for event in events if event in m.COMMANDS], list(m.COMMANDS))
        self.assertEqual(events[-1], ('finish', {'native_stopped': True}))
        self.assertEqual(result['account']['status'], 'unavailable')
        self.assertFalse(result['account']['native_identity_verified'])
        self.assertEqual(result['quota']['status'], 'unavailable')
        self.assertFalse(result['same_process_account_model_quota'])
        self.assertFalse(result['task_execution_enabled'])
        self.assertEqual(result['model_calls'], 0)
        self.assertFalse(result['claims_tasks'])
        self.assertNotIn('private', json.dumps(result))

    def test_native_failure_quarantines_and_never_releases(self):
        events = []
        def runner(*args): raise m.MetadataError('fixture failure')
        with tempfile.TemporaryDirectory() as root, self.assertRaises(m.MetadataError):
            m.collect(self.session(Path(root), events), runner=runner)
        self.assertEqual(events[-1], ('quarantine', 'provider_refresh_uncertain'))
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'finish' for e in events))

    def test_wrong_owner_or_inactive_lease_never_starts_native(self):
        for field in ('owner', 'state'):
            events = []
            with tempfile.TemporaryDirectory() as root:
                session = self.session(Path(root), events)
                if field == 'owner': session.lease.account_ref = 'c' * 64
                else: session.state = 'new'
                runner = Mock()
                with self.assertRaises(m.MetadataError): m.collect(session, runner=runner)
                runner.assert_not_called()

    def test_metadata_hook_requires_broker_without_heartbeat(self):
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
