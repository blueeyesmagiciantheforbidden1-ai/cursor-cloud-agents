"""Offline first-LIVE Codex lifecycle tests; no native/provider/cloud execution."""
import copy
from collections import deque
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / 'codex-cloud-transport'))
sys.path.insert(0, str(ROOT))
from providers import codex as c

EMAIL = 'cursor-owner@example.invalid'
BACKEND = 'synthetic-account-id'
CONFIG = {'forced_login_method': 'chatgpt', 'cli_auth_credentials_store': 'file',
          'model_provider': 'openai', 'approval_policy': 'never', 'sandbox_mode': 'read-only'}


def rates(percentage=12):
    return {'accountId': BACKEND, 'ordinaryUsageAllowed': True,
            'rateLimitsByLimitId': {'codex': {'limitId': 'codex', 'spendControlReached': False,
                'credits': {'hasCredits': False, 'unlimited': False, 'balance': '0'},
                'primary': {'usedPercent': percentage, 'resetsAt': int(time.time()) + 3600}}}}


def model():
    return {'model': c.MODEL, 'hidden': False,
            'supportedReasoningEfforts': [{'reasoningEffort': 'high'}, {'reasoningEffort': 'ultra'}]}


class FakeProcess:
    def __init__(self):
        self.exit_code = None

    def poll(self):
        return self.exit_code


class FixtureRPC(c.WarmRPC):
    """Use production request/parser/gate/result paths; replace only native IO."""
    def __init__(self, home, renew, *, timeout_seconds, execution_mode):
        self.home, self.renew = home, renew
        self.deadline = time.monotonic() + timeout_seconds
        self.next_renew = time.monotonic() + 25
        self.protocol_state, self.index = 'new', 0
        self.turn_submitted, self.clean_shutdown = False, False
        self.pending_events, self.passive_notifications = [], []
        self.event_count = 0
        self.frames, self.wire = queue.Queue(), deque()
        self.failed = threading.Event()
        self.readers, self.writers = [], []
        self.process = FakeProcess()
        self.requests, self.order = [], []
        self.account = {'requiresOpenaiAuth': True,
                        'account': {'type': 'chatgpt', 'email': EMAIL, 'planType': 'pro'}}
        self.catalog, self.rates, self.config = [model()], rates(), copy.deepcopy(CONFIG)
        self.answer, self.stop_error, self.fail_prompt = 'A real-parser offline answer.', False, False
        self.refresh_notification = False
        self.bad_tool = False

    def _send(self, value=None, *, close=False):
        if close:
            self.process.exit_code = 0
            return
        if 'id' not in value:
            return
        self.requests.append(copy.deepcopy(value))
        method, params = value['method'], value['params']
        responses = {'initialize': {}, 'account/read': self.account,
                     'model/list': {'data': self.catalog, 'nextCursor': None},
                     'account/rateLimits/read': self.rates, 'config/read': {'config': self.config},
                     'configRequirements/read': {'requirements': None}}
        if method in responses:
            result = responses[method]
            if self.refresh_notification and self.protocol_state == 'metadata':
                self.wire.append({'method': 'thread/status/changed', 'params': {'threadId': 'thread-live'}})
                self.refresh_notification = False
        elif method == 'thread/start':
            result = {'model': c.MODEL, 'modelProvider': 'openai', 'reasoningEffort': c.EFFORT,
                      'cwd': '/workspace/default', 'approvalPolicy': 'never',
                      'sandbox': {'type': 'readOnly', 'networkAccess': False},
                      'thread': {'id': 'thread-live', 'ephemeral': True, 'turns': []}}
        elif method == 'turn/start':
            if self.fail_prompt:
                self.wire.append({'id': 999, 'result': {}})
                return
            result = {'turn': {'id': 'turn-live', 'status': 'inProgress'}}
        else:
            raise AssertionError('unexpected fixture method')
        self.wire.append({'id': value['id'], 'result': copy.deepcopy(result)})
        if method == 'turn/start':
            item = {'id': 'answer-1', 'type': 'agentMessage', 'text': self.answer, 'phase': 'final_answer'}
            if self.bad_tool:
                item = {'id': 'tool-1', 'type': 'commandExecution'}
            self.wire.extend([
                {'method': 'item/completed', 'params': {'threadId': 'thread-live', 'turnId': 'turn-live', 'item': item}},
                {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread-live', 'turnId': 'turn-live',
                    'tokenUsage': {'last': {'inputTokens': 12, 'outputTokens': 5, 'cachedInputTokens': 0,
                                          'reasoningOutputTokens': 2, 'totalTokens': 17}}}},
                {'method': 'turn/completed', 'params': {'threadId': 'thread-live',
                    'turn': {'id': 'turn-live', 'status': 'completed', 'error': None}}},
            ])

    def _frame(self):
        self.tick()
        if not self.wire:
            raise c.transport.TransportError('native_exited_before_completion')
        return self.wire.popleft()

    def stop_group(self):
        self.order.append('stop')
        if self.stop_error:
            raise OSError('SYNTHETIC_PRIVATE_STOP_ERROR')
        self.process.exit_code = 0


class CodexLive(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='runcrew-codex-offline-')  # system temp: the image app dir is read-only
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / 'home'
        self.home.mkdir()
        self.native = None
        self.mutate = lambda native: None
        lease = SimpleNamespace(profile='blueeyes', account_ref=c.metadata.OWNER_REFS['blueeyes'],
            canonical_account_ref=c.metadata.canonical_account_ref(BACKEND),
            execution='synthetic-job/executions/exact', execution_uid='synthetic-uid')
        broker = SimpleNamespace(execution=lease.execution, execution_uid=lease.execution_uid,
            assert_current=Mock(), renew=Mock(), quarantine=Mock())
        self.session = SimpleNamespace(state='active', home=self.home, lease=lease, broker=broker)

        def finish(**kwargs):
            self.assertEqual(kwargs, {'native_stopped': True})
            self.assertIsNotNone(self.native.process.poll())
            self.native.order.append('commit-release')
            self.session.state = 'committed'
            return 'synthetic-version'

        self.session.finish = Mock(side_effect=finish)

        def factory(*args, **kwargs):
            self.native = FixtureRPC(*args, **kwargs)
            self.mutate(self.native)
            return self.native

        self.addCleanup(patch.stopall)
        patch.object(c, 'WarmRPC', side_effect=factory).start()
        patch.object(c, 'CONFIG_SHA', c.protocol_gate.digest(CONFIG)).start()
        self.heartbeat = Mock(return_value=True)

    def prepare(self):
        return c.prepare(self.session, self.heartbeat, time.monotonic() + 120)

    def prompt_count(self):
        return sum(r['method'] == 'turn/start' for r in self.native.requests)

    def test_prepare_is_model_ready_without_any_turn_or_inference(self):
        handle = self.prepare()
        self.assertEqual(handle.state, 'ready')
        self.assertEqual((handle.model, handle.effort), ('gpt-6-astra', 'ultra'))
        self.assertTrue(handle.readiness['ready_for_project_prompt'])
        self.assertFalse(handle.readiness['full_coding_ready'])
        self.assertFalse(handle.readiness['automatic_improvement_ready'])
        self.assertEqual(self.prompt_count(), 0)
        self.assertEqual(sum(r['method'] == 'thread/start' for r in self.native.requests), 1)
        self.assertEqual((self.home / 'config.toml').read_text(), c.metadata.CONFIG)
        self.session.finish.assert_not_called()
        c.close(handle)
        c.close(handle)
        self.assertEqual(self.native.order, ['stop', 'commit-release'])

    def test_unknown_percentage_is_preserved_with_actual_zero_credit_enforcement(self):
        self.mutate = lambda native: setattr(native, 'rates', rates(None))
        handle = self.prepare()
        self.assertIsNone(handle.usage['included_used_percent'])
        self.assertFalse(handle.usage['extra_spending_enabled'])
        result = c.execute(handle, 'An explicit project question.', time.monotonic() + 180)
        self.assertIsNone(result['preflight']['quota']['included_used_percent'])
        self.assertEqual(result['usage']['outputTokens'], 5)
        self.assertEqual(self.native.order, ['stop', 'commit-release'])

    def test_success_rechecks_same_account_and_catalog_then_commits_before_return(self):
        handle = self.prepare()
        before = len(self.native.requests)
        self.native.refresh_notification = True
        result = c.execute(handle, 'Project task.', time.monotonic() + 180)
        methods = [r['method'] for r in self.native.requests[before:]]
        self.assertEqual(methods, ['account/read', 'model/list', 'account/rateLimits/read',
                                  'config/read', 'configRequirements/read', 'turn/start'])
        self.assertEqual(result['text'], self.native.answer)
        self.assertEqual(result['credential_writeback'], 'committed')
        self.assertTrue(result['native_stopped'])
        self.assertFalse(result['model_execution_attested'])
        self.assertFalse(result['automatic_retry'])
        self.assertEqual(self.prompt_count(), 1)
        with self.assertRaises(c.LiveCodexError):
            c.execute(handle, 'No second task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 1)

    def test_prepared_session_cannot_be_prepared_again_under_same_grant(self):
        handle = self.prepare()
        with self.assertRaisesRegex(c.LiveCodexError, 'native_session_already_prepared'):
            self.prepare()
        self.assertEqual(self.prompt_count(), 0)
        c.close(handle)

    def test_improvement_is_rejected_even_when_all_usage_is_known(self):
        handle = self.prepare()
        with self.assertRaisesRegex(c.LiveCodexError, 'automatic_improvement_not_enabled'):
            c.execute(handle, 'Improve yourself.', time.monotonic() + 180, task_kind='improvement')
        self.assertEqual(self.prompt_count(), 0)
        self.assertEqual(handle.state, 'closed')

    def test_native_included_permission_is_required_and_no_credits_are_consumed(self):
        handle = self.prepare()
        self.native.rates['ordinaryUsageAllowed'] = False
        with self.assertRaises(c.LiveCodexError):
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 0)
        self.assertEqual(self.session.finish.call_count, 1)
        self.assertTrue(all(r['method'] not in ('account/rateLimitResetCredit/consume', 'account/login/start')
                            for r in self.native.requests))

    def test_owner_change_during_idle_quarantines_without_committing_changed_credentials(self):
        handle = self.prepare()
        self.native.account['account']['email'] = 'different@example.invalid'
        with self.assertRaises(c.LiveCodexError):
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 0)
        self.session.finish.assert_not_called()
        self.session.broker.quarantine.assert_called_once()
        self.assertEqual(handle.state, 'quarantined')

    def test_unknown_or_extra_billing_routes_are_rejected(self):
        canonical = self.session.lease.canonical_account_ref
        changes = [('hasCredits', True), ('unlimited', True), ('balance', '0.01'),
                   ('balance', None), ('balance', 'NaN'), ('balance', 0)]
        for key, value in changes:
            candidate = rates(None)
            candidate['rateLimitsByLimitId']['codex']['credits'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(c.LiveCodexError):
                c._quota(candidate, canonical)
        for value in (None, False, 1):
            candidate = rates(None); candidate['ordinaryUsageAllowed'] = value
            with self.subTest(permission=value), self.assertRaises(c.LiveCodexError):
                c._quota(candidate, canonical)

    def test_known_exhaustion_bad_reset_and_boolean_percentages_fail(self):
        canonical = self.session.lease.canonical_account_ref
        for percentage in (100, 101, True, False, -1, float('nan')):
            with self.subTest(percentage=percentage), self.assertRaises(c.LiveCodexError):
                c._quota(rates(percentage), canonical)
        candidate = rates(0); candidate['rateLimitsByLimitId']['codex']['primary']['resetsAt'] = int(time.time()) - 1
        with self.assertRaises(c.LiveCodexError): c._quota(candidate, canonical)

    def test_catalog_drift_blocks_prompt_without_lower_model_fallback(self):
        handle = self.prepare()
        self.native.catalog[0]['supportedReasoningEfforts'] = [{'reasoningEffort': 'high'}]
        with self.assertRaises(c.LiveCodexError):
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 0)

    def test_effective_config_change_blocks_prompt(self):
        handle = self.prepare()
        self.native.config['forced_login_method'] = 'api'
        with self.assertRaises(c.LiveCodexError):
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 0)

    def test_unknown_turn_response_remains_uncertain_and_is_never_replayed(self):
        handle = self.prepare(); self.native.fail_prompt = True
        with self.assertRaises(c.LiveCodexError) as caught:
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertTrue(caught.exception.model_call_attempted)
        self.assertEqual(caught.exception.credential_writeback, 'committed')
        self.assertEqual(self.prompt_count(), 1)
        with self.assertRaises(c.LiveCodexError): c.execute(handle, 'No replay.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 1)

    def test_tool_items_are_rejected_in_first_live_prompt_mode(self):
        handle = self.prepare(); self.native.bad_tool = True
        with self.assertRaisesRegex(c.LiveCodexError, 'tool_observed_during_read_only_review'):
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 1)

    def test_stop_uncertainty_never_commits_or_exposes_private_error(self):
        handle = self.prepare(); self.native.stop_error = True
        with self.assertRaisesRegex(c.LiveCodexError, '^credential_reconciliation_required$') as caught:
            c.close(handle)
        self.assertNotIn('SYNTHETIC_PRIVATE', str(caught.exception))
        self.session.finish.assert_not_called()
        self.assertEqual(handle.state, 'quarantined')
        with self.assertRaises(c.LiveCodexError): c.close(handle)
        self.assertEqual(self.native.order, ['stop'])

    def test_commit_uncertainty_is_not_retried(self):
        handle = self.prepare()
        self.session.finish.side_effect = RuntimeError('SYNTHETIC_PRIVATE_COMMIT_ERROR')
        with self.assertRaises(c.LiveCodexError): c.close(handle)
        with self.assertRaises(c.LiveCodexError): c.close(handle)
        self.assertEqual(self.session.finish.call_count, 1)
        self.assertEqual(handle.state, 'quarantined')

    def test_idle_hub_heartbeat_loss_keeps_the_warm_process(self):
        handle = self.prepare()
        self.heartbeat.return_value = False
        handle.next_renew = 0
        readiness = c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.assertTrue(readiness['ready_for_project_prompt'])
        self.assertEqual(self.prompt_count(), 0)
        self.session.finish.assert_not_called()
        self.heartbeat.return_value = True
        c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.session.broker.renew.assert_called()
        c.close(handle)

    def test_idle_maintenance_renews_without_inference_and_drains_expiry(self):
        handle = self.prepare(); handle.next_renew = 0
        count = len(self.native.requests)
        c.maintain(handle)
        self.assertEqual(len(self.native.requests), count)
        self.assertGreaterEqual(self.session.broker.renew.call_count, 2)
        handle.warm_deadline = time.monotonic() - 1
        with self.assertRaisesRegex(c.LiveCodexError, 'warm_session_expired'): c.maintain(handle)
        self.assertEqual(handle.state, 'closed')
        self.assertEqual(self.prompt_count(), 0)

    def test_dead_idle_process_is_not_advertised_as_ready_after_maintenance(self):
        handle = self.prepare(); self.native.process.exit_code = 1
        with self.assertRaisesRegex(c.LiveCodexError, 'native_not_running'): c.maintain(handle)
        self.assertFalse(handle.readiness['ready_for_project_prompt'])

    def test_unrequested_work_before_prompt_is_rejected_before_send(self):
        handle = self.prepare()
        self.native.frames.put(json.dumps({'method': 'turn/started', 'params': {
            'threadId': 'thread-live', 'turn': {'id': 'unrequested', 'status': 'inProgress'}}}).encode())
        with self.assertRaisesRegex(c.LiveCodexError, 'native_work_before_prompt'): c.maintain(handle)
        self.assertEqual(self.prompt_count(), 0)

    def test_large_project_prompt_preserves_all_bytes_and_native_turn_bound(self):
        handle = self.prepare(); prompt = 'x' * 200000
        result = c.execute(handle, prompt, time.monotonic() + 899)
        sent = next(row for row in self.native.requests if row['method'] == 'turn/start')
        self.assertEqual(sent['params']['input'][0]['text'], prompt)
        self.assertEqual(result['prompt_sha256'], c.hashlib.sha256(prompt.encode()).hexdigest())
        self.assertLessEqual(self.native.deadline - time.monotonic(), 600)

    def test_large_answer_is_not_truncated_into_success(self):
        handle = self.prepare(); self.native.answer = 'x' * 15001
        with self.assertRaisesRegex(c.LiveCodexError, 'answer_exceeds_hub_limit'):
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 1)

    def test_concurrent_or_reentrant_operations_are_denied(self):
        handle = self.prepare(); handle.lock.acquire()
        with self.assertRaisesRegex(c.LiveCodexError, 'concurrent_provider_operation'): c.maintain(handle)
        self.assertEqual(self.prompt_count(), 0)
        handle.lock.release(); c.close(handle)


class WireWrites(unittest.TestCase):
    def test_real_sender_completes_only_unwritten_suffix_of_large_prompt(self):
        class ShortWriter(io.BytesIO):
            def write(self, data):
                return super().write(bytes(data[:17]))
        native = object.__new__(c.WarmRPC)
        native.process = SimpleNamespace(stdin=ShortWriter())
        native.deadline, native.writers = time.monotonic() + 5, []
        native.tick = lambda: None
        message = {'id': 8, 'method': 'turn/start', 'params': {'text': '\u00e9' * 40000}}
        c.WarmRPC._send(native, message)
        self.assertEqual(json.loads(native.process.stdin.getvalue()), message)
        self.assertEqual(native.process.stdin.getvalue().count(b'\n'), 1)

    def test_real_sender_rejects_zero_write_progress(self):
        native = object.__new__(c.WarmRPC)
        native.process = SimpleNamespace(stdin=SimpleNamespace(write=lambda data: 0, flush=lambda: None))
        native.deadline, native.writers = time.monotonic() + 5, []
        native.tick = lambda: None
        with self.assertRaisesRegex(c.LiveCodexError, 'native_input_failed'):
            c.WarmRPC._send(native, {'id': 1, 'method': 'initialize', 'params': {}})


if __name__ == '__main__':
    unittest.main()
