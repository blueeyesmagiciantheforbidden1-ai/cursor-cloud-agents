"""Offline scripted native protocol only; never launch Claude or call a provider."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from agent_hub.subscription_auth import claude_environment
from tests.test_claude_runtime import _Process

PATH = Path(__file__).resolve().parents[1] / 'deploy' / 'claude-worker' / 'eligibility_probe.py'
SPEC = importlib.util.spec_from_file_location('runcrew_eligibility_probe_test_module', PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
NOW = 20000
OWNER = hashlib.sha256(b'owner@example.invalid').hexdigest()


class Process(_Process):
    def __init__(self, *, before_controls=None, extra_frames=None, trailing_frames=None,
                 before_init_frames=None, lifecycle=True, **kwargs):
        super().__init__(**kwargs)
        self.before_controls, self.extra_frames, self.trailing_frames = before_controls, extra_frames, trailing_frames
        self.before_init_frames = before_init_frames
        self.emit_lifecycle = lifecycle
        if 'settings' not in kwargs:
            self.settings['applied']['model'] = probe.MODEL
        if 'result' not in kwargs:
            self.result = {'type': 'result', 'subtype': 'success', 'is_error': False,
                'result': 'READY', 'num_turns': 1, 'total_cost_usd': 0.001,
                'usage': {'input_tokens': 12, 'output_tokens': 1, 'private_field': 'do-not-log'},
                'modelUsage': {probe.MODEL: {'inputTokens': 12, 'outputTokens': 1, 'costUSD': 0.001}}}

    def accept(self, message):
        if message['type'] != 'user':
            if self.before_controls:
                for event in self.before_controls(message):
                    self.stdout.emit(event)
            return super().accept(message)
        self.received.append(message)
        if self.emit_lifecycle:
            self.stdout.emit(self.lifecycle(message, 'queued'))
        if self.before_init_frames:
            for event in self.before_init_frames(message):
                self.stdout.emit(event)
        self.stdout.emit({'type': 'system', 'subtype': 'init', 'model': probe.MODEL,
                          'tools': [], 'mcp_servers': [], 'session_id': self.session_id})
        if self.emit_lifecycle:
            self.stdout.emit(self.lifecycle(message, 'started'))
        if self.extra_frames:
            for event in self.extra_frames(message):
                self.stdout.emit(event)
        self.stdout.emit(self.result)
        if self.emit_lifecycle:
            self.stdout.emit(self.lifecycle(message, 'completed'))
        if self.trailing_frames:
            for event in self.trailing_frames(message):
                self.stdout.emit(event)


def stream_frames():
    """A complete optional SDK partial-output lifecycle with no tools."""
    session = str(uuid.uuid4())
    events = [
        {'type': 'message_start', 'message': {'role': 'assistant', 'model': probe.MODEL,
                                            'content': [], 'stop_reason': None}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'private-thinking'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'private-signature'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'READY'}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}},
        {'type': 'message_stop'},
    ]
    return [{'type': 'stream_event', 'event': event, 'uuid': str(uuid.uuid4()),
             'session_id': session, 'parent_tool_use_id': None} for event in events]


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.environment = claude_environment('OPAQUE-SYNTHETIC-TOKEN')
        self.enrollment = {'schema_version': 1, 'account_ref': OWNER, 'owner_verified': True,
            'auth_route': 'first_party_subscription', 'evidence_sha256': 'a' * 64,
            'credential_sha256': hashlib.sha256(b'OPAQUE-SYNTHETIC-TOKEN').hexdigest()}
        self.billing = {'schema_version': 1, 'account_ref': OWNER, 'kind': 'provider_browser_billing_controls',
            'plan': 'max20x', 'evidence_sha256': 'b' * 64, 'observed_at': NOW - 10, 'expires_at': NOW + 890,
            'provider_cap_enforced': True, 'currency': 'USD', 'auto_reload_enabled': False,
            'automatic_purchase_enabled': False, 'api_fallback_enabled': False,
            'existing_balance_microusd': 100000000, 'remaining_spend_cap_microusd': 40000000}
        self.operator = {'schema_version': 1, 'enabled': True, 'invocation_id': uuid.uuid4().hex,
            'expected_account_ref': OWNER, 'expected_cli_sha256': probe.CLI_SHA256,
            'expires_at': NOW + 890, 'billing_max_age_seconds': 900,
            'included_first': True, 'existing_credits_authorized': True,
            'external_one_shot_guard': {'task_count': 1, 'parallelism': 1, 'max_retries': 0,
                'prior_executions': 0, 'intent_journaled': True, 'automatic_replay_disabled': True}}
        self.seal()

    def seal(self):
        self.operator['billing_sha256'] = probe.receipt_digest(self.billing)
        self.operator['enrollment_sha256'] = probe.receipt_digest(self.enrollment)

    def run_fixture(self, process=None, *, timeout=2):
        self.process = process or Process()
        with patch.object(probe, '_local_check') as local, \
                patch.object(probe.subprocess, 'Popen', return_value=self.process) as spawn, \
                patch.object(probe, '_terminate', side_effect=lambda p: p.terminate()) as terminate, \
                patch.object(probe.time, 'time', return_value=NOW):
            self.local, self.spawn, self.terminate = local, spawn, terminate
            return probe.run_probe(self.operator, self.billing, self.enrollment,
                                   environment=self.environment, timeout_seconds=timeout)

    def test_one_fixed_prompt_after_same_process_controls_and_safe_receipt(self):
        result = self.run_fixture()
        self.assertEqual(result['outcome'], 'verified_eligibility')
        self.assertTrue(result['server_accepted'])
        self.assertEqual(result['actual_model'], probe.MODEL)
        self.assertEqual(result['applied_effort'], 'max')
        self.assertEqual([m['type'] for m in self.process.received], ['control_request', 'control_request', 'user'])
        self.assertEqual(self.process.received[-1]['message']['content'], 'Reply exactly READY.')
        self.assertEqual(self.spawn.call_count, 1)
        self.assertNotIn(probe.PROMPT, self.spawn.call_args.args[0])
        self.assertFalse(self.spawn.call_args.kwargs['shell'])
        self.assertTrue(self.spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(result['usage']['api_equivalent_cost_usd'], 0.001)
        self.assertFalse(result['actual_account_charge_verified'])
        self.assertEqual(result['actual_billing_route'], 'unknown')
        self.assertFalse(result['complete_account_catalog'])
        self.assertFalse(result['native_transport_retries_verified_disabled'])
        output = json.dumps(result)
        for private in ('OPAQUE-SYNTHETIC-TOKEN', 'private-raw-diagnostic', 'do-not-log', OWNER, 'owner@example.invalid'):
            self.assertNotIn(private, output)

    def test_command_has_no_tools_mcp_or_additional_model(self):
        argv = probe.ARGV
        expected = {'--tools': '', '--disallowedTools': '*', '--max-turns': '1', '--model': probe.MODEL,
                    '--effort': 'max', '--mcp-config': '{"mcpServers":{}}', '--permission-prompts': 'none'}
        for flag, value in expected.items():
            self.assertEqual(argv[argv.index(flag) + 1], value)
        for forbidden in ('--fallback-model', '--advisor', '--agents', '--dangerously-skip-permissions', '--add-dir'):
            self.assertNotIn(forbidden, argv)
        self.assertIn('--safe-mode', argv)
        self.assertIn('--restricted', argv)

    def test_owner_or_exact_credential_mismatch_never_spawns(self):
        for key, value in [('account_ref', 'c' * 64), ('credential_sha256', 'd' * 64)]:
            self.setUp()
            self.enrollment[key] = value
            self.seal()
            with self.subTest(key=key), self.assertRaises(probe.ProbeError):
                self.run_fixture()
            self.spawn.assert_not_called()

    def test_receipt_hash_or_current_billing_controls_required(self):
        for changed in ({'observed_at': NOW - 901}, {'expires_at': NOW}, {'currency': 'unknown'},
                        {'auto_reload_enabled': True}, {'provider_cap_enforced': False},
                        {'remaining_spend_cap_microusd': 0}):
            self.setUp()
            self.billing.update(changed)
            self.seal()
            with self.subTest(changed=changed), self.assertRaises(probe.ProbeError):
                self.run_fixture()
            self.spawn.assert_not_called()
        self.setUp()
        self.operator['billing_sha256'] = '0' * 64
        with self.assertRaises(probe.ProbeError):
            self.run_fixture()
        self.spawn.assert_not_called()

    def test_unknown_or_api_environment_never_spawns(self):
        self.environment['ANTHROPIC_API_KEY'] = 'must-not-pass'
        with self.assertRaises(probe.ProbeError):
            self.run_fixture()
        self.spawn.assert_not_called()

    def test_external_execution_guard_and_in_process_replay(self):
        self.operator['external_one_shot_guard']['max_retries'] = 1
        with self.assertRaises(probe.ProbeError):
            self.run_fixture()
        self.spawn.assert_not_called()
        self.operator['external_one_shot_guard']['max_retries'] = 0
        self.assertEqual(self.run_fixture()['outcome'], 'verified_eligibility')
        with self.assertRaisesRegex(probe.ProbeError, 'already_consumed'):
            self.run_fixture()
        self.spawn.assert_not_called()

    def test_no_configurable_prompt_model_tools_or_budget(self):
        for key in ('prompt', 'model', 'tools', 'max_budget_usd'):
            self.setUp()
            self.operator[key] = 'unapproved'
            with self.subTest(key=key), self.assertRaises(probe.ProbeError):
                self.run_fixture()
            self.spawn.assert_not_called()

    def test_native_wrong_owner_or_api_route_does_not_receive_prompt(self):
        for changed in ({'email': 'wrong@example.invalid'}, {'apiProvider': 'vertex'}, {'apiKeySource': 'ANTHROPIC_API_KEY'}):
            self.setUp()
            process = Process()
            process.account.update(changed)
            result = self.run_fixture(process)
            self.assertEqual(result['outcome'], 'failed')
            self.assertFalse(result['prompt_sent'])
            self.assertNotIn('user', [m['type'] for m in process.received])

    def test_applied_model_effort_or_managed_settings_block_prompt(self):
        for changed in ({'effective': {'env': {'untrusted': 'private'}}}, {'sources': ['managed']},
                        {'applied': {'model': probe.MODEL, 'effort': 'high', 'advisor': None, 'ultracode': False}}):
            self.setUp()
            process = Process()
            process.settings.update(changed)
            result = self.run_fixture(process)
            self.assertEqual(result['outcome'], 'failed')
            self.assertFalse(result['prompt_sent'])
            self.assertNotIn('private', json.dumps(result))

    def test_result_must_be_ready_one_turn_and_exact_served_model(self):
        for changed in ({'result': 'READY.'}, {'result': None}, {'result': ' ' * 129 + 'READY'},
                        {'num_turns': 2}, {'num_turns': True}, {'is_error': True},
                        {'modelUsage': None}, {'modelUsage': []}, {'modelUsage': {}},
                        {'modelUsage': {'claude-other-5': {}}},
                        {'modelUsage': {probe.MODEL: {}, 'claude-other-5': {}}}):
            self.setUp()
            process = Process()
            process.result.update(changed)
            with self.subTest(changed=changed):
                result = self.run_fixture(process)
                self.assertEqual(result['outcome'], 'failed')
                self.assertTrue(result['prompt_sent'])
                if 'result' in changed or 'num_turns' in changed:
                    self.assertTrue(result['server_accepted'])
                else:
                    self.assertIsNot(result['server_accepted'], True)
                self.assertEqual(self.spawn.call_count, 1)

    def test_harmless_ready_whitespace_is_accepted_without_raw_result_logging(self):
        for reply in ('READY\n', '  READY  ', '\r\nREADY\r\n'):
            self.setUp()
            process = Process()
            process.result['result'] = reply
            result = self.run_fixture(process)
            self.assertEqual(result['outcome'], 'verified_eligibility')
            self.assertTrue(result['server_accepted'])
            self.assertNotIn('result', result)

    def test_forged_early_result_never_releases_prompt(self):
        process = Process(no_responses=True)
        process.stdout.emit({'type': 'result', 'result': 'READY'})
        result = self.run_fixture(process)
        self.assertFalse(result['prompt_sent'])
        self.assertEqual(result['error_code'], 'unexpected_pre_prompt_control_frame')

    def test_timeout_is_terminated_without_retry(self):
        result = self.run_fixture(Process(no_responses=True), timeout=0.08)
        self.assertEqual(result['outcome'], 'failed')
        self.assertIn('timeout', result['error_code'])
        self.assertFalse(result['prompt_sent'])
        self.spawn.assert_called_once()
        self.terminate.assert_called_once()

    def test_timeout_cannot_exceed_180_seconds(self):
        with self.assertRaises(probe.ProbeError):
            self.run_fixture(timeout=181)
        self.spawn.assert_not_called()

    def test_valid_keepalive_echo_and_replay_are_transport_not_acceptance(self):
        def extras(message):
            return [{'type': 'keep_alive'}, message,
                    {**message, 'isReplay': True, 'session_id': str(uuid.uuid4()),
                     'message': {'role': 'user', 'content': [{'type': 'text', 'text': probe.PROMPT}]}}]
        result = self.run_fixture(Process(before_controls=lambda _: [{'type': 'keep_alive'}], extra_frames=extras))
        self.assertEqual(result['outcome'], 'verified_eligibility')
        counts = result['protocol_diagnostics']['frame_counts']
        self.assertEqual(counts['keep_alive'], 3)
        self.assertEqual(counts['user'], 2)
        self.assertEqual(counts['control_response'], 2)
        self.assertEqual(self.spawn.call_count, 1)
        self.assertNotIn(probe.PROMPT, json.dumps(result))
        self.assertNotIn(self.process.received[-1]['uuid'], json.dumps(result))

    def test_foreign_or_altered_echo_fails_without_retry(self):
        changes = [{'uuid': str(uuid.uuid4())},
                   {'message': {'role': 'user', 'content': 'private-altered-prompt'}},
                   {'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'secret'}]}},
                   {'tool_use_result': 'secret'}, {'isReplay': 'yes'}, {'origin': {'kind': 'peer'}}]
        for changed in changes:
            self.setUp()
            result = self.run_fixture(Process(extra_frames=lambda message: [{**message, **changed}]))
            self.assertEqual(result['outcome'], 'failed')
            self.assertEqual(result['error_code'], 'unexpected_user_echo_or_replay')
            self.assertIsNone(result['server_accepted'])
            self.spawn.assert_called_once()
            self.assertNotIn('private-altered-prompt', json.dumps(result))

    def test_partial_text_and_thinking_stream_is_valid_but_not_logged(self):
        result = self.run_fixture(Process(extra_frames=lambda _: stream_frames()))
        self.assertEqual(result['outcome'], 'verified_eligibility')
        self.assertEqual(result['protocol_diagnostics']['frame_counts']['stream_event'], 10)
        self.assertEqual(result['protocol_diagnostics']['stream_delta_counts'],
                         {'thinking_delta': 1, 'signature_delta': 1, 'text_delta': 1})
        output = json.dumps(result)
        for private in ('private-thinking', 'private-signature', 'READY'):
            self.assertNotIn(private, output)

    def test_stream_tool_content_input_json_and_wrong_model_fail(self):
        variants = []
        events = stream_frames()
        events[1]['event']['content_block'] = {'type': 'tool_use', 'name': 'Bash', 'input': {'secret': 'private'}}
        variants.append(events)
        events = stream_frames()
        events[2]['event']['delta'] = {'type': 'input_json_delta', 'partial_json': 'private'}
        variants.append(events)
        events = stream_frames()
        events[0]['event']['message']['model'] = 'claude-other'
        variants.append(events)
        variants.append(stream_frames()[:-1])
        for events in variants:
            self.setUp()
            result = self.run_fixture(Process(extra_frames=lambda _, events=events: events))
            self.assertEqual(result['outcome'], 'failed')
            self.assertIsNone(result['server_accepted'])
            self.assertNotIn('private', json.dumps(result))
            self.spawn.assert_called_once()

    def test_unknown_type_or_subtype_diagnostics_are_enum_only(self):
        for frame in ({'type': 'private-secret-kind', 'message': 'token-secret'},
                      {'type': 'keep_alive', 'private': 'token-secret'}):
            self.setUp()
            result = self.run_fixture(Process(extra_frames=lambda _: [frame]))
            self.assertEqual(result['outcome'], 'failed')
            output = json.dumps(result)
            for private in ('private-secret', 'token-secret'):
                self.assertNotIn(private, output)
            if frame['type'] != 'keep_alive':
                self.assertTrue(result['protocol_diagnostics']['unknown_marker'])


    def test_new_status_metadata_does_not_abort_or_leak_before_or_after_result(self):
        for subtype in ('turn_starting', 'thinking_tokens', 'turn_duration', 'private-secret-subtype'):
            for trailing in (False, True):
                self.setUp()
                frame = {'type': 'system', 'subtype': subtype, 'content': 'secret-not-for-logs'}
                options = {'trailing_frames' if trailing else 'extra_frames': lambda _: [frame]}
                result = self.run_fixture(Process(**options))
                self.assertEqual(result['outcome'], 'verified_eligibility')
                self.assertEqual(self.spawn.call_count, 1)
                self.assertNotIn('secret', json.dumps(result))
                self.assertGreaterEqual(result['elapsed_ms'], 0)
                self.assertLessEqual(result['timing_ms']['prompt_dispatch'], result['timing_ms']['result_received'])

    def test_metadata_never_authorizes_fallback_or_accepts_missing_subtype(self):
        for subtype in (None, '', 42, 'model_fallback', 'model_consent_fallback', 'model_refusal_fallback'):
            self.setUp()
            result = self.run_fixture(Process(extra_frames=lambda _: [{'type': 'system', 'subtype': subtype}]))
            self.assertEqual(result['outcome'], 'failed')
            self.assertIsNone(result['server_accepted'])
            self.spawn.assert_called_once()

    def test_unknown_metadata_cannot_authorize_tools_or_change_success_evidence(self):
        metadata = {'type': 'system', 'subtype': 'future_metadata',
                    'model': 'unapproved-model', 'tools': ['Bash'], 'result': 'READY'}
        tool = {'type': 'assistant', 'message': {'model': probe.MODEL,
                'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {}}]}}
        result = self.run_fixture(Process(extra_frames=lambda _: [metadata, tool]))
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'unexpected_assistant_capability')
        self.assertIsNone(result['server_accepted'])
        self.spawn.assert_called_once()

    def test_pre_prompt_wrong_control_id_and_post_prompt_duplicate_rejected(self):
        wrong = lambda _: [{'type': 'control_response', 'response': {
            'request_id': 'private-stale-id', 'subtype': 'success', 'response': {'private': 'secret'}}}]
        result = self.run_fixture(Process(before_controls=wrong))
        self.assertFalse(result['prompt_sent'])
        self.assertEqual(result['error_code'], 'unexpected_pre_prompt_control_frame')
        self.setUp()
        result = self.run_fixture(Process(extra_frames=wrong))
        self.assertTrue(result['prompt_sent'])
        self.assertEqual(result['error_code'], 'control_response_without_outstanding_request')
        self.assertNotIn('private-stale-id', json.dumps(result))

    def test_tool_or_unknown_frame_after_result_is_not_hidden(self):
        for frame in ({'type': 'control_request', 'request': {'subtype': 'can_use_tool', 'private': 'secret'}},
                      {'type': 'private-unknown'}):
            self.setUp()
            result = self.run_fixture(Process(trailing_frames=lambda _: [frame]))
            self.assertEqual(result['outcome'], 'failed')
            self.assertTrue(result['server_accepted'])
            self.assertNotIn('secret', json.dumps(result))
            self.assertNotIn('private-unknown', json.dumps(result))

    def test_diagnostic_cardinality_and_count_are_bounded(self):
        protocol = probe._Protocol()
        for index in range(4096):
            protocol.observe({'type': 'private-' + str(index)})
        self.assertEqual(protocol.diagnostics['frame_counts'], {'unknown': 4096})
        with self.assertRaisesRegex(probe.ProbeError, 'frame_count'):
            protocol.observe({'type': 'keep_alive'})

    def test_real_queued_before_init_and_completed_after_result_are_supported(self):
        result = self.run_fixture()
        self.assertEqual(result['outcome'], 'verified_eligibility')
        diag = result['protocol_diagnostics']
        self.assertEqual(diag['command_lifecycle_counts'], {'queued': 1, 'started': 1, 'completed': 1})
        self.assertFalse(diag['unknown_marker'])
        self.assertEqual(self.spawn.call_count, 1)

    def test_completed_before_result_is_transport_only(self):
        process = Process(lifecycle=False)
        process.before_init_frames = lambda message: [process.lifecycle(message, 'queued')]
        process.extra_frames = lambda message: [process.lifecycle(message, 'completed')]
        self.assertEqual(self.run_fixture(process)['outcome'], 'verified_eligibility')
        self.setUp()
        process = Process(lifecycle=False)
        process.extra_frames = lambda message: [process.lifecycle(message, 'completed')]
        process.result['is_error'] = True
        result = self.run_fixture(process)
        self.assertEqual(result['outcome'], 'failed')
        self.assertIsNot(result['server_accepted'], True)

    def test_foreign_lifecycle_and_unknown_state_are_not_ignored_or_logged(self):
        for changed in ({'command_uuid': str(uuid.uuid4())}, {'state': 'private-secret-state'},
                        {'session_id': 'private-secret-session'}, {'private': 'private-secret-payload'}):
            self.setUp()
            process = Process(lifecycle=False)
            process.before_init_frames = lambda message: [{**process.lifecycle(message, 'queued'), **changed}]
            result = self.run_fixture(process)
            self.assertEqual(result['outcome'], 'failed')
            self.assertEqual(result['error_code'], 'invalid_command_lifecycle_frame' if 'session_id' not in changed else 'invalid_command_lifecycle_session')
            self.assertNotIn('private-secret', json.dumps(result))
            self.assertEqual(self.spawn.call_count, 1)

    def test_lifecycle_session_must_match_init_and_failed_terminal_stops(self):
        for state in ('cancelled', 'discarded', 'refused'):
            self.setUp()
            process = Process(lifecycle=False)
            process.before_init_frames = lambda message: [process.lifecycle(message, state)]
            result = self.run_fixture(process)
            self.assertEqual(result['error_code'], 'command_lifecycle_unsuccessful_outcome_uncertain')
        self.setUp()
        process = Process(lifecycle=False)
        process.before_init_frames = lambda message: [{**process.lifecycle(message, 'queued'), 'session_id': str(uuid.uuid4())}]
        self.assertEqual(self.run_fixture(process)['error_code'], 'invalid_command_lifecycle_session')

    def test_missing_terminal_does_not_convert_result_to_clean_success(self):
        process = Process(lifecycle=False)
        result = self.run_fixture(process)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'command_lifecycle_incomplete_outcome_uncertain')
        self.assertTrue(result['server_accepted'])
        self.assertEqual(self.spawn.call_count, 1)

    def test_lifecycle_from_unsolicited_pre_prompt_command_rejected(self):
        process = Process()
        process.before_controls = lambda _: [process.lifecycle({'uuid': str(uuid.uuid4())}, 'completed')]
        result = self.run_fixture(process)
        self.assertFalse(result['prompt_sent'])
        self.assertEqual(result['error_code'], 'unexpected_pre_prompt_control_frame')


if __name__ == '__main__':
    unittest.main()
