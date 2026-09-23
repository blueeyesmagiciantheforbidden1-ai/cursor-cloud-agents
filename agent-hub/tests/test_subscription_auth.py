"""Offline security boundary checks; no vendor or model request is performed."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_hub.adapters import Command
from agent_hub.billing_policy import BillingPolicy, evaluate_billing_policy
from agent_hub import subscription_auth as auth


def approved_command(execution_mode='read_only'):
    tools = 'Read,Grep,Glob' if execution_mode == 'read_only' else 'Read,Grep,Glob,Edit,Write,Bash'
    return Command((str(auth.CLAUDE_EXECUTABLE), '-p', '--output-format', 'json',
                    '--safe-mode', '--restricted', '--strict-mcp-config',
                    '--setting-sources', '', '--tools', tools,
                    '--allowedTools', tools, '--disallowedTools', 'mcp__*',
                    '--permission-mode', 'dontAsk', '--model', 'claude-opus-5',
                    '--effort', 'max'), stdin='A private task must never reach auth status.')


class ClaudeSubscriptionAuditTests(unittest.TestCase):
    def setUp(self):
        signal_patch = patch.object(auth.signal, 'SIGKILL', 9, create=True)
        signal_patch.start()
        self.addCleanup(signal_patch.stop)
        self.command = approved_command()
        self.env = auth.claude_environment('fixture-not-a-real-credential')
        self.workspace = auth.CLAUDE_WORKSPACE
        self.status = {
            'loggedIn': True, 'authMethod': 'oauth_token', 'apiProvider': 'firstParty',
            'configDirectory': str(auth.CLAUDE_CONFIG),
            'email': 'private-identity@example.invalid',
        }

    def mocked_runtime(self, body=None, *, exit_code=0, clean=(True, True)):
        stack = ExitStack()
        stack.enter_context(patch.object(auth.sys, 'platform', 'linux'))
        stack.enter_context(patch.object(auth.os, 'geteuid', return_value=10001, create=True))
        stack.enter_context(patch.object(auth, '_profile_matches', return_value=True))
        stack.enter_context(patch.object(auth, '_local_configuration_clean', side_effect=clean))
        result = (exit_code, json.dumps(self.status if body is None else body).encode())
        self.capture = stack.enter_context(patch.object(auth, '_capture_status', return_value=result))
        return stack

    def test_fresh_status_is_sanitized_route_evidence_but_cannot_authorize_inference(self):
        before = time.time()
        with self.mocked_runtime():
            result = auth.audit_claude_subscription(self.command, self.workspace, self.env)
        self.assertEqual(result.code, 'effective_managed_policy_unverified')
        self.assertEqual(result.auth_route, 'subscription_login')
        self.assertGreaterEqual(result.observed_at, before)
        self.assertFalse(result.local_configuration_checked)
        self.assertEqual(result.auth_status, 'unknown')
        self.assertNotIn('private-identity', repr(result))
        self.assertNotIn(self.env['CLAUDE_CODE_OAUTH_TOKEN'], repr(result))
        decision = evaluate_billing_policy('claude', self.env,
                                           BillingPolicy(mode='subscription_only'),
                                           result.billing_evidence())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, 'unchecked_configuration')
        self.capture.assert_called_once_with(self.env, self.workspace)

    def test_overrides_and_unknown_environment_keys_block_before_cli(self):
        for key, value in {
            'ANTHROPIC_API_KEY': 'do-not-output', 'ANTHROPIC_AUTH_TOKEN': 'do-not-output',
            'ANTHROPIC_BASE_URL': 'https://example.invalid', 'CLAUDE_CODE_USE_VERTEX': '1',
            'CLAUDE_CODE_EFFORT_LEVEL': 'low', 'ANTHROPIC_MODEL': 'alias',
            'HTTPS_PROXY': 'https://proxy.invalid', 'NODE_OPTIONS': '--require malicious',
            'LD_PRELOAD': '/malicious.so', 'ANTHROPIC_PROFILE': 'alternate',
            'HUB_AGENT_TOKEN': 'controller-secret', 'UNKNOWN_SETTING': '',
        }.items():
            with self.subTest(key=key), self.mocked_runtime():
                result = auth.audit_claude_subscription(self.command, self.workspace,
                                                       {**self.env, key: value})
                self.assertEqual(result.code, 'unapproved_environment')
                self.capture.assert_not_called()

    def test_environment_is_constructed_not_inherited_and_token_prefix_is_not_evidence(self):
        with patch.dict(auth.os.environ, {'ANTHROPIC_API_KEY': 'secret', 'HUB_TOKEN': 'secret'}):
            result = auth.claude_environment('not-a-vendor-token')
        self.assertNotIn('ANTHROPIC_API_KEY', result)
        self.assertNotIn('HUB_TOKEN', result)
        self.assertTrue(auth._environment_matches(result))
        for invalid in ('', 'a\nb', '\x00', 'a b', 'x' * 16_385, None):
            with self.subTest(invalid_type=type(invalid).__name__), self.assertRaises(ValueError):
                auth.claude_environment(invalid)

    def test_command_does_not_allow_shell_wrappers_routing_settings_or_aliases(self):
        self.assertTrue(auth._command_matches(self.command))
        candidates = [
            replace(self.command, argv=('sh', '-c', 'claude auth status')),
            replace(self.command, argv=self.command.argv + ('--settings', '{"env":{}}')),
            replace(self.command, argv=self.command.argv + ('--bare',)),
            replace(self.command, argv=self.command.argv + ('--safe-mode',)),
            replace(self.command, argv=self.command.argv + ('--model', 'claude-opus-5')),
            replace(self.command, prompt_argument=1),
            replace(self.command, prompt_file='task'),
            replace(self.command, stdin=None),
        ]
        for value in ('best', 'opus', 'sonnet', 'default', 'auto', 'https://override.invalid',
                      'claude-opus-5 --settings malicious'):
            argv = list(self.command.argv)
            argv[argv.index('--model') + 1] = value
            candidates.append(replace(self.command, argv=tuple(argv)))
        for candidate in candidates:
            with self.subTest(argv=candidate.preview()), self.mocked_runtime():
                result = auth.audit_claude_subscription(candidate, self.workspace, self.env)
                self.assertEqual(result.code, 'unapproved_command')
                self.capture.assert_not_called()

    def test_missing_or_changed_isolation_flags_block(self):
        for flag in ('--safe-mode', '--restricted', '--strict-mcp-config'):
            argv = list(self.command.argv)
            argv.remove(flag)
            self.assertFalse(auth._command_matches(replace(self.command, argv=tuple(argv))))

    def test_project_tools_require_exact_trusted_mode_profile(self):
        project = approved_command('project_work')
        self.assertFalse(auth._command_matches(project))
        self.assertFalse(auth._command_matches(self.command, execution_mode='project_work'))
        self.assertTrue(auth._command_matches(project, execution_mode='project_work'))
        for flag, value in (('--allowedTools', '*'), ('--tools', 'default'),
                            ('--permission-mode', 'bypassPermissions')):
            argv = list(project.argv)
            argv[argv.index(flag) + 1] = value
            self.assertFalse(auth._command_matches(replace(project, argv=tuple(argv)),
                                                   execution_mode='project_work'))
        with self.mocked_runtime():
            result = auth.audit_claude_subscription(project, self.workspace, self.env,
                                                    execution_mode='project_work')
        self.assertEqual(result.auth_route, 'subscription_login')
        self.assertFalse(result.local_configuration_checked)

    def test_only_verified_context_suffix_is_supported(self):
        for value, expected in (('claude-opus-5[1m]', True), ('claude-opus-5[other]', False),
                                ('claude-opus-5[1m][1m]', False), ('opus[1m]', False)):
            argv = list(self.command.argv)
            argv[argv.index('--model') + 1] = value
            self.assertEqual(auth._command_matches(replace(self.command, argv=tuple(argv))), expected)
        for flag, value in (('--setting-sources', 'user,project'), ('--tools', 'Bash,Read'),
                            ('--permission-mode', 'bypassPermissions'), ('--effort', 'automatic')):
            argv = list(self.command.argv)
            argv[argv.index(flag) + 1] = value
            self.assertFalse(auth._command_matches(replace(self.command, argv=tuple(argv))))

    def test_other_routes_and_failed_status_never_become_subscription_evidence(self):
        mutations = [
            {'loggedIn': False}, {'loggedIn': 1}, {'authMethod': 'claude.ai'},
            {'authMethod': 'api_key'}, {'authMethod': 'api_key_helper'},
            {'apiProvider': 'vertex'}, {'apiProvider': 'gateway'},
            {'apiKeySource': 'ANTHROPIC_API_KEY'}, {'forcedLoginMethod': 'gateway'},
            {'configDirectory': '/other/config'},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.mocked_runtime({**self.status, **mutation}):
                result = auth.audit_claude_subscription(self.command, self.workspace, self.env)
                self.assertEqual(result.code, 'subscription_route_unverified')
                self.assertIsNone(result.billing_evidence())
        with self.mocked_runtime(exit_code=1):
            self.assertIsNone(auth.audit_claude_subscription(
                self.command, self.workspace, self.env).billing_evidence())

    def test_configuration_injected_during_status_blocks_observation(self):
        with self.mocked_runtime(clean=(True, False)):
            result = auth.audit_claude_subscription(self.command, self.workspace, self.env)
        self.assertEqual(result.code, 'local_configuration_changed')
        self.assertIsNone(result.billing_evidence())

    def test_missing_profile_unclean_config_and_root_runtime_never_launch_status(self):
        for helper, code in (('_profile_matches', 'unverified_native_binary'),
                             ('_local_configuration_clean', 'unapproved_local_configuration')):
            with self.mocked_runtime(), patch.object(auth, helper, return_value=False):
                self.assertEqual(auth.audit_claude_subscription(
                    self.command, self.workspace, self.env).code, code)
                self.capture.assert_not_called()
        with self.mocked_runtime(), patch.object(auth.os, 'geteuid', return_value=0):
            self.assertEqual(auth.audit_claude_subscription(
                self.command, self.workspace, self.env).code, 'unsupported_runtime')
            self.capture.assert_not_called()

    def test_status_failure_malformed_or_ambiguous_json_remain_unknown(self):
        results = [None, (0, b'not-json'), (0, b'[]'),
                   (0, b'{"loggedIn":false,"loggedIn":true}'), (0, b'{"value":NaN}')]
        for status in results:
            with self.subTest(status=status), self.mocked_runtime():
                self.capture.return_value = status
                result = auth.audit_claude_subscription(self.command, self.workspace, self.env)
                self.assertIsNone(result.billing_evidence())

    def test_other_providers_have_no_invented_preflight(self):
        with patch.object(auth, 'audit_claude_subscription') as audit:
            for provider in ('codex', 'cursor', 'copilot', 'grok'):
                self.assertIsNone(auth.preflight_subscription_route(
                    provider, self.command, self.workspace, self.env))
        audit.assert_not_called()

    def test_capture_uses_only_fixed_status_arguments_and_no_task_or_stderr(self):
        process = Mock(pid=12345, returncode=0, stdout=io.BytesIO(b'{}'))
        process.poll.return_value = 0
        with patch.object(auth.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(auth.os, 'killpg', create=True) as cleanup:
            self.assertEqual(auth._capture_status(self.env, self.workspace), (0, b'{}'))
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[-3:], ('auth', 'status', '--json'))
        self.assertNotIn(self.command.stdin, argv)
        self.assertEqual(spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs['env'], self.env)
        self.assertFalse(spawn.call_args.kwargs['shell'])
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        cleanup.assert_called_once()

    def test_capture_drops_oversized_status_and_cleans_process(self):
        process = Mock(pid=12345, returncode=0,
                       stdout=io.BytesIO(b'x' * (auth._MAX_STATUS_BYTES + 1)))
        process.poll.return_value = 0
        with patch.object(auth.subprocess, 'Popen', return_value=process), \
                patch.object(auth.os, 'killpg', create=True) as cleanup:
            self.assertIsNone(auth._capture_status(self.env, self.workspace))
        cleanup.assert_called_once()

    def test_capture_timeout_cleans_process(self):
        process = Mock(pid=12345, stdout=io.BytesIO(b''))
        process.poll.return_value = None
        with patch.object(auth.subprocess, 'Popen', return_value=process), \
                patch.object(auth, '_PROBE_TIMEOUT_SECONDS', 0), \
                patch.object(auth.os, 'killpg', create=True) as cleanup:
            self.assertIsNone(auth._capture_status(self.env, self.workspace))
        cleanup.assert_called_once()


class ClaudeProfileIntegrityTests(unittest.TestCase):
    def test_manifest_hash_version_native_format_and_exact_schema_are_required(self):
        with tempfile.TemporaryDirectory(prefix='hub-claude-profile-') as td:
            root = Path(td)
            native = root / 'claude'
            manifest = root / 'runtime-profile.json'
            native.write_bytes(b'\x7fELFfixture-native')
            profile = {'schema': 1, 'cli_version': '2.1.275',
                       'sha256': hashlib.sha256(native.read_bytes()).hexdigest()}
            manifest.write_text(json.dumps(profile))
            with patch.object(auth, 'CLAUDE_PROFILE', manifest), \
                    patch.object(auth, 'CLAUDE_EXECUTABLE', native), \
                    patch.object(auth, '_immutable_path', return_value=True), \
                    patch.object(auth.os, 'access', return_value=True):
                self.assertTrue(auth._profile_matches())
                for mutation in ({'sha256': '0' * 64}, {'cli_version': 'unreviewed'},
                                 {'schema': True}, {'remote_policy_checked': True}):
                    manifest.write_text(json.dumps({**profile, **mutation}))
                    self.assertFalse(auth._profile_matches())
                native.write_bytes(b'#!/bin/sh\necho forged-status')
                manifest.write_text(json.dumps({**profile,
                    'sha256': hashlib.sha256(native.read_bytes()).hexdigest()}))
                self.assertFalse(auth._profile_matches())


if __name__ == '__main__':
    unittest.main()
