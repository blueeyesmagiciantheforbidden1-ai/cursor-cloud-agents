"""Offline first-LIVE Codex lifecycle tests; no native/provider/cloud execution."""
import copy
from collections import deque
from datetime import datetime
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
sys.path.insert(0, str(ROOT.parent / 'agent-hub'))
sys.path.insert(0, str(ROOT.parent / 'codex-cloud-transport'))
sys.path.insert(0, str(ROOT))
import broker_renew
import live_loop
from agent_hub.cloud_credential_broker import BrokerError, Conflict, MutationUncertain
from agent_hub.credential_broker_service import BoundaryError
from providers import codex as c

EMAIL = 'cursor-owner@example.invalid'


class StepClock:
    """Monotonic stand-in. It moves only when a test calls advance()."""

    def __init__(self, now=1_000_000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds=1.0):
        self.now += seconds
        return self.now


def bind_lease_clock(handle, clock):
    """for_lease captured time.monotonic at import, so rebind before the scenario."""
    handle.lease_clock.clock = clock
    handle.lease_clock.renewed_at = clock()
    return clock()
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
        self.turn_error = None

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
            if self.turn_error is not None:
                self.wire.append({'method': 'turn/completed', 'params': {
                    'threadId': 'thread-live', 'turn': {'id': 'turn-live', 'status': 'failed',
                                                       'error': copy.deepcopy(self.turn_error)}}})
                return
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
        self._grant = 0
        self.native = None
        self.mutate = lambda native: None
        self._install_session(self.home)

        def factory(*args, **kwargs):
            self.native = FixtureRPC(*args, **kwargs)
            self.mutate(self.native)
            return self.native

        self.addCleanup(patch.stopall)
        patch.object(c, 'WarmRPC', side_effect=factory).start()
        patch.object(c, 'CONFIG_SHA', c.protocol_gate.digest(CONFIG)).start()
        self.heartbeat = Mock(return_value=True)

    def _install_session(self, home):
        """One broker grant. A second prepare on the same session is rejected."""
        lease = SimpleNamespace(profile='blueeyes', account_ref=c.metadata.OWNER_REFS['blueeyes'],
            canonical_account_ref=c.metadata.canonical_account_ref(BACKEND),
            execution='synthetic-job/executions/exact', execution_uid='synthetic-uid')
        broker = SimpleNamespace(execution=lease.execution, execution_uid=lease.execution_uid,
            assert_current=Mock(), renew=Mock(), quarantine=Mock())
        session = SimpleNamespace(state='active', home=home, lease=lease, broker=broker)

        def finish(**kwargs):
            self.assertEqual(kwargs, {'native_stopped': True})
            self.assertIsNotNone(self.native.process.poll())
            self.native.order.append('commit-release')
            session.state = 'committed'
            return 'synthetic-version'

        session.finish = Mock(side_effect=finish)
        self.session = session
        self.heartbeat = Mock(return_value=True)
        self.native = None

    def _fresh_session(self):
        self._grant += 1
        home = Path(self.temporary.name) / f'home-{self._grant}'
        home.mkdir()
        self._install_session(home)

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

    def test_quota_carries_window_duration_mins_and_observed_at(self):
        """Alpha runs this: codex.py imports private metadata/codex-cloud-transport (not on Demand)."""
        canonical = self.session.lease.canonical_account_ref
        value = rates(12)
        bucket = value['rateLimitsByLimitId']['codex']
        bucket['primary']['windowDurationMins'] = 300
        bucket['secondary'] = {
            'usedPercent': 40, 'resetsAt': int(time.time()) + 86400,
            'windowDurationMins': 10080,
        }
        got = c._quota(value, canonical)
        self.assertIn('observed_at', got)
        self.assertIsInstance(got['observed_at'], str)
        datetime.fromisoformat(got['observed_at'].replace('Z', '+00:00'))
        by_name = {row['window']: row for row in got['windows']}
        self.assertEqual(by_name['primary']['window_duration_mins'], 300)
        self.assertEqual(by_name['secondary']['window_duration_mins'], 10080)

    def test_quota_invalid_window_duration_mins_stored_as_none(self):
        """Alpha runs this: codex.py imports private metadata/codex-cloud-transport (not on Demand).

        Existing test_codex assertions comparing the whole preflight or quota dict may need
        the two new keys (window_duration_mins, observed_at). Whole-dict equality assertions
        found in this file: none. Key-level quota/preflight checks that Alpha should note:
        tests/test_codex.py:230 (handle.usage['included_used_percent']),
        tests/test_codex.py:233 (result['preflight']['quota']['included_used_percent']),
        tests/test_codex.py:322-327 (observed_at / window_duration_mins key checks).
        """
        canonical = self.session.lease.canonical_account_ref
        for bad in (0, 600000, '300', 12.5, True):
            candidate = rates(12)
            candidate['rateLimitsByLimitId']['codex']['primary']['windowDurationMins'] = bad
            with self.subTest(duration=bad):
                got = c._quota(candidate, canonical)
                by_name = {row['window']: row for row in got['windows']}
                self.assertIsNone(by_name['primary']['window_duration_mins'])
        value = rates(12)
        bucket = value['rateLimitsByLimitId']['codex']
        bucket['primary']['windowDurationMins'] = 300
        bucket['secondary'] = {
            'usedPercent': 40, 'resetsAt': int(time.time()) + 86400,
            'windowDurationMins': 10080,
        }
        got = c._quota(value, canonical)
        by_name = {row['window']: row for row in got['windows']}
        self.assertEqual(by_name['primary']['window_duration_mins'], 300)
        self.assertEqual(by_name['secondary']['window_duration_mins'], 10080)

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
        self.assertLessEqual(handle.next_renew, time.monotonic())
        self.assertEqual(handle.state, 'ready')
        self.assertTrue(readiness['ready_for_project_prompt'])
        self.assertEqual(self.prompt_count(), 0)
        self.session.finish.assert_not_called()
        self.heartbeat.return_value = True
        c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.session.broker.renew.assert_called()
        c.close(handle)

    def test_tick_driven_renew_heartbeat_loss_keeps_the_warm_process_and_retries(self):
        handle = self.prepare()
        # The explicit maintain() renew stays in the future. Only tick() is due,
        # and it must not advance next_renew when renew() raises.
        handle.next_renew = time.monotonic() + 1000
        handle.native.next_renew = time.monotonic() - 1
        self.heartbeat.return_value = False
        before = self.session.broker.renew.call_count
        readiness = c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.assertFalse(handle.consumed)
        self.assertTrue(readiness['ready_for_project_prompt'])
        self.assertLessEqual(handle.native.next_renew, time.monotonic())
        self.assertEqual(self.session.broker.renew.call_count, before + 1)
        self.assertEqual(self.prompt_count(), 0)
        self.session.finish.assert_not_called()
        self.assertNotIn('stop', self.native.order)
        self.assertIsNone(self.native.process.poll())
        self.heartbeat.return_value = True
        c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.assertGreater(handle.native.next_renew, time.monotonic())
        self.assertEqual(self.session.broker.renew.call_count, before + 2)
        self.session.finish.assert_not_called()
        c.close(handle)

    def test_poll_idle_hub_loss_is_retried_until_a_task_is_claimed(self):
        # maintain wraps poll_idle: a hub_lease_lost from the renew callback is an
        # idle retry only while the warm handle is ready and unclaimed.
        handle = self.prepare()
        handle.next_renew = time.monotonic() + 1000
        due = handle.native.next_renew = time.monotonic() - 1
        self.heartbeat.return_value = False
        before = self.session.broker.renew.call_count
        readiness = c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.assertFalse(handle.consumed)
        self.assertTrue(readiness['ready_for_project_prompt'])
        self.assertEqual(self.session.broker.renew.call_count, before + 1)
        self.assertGreater(handle.next_renew, time.monotonic())
        self.assertEqual(handle.native.next_renew, due)
        self.session.finish.assert_not_called()
        self.assertIsNone(self.native.process.poll())
        self.assertEqual(self.prompt_count(), 0)
        self.heartbeat.return_value = True
        c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.assertFalse(handle.consumed)
        self.assertGreater(handle.native.next_renew, time.monotonic())
        self.assertEqual(self.session.broker.renew.call_count, before + 2)
        self.session.finish.assert_not_called()
        c.close(handle)

        self._fresh_session()
        handle = self.prepare()
        handle.native.next_renew = time.monotonic() + 1000
        real_poll = handle.native.poll_idle

        def poll_idle():
            # Arm only this call, after execute's strict renew and metadata collect.
            handle.native.next_renew = 0
            return real_poll()

        handle.native.poll_idle = poll_idle

        def beat():
            return handle.native.next_renew > time.monotonic()

        self.heartbeat.side_effect = beat
        with self.assertRaisesRegex(c.LiveCodexError, '^hub_lease_lost$') as caught:
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertTrue(handle.consumed)
        self.assertEqual(handle.state, 'closed')
        self.assertEqual(self.prompt_count(), 0)
        self.assertFalse(caught.exception.model_call_attempted)

    def _broker_renew_failure(self, handle):
        self.session.broker.renew.side_effect = RuntimeError('SYNTHETIC_BROKER_DOWN')
        with self.assertRaises(c.LiveCodexError) as caught:
            c.maintain(handle)
        self.assertEqual(str(caught.exception), 'codex_live_operation_failed')
        self.assertNotIn('SYNTHETIC', str(caught.exception))
        self.assertEqual(handle.state, 'closed')
        self.assertEqual(self.prompt_count(), 0)

    def test_explicit_idle_broker_renew_failure_still_fails(self):
        handle = self.prepare()
        handle.next_renew = 0
        handle.native.next_renew = time.monotonic() + 1000
        self._broker_renew_failure(handle)

    def test_tick_idle_broker_renew_failure_still_fails(self):
        handle = self.prepare()
        handle.next_renew = time.monotonic() + 1000
        handle.native.next_renew = time.monotonic() - 1
        self._broker_renew_failure(handle)

    def test_expired_warm_window_does_not_start_execute_setup(self):
        handle = self.prepare()
        handle.warm_deadline = time.monotonic() - 1
        with self.assertRaisesRegex(c.LiveCodexError, 'warm_session_expired') as caught:
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 0)
        self.assertFalse(caught.exception.model_call_attempted)
        self.assertEqual(handle.state, 'closed')

    def test_execute_native_deadline_is_not_capped_by_the_warm_window(self):
        handle = self.prepare()
        started = time.monotonic()
        handle.warm_deadline = started + 100
        c.execute(handle, 'Project task.', started + 500)
        self.assertEqual(self.native.deadline, started + 500 - c.FINALIZE_RESERVE)
        self.assertGreater(self.native.deadline, handle.warm_deadline)
        self.assertEqual(self.prompt_count(), 1)

    def test_execute_setup_transport_error_stays_a_fixed_code(self):
        handle = self.prepare()
        def poll_idle():
            raise OSError('SYNTHETIC_NATIVE_PIPE')
        handle.native.poll_idle = poll_idle
        with self.assertRaises(c.LiveCodexError) as caught:
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(str(caught.exception), 'codex_live_operation_failed')
        self.assertNotIn('SYNTHETIC', str(caught.exception))
        self.assertEqual(self.prompt_count(), 0)
        self.assertFalse(caught.exception.model_call_attempted)

    def test_quota_signal_helper_maps_tokens_not_transient_429(self):
        secret = 'insufficient_quota for user@example.com balance'
        self.assertTrue(c._quota_exhausted_signal({'turn': {'error': {'code': 'insufficient_quota',
                                                                        'message': secret}}}))
        self.assertTrue(c._quota_exhausted_signal({'error': {'type': 'usage_limit'}}))
        self.assertTrue(c._quota_exhausted_signal({'error': {'message': 'billing hard limit reached'}}))
        self.assertTrue(c._quota_exhausted_signal({'error': {'message': 'credit exhausted'}}))
        self.assertFalse(c._quota_exhausted_signal({'error': {'code': 429, 'message': 'rate_limit'}}))
        self.assertFalse(c._quota_exhausted_signal({'error': {'code': 429, 'type': 'rate_limit'}}))
        self.assertFalse(c._quota_exhausted_signal({'error': {'message': 'temporary rate limit'}}))
        self.assertIs(c._quota_exhausted_signal(secret), True)

    def test_content_events_with_billing_words_are_not_quota(self):
        text = 'Room notes about billing, credit, and usage limit policies.'
        content_events = (
            {'method': 'item/completed', 'params': {
                'threadId': 'thread-live', 'turnId': 'turn-live',
                'item': {'id': 'answer-1', 'type': 'agentMessage', 'text': text,
                         'phase': 'final_answer'}}},
            {'method': 'item/agentMessage/delta', 'params': {
                'threadId': 'thread-live', 'turnId': 'turn-live', 'delta': text}},
            {'method': 'turn/completed', 'params': {
                'threadId': 'thread-live',
                'turn': {'id': 'turn-live', 'status': 'completed', 'error': None}}},
        )
        for event in content_events:
            self.assertFalse(c._midturn_quota_exhausted(event), event.get('method'))
        handle = self.prepare()
        self.native.answer = text
        result = c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(result['text'], text)

    def test_failed_turn_and_jsonrpc_quota_events_are_codex_quota_exhausted(self):
        failed = {'method': 'turn/completed', 'params': {
            'threadId': 'thread-live',
            'turn': {'id': 'turn-live', 'status': 'failed',
                     'error': {'code': 'insufficient_quota',
                               'message': 'You exceeded your current quota'}}}}
        self.assertTrue(c._midturn_quota_exhausted(failed))
        self.assertTrue(c._midturn_quota_exhausted({
            'jsonrpc': '2.0', 'id': 7,
            'error': {'code': 'insufficient_quota', 'message': 'billing hard limit'}}))
        self.assertFalse(c._midturn_quota_exhausted({
            'jsonrpc': '2.0', 'id': 8,
            'error': {'code': 429, 'message': 'rate_limit'}}))

    def test_midturn_quota_event_is_codex_quota_exhausted(self):
        handle = self.prepare()
        self.native.turn_error = {
            'code': 'insufficient_quota',
            'message': 'You exceeded your current quota for user@example.com',
        }
        with self.assertRaises(c.LiveCodexError) as caught:
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(str(caught.exception), 'codex_quota_exhausted')
        self.assertNotIn('example.com', str(caught.exception))
        self.assertTrue(caught.exception.model_call_attempted)

    def test_transient_429_without_exhaustion_token_stays_generic(self):
        handle = self.prepare()
        self.native.turn_error = {'code': 429, 'message': 'rate_limit try again shortly'}
        with self.assertRaises(c.LiveCodexError) as caught:
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertNotEqual(str(caught.exception), 'codex_quota_exhausted')
        self.assertNotIn('rate_limit', str(caught.exception))
        self.assertNotEqual(str(caught.exception), 'included_quota_exhausted')

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

    def test_idle_transient_renew_failure_keeps_warm_process(self):
        errors = [MutationUncertain('broker_http_outcome_uncertain'),
                  BoundaryError('metadata_identity_unavailable')]
        for error in errors:
            with self.subTest(error=str(error)):
                self._fresh_session()
                clock = StepClock()
                with patch('time.monotonic', clock):
                    handle = self.prepare()
                    anchor = bind_lease_clock(handle, clock)
                    self.session.broker.renew.reset_mock()
                    self.heartbeat.reset_mock()
                    handle.next_renew = 0
                    handle.native.next_renew = clock() + 1000
                    self.session.broker.renew.side_effect = error
                    readiness = c.maintain(handle)
                    self.assertEqual(handle.state, 'ready')
                    self.assertTrue(readiness['ready_for_project_prompt'])
                    self.session.finish.assert_not_called()
                    self.assertEqual(handle.lease_clock.renewed_at, anchor)
                    self.assertEqual(handle.next_renew, clock() + broker_renew.RETRY_SECONDS)
                    self.heartbeat.assert_called()
                    clock.advance()
                    self.session.broker.renew.side_effect = None
                    handle.next_renew = 0
                    c.maintain(handle)
                    self.assertGreaterEqual(handle.lease_clock.renewed_at, anchor)
                    self.assertEqual(handle.lease_clock.renewed_at, clock())
                    c.close(handle)

    def test_tick_path_transient_renew_failure_is_rearmed_for_retry(self):
        handle = self.prepare()
        self.session.broker.renew.reset_mock(side_effect=True)
        handle.next_renew = time.monotonic() + 1000
        handle.native.next_renew = 0
        before = self.session.broker.renew.call_count
        self.session.broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
        readiness = c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.assertTrue(readiness['ready_for_project_prompt'])
        self.assertEqual(self.session.broker.renew.call_count, before + 1)
        self.assertLessEqual(handle.native.next_renew, time.monotonic() + broker_renew.RETRY_SECONDS)
        self.assertLess(handle.native.next_renew, time.monotonic() + 25)
        c.close(handle)

    def test_tick_path_hub_heartbeat_loss_keeps_warm_process(self):
        handle = self.prepare()
        handle.next_renew = time.monotonic() + 1000
        handle.native.next_renew = 0
        self.heartbeat.return_value = False
        readiness = c.maintain(handle)
        self.assertEqual(handle.state, 'ready')
        self.assertTrue(readiness['ready_for_project_prompt'])
        self.session.finish.assert_not_called()
        self.assertIsNone(self.native.process.poll())
        c.close(handle)

    def test_renew_tolerance_is_bounded(self):
        for tick in (False, True):
            with self.subTest(tick=tick):
                self._fresh_session()
                handle = self.prepare()
                handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
                self.session.broker.renew.side_effect = MutationUncertain('x')
                if tick:
                    handle.next_renew = time.monotonic() + 1000
                    handle.native.next_renew = 0
                else:
                    handle.next_renew = 0
                    handle.native.next_renew = time.monotonic() + 1000
                with self.assertRaisesRegex(c.LiveCodexError, '^codex_broker_renew_failed$') as caught:
                    c.maintain(handle)
                self.assertEqual(handle.state, 'closed')
                self.assertEqual(live_loop.maintain_fault(caught.exception), 'fail')

    def test_rejections_are_immediate(self):
        errors = [Conflict('broker_operation_rejected'), BrokerError('broker_request_denied'),
                  BoundaryError('credential_fence_lost')]
        for error in errors:
            with self.subTest(error=str(error)):
                self._fresh_session()
                handle = self.prepare()
                anchor = handle.lease_clock.renewed_at
                handle.next_renew = 0
                handle.native.next_renew = time.monotonic() + 1000
                self.session.broker.renew.side_effect = error
                with self.assertRaisesRegex(c.LiveCodexError, '^codex_broker_renew_rejected$'):
                    c.maintain(handle)
                self.assertEqual(handle.lease_clock.renewed_at, anchor)

    def test_execute_pre_prompt_renew_is_strict(self):
        handle = self.prepare()
        handle.native.next_renew = time.monotonic() + 1000
        self.session.broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
        with self.assertRaisesRegex(c.LiveCodexError, '^codex_broker_renew_failed$'):
            c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(self.prompt_count(), 0)

    def _arm_turn_renew(self, native):
        real = native._frame
        armed = {'done': False}

        def frame():
            if not armed['done'] and any(item['method'] == 'turn/start' for item in native.requests):
                armed['done'] = True
                native.next_renew = 0
            return real()

        native._frame = frame

    def test_execute_pump_tolerates_one_blip_during_turn(self):
        handle = self.prepare()
        self._arm_turn_renew(self.native)
        pending = [None, MutationUncertain('broker_http_outcome_uncertain')]

        def effect(lease):
            item = pending.pop(0) if pending else None
            if item is not None:
                raise item

        self.session.broker.renew.side_effect = effect
        beats = {'n': 0}
        previous = self.heartbeat.side_effect

        def beat():
            beats['n'] += 1
            return True

        self.heartbeat.side_effect = beat
        result = c.execute(handle, 'Project task.', time.monotonic() + 180)
        self.assertEqual(result['text'], 'A real-parser offline answer.')
        self.assertEqual(self.session.state, 'committed')
        self.assertEqual(self.prompt_count(), 1)
        self.assertGreaterEqual(beats['n'], 2)
        self.heartbeat.side_effect = previous

    def test_execute_pump_past_window_fails_the_turn(self):
        handle = self.prepare()
        self._arm_turn_renew(self.native)
        calls = {'n': 0}

        def stale(lease):
            calls['n'] += 1
            if calls['n'] >= 2:
                handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
                raise MutationUncertain('x')

        self.session.broker.renew.side_effect = stale
        with self.assertRaisesRegex(c.LiveCodexError, '^codex_broker_renew_failed$'):
            c.execute(handle, 'Project task.', time.monotonic() + 180)

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

    def test_warm_rpc_tick_lowers_next_renew_to_retry(self):
        native = object.__new__(c.WarmRPC)
        native.failed = threading.Event()
        native.deadline = time.monotonic() + 100
        native.renew = lambda: None
        retry = time.monotonic() + 10
        native.renew_retry_at = retry
        native.next_renew = 0
        native.tick()
        self.assertEqual(native.next_renew, retry)
        self.assertIsNone(native.renew_retry_at)
        native.next_renew = 0
        before = time.monotonic()
        native.tick()
        self.assertAlmostEqual(native.next_renew, before + 25, delta=1)
        self.assertIsNone(native.renew_retry_at)

    def test_real_sender_rejects_zero_write_progress(self):
        native = object.__new__(c.WarmRPC)
        native.process = SimpleNamespace(stdin=SimpleNamespace(write=lambda data: 0, flush=lambda: None))
        native.deadline, native.writers = time.monotonic() + 5, []
        native.tick = lambda: None
        with self.assertRaisesRegex(c.LiveCodexError, 'native_input_failed'):
            c.WarmRPC._send(native, {'id': 1, 'method': 'initialize', 'params': {}})


if __name__ == '__main__':
    unittest.main()
