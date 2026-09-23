"""Offline launch construction; synthetic native-header files are never executed."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_hub import adapters
from agent_hub.model_policy import AGENTS
from agent_hub.model_runtime import ModelRuntimeError, select_worker_model


NOW = 2000000000
ACCOUNT = 'a' * 64
PROMPT = 'Synthetic private prompt with --model decoy and Unicode \u2603'


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-model-runtime-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executables = {}
        registry, catalogs = {}, {}
        for agent in AGENTS:
            path = self.root / (agent + '.exe')
            path.write_bytes(b'MZ\x00\x00SYNTHETIC-NOT-EXECUTED-' + agent.encode())
            path.chmod(0o700)
            self.executables[agent] = path
            canonical = 'synthetic:' + agent
            registry[canonical] = {'family': agent, 'identity_evidence_sha256': ACCOUNT}
            row = {'model_ref': canonical, 'strength_rank': 1, 'accessible': True,
                   'billing': 'subscription_included', 'allowance': 'available',
                   'effort_control': {'codex': 'codex-config', 'claude': 'claude-effort', 'cursor': 'model-variant',
                                      'copilot': 'copilot-effort', 'grok': 'grok-effort'}[agent],
                   'effective_capabilities_verified': True, 'capability_evidence_sha256': ACCOUNT,
                   'efforts': [{'level': 'high', 'cli_model_id': 'synthetic-' + agent,
                                'model_ref': canonical, 'pin_verified': True}]}
            catalogs[agent] = {'observed_at': NOW - 10, 'expires_at': NOW + 1000,
                               'revision': 'synthetic-v1', 'evidence_sha256': ACCOUNT,
                               'complete': True, 'source': 'account_catalog', 'account_ref': ACCOUNT,
                               'cli_version': 'synthetic-1.0', 'cli_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'auth_route': 'subscription', 'overage_disabled': True,
                               'ranking_evidence_sha256': ACCOUNT, 'candidates': [row]}
        self.data = {'schema_version': 1, 'registry': {'observed_at': NOW - 10, 'expires_at': NOW + 1000,
                     'revision': 'synthetic-v1', 'evidence_sha256': ACCOUNT, 'models': registry, 'aliases': {}},
                     'catalogs': catalogs}
        self.bundle_path = self.root / 'catalog.json'
        self.save()
        network = patch('socket.socket.connect', side_effect=AssertionError('No network in runtime tests'))
        network.start()
        self.addCleanup(network.stop)

    def save(self):
        self.bundle_path.write_text(json.dumps(self.data), encoding='utf-8')

    def command(self, agent):
        return adapters.build_command(agent, PROMPT, executable=str(self.executables[agent]))

    def select(self, agent='codex', **changes):
        arguments = {'agent_id': agent, 'bundle_path': self.bundle_path, 'expected_account_ref': ACCOUNT,
                     'command': self.command(agent), 'now': NOW, **changes}
        return select_worker_model(**arguments)

    def test_all_five_commands_preserve_prompt_bindings_and_fixed_restrictions(self):
        for agent in AGENTS:
            original = self.command(agent)
            with self.subTest(agent=agent):
                selected, plan = self.select(agent, command=original)
                self.assertEqual(plan['distinct_canonical_models'], 5)
                self.assertEqual(selected.stdin, original.stdin)
                self.assertEqual(selected.prompt_file, original.prompt_file)
                self.assertEqual(selected.prompt_argument, original.prompt_argument)
                self.assertEqual(selected.prompt_file_argument, original.prompt_file_argument)
                self.assertEqual(selected.argv.count('--model'), 1)
                self.assertIn('synthetic-' + agent, selected.argv)
                if selected.prompt_argument is not None:
                    self.assertEqual(selected.argv[selected.prompt_argument], PROMPT)
                self.assertNotIn(PROMPT, ' '.join(selected.preview()))
                if agent == 'codex':
                    self.assertEqual(selected.argv[-1], '-')
                    self.assertEqual(selected.argv[:len(original.argv)-1], original.argv[:-1])
                    self.assertEqual(selected.argv[-5:-1], ('--model', 'synthetic-codex', '--config',
                                                           'model_reasoning_effort="high"'))
                else:
                    self.assertEqual(selected.argv[:len(original.argv)], original.argv)

    def test_account_comparison_uses_independent_expected_reference(self):
        with self.assertRaises(ModelRuntimeError) as failure:
            self.select(expected_account_ref='b' * 64)
        self.assertEqual(str(failure.exception), 'expected_account_mismatch')
        self.assertNotIn(ACCOUNT, str(failure.exception))
        for value in (None, '', 'raw-email@example.invalid', 'A' * 64):
            with self.assertRaises(ModelRuntimeError):
                self.select(expected_account_ref=value)

    def test_unknown_execution_mode_cannot_expand_permissions(self):
        for mode in ('admin', '', None, True):
            with self.subTest(mode=mode), self.assertRaises(ModelRuntimeError) as failure:
                self.select(execution_mode=mode)
            self.assertEqual(str(failure.exception), 'unsupported_execution_mode')

    def test_claude_project_work_requires_matching_trusted_profile(self):
        command = adapters.build_command('claude', PROMPT, executable=str(self.executables['claude']),
                                         execution_mode='project_work')
        selected, plan = self.select('claude', command=command, execution_mode='project_work')
        self.assertEqual(selected.argv[:len(command.argv)], command.argv)
        self.assertEqual(selected.stdin, PROMPT)
        self.assertEqual(plan['selections']['claude']['cli_model_id'], 'synthetic-claude')
        for candidate, mode in ((command, 'read_only'), (self.command('claude'), 'project_work')):
            with self.subTest(mode=mode), self.assertRaises(ModelRuntimeError) as failure:
                self.select('claude', command=candidate, execution_mode=mode)
            self.assertEqual(str(failure.exception), 'canonical_native_command_required')

    def test_binary_hash_mismatch_blocks_before_returning_launch_command(self):
        self.executables['codex'].write_bytes(b'MZ\x00\x00MODIFIED')
        with self.assertRaises(ModelRuntimeError) as failure:
            self.select()
        self.assertEqual(str(failure.exception), 'cli_binary_digest_mismatch')

    def test_script_with_matching_catalog_hash_still_is_not_a_native_cli(self):
        self.executables['codex'].write_bytes(b'#!/usr/bin/python\nprint("not native")')
        self.data['catalogs']['codex']['cli_sha256'] = hashlib.sha256(self.executables['codex'].read_bytes()).hexdigest()
        self.save()
        with self.assertRaises(ModelRuntimeError) as failure:
            self.select()
        self.assertEqual(str(failure.exception), 'native_cli_required')

    def test_wrapper_prefix_existing_model_flags_and_changed_permissions_rejected(self):
        command = self.command('codex')
        candidates = (replace(command, argv=(command.argv[0], 'wrapper.js', *command.argv[1:])),
                      replace(command, argv=(*command.argv[:-1], '--model', 'unapproved', '-')),
                      replace(command, argv=tuple('danger-full-access' if part == 'read-only' else part for part in command.argv)))
        for candidate in candidates:
            with self.subTest(argv_length=len(candidate.argv)), self.assertRaises(ModelRuntimeError) as failure:
                self.select(command=candidate)
            self.assertEqual(str(failure.exception), 'canonical_native_command_required')

    def test_interpreter_name_is_not_accepted_as_native_agent(self):
        path = self.root / 'python.exe'
        path.write_bytes(b'MZ\x00\x00SYNTHETIC-INTERPRETER')
        path.chmod(0o700)
        command = adapters.build_command('codex', PROMPT, executable=str(path))
        with self.assertRaises(ModelRuntimeError) as failure:
            self.select(command=command)
        self.assertEqual(str(failure.exception), 'native_cli_required')

    def test_invalid_prompt_field_does_not_corrupt_indices(self):
        for command in (replace(self.command('cursor'), prompt_argument=999),
                        replace(self.command('cursor'), prompt_argument=True),
                        replace(self.command('cursor'), stdin='unexpected')):
            with self.assertRaises(ModelRuntimeError):
                self.select('cursor', command=command)
        with self.assertRaises(ModelRuntimeError):
            self.select('grok', command=replace(self.command('grok'), prompt_file=None))

    def test_bundle_is_reloaded_each_launch_and_updated_plan_is_returned(self):
        first_command, first_plan = self.select()
        self.data['catalogs']['codex']['revision'] = 'synthetic-v2'
        self.data['catalogs']['codex']['candidates'][0]['efforts'].append(
            {'level': 'max', 'cli_model_id': 'synthetic-codex', 'model_ref': 'synthetic:codex', 'pin_verified': True})
        self.save()
        second_command, second_plan = self.select()
        self.assertNotEqual(first_plan['bundle_sha256'], second_plan['bundle_sha256'])
        self.assertNotEqual(first_command.argv, second_command.argv)
        self.assertEqual(second_plan['selections']['codex']['effort'], 'max')

    def test_requires_complete_fresh_global_stack_even_for_one_worker(self):
        self.data['catalogs']['cursor']['expires_at'] = NOW
        self.save()
        with self.assertRaises(ModelRuntimeError) as failure:
            self.select('claude')
        self.assertEqual(str(failure.exception), 'model_policy_stale_or_invalid_catalog')
        self.data['catalogs']['cursor']['expires_at'] = NOW + 1000
        del self.data['catalogs']['grok']
        self.save()
        with self.assertRaises(ModelRuntimeError):
            self.select('claude')

    def test_existing_credit_launch_uses_provider_cap_without_hub_reservation(self):
        catalog = self.data['catalogs']['claude']
        catalog['overage_disabled'] = False
        catalog['candidates'][0]['billing'] = 'existing_credits'
        catalog['credit_controls'] = {
            'verified': True, 'provider_cap_enforced': True,
            'existing_balance_microusd': 2000000, 'remaining_spend_cap_microusd': 1000000,
            'auto_reload_enabled': False, 'automatic_purchase_enabled': False,
            'api_fallback_enabled': False, 'evidence_sha256': ACCOUNT, 'pool_ref': ACCOUNT}
        self.save()
        command, plan = self.select('claude')
        selected = plan['selections']['claude']
        self.assertEqual(selected['billing'], 'existing_credits')
        self.assertEqual(selected['credit_enforcement'], 'provider_existing_balance_and_cap')
        self.assertEqual(selected['provider_credit_allowance_microusd'], 1000000)
        self.assertNotIn('credit_reserve_microusd', selected)
        self.assertIn('synthetic-claude', command.argv)
        catalog['credit_controls']['provider_cap_enforced'] = False
        self.save()
        with self.assertRaises(ModelRuntimeError) as failure:
            self.select('claude')
        self.assertEqual(str(failure.exception), 'model_policy_existing_credit_controls_unverified')

    def test_explicit_trusted_smoke_fleet_does_not_require_unenrolled_catalogs(self):
        self.data['catalogs'] = {'claude': self.data['catalogs']['claude']}
        self.save()
        selected, plan = self.select('claude', active_agents=('claude',))
        self.assertEqual(set(plan['selections']), {'claude'})
        self.assertEqual(plan['distinct_canonical_models'], 1)
        self.assertIn('synthetic-claude', selected.argv)
        with self.assertRaises(ModelRuntimeError):
            self.select('claude')
        for agents in (('codex',), (), 'claude', None, ('claude', 'claude'), ('claude', 'unknown')):
            with self.subTest(agents=agents), self.assertRaises(ModelRuntimeError):
                self.select('claude', active_agents=agents)

    def test_strict_json_rejects_duplicates_nonfinite_bom_and_excess_depth(self):
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"value":NaN}', b'{"value":1e999}',
                    b'\xef\xbb\xbf{}', b'\xff', b'[]', b'{"nested":' + b'[' * 30 + b'0' + b']' * 30 + b'}'):
            with self.subTest(length=len(raw)):
                self.bundle_path.write_bytes(raw)
                with self.assertRaises(ModelRuntimeError):
                    self.select()

    def test_relative_missing_and_oversized_files_rejected(self):
        for path in ('relative.json', self.root / 'missing.json'):
            with self.assertRaises(ModelRuntimeError):
                self.select(bundle_path=path)
        self.bundle_path.write_bytes(b'x' * 256001)
        with self.assertRaises(ModelRuntimeError) as failure:
            self.select()
        self.assertEqual(str(failure.exception), 'control_plane_file_size_invalid')

    def test_linked_catalog_is_rejected_when_platform_allows_links(self):
        link = self.root / 'linked.json'
        try:
            link.symlink_to(self.bundle_path)
        except OSError:
            self.skipTest('Platform account cannot create symbolic links')
        with self.assertRaises(ModelRuntimeError) as failure:
            self.select(bundle_path=link)
        self.assertEqual(str(failure.exception), 'linked_control_plane_path_forbidden')

    def test_metadata_change_during_read_fails_closed(self):
        import agent_hub.model_runtime as runtime
        actual = runtime.os.fstat
        calls = []
        def changed(fd):
            value = actual(fd)
            calls.append(True)
            if len(calls) == 2:
                from types import SimpleNamespace
                return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino, st_size=value.st_size,
                                       st_mtime_ns=value.st_mtime_ns + 1)
            return value
        with patch.object(runtime.os, 'fstat', side_effect=changed), self.assertRaises(ModelRuntimeError) as failure:
            self.select()
        self.assertEqual(str(failure.exception), 'control_plane_file_changed')


if __name__ == '__main__':
    unittest.main()
