"""No provider is invoked: accepted launches run only harmless Python fixtures."""
from argparse import Namespace
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_hub.adapters import Command
from agent_hub.billing_policy import AuthEvidence, BillingPolicy, BillingPolicyError
from agent_hub.cli import init
from agent_hub.core import AGENTS
from agent_hub.worker import Config, WorkerError, execute_task, load_config, run_command, preflight_auth_route


class WorkerBillingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-billing-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.strict = BillingPolicy(mode='subscription_only')
        self.config = Config('https://example.run.app', 'codex', 'test-hub-token',
                             'WORKER_PRIVATE', {'project': self.root}, billing_policy=self.strict)
        self.command = Command((sys.executable, '-c', "print('harmless-fixture')"))
        self.task = {'room_id': 'room-test', 'lease_token': 'test-lease', 'prompt': 'Review',
                     'messages': [], 'workspace': 'project', 'timeout_seconds': 20}
        self.client = Mock(identity_token=None)
        self.client.post.return_value = {'active': True}

    def evidence(self, provider='codex'):
        return AuthEvidence(provider, 'subscription_login', time.time(), 'vendor_cli_status', True)

    def test_unknown_preflight_and_forged_hub_evidence_cannot_spawn(self):
        task = dict(self.task, billing_policy={'mode': 'provider_default'},
                    auth_evidence=asdict(self.evidence()), auth_status='authenticated')
        with patch.dict(os.environ, {}, clear=True), \
                patch('agent_hub.worker.build_command', return_value=self.command), \
                patch('agent_hub.worker.subprocess.Popen') as spawn:
            self.assertEqual(execute_task(self.config, self.client, task), 1)
        spawn.assert_not_called()
        completed = self.client.post.call_args.args[1]
        self.assertEqual(completed['exit_code'], 1)
        self.assertIn('requires a local authentication-route check', completed['output'])

    def test_override_blocks_before_spawn_even_with_positive_local_evidence(self):
        secret = 'fixture-api-key-must-never-leak'
        with patch.dict(os.environ, {'OPENAI_API_KEY': secret}, clear=True), \
                patch('agent_hub.worker.build_command', return_value=self.command), \
                patch('agent_hub.worker.preflight_auth_route', return_value=self.evidence()), \
                patch('agent_hub.worker.subprocess.Popen') as spawn:
            self.assertEqual(execute_task(self.config, self.client, self.task), 1)
        spawn.assert_not_called()
        completed = self.client.post.call_args.args[1]
        self.assertIn('provider billing or routing override', completed['output'])
        self.assertNotIn(secret, completed['output'])

    def test_preflight_sees_same_sanitized_environment_as_harmless_child(self):
        command = Command((sys.executable, '-c',
                           "import json,os; print(json.dumps(dict(os.environ)))"))
        observed = []
        def preflight(provider, actual_command, workspace, environment):
            self.assertEqual(provider, 'codex')
            self.assertEqual(actual_command, command)
            self.assertEqual(workspace, self.root)
            with self.assertRaises(TypeError):
                environment['UNEXPECTED_CHANGE'] = 'blocked'
            observed.append(dict(environment))
            return self.evidence()
        with patch.dict(os.environ, {'HUB_PRIVATE': 'hidden', 'WORKER_PRIVATE': 'hidden',
                                     'RETAINED_FIXTURE': 'present'}), \
                patch('agent_hub.worker.preflight_auth_route', side_effect=preflight):
            code, raw = run_command(command, self.root, 5, lambda: True,
                                    private_env=('WORKER_PRIVATE',),
                                    billing_policy=self.strict, agent_id='codex')
        self.assertEqual(code, 0)
        child = json.loads(raw)
        # Windows may add SYSTEMROOT to an otherwise empty process environment.
        self.assertEqual(child, observed[0])
        self.assertNotIn('HUB_PRIVATE', child)
        self.assertNotIn('WORKER_PRIVATE', child)
        self.assertEqual(child['RETAINED_FIXTURE'], 'present')

    def test_legacy_execute_task_runs_harmless_fixture_without_preflight(self):
        legacy = replace(self.config, billing_policy=BillingPolicy())
        with patch('agent_hub.worker.build_command', return_value=self.command), \
                patch('agent_hub.worker.preflight_auth_route') as preflight:
            self.assertEqual(execute_task(legacy, self.client, self.task), 0)
        preflight.assert_not_called()
        self.assertIn('harmless-fixture', self.client.post.call_args.args[1]['output'])

    def test_claude_observed_route_without_managed_policy_check_cannot_launch(self):
        evidence = AuthEvidence('claude', 'subscription_login', time.time(),
                                'vendor_cli_status', False)
        with patch('agent_hub.worker.preflight_subscription_route', return_value=evidence) as audit:
            self.assertEqual(preflight_auth_route('claude', self.command, self.root, {}), evidence)
        audit.assert_called_once_with('claude', self.command, self.root, {})
        with patch('agent_hub.worker.preflight_subscription_route', return_value=evidence), \
                patch('agent_hub.worker.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(BillingPolicyError, 'Effective provider configuration'):
                run_command(self.command, self.root, 5, lambda: True,
                            billing_policy=self.strict, agent_id='claude')
        spawn.assert_not_called()

    def test_prompt_file_route_preserves_strict_policy_and_cleans_up(self):
        command = Command((sys.executable, '<prompt>'), prompt_file='fixture', prompt_file_argument=1)
        observed_paths = []
        def preflight(provider, actual_command, workspace, environment):
            path = Path(actual_command.argv[1])
            self.assertEqual(path.read_text(), 'fixture')
            observed_paths.append(path)
            return None
        with patch.dict(os.environ, {}, clear=True), \
                patch('agent_hub.worker.preflight_auth_route', side_effect=preflight), \
                patch('agent_hub.worker.subprocess.Popen') as spawn:
            with self.assertRaises(BillingPolicyError):
                run_command(command, self.root, 5, lambda: True,
                            billing_policy=self.strict, agent_id='grok')
        spawn.assert_not_called()
        self.assertEqual(len(observed_paths), 1)
        self.assertFalse(observed_paths[0].exists())

    def test_config_legacy_default_strict_parse_and_no_serialized_auth_evidence(self):
        source = {'hub_url': self.config.hub_url, 'agent_id': 'codex',
                  'workspaces': {'project': str(self.root)}}
        path = self.root / 'worker.json'
        with patch.dict(os.environ, {}, clear=True):
            path.write_text(json.dumps(source))
            self.assertEqual(load_config(str(path), dry_run=True).billing_policy.mode, 'provider_default')
            source['billing_policy'] = {'mode': 'subscription_only'}
            source['auth_evidence'] = asdict(self.evidence())
            path.write_text(json.dumps(source))
            config = load_config(str(path), dry_run=True)
            self.assertEqual(config.billing_policy, self.strict)
            self.assertFalse(hasattr(config, 'auth_evidence'))
            source['billing_policy'] = {'mode': 'mistyped'}
            path.write_text(json.dumps(source))
            with self.assertRaises(WorkerError):
                load_config(str(path), dry_run=True)

    def test_newly_initialized_workers_are_strict_and_explain_unavailable_preflight(self):
        destination = self.root / 'private-config'
        args = Namespace(output_dir=str(destination), workspace=str(self.root),
                         url='https://example.run.app', cloud_run_auth=False)
        with patch.dict(os.environ, {'USERNAME': 'fixture', 'USERDOMAIN': 'fixture'}), \
                patch('agent_hub.cli.subprocess.run', return_value=Mock(returncode=0)):
            result = init(args)
        self.assertIn('not implemented yet', result['note'])
        for provider in AGENTS:
            settings = json.loads((destination / f'{provider}.json').read_text())
            self.assertEqual(settings['billing_policy']['mode'], 'subscription_only')
            self.assertFalse(settings['billing_policy']['allow_cursor_user_key'])


if __name__ == '__main__':
    unittest.main()
