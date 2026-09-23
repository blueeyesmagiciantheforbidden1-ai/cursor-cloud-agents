"""No provider is invoked; a scripted in-memory subprocess speaks the protocol."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import queue
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from agent_hub.adapters import AGENTS, AdapterError, Command, build_command, build_prompt
from agent_hub.billing_policy import BillingPolicy
from agent_hub.claude_runtime import ClaudeRuntimeError, run_verified_claude, _CommandLifecycle
from agent_hub.worker import Config, WorkerError, execute_task, load_config, run_command
from tests.test_subscription_auth import approved_command


class _Output:
    def __init__(self):
        self.lines = queue.Queue()
        self.closed = False

    def emit(self, value):
        self.lines.put(json.dumps(value).encode() + b'\n')

    def readline(self, limit):
        return self.lines.get()

    def close(self):
        self.closed = True


class _Input:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, raw):
        self.process.accept(json.loads(raw))
        return len(raw)

    def flush(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self.process.returncode = 0
            self.process.stdout.lines.put(b'')


class _Process:
    def __init__(self, *, account=None, settings=None, result=None, no_responses=False):
        self.stdout, self.stderr = _Output(), io.BytesIO(b'private-raw-diagnostic')
        self.stdin = _Input(self)
        self.returncode = None
        self.pid = 12345
        self.session_id = str(uuid.uuid4())
        self.received = []
        self.no_responses = no_responses
        self.account = account if account is not None else {
            'tokenSource': 'CLAUDE_CODE_OAUTH_TOKEN', 'apiProvider': 'firstParty',
        }
        self.settings = settings if settings is not None else {
            'effective': {}, 'sources': [],
            'applied': {'model': 'claude-opus-5', 'effort': 'max', 'advisor': None, 'ultracode': False},
        }
        self.result = result if result is not None else {
            'type': 'result', 'subtype': 'success', 'is_error': False,
            'result': 'A useful fixture review.', 'modelUsage': {'claude-opus-5': {'costUSD': 0}},
        }

    def accept(self, message):
        self.received.append(message)
        if self.no_responses:
            return
        if message['type'] == 'control_request':
            body = {'account': self.account} if message['request']['subtype'] == 'initialize' else self.settings
            self.stdout.emit({'type': 'control_response', 'response': {
                'request_id': message['request_id'], 'subtype': 'success', 'response': body,
            }})
        elif message['type'] == 'user':
            self.stdout.emit(self.lifecycle(message, 'queued'))
            self.stdout.emit({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5',
                              'session_id': self.session_id})
            self.stdout.emit(self.lifecycle(message, 'started'))
            self.stdout.emit(self.result)
            self.stdout.emit(self.lifecycle(message, 'completed'))

    def lifecycle(self, message, state):
        return {'type': 'command_lifecycle', 'command_uuid': message['uuid'],
                'state': state, 'uuid': str(uuid.uuid4()), 'session_id': self.session_id}

    def poll(self):
        return self.returncode

    def terminate(self):
        self.stdin.close()


class ClaudeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.command = approved_command()
        self.policy = BillingPolicy(mode='subscription_only')
        self.selection = {'agent': 'claude', 'cli_model_id': 'claude-opus-5', 'effort': 'max',
                          'billing': 'subscription_included',
                          'account_ref': hashlib.sha256(b'owner@example.invalid').hexdigest()}
        self.heartbeat = Mock(return_value=True)
        self.containment = Mock()

    def run_fixture(self, process=None, *, timeout=2, selection=None, audit=None, **kwargs):
        process = process or _Process()
        self.process = process
        with patch('agent_hub.claude_runtime.claude_launch_context_error', return_value=audit) as context, \
                patch('agent_hub.claude_runtime.subprocess.Popen', return_value=process) as spawn:
            self.spawn = spawn
            self.context = context
            return run_verified_claude(self.command, Path('/workspace/default'), timeout,
                self.heartbeat, environment={}, selection=self.selection if selection is None else selection,
                billing_policy=self.policy, terminate_tree=lambda p: p.terminate(),
                contain_process=lambda p: self.containment, plan_expires_at=time.time() + 60, **kwargs)

    def test_task_is_sent_only_after_both_same_process_checks(self):
        code, raw = self.run_fixture()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(raw)['result'], 'A useful fixture review.')
        messages = self.process.received
        self.assertEqual([m['type'] for m in messages], ['control_request', 'control_request', 'user'])
        self.assertEqual([m['request']['subtype'] for m in messages[:2]], ['initialize', 'get_settings'])
        self.assertNotIn(self.command.stdin, json.dumps(messages[:2]))
        self.assertEqual(messages[2]['message'], {'role': 'user', 'content': self.command.stdin})
        self.assertNotIn(self.command.stdin, self.spawn.call_args.args[0])
        self.assertIn('--input-format', self.spawn.call_args.args[0])
        self.assertEqual(self.spawn.call_count, 1)
        self.heartbeat.assert_called_once()
        self.containment.close.assert_called_once()
        self.assertNotIn('private-raw-diagnostic', raw)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', raw)

    def test_wrong_provider_or_credential_never_receives_task(self):
        for changed in ({'apiProvider': 'vertex'}, {'apiProvider': 'gateway'},
                        {'tokenSource': 'ANTHROPIC_AUTH_TOKEN'}, {'apiKeySource': 'ANTHROPIC_API_KEY'},
                        {'subscriptionType': 'team'}):
            process = _Process()
            process.account.update(changed)
            with self.subTest(changed=changed), self.assertRaises(ClaudeRuntimeError):
                self.run_fixture(process)
            self.assertNotIn('user', [m['type'] for m in process.received])
            self.assertIsNotNone(process.returncode)

    def test_reported_owner_is_checked_and_missing_setup_token_email_is_not_invented(self):
        valid = _Process(account={'tokenSource': 'CLAUDE_CODE_OAUTH_TOKEN', 'apiProvider': 'firstParty',
                                  'email': ' Owner@Example.Invalid ', 'subscriptionType': 'max'})
        self.assertEqual(self.run_fixture(valid)[0], 0)
        wrong = _Process(account={**valid.account, 'email': 'wrong@example.invalid'})
        with self.assertRaisesRegex(ClaudeRuntimeError, 'different owner'):
            self.run_fixture(wrong)
        self.assertNotIn('user', [m['type'] for m in wrong.received])

    def test_changed_model_effort_or_managed_settings_never_receive_task(self):
        changes = [
            {'effective': {'env': {'ANTHROPIC_API_KEY': 'must-not-leak'}}},
            {'sources': [{'source': 'policySettings', 'settings': {'apiKeyHelper': 'must-not-execute'}}]},
            {'applied': {'model': 'claude-sonnet-5', 'effort': 'max', 'advisor': None, 'ultracode': False}},
            {'applied': {'model': 'claude-opus-5', 'effort': 'high', 'advisor': None, 'ultracode': False}},
            {'applied': {'model': 'claude-opus-5', 'effort': 'max', 'advisor': 'other', 'ultracode': False}},
            {'errors': [{'message': 'private-config'}]},
        ]
        for changed in changes:
            process = _Process()
            process.settings.update(changed)
            with self.subTest(changed=changed), self.assertRaises(ClaudeRuntimeError) as caught:
                self.run_fixture(process)
            self.assertNotIn('user', [m['type'] for m in process.received])
            self.assertNotIn('must-not-', str(caught.exception))
            self.assertNotIn('private-config', str(caught.exception))

    def test_cancellation_before_task_release_stops_process_without_prompt(self):
        self.heartbeat.return_value = False
        code, text = self.run_fixture()
        self.assertEqual(code, 124)
        self.assertIn('cancelled', text)
        self.assertNotIn('user', [m['type'] for m in self.process.received])
        self.assertIsNotNone(self.process.returncode)

    def test_unresponsive_handshake_times_out_and_is_terminated(self):
        code, text = self.run_fixture(_Process(no_responses=True), timeout=0.05)
        self.assertEqual(code, 124)
        self.assertIn('timeout', text)
        self.assertNotIn('user', [m['type'] for m in self.process.received])
        self.assertIsNotNone(self.process.returncode)

    def test_local_audit_failure_does_not_spawn_inference_process(self):
        with self.assertRaisesRegex(ClaudeRuntimeError, 'local authentication'):
            self.run_fixture(audit='unverified_native_binary')
        self.spawn.assert_not_called()

    def test_unknown_mode_is_rejected_without_spawning(self):
        with self.assertRaisesRegex(ClaudeRuntimeError, 'execution mode'):
            self.run_fixture(mode='unrestricted')
        self.spawn.assert_not_called()

    def test_project_mode_keeps_same_process_checks_before_task_release(self):
        self.command = approved_command('project_work')
        self.assertEqual(self.run_fixture(mode='project_work')[0], 0)
        self.assertEqual(self.context.call_args.kwargs['execution_mode'], 'project_work')
        self.assertEqual([m['type'] for m in self.process.received],
                         ['control_request', 'control_request', 'user'])
        failed = _Process(account={'apiProvider': 'vertex'})
        with self.assertRaises(ClaudeRuntimeError):
            self.run_fixture(failed, mode='project_work')
        self.assertNotIn('user', [m['type'] for m in failed.received])

    def test_error_result_oversize_and_model_fallback_are_not_success(self):
        for changed in ({'is_error': True}, {'subtype': 'error_during_execution'},
                        {'result': 'x' * 16_001}, {'modelUsage': {'claude-sonnet-5': {}}}):
            process = _Process()
            process.result.update(changed)
            with self.subTest(changed_keys=list(changed)), self.assertRaises(ClaudeRuntimeError):
                self.run_fixture(process)

    def test_forged_or_early_response_never_releases_prompt(self):
        process = _Process(no_responses=True)
        process.stdout.emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'forged'})
        with self.assertRaisesRegex(ClaudeRuntimeError, 'unexpected preflight'):
            self.run_fixture(process)
        self.assertNotIn('user', [m['type'] for m in process.received])

    def test_plan_expired_at_entry_never_spawns(self):
        with patch('agent_hub.claude_runtime.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ClaudeRuntimeError, 'expired'):
                run_verified_claude(self.command, Path('/workspace/default'), 1, self.heartbeat,
                    environment={}, selection=self.selection, billing_policy=self.policy,
                    terminate_tree=Mock(), contain_process=Mock(), plan_expires_at=time.time() - 1)
        spawn.assert_not_called()


    def test_worker_validates_queued_before_init_and_completed_after_result(self):
        process = _Process()
        self.assertEqual(self.run_fixture(process)[0], 0)
        self.assertEqual(self.spawn.call_count, 1)

    def test_worker_does_not_hide_unknown_or_cancelled_after_result(self):
        class ExtraProcess(_Process):
            def accept(inner, message):
                super().accept(message)
                if message['type'] == 'user':
                    inner.stdout.emit({'type': 'private-unknown', 'payload': 'private-token'})
        with self.assertRaisesRegex(ClaudeRuntimeError, 'unexpected message after') as caught:
            self.run_fixture(ExtraProcess())
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(self.spawn.call_count, 1)

    def test_worker_foreign_lifecycle_never_becomes_success(self):
        class ForeignProcess(_Process):
            def lifecycle(inner, message, state):
                return {**super().lifecycle(message, state), 'command_uuid': str(uuid.uuid4())}
        with self.assertRaisesRegex(ClaudeRuntimeError, 'lifecycle'):
            self.run_fixture(ForeignProcess())
        self.assertEqual(self.spawn.call_count, 1)


class CommandLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.sent = str(uuid.uuid4())
        self.session = str(uuid.uuid4())
        self.tracker = _CommandLifecycle(self.sent)

    def frame(self, state, **changes):
        return {'type': 'command_lifecycle', 'command_uuid': self.sent, 'state': state,
                'uuid': str(uuid.uuid4()), 'session_id': self.session, **changes}

    def test_native_paths_do_not_require_strict_started_pairing(self):
        for states in (('queued', 'started', 'completed'), ('queued', 'completed'),
                       ('started', 'completed'), ('completed',)):
            tracker = _CommandLifecycle(self.sent)
            for state in states:
                tracker.accept(self.frame(state))
            tracker.finish()

    def test_foreign_ids_extra_payload_and_unknown_states_reject_without_echo(self):
        for changes in ({'command_uuid': str(uuid.uuid4())}, {'uuid': 'private-secret'},
                        {'session_id': 'private-session'}, {'state': 'private-state'},
                        {'payload': 'private-token'}, {'state': []}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(ClaudeRuntimeError) as caught:
                _CommandLifecycle(self.sent).accept({**self.frame('queued'), **changes})
            self.assertNotIn('private', str(caught.exception))

    def test_duplicate_reverse_or_second_terminal_rejected(self):
        for states in (('queued', 'queued'), ('started', 'queued'),
                       ('completed', 'started'), ('completed', 'completed')):
            tracker = _CommandLifecycle(self.sent)
            tracker.accept(self.frame(states[0]))
            with self.assertRaisesRegex(ClaudeRuntimeError, 'transition'):
                tracker.accept(self.frame(states[1]))

    def test_native_frame_uuid_and_session_are_bound(self):
        first = self.frame('queued')
        self.tracker.accept(first)
        for changed in ({'uuid': first['uuid']}, {'session_id': str(uuid.uuid4())}):
            with self.assertRaises(ClaudeRuntimeError):
                self.tracker.accept(self.frame('started', **changed))

    def test_unsuccessful_and_missing_terminal_are_not_success_or_retry(self):
        for state in ('cancelled', 'discarded', 'refused'):
            with self.assertRaisesRegex(ClaudeRuntimeError, 'uncertain'):
                _CommandLifecycle(self.sent).accept(self.frame(state))
        for states in ((), ('queued',), ('queued', 'started')):
            tracker = _CommandLifecycle(self.sent)
            for state in states:
                tracker.accept(self.frame(state))
            with self.assertRaisesRegex(ClaudeRuntimeError, 'incomplete'):
                tracker.finish()


class ClaudeWorkerIntegrationTests(unittest.TestCase):
    def test_task_cannot_supply_or_bypass_trusted_local_model_plan(self):
        with tempfile.TemporaryDirectory(prefix='hub-model-binding-') as td:
            root = Path(td).resolve()
            config = Config('https://hub.example.invalid', 'claude', 'fixture-hub-token',
                            'HUB_AGENT_TOKEN', {'default': root},
                            billing_policy=BillingPolicy(mode='subscription_only'),
                            model_policy_required=True, model_policy_bundle=root / 'bundle.json',
                            expected_account_ref='a' * 64)
            command = approved_command()
            selected = replace(command, argv=command.argv + ('--fixture-selected',))
            plan = {'selections': {'claude': {'agent': 'claude'}}, 'expires_at': time.time() + 60}
            client = Mock(identity_token=None)
            client.post.return_value = {'active': True}
            task = {'room_id': 'fixture-room', 'lease_token': 'fixture-lease', 'workspace': 'default',
                    'prompt': 'Review', 'messages': [], 'timeout_seconds': 30,
                    'model_plan': {'forged': True}, 'model_policy_required': False,
                    'model_policy_bundle': '/attacker/bundle.json', 'expected_account_ref': 'b' * 64,
                    'execution_mode': 'project_work', 'model_policy_agents': ['claude']}
            with patch('agent_hub.worker.build_command', return_value=command), \
                    patch('agent_hub.model_runtime.select_worker_model', return_value=(selected, plan)) as select, \
                    patch('agent_hub.worker.run_command', return_value=(0, '{"result":"fixture review"}')) as run:
                self.assertEqual(execute_task(config, client, task), 0)
            select.assert_called_once_with('claude', root / 'bundle.json', 'a' * 64, command,
                                           execution_mode='read_only', active_agents=tuple(AGENTS))
            self.assertEqual(run.call_args.args[0], selected)
            self.assertIs(run.call_args.kwargs['model_plan'], plan)
            self.assertEqual(run.call_args.kwargs['execution_mode'], 'read_only')
            with patch('agent_hub.worker.build_command', return_value=command), \
                    patch('agent_hub.model_runtime.select_worker_model', side_effect=ValueError('stale model catalog')), \
                    patch('agent_hub.worker.run_command') as run:
                self.assertEqual(execute_task(config, client, task), 1)
            run.assert_not_called()

    def test_model_plan_selects_verified_runtime_and_strips_controller_secrets(self):
        plan = {'selections': {'claude': {'agent': 'claude'}}, 'expires_at': time.time() + 60}
        command = approved_command()
        with patch.dict('os.environ', {'HUB_TOKEN': 'secret', 'WORKER_PRIVATE': 'secret', 'CLAUDE_CODE_OAUTH_TOKEN': 'fixture'}, clear=True), \
                patch('agent_hub.worker.run_verified_claude', return_value=(0, '{"result":"ok"}')) as runtime:
            self.assertEqual(run_command(command, Path('/workspace/default'), 2, lambda: True,
                private_env=('WORKER_PRIVATE',), billing_policy=BillingPolicy(mode='subscription_only'),
                agent_id='claude', model_plan=plan)[0], 0)
        environment = runtime.call_args.kwargs['environment']
        self.assertNotIn('HUB_TOKEN', environment)
        self.assertNotIn('WORKER_PRIVATE', environment)
        self.assertEqual(environment['CLAUDE_CODE_OAUTH_TOKEN'], 'fixture')
        self.assertEqual(runtime.call_args.kwargs['selection'], plan['selections']['claude'])

    def test_project_mode_cannot_use_unverified_generic_process_path(self):
        for agent, plan in (('claude', None), ('codex', {'selections': {}})):
            with patch('agent_hub.worker.subprocess.Popen') as spawn, \
                    patch('agent_hub.worker.run_verified_claude') as runtime:
                with self.assertRaisesRegex(WorkerError, 'Project work'):
                    run_command(approved_command('project_work'), Path('/workspace/default'),
                                1, lambda: True, execution_mode='project_work',
                                agent_id=agent, model_plan=plan)
            spawn.assert_not_called()
            runtime.assert_not_called()

    def test_project_mode_adapter_and_prompt_are_operator_selected(self):
        task = {'prompt': 'Fix project tests', 'execution_mode': 'project_work'}
        self.assertIn('read-only', build_prompt(task, 'claude'))
        self.assertNotIn('read-only', build_prompt(task, 'claude', execution_mode='project_work'))
        with patch('agent_hub.adapters.resolve_executable', return_value=('/native/claude',)):
            command = build_command('claude', 'task', execution_mode='project_work')
        self.assertEqual(command.argv[command.argv.index('--tools') + 1],
                         'Read,Grep,Glob,Edit,Write,Bash')
        for agent in ('codex', 'cursor', 'copilot', 'grok'):
            with self.subTest(agent=agent), self.assertRaises(AdapterError):
                build_command(agent, 'task', execution_mode='project_work')

    def test_local_model_policy_config_validates_types_and_required_dependencies(self):
        with tempfile.TemporaryDirectory(prefix='hub-model-config-') as td:
            root = Path(td)
            config_path = root / 'worker.json'
            data = {'hub_url': 'https://example.run.app', 'agent_id': 'claude',
                    'workspaces': {'default': str(root)}, 'billing_policy': {'mode': 'subscription_only'},
                    'model_policy_required': True, 'model_policy_bundle': str(root / 'bundle.json'),
                    'expected_account_ref': 'a' * 64}
            config_path.write_text(json.dumps(data))
            with patch.dict('os.environ', {}, clear=True):
                config = load_config(str(config_path), dry_run=True)
                self.assertTrue(config.model_policy_required)
                self.assertEqual(config.model_policy_bundle, root / 'bundle.json')
                self.assertEqual(config.execution_mode, 'read_only')
                self.assertEqual(config.model_policy_agents, tuple(AGENTS))
                for changed in ({'model_policy_required': 'true'}, {'model_policy_bundle': 'relative.json'},
                                {'model_policy_bundle': None}, {'expected_account_ref': 'owner@example.invalid'},
                                {'billing_policy': {'mode': 'provider_default'}}):
                    config_path.write_text(json.dumps({**data, **changed}))
                    with self.subTest(changed=changed), self.assertRaises(WorkerError):
                        load_config(str(config_path), dry_run=True)
                project = {**data, 'execution_mode': 'project_work', 'model_policy_agents': ['claude']}
                config_path.write_text(json.dumps(project))
                config = load_config(str(config_path), dry_run=True)
                self.assertEqual(config.execution_mode, 'project_work')
                self.assertEqual(config.model_policy_agents, ('claude',))
                for changed in ({'execution_mode': 'unrestricted'}, {'model_policy_required': False},
                                {'agent_id': 'codex'}, {'model_policy_agents': []},
                                {'model_policy_agents': ['claude', 'claude']},
                                {'model_policy_agents': ['grok']}, {'model_policy_agents': ['unknown']},
                                {'model_policy_agents': 'claude'}, {'model_policy_agents': [{}]}):
                    config_path.write_text(json.dumps({**project, **changed}))
                    with self.subTest(changed=changed), self.assertRaises(WorkerError):
                        load_config(str(config_path), dry_run=True)


if __name__ == '__main__':
    unittest.main()
