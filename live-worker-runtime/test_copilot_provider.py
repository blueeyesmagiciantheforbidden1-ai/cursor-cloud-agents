"""Offline lifecycle, admission and transport tests; no provider/Cloud calls."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import broker_renew
import live_loop
import provider_errors
from agent_hub.cloud_credential_broker import BrokerError, Conflict, MutationUncertain
from agent_hub.credential_broker_service import BoundaryError
from providers import copilot as c


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


def quota():
    return {'quotaSnapshots': {'premium_interactions': {
        'isUnlimitedEntitlement': False, 'entitlementRequests': 20000, 'usedRequests': 180,
        'remainingPercentage': 99.1, 'overage': 0,
        'usageAllowedWithExhaustedQuota': False, 'overageAllowedWithExhaustedQuota': False}}}


def catalog():
    return {'models': [{'id': c.MODEL, 'policy': {'state': 'enabled'},
        'capabilities': {'supports': {'reasoningEffort': True}},
        'supportedReasoningEfforts': ['low', 'high', 'max']}]}


def answer_events():
    return [{'type': 'assistant.turn_start', 'data': {}},
        {'type': 'assistant.message', 'data': {'content': 'The project answer.', 'model': c.MODEL}},
        {'type': 'assistant.usage', 'data': {'model': c.MODEL, 'reasoningEffort': c.EFFORT,
            'isByok': False, 'isAuto': False, 'inputTokens': 2095, 'outputTokens': 293}},
        {'type': 'session.idle', 'data': {}}]


class FakeNative:
    instances = []
    def __init__(self, home, renew, deadline):
        self.home, self.renew, self.deadline = home, renew, deadline
        self.calls, self.events = [], []
        self.native_stopped = self.clean_shutdown = False
        self.process = SimpleNamespace(poll=lambda: -9 if self.native_stopped else None)
        self.quota = quota()
        self.owner = {'isAuthenticated': True, 'authType': 'user', 'login': c.EXPECTED_LOGIN, 'host': 'github.com'}
        self.catalog = catalog()
        self.answer = answer_events()
        self.fail_send = self.fail_stop = self.fail_maintain = False
        self.fail_maintain_code = None
        self.maintain_error = None
        # Matches Native.__init__: the first renew is not due immediately.
        # maintain() advances this only after renew() returns, same as _tick.
        self.next_renew = time.monotonic() + 3600
        self.index = 0
        self.late = []
        self.instances.append(self)

    def request(self, method, params=None):
        c.Native._tick(self)
        self.index += 1
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'connect': return {'protocolVersion': 3}
        if method == 'auth.getStatus': return copy.deepcopy(self.owner)
        if method == 'models.list': return copy.deepcopy(self.catalog)
        if method == 'account.getQuota': return copy.deepcopy(self.quota)
        if method == 'session.create':
            self.sid = params['sessionId']
            return {'sessionId': self.sid}
        if method == 'session.model.setAllowedModels':
            return {'allowedModels': [c.MODEL], 'effectiveAllowedModels': [c.MODEL]}
        if method == 'session.model.getCurrent': return {'modelId': c.MODEL, 'reasoningEffort': c.EFFORT}
        if method == 'session.send':
            if self.fail_send: raise ValueError('opaque private native data must never escape')
            self.events.extend(copy.deepcopy(self.answer))
            return {'messageId': 'one-native-message'}
        if method == 'runtime.shutdown':
            self.clean_shutdown = True
            return {}
        raise AssertionError('unexpected method')

    def maintain(self):
        # Same order as Native._tick: a raising renew leaves next_renew due.
        c.Native._tick(self)
        if self.maintain_error is not None: raise self.maintain_error
        if self.fail_maintain: raise c.CopilotError('copilot_native_deadline_expired')
        if self.fail_maintain_code: raise c.CopilotError(self.fail_maintain_code)
        if self.process.poll() is not None: raise c.CopilotError('copilot_warm_process_ended')

    def next_event(self):
        c.Native._tick(self)
        return self.events.pop(0)

    def drain_after_shutdown(self):
        self.native_stopped = True
        self.events.extend(copy.deepcopy(self.late))

    def close(self):
        if self.fail_stop: raise OSError('private detail')
        self.native_stopped = True


class Lifecycle(unittest.TestCase):
    def setUp(self):
        home = Path('/mock-private-home')
        self.broker = SimpleNamespace(assert_current=Mock(), renew=Mock(), quarantine=Mock())
        self.session = SimpleNamespace(state='active', home=home, broker=self.broker,
            lease=SimpleNamespace(account_ref=c.ACCOUNT_REF, canonical_account_ref='a'*64))
        self.finished_after_stop = []
        def finish(*, native_stopped):
            self.assertTrue(native_stopped)
            self.assertTrue(FakeNative.instances[-1].native_stopped)
            self.finished_after_stop.append(True)
            self.session.state = 'committed'
            return 'version/42'
        self.session.finish = Mock(side_effect=finish)
        self.heartbeat = Mock(return_value=True)
        self.native_patch = patch.object(c, 'NativeProcess', FakeNative)
        self.verify_patch = patch.object(c, 'verify_native')
        self.home_patch = patch.object(c, '_home')
        self.native_patch.start(); self.verify_patch.start(); self.home_patch.start()

    def tearDown(self):
        self.native_patch.stop(); self.verify_patch.stop(); self.home_patch.stop()

    def prepare(self):
        return c.prepare(self.session, self.heartbeat, time.monotonic()+3500)

    def _new_grant(self):
        """Fresh broker and session state so one subtest cannot leak into the next."""
        self.broker = SimpleNamespace(assert_current=Mock(), renew=Mock(), quarantine=Mock())
        self.session.broker = self.broker
        self.session.state = 'active'
        self.finished_after_stop = []

        def finish(*, native_stopped):
            self.assertTrue(native_stopped)
            self.assertTrue(FakeNative.instances[-1].native_stopped)
            self.finished_after_stop.append(True)
            self.session.state = 'committed'
            return 'version/42'

        self.session.finish = Mock(side_effect=finish)
        self.heartbeat = Mock(return_value=True)

    def test_ready_is_warm_with_no_prompt_and_no_tools(self):
        handle = self.prepare()
        self.assertTrue(handle.readiness['ready_for_project_prompt'])
        self.assertFalse(handle.readiness['full_coding_ready'])
        self.assertEqual((c.MODEL, c.EFFORT), ('kimi-k3', 'max'))
        self.assertNotIn('session.send', [x[0] for x in handle.native.calls])
        create = next(params for method, params in handle.native.calls if method == 'session.create')
        for key in ('availableTools', 'tools', 'customAgents'):
            self.assertEqual(create[key], [])
        self.assertEqual(create['mcpServers'], {})
        self.assertFalse(create['requestPermission'])
        self.assertFalse(create['requestAutoModeSwitch'])
        self.assertFalse(self.session.finish.called)
        c.close(handle)

    def test_actual_return_stops_commits_then_returns_and_cannot_replay(self):
        handle = self.prepare(); native = handle.native
        result = c.execute(handle, 'Collaborate on the project.', time.monotonic()+800)
        self.assertEqual(result['text'], 'The project answer.')
        self.assertEqual(result['credential_writeback'], 'committed')
        self.assertEqual(result['usage']['outputTokens'], 293)
        self.assertTrue(result['server_usage_verified'])
        self.assertTrue(native.native_stopped)
        self.assertEqual(self.finished_after_stop, [True])
        self.assertEqual([x[0] for x in native.calls].count('session.send'), 1)
        self.assertEqual([x[0] for x in native.calls].count('account.getQuota'), 2)
        c.close(handle)
        self.assertEqual(self.session.finish.call_count, 1)
        with self.assertRaisesRegex(c.CopilotError, 'replay'):
            c.execute(handle, 'repeat', time.monotonic()+100)

    def test_missing_percentage_remains_unavailable_project_only(self):
        handle = self.prepare()
        del handle.native.quota['quotaSnapshots']['premium_interactions']['remainingPercentage']
        result = c.execute(handle, 'project', time.monotonic()+100)
        self.assertIsNone(result['preflight']['quota']['native_included_used_percent'])
        self.assertFalse(result['same_process_account_model_quota'])
        self.assertFalse(result['automatic_improvement_ready'])

    def test_improvement_denied_without_prompt(self):
        handle = self.prepare(); native = handle.native
        with self.assertRaisesRegex(c.CopilotError, 'improvement'):
            c.execute(handle, 'improve', time.monotonic()+100, task_kind='improvement')
        self.assertNotIn('session.send', [x[0] for x in native.calls])
        self.assertTrue(handle.finished)

    def test_stale_warm_quota_cannot_admit_new_overage(self):
        handle = self.prepare(); native = handle.native
        native.quota['quotaSnapshots']['premium_interactions']['overageAllowedWithExhaustedQuota'] = True
        with self.assertRaisesRegex(c.CopilotError, 'zero_extra'):
            c.execute(handle, 'project', time.monotonic()+100)
        self.assertNotIn('session.send', [x[0] for x in native.calls])
        self.assertTrue(handle.finished)

    def test_changed_owner_never_commits_new_credentials(self):
        handle = self.prepare(); native = handle.native
        native.owner['login'] = 'another-user'
        with self.assertRaises(c.CopilotError):
            c.execute(handle, 'project', time.monotonic()+100)
        self.assertTrue(native.native_stopped)
        self.session.finish.assert_not_called()
        self.broker.quarantine.assert_called_once()

    def test_unknown_send_failure_remains_uncertain_commits_no_replay(self):
        handle = self.prepare(); native = handle.native; native.fail_send = True
        with self.assertRaisesRegex(c.CopilotError, '^copilot_task_outcome_requires_reconciliation$'):
            c.execute(handle, 'project', time.monotonic()+100)
        self.assertTrue(handle.attempted)
        self.assertTrue(handle.finished)
        self.assertEqual([x[0] for x in native.calls].count('session.send'), 1)

    def test_stop_failure_prevents_commit_and_return(self):
        handle = self.prepare(); native = handle.native; native.fail_stop = True
        with self.assertRaisesRegex(c.CopilotError, 'reconciliation'):
            c.execute(handle, 'project', time.monotonic()+100)
        self.session.finish.assert_not_called()
        self.broker.quarantine.assert_called_once()
        with self.assertRaises(c.CopilotError): c.close(handle)

    def test_commit_failure_is_not_retried(self):
        handle = self.prepare()
        self.session.finish.side_effect = RuntimeError('opaque broker credential')
        with self.assertRaisesRegex(c.CopilotError, '^copilot_close_requires_reconciliation$'):
            c.execute(handle, 'project', time.monotonic()+100)
        with self.assertRaises(c.CopilotError): c.close(handle)
        self.assertEqual(self.session.finish.call_count, 1)

    def test_native_deadline_during_idle_keeps_its_code_and_closes(self):
        handle = self.prepare(); native = handle.native
        native.fail_maintain = True
        with self.assertRaisesRegex(c.CopilotError, '^copilot_native_deadline_expired$'): c.maintain(handle)
        self.assertTrue(handle.finished)
        self.assertIsNone(handle.native)
        self.assertNotIn('session.send', [x[0] for x in native.calls])

    def test_startup_budget_is_not_the_idle_cap(self):
        before = time.monotonic()
        handle = c.prepare(self.session, self.heartbeat, before + 45)
        self.assertGreaterEqual(handle.idle_deadline, before + c.WARM_SECONDS)
        self.assertLess(handle.idle_deadline, before + c.WARM_SECONDS + 30)
        self.assertEqual(handle.native.deadline, handle.idle_deadline)
        c.maintain(handle)
        self.assertEqual(handle.native.deadline, handle.idle_deadline)
        c.close(handle)

    def test_idle_process_exit_is_warm_session_lost_without_a_prompt(self):
        handle = self.prepare(); native = handle.native
        native.native_stopped = True
        with self.assertRaisesRegex(c.CopilotError, '^copilot_warm_session_lost$'): c.maintain(handle)
        self.assertTrue(handle.finished)
        self.assertNotIn('session.send', [x[0] for x in native.calls])
        self.assertEqual(self.session.finish.call_count, 1)

    def test_other_vetted_idle_codes_close_and_pass_through(self):
        codes = (
            'copilot_unexpected_native_frame', 'copilot_native_session_mismatch',
            'copilot_native_json_invalid', 'copilot_native_event_limit',
            'copilot_native_frame_limit', 'copilot_native_output_limit',
            'copilot_tools_forbidden', 'copilot_unexpected_pre_prompt_activity',
            'copilot_native_deadline_expired')
        for code in codes:
            with self.subTest(code=code):
                self.session.state = 'active'
                handle = self.prepare(); native = handle.native
                native.fail_maintain_code = code
                with self.assertRaisesRegex(c.CopilotError, '^' + code + '$') as caught:
                    c.maintain(handle)
                self.assertIsInstance(caught.exception, c.CopilotError)
                self.assertTrue(handle.finished)
                self.assertIsNone(handle.native)
                self.assertNotIn('session.send', [x[0] for x in native.calls])

    def test_hub_heartbeat_lost_keeps_native_session_and_retries_renew(self):
        handle = self.prepare(); native = handle.native
        due = native.next_renew = time.monotonic() - 1
        self.heartbeat.return_value = False
        with self.assertRaisesRegex(c.CopilotError, '^copilot_hub_heartbeat_lost$'):
            c.maintain(handle)
        self.assertFalse(handle.finished)
        self.assertFalse(handle.close_failed)
        self.assertIs(handle.native, native)
        self.assertFalse(native.native_stopped)
        self.assertIsNone(native.process.poll())
        self.assertEqual(native.next_renew, due)
        self.assertEqual(self.broker.renew.call_count, 1)
        self.session.finish.assert_not_called()
        self.broker.quarantine.assert_not_called()
        self.heartbeat.return_value = True
        readiness = c.maintain(handle)
        self.assertTrue(readiness['ready_for_project_prompt'])
        self.assertEqual(self.broker.renew.call_count, 2)
        self.assertGreater(native.next_renew, due)
        self.assertFalse(handle.finished)
        self.assertIs(handle.native, native)

    def test_transient_renew_failure_keeps_idle_session_and_retries(self):
        errors = (MutationUncertain('private mutation detail'), BoundaryError('transport_failed'),
                  BoundaryError('metadata_identity_unavailable'))
        for error in errors:
            with self.subTest(error=type(error).__name__ + ':' + str(error)):
                self._new_grant()
                clock = StepClock()
                with patch('time.monotonic', clock):
                    handle = self.prepare(); native = handle.native
                    anchor = bind_lease_clock(handle, clock)
                    self.broker.renew.reset_mock()
                    self.heartbeat.reset_mock()
                    self.broker.renew.side_effect = error
                    native.next_renew = clock() - 1
                    readiness = c.maintain(handle)
                    self.assertTrue(readiness['ready_for_project_prompt'])
                    self.assertFalse(handle.finished)
                    self.assertFalse(handle.close_failed)
                    self.assertIs(handle.native, native)
                    self.assertIsNone(native.process.poll())
                    self.assertEqual(self.broker.renew.call_count, 1)
                    self.heartbeat.assert_called()
                    self.session.finish.assert_not_called()
                    self.broker.quarantine.assert_not_called()
                    self.assertEqual(handle.lease_clock.renewed_at, anchor)
                    self.assertEqual(native.next_renew, clock() + broker_renew.RETRY_SECONDS)
                    clock.advance()
                    self.broker.renew.side_effect = None
                    native.next_renew = clock() - 1
                    c.maintain(handle)
                    self.assertEqual(self.broker.renew.call_count, 2)
                    self.assertGreaterEqual(handle.lease_clock.renewed_at, anchor)
                    self.assertEqual(handle.lease_clock.renewed_at, clock())
                    self.assertFalse(handle.lease_clock.degraded)
                    self.assertEqual(native.next_renew, clock() + 20)

    def test_renew_tolerance_exhausted_fails_with_vetted_code(self):
        handle = self.prepare(); native = handle.native
        handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
        self.broker.renew.side_effect = MutationUncertain('private detail')
        native.next_renew = time.monotonic() - 1
        with self.assertRaisesRegex(c.CopilotError, '^copilot_broker_renew_failed$') as caught:
            c.maintain(handle)
        self.assertNotIn('private', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertTrue(handle.finished)
        self.assertIsNone(handle.native)
        self.assertTrue(native.native_stopped)
        self.session.finish.assert_called_once()
        self.broker.quarantine.assert_not_called()
        self.assertNotIn('session.send', [x[0] for x in native.calls])

    def test_conflict_denied_and_local_boundary_are_rejected_at_once(self):
        for error in (Conflict('broker_operation_rejected'), BrokerError('broker_request_denied'),
                      BoundaryError('credential_fence_lost')):
            with self.subTest(error=str(error)):
                self.session.state = 'active'
                self.broker.renew.reset_mock()
                self.broker.renew.side_effect = error
                handle = self.prepare(); native = handle.native
                native.next_renew = time.monotonic() - 1
                with self.assertRaisesRegex(c.CopilotError, '^copilot_broker_renew_rejected$'):
                    c.maintain(handle)
                self.assertEqual(self.broker.renew.call_count, 1)
                self.assertTrue(handle.finished)
                self.assertTrue(native.native_stopped)

    def test_tolerated_renew_still_heartbeats_and_lost_heartbeat_keeps_renew_due(self):
        handle = self.prepare(); native = handle.native
        due = native.next_renew = time.monotonic() - 1
        self.broker.renew.side_effect = MutationUncertain('x')
        self.heartbeat.return_value = False
        with self.assertRaisesRegex(c.CopilotError, '^copilot_hub_heartbeat_lost$'):
            c.maintain(handle)
        self.assertFalse(handle.finished)
        self.assertEqual(native.next_renew, due)
        self.heartbeat.assert_called()

    def test_success_anchor_is_attempt_start_and_uncertain_never_moves_it(self):
        handle = self.prepare(); native = handle.native
        seen = {}

        def effect(lease):
            seen['t'] = time.monotonic()

        self.broker.renew.side_effect = effect
        native.next_renew = time.monotonic() - 1
        c.maintain(handle)
        self.assertLessEqual(handle.lease_clock.renewed_at, seen['t'])
        anchor = handle.lease_clock.renewed_at
        self.broker.renew.side_effect = MutationUncertain('x')
        native.next_renew = time.monotonic() - 1
        c.maintain(handle)
        self.assertEqual(handle.lease_clock.renewed_at, anchor)

    def test_prepare_anchors_lease_clock(self):
        before = time.monotonic()
        handle = self.prepare()
        after = time.monotonic()
        self.assertGreaterEqual(handle.lease_clock.renewed_at, before)
        self.assertLessEqual(handle.lease_clock.renewed_at, after)
        c.close(handle)
        self.session.lease.lease_id = 'ab' * 16
        t0 = time.monotonic() - 4
        with patch.dict(broker_renew._acquire_started, {self.session.lease.lease_id: t0}):
            self.session.state = 'active'
            handle = self.prepare()
        self.assertEqual(handle.lease_clock.renewed_at, t0)

    def test_dead_pipe_is_warm_session_lost(self):
        handle = self.prepare(); native = handle.native
        native.maintain_error = OSError(32, 'private dead pipe')
        with self.assertRaisesRegex(c.CopilotError, '^copilot_warm_session_lost$') as caught:
            c.maintain(handle)
        self.assertNotIn('private', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(handle.finished)
        self.assertTrue(native.native_stopped)
        self.assertNotIn('session.send', [x[0] for x in native.calls])

    def test_opaque_idle_failure_is_idle_renew_failed_not_session_loss(self):
        handle = self.prepare(); native = handle.native
        def boom(): raise RuntimeError('private native path must not escape')
        native.maintain = boom
        with self.assertRaisesRegex(c.CopilotError, '^copilot_idle_maintain_failed$') as caught:
            c.maintain(handle)
        self.assertNotIn('private', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(handle.finished)

    def test_execute_turn_survives_one_uncertain_renew(self):
        handle = self.prepare(); native = handle.native
        original = native.next_event

        def next_event():
            native.next_renew = time.monotonic() - 1
            return original()

        native.next_event = next_event
        state = {'n': 0}

        def effect(lease):
            state['n'] += 1
            if state['n'] == 1:
                raise MutationUncertain('x')

        self.broker.renew.side_effect = effect
        beats = self.heartbeat.call_count
        result = c.execute(handle, 'project', time.monotonic() + 100)
        self.assertEqual(result['text'], 'The project answer.')
        self.assertEqual(result['credential_writeback'], 'committed')
        self.session.finish.assert_called_once()
        self.assertEqual(sum(name == 'session.send' for name, _ in native.calls), 1)
        self.assertGreater(self.heartbeat.call_count, beats)

    def test_execute_renew_past_window_is_broker_renew_failed(self):
        handle = self.prepare(); native = handle.native
        handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
        original = native.next_event

        def next_event():
            native.next_renew = time.monotonic() - 1
            return original()

        native.next_event = next_event
        self.broker.renew.side_effect = MutationUncertain('private detail')
        with self.assertRaisesRegex(c.CopilotError, '^copilot_broker_renew_failed$') as caught:
            c.execute(handle, 'project', time.monotonic() + 100)
        self.assertNotEqual(str(caught.exception), 'copilot_task_outcome_requires_reconciliation')
        self.assertTrue(handle.finished)
        self.assertLessEqual(sum(name == 'session.send' for name, _ in native.calls), 1)

    def test_degraded_lease_gets_strict_renew_before_send(self):
        handle = self.prepare(); native = handle.native
        native.next_renew = time.monotonic() - 1
        self.broker.renew.side_effect = MutationUncertain('x')
        c.maintain(handle)
        self.assertTrue(handle.lease_clock.degraded)
        order = []
        self.broker.renew.side_effect = lambda lease: order.append('renew')
        self.broker.assert_current.side_effect = lambda lease: order.append('assert')
        result = c.execute(handle, 'project', time.monotonic() + 100)
        self.assertEqual(result['text'], 'The project answer.')
        self.assertEqual(sum(name == 'session.send' for name, _ in native.calls), 1)
        self.assertLess(order.index('renew'), order.index('assert'))
        self.session.state = 'active'
        handle = self.prepare(); native = handle.native
        native.next_renew = time.monotonic() - 1
        self.broker.renew.side_effect = MutationUncertain('x')
        self.broker.assert_current.side_effect = None
        c.maintain(handle)
        with self.assertRaisesRegex(c.CopilotError, '^copilot_broker_renew_failed$'):
            c.execute(handle, 'project', time.monotonic() + 100)
        self.assertEqual(sum(name == 'session.send' for name, _ in native.calls), 0)
        self.assertTrue(handle.finished)
        self.session.state = 'active'
        handle = self.prepare(); native = handle.native
        before = self.broker.renew.call_count
        self.broker.renew.side_effect = None
        self.broker.assert_current.side_effect = None
        c.execute(handle, 'project', time.monotonic() + 100)
        self.assertEqual(self.broker.renew.call_count, before)

    def test_idle_deadline_does_not_slide(self):
        handle = self.prepare(); original = handle.idle_deadline
        c.maintain(handle); self.assertEqual(handle.idle_deadline, original)
        handle.idle_deadline = time.monotonic()-1
        with self.assertRaisesRegex(c.CopilotError, '^copilot_warm_session_lost$'): c.maintain(handle)
        self.assertTrue(handle.finished)

    def test_task_timeout_cannot_exceed_900_seconds(self):
        handle = self.prepare(); native = handle.native
        with self.assertRaisesRegex(c.CopilotError, 'deadline_too_long'):
            c.execute(handle, 'project', time.monotonic()+901)
        self.assertNotIn('session.send', [x[0] for x in native.calls])

    def test_late_model_activity_blocks_success_but_safe_refresh_commits(self):
        handle = self.prepare()
        handle.native.late = [{'type': 'assistant.message', 'data': {'content': 'late'}}]
        with self.assertRaisesRegex(c.CopilotError, 'activity_after_completion'):
            c.execute(handle, 'project', time.monotonic()+100)
        self.assertTrue(handle.finished)

    def test_input_and_output_limits_are_bytes_not_commissioning_words(self):
        handle = self.prepare()
        handle.native.answer[1]['data']['content'] = 'word '*2500
        result = c.execute(handle, 'x'*200000, time.monotonic()+100)
        self.assertGreater(len(result['text'].split()), 40)
        self.assertLessEqual(len(result['text'].encode()), 15000)

    def test_quota_refresh_fires_after_quota_refresh_seconds_not_before(self):
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            before = sum(1 for m, _ in handle.native.calls if m == 'account.getQuota')
            c.maintain(handle)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before)
            clock.advance(c.QUOTA_REFRESH_SECONDS - 1)
            c.maintain(handle)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before)
            clock.advance(1)
            c.maintain(handle)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before + 1)
            c.close(handle)

    def test_quota_refresh_success_replaces_observed_at(self):
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            handle.next_quota_refresh = clock()
            old = handle.preflight['quota']['observed_at']
            verified = handle.owner_verified
            handle.native.quota = quota()
            handle.native.quota['quotaSnapshots']['premium_interactions']['remainingPercentage'] = 55.0
            fixed = datetime(2099, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

            class FakeDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed

            before_auth = sum(1 for m, _ in handle.native.calls if m == 'auth.getStatus')
            with patch('providers.copilot.datetime', FakeDateTime):
                c.maintain(handle)
            self.assertEqual(handle.preflight['quota']['observed_at'], fixed.isoformat())
            self.assertAlmostEqual(handle.preflight['quota']['native_included_used_percent'], 45.0)
            self.assertNotEqual(handle.preflight['quota']['observed_at'], old)
            self.assertEqual(handle.next_quota_refresh, clock() + c.QUOTA_REFRESH_SECONDS)
            self.assertEqual(handle.owner_verified, verified)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'auth.getStatus'), before_auth)
            c.close(handle)

    def test_quota_refresh_validation_failure_keeps_old_row_and_retries_at_120s(self):
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            old = copy.deepcopy(handle.preflight['quota'])
            verified = handle.owner_verified
            handle.next_quota_refresh = clock()
            handle.native.quota = {'broken': True}
            before = sum(1 for m, _ in handle.native.calls if m == 'account.getQuota')
            readiness = c.maintain(handle)
            self.assertTrue(readiness['ready_for_project_prompt'])
            self.assertEqual(handle.preflight['quota'], old)
            self.assertEqual(handle.owner_verified, verified)
            self.assertEqual(handle.next_quota_refresh, clock() + c.QUOTA_RETRY_SECONDS)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before + 1)
            clock.advance(c.QUOTA_RETRY_SECONDS - 1)
            c.maintain(handle)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before + 1)
            clock.advance(1)
            c.maintain(handle)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before + 2)
            self.assertEqual(handle.owner_verified, verified)
            c.close(handle)

    def test_quota_refresh_exhausted_raises_quota_park(self):
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            verified = handle.owner_verified
            handle.next_quota_refresh = clock()
            # Hit copilot_included_allowance_exhausted (is_quota), not the
            # unverified_or_exhausted path (used >= entitlement).
            exhausted = quota()
            exhausted['quotaSnapshots']['premium_interactions']['remainingPercentage'] = 0
            handle.native.quota = exhausted
            with self.assertRaisesRegex(c.CopilotError, '^copilot_quota_exhausted$') as caught:
                c.maintain(handle)
            self.assertEqual(provider_errors.error_code(caught.exception), 'copilot_quota_exhausted')
            self.assertTrue(provider_errors.is_quota(provider_errors.error_code(caught.exception)))
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'fail')
            self.assertEqual(handle.owner_verified, verified)
            self.assertTrue(handle.finished)

    def test_quota_refresh_error_frame_swallowed_retries_at_120s(self):
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            old = copy.deepcopy(handle.preflight['quota'])
            verified = handle.owner_verified
            handle.next_quota_refresh = clock()
            real_request = handle.native.request
            before = sum(1 for m, _ in handle.native.calls if m == 'account.getQuota')

            def error_frame(method, params=None):
                if method == 'account.getQuota':
                    handle.native.index += 1
                    handle.native.calls.append((method, copy.deepcopy(params)))
                    err = c.CopilotError('copilot_native_request_failed')
                    err.error_frame = True
                    raise err
                return real_request(method, params)

            handle.native.request = error_frame
            readiness = c.maintain(handle)
            self.assertTrue(readiness['ready_for_project_prompt'])
            self.assertEqual(handle.preflight['quota'], old)
            self.assertEqual(handle.owner_verified, verified)
            self.assertEqual(handle.next_quota_refresh, clock() + c.QUOTA_RETRY_SECONDS)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before + 1)
            clock.advance(c.QUOTA_RETRY_SECONDS - 1)
            c.maintain(handle)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before + 1)
            clock.advance(1)
            c.maintain(handle)
            self.assertEqual(sum(1 for m, _ in handle.native.calls if m == 'account.getQuota'), before + 2)
            self.assertEqual(handle.owner_verified, verified)
            self.assertFalse(handle.finished)
            c.close(handle)

    def test_quota_refresh_error_frame_quota_exhausted_parks(self):
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            handle.next_quota_refresh = clock()
            real_request = handle.native.request

            def exhausted_frame(method, params=None):
                if method == 'account.getQuota':
                    handle.native.index += 1
                    handle.native.calls.append((method, copy.deepcopy(params)))
                    raise c.CopilotError('copilot_quota_exhausted')
                return real_request(method, params)

            handle.native.request = exhausted_frame
            with self.assertRaisesRegex(c.CopilotError, '^copilot_quota_exhausted$') as caught:
                c.maintain(handle)
            self.assertTrue(provider_errors.is_quota(provider_errors.error_code(caught.exception)))
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'fail')
            self.assertTrue(handle.finished)

    def test_quota_refresh_id_mismatch_drains(self):
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            verified = handle.owner_verified
            handle.next_quota_refresh = clock()
            real_request = handle.native.request

            def id_mismatch(method, params=None):
                if method == 'account.getQuota':
                    handle.native.index += 1
                    handle.native.calls.append((method, copy.deepcopy(params)))
                    # Untagged: same code as error frame, but no error_frame attr.
                    raise c.CopilotError('copilot_native_request_failed')
                return real_request(method, params)

            handle.native.request = id_mismatch
            with self.assertRaisesRegex(c.CopilotError, '^copilot_quota_refresh_transport_lost$') as caught:
                c.maintain(handle)
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'drain')
            self.assertEqual(handle.owner_verified, verified)
            self.assertTrue(handle.finished)

    def test_quota_refresh_vetted_passthrough_keeps_own_code(self):
        codes = (
            'copilot_broker_renew_failed', 'copilot_broker_renew_rejected',
            'copilot_hub_heartbeat_lost', 'copilot_tools_forbidden',
            'copilot_unexpected_pre_prompt_activity',
            'copilot_unexpected_pre_prompt_assistant_message')
        clock = StepClock()
        for code in codes:
            with self.subTest(code=code):
                with patch('time.monotonic', clock):
                    self._new_grant()
                    handle = self.prepare()
                    bind_lease_clock(handle, clock)
                    handle.native.next_renew = clock() + 10000
                    handle.next_quota_refresh = clock()
                    real_request = handle.native.request

                    def vetted(method, params=None, *, _code=code):
                        if method == 'account.getQuota':
                            handle.native.index += 1
                            handle.native.calls.append((method, copy.deepcopy(params)))
                            raise c.CopilotError(_code)
                        return real_request(method, params)

                    handle.native.request = vetted
                    with self.assertRaisesRegex(c.CopilotError, '^' + code + '$') as caught:
                        c.maintain(handle)
                    self.assertEqual(provider_errors.error_code(caught.exception), code)
                    self.assertNotEqual(str(caught.exception), 'copilot_quota_refresh_transport_lost')
                    self.assertNotEqual(str(caught.exception), 'copilot_warm_session_lost')
                    self.assertNotEqual(str(caught.exception), 'copilot_idle_maintain_failed')
                    if code == 'copilot_hub_heartbeat_lost':
                        self.assertFalse(handle.finished)
                    else:
                        self.assertTrue(handle.finished)

    def test_quota_refresh_transport_codes_drain(self):
        codes = (
            'copilot_native_deadline_expired', 'copilot_native_rpc_closed',
            'copilot_native_frame_limit', 'copilot_native_output_limit')
        clock = StepClock()
        for code in codes:
            with self.subTest(code=code):
                with patch('time.monotonic', clock):
                    self._new_grant()
                    handle = self.prepare()
                    bind_lease_clock(handle, clock)
                    handle.native.next_renew = clock() + 10000
                    handle.next_quota_refresh = clock()
                    real_request = handle.native.request

                    def transport(method, params=None, *, _code=code):
                        if method == 'account.getQuota':
                            handle.native.index += 1
                            handle.native.calls.append((method, copy.deepcopy(params)))
                            raise c.CopilotError(_code)
                        return real_request(method, params)

                    handle.native.request = transport
                    with self.assertRaisesRegex(
                            c.CopilotError, '^copilot_quota_refresh_transport_lost$') as caught:
                        c.maintain(handle)
                    self.assertEqual(live_loop.maintain_fault(caught.exception), 'drain')
                    self.assertTrue(handle.finished)

    def test_quota_refresh_transport_failure_propagates_from_maintain(self):
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            verified = handle.owner_verified
            handle.next_quota_refresh = clock()
            real_request = handle.native.request

            def boom(method, params=None):
                if method == 'account.getQuota':
                    handle.native.index += 1
                    handle.native.calls.append((method, copy.deepcopy(params)))
                    raise OSError('synthetic transport failure')
                return real_request(method, params)

            handle.native.request = boom
            with self.assertRaisesRegex(c.CopilotError, '^copilot_quota_refresh_transport_lost$') as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'copilot_quota_refresh_transport_lost')
            self.assertEqual(provider_errors.error_code(caught.exception),
                             'copilot_quota_refresh_transport_lost')
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'drain')
            self.assertEqual(handle.owner_verified, verified)
            self.assertTrue(handle.finished)

    def test_quota_refresh_deadline_bound_drains_on_hang(self):
        # Idle native.deadline is the ~4500 s warm horizon; refresh must re-bound
        # to 30 s so a hung account.getQuota cannot block maintain()/hub reports.
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            handle.next_quota_refresh = clock()
            warm_deadline = handle.native.deadline
            self.assertEqual(warm_deadline, handle.idle_deadline)
            real_request = handle.native.request
            seen = {}

            def hang(method, params=None):
                if method == 'account.getQuota':
                    handle.native.index += 1
                    handle.native.calls.append((method, copy.deepcopy(params)))
                    seen['start'] = clock()
                    seen['bound'] = handle.native.deadline
                    while clock() < handle.native.deadline:
                        clock.advance(1)
                    raise c.CopilotError('copilot_native_deadline_expired')
                return real_request(method, params)

            handle.native.request = hang
            native = handle.native  # close() on the drain path clears handle.native
            with self.assertRaisesRegex(c.CopilotError, '^copilot_quota_refresh_transport_lost$') as caught:
                c.maintain(handle)
            self.assertEqual(seen['bound'], seen['start'] + c.QUOTA_REFRESH_DEADLINE_SECONDS)
            self.assertLess(seen['bound'], warm_deadline)
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'drain')
            self.assertEqual(native.deadline, warm_deadline)
            self.assertTrue(handle.finished)

    def test_quota_refresh_skipped_when_native_request_budget_tight(self):
        # prepare=7, refresh=1, execute=7; skip when index + 1 + 7 > 24.
        clock = StepClock()
        with patch('time.monotonic', clock):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 10000
            self.assertEqual(handle.native.index, 7)
            handle.native.index = 17  # 17 + 1 + 7 = 25 > 24
            old = copy.deepcopy(handle.preflight['quota'])
            before = len(handle.native.calls)
            handle.next_quota_refresh = clock()
            c.maintain(handle)
            self.assertEqual(handle.preflight['quota'], old)
            self.assertEqual(len(handle.native.calls), before)
            self.assertEqual(handle.native.index, 17)
            result = c.execute(handle, 'project', clock() + 100)
            self.assertEqual(result['text'], 'The project answer.')
            self.assertTrue(result['native_stopped'])

    def test_quota_refresh_idle_horizon_keeps_observed_at_within_900s(self):
        # 1 request/refresh → enough budget for a full idle horizon at 600 s steps.
        clock = StepClock()
        origin = datetime(2099, 1, 1, tzinfo=timezone.utc)

        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return origin.fromtimestamp(origin.timestamp() + (clock() - 1_000_000.0), tz=timezone.utc)

        with patch('time.monotonic', clock), patch('providers.copilot.datetime', FakeDateTime):
            handle = self.prepare()
            bind_lease_clock(handle, clock)
            handle.native.next_renew = clock() + 100000
            while clock() + c.QUOTA_REFRESH_SECONDS < handle.idle_deadline:
                clock.advance(c.QUOTA_REFRESH_SECONDS)
                c.maintain(handle)
                observed = datetime.fromisoformat(handle.preflight['quota']['observed_at'])
                age = (FakeDateTime.now(timezone.utc) - observed).total_seconds()
                self.assertLessEqual(age, 900)
            quota_calls = sum(1 for m, _ in handle.native.calls if m == 'account.getQuota')
            # prepare issues 1 getQuota; idle steps add one per 600 s until the horizon.
            self.assertGreaterEqual(quota_calls, 1 + (c.WARM_SECONDS // c.QUOTA_REFRESH_SECONDS) - 1)
            self.assertLessEqual(handle.native.index,
                                 c._NATIVE_REQUEST_CAP - c._EXECUTE_NATIVE_REQUESTS)
            c.close(handle)


class Admission(unittest.TestCase):
    def test_permission_flags_are_required_and_false_is_not_zero(self):
        for field in ('usageAllowedWithExhaustedQuota', 'overageAllowedWithExhaustedQuota'):
            for value in (None, True, 0, 'false'):
                with self.subTest(field=field, value=value):
                    q = quota(); q['quotaSnapshots']['premium_interactions'][field] = value
                    with self.assertRaises(c.CopilotError): c._billing(q, 'a'*64)

    def test_balance_without_permission_flags_does_not_prove_zero_extra(self):
        q = quota(); del q['quotaSnapshots']['premium_interactions']['overageAllowedWithExhaustedQuota']
        with self.assertRaises(c.CopilotError): c._billing(q, 'a'*64)

    def test_unlimited_auxiliary_pools_are_not_premium_allowance(self):
        q = quota()
        auxiliary = dict(q['quotaSnapshots']['premium_interactions'], isUnlimitedEntitlement=True,
                         entitlementRequests=0, usedRequests=0, remainingPercentage=100)
        q['quotaSnapshots']['chat'] = auxiliary
        result = c._billing(q, 'a'*64)
        self.assertAlmostEqual(result['native_included_used_percent'], 0.9)
        self.assertIsNone(result['pools'][1]['native_included_used_percent'])
        del q['quotaSnapshots']['premium_interactions']
        with self.assertRaisesRegex(c.CopilotError, 'premium_allowance'): c._billing(q, 'a'*64)

    def test_bad_numeric_or_exhausted_allowance_rejected(self):
        for field, value in [('overage', True), ('overage', 1), ('usedRequests', 20000),
                             ('remainingPercentage', 0), ('remainingPercentage', float('nan')),
                             ('entitlementRequests', -1), ('isUnlimitedEntitlement', True)]:
            with self.subTest(field=field, value=value):
                q = quota(); q['quotaSnapshots']['premium_interactions'][field] = value
                with self.assertRaises(c.CopilotError): c._billing(q, 'a'*64)

    def test_catalog_uses_enabled_reviewed_family_and_maximum_effort(self):
        self.assertEqual(c._catalog(catalog())['reviewed_maximum_effort'], 'max')
        for field, value in [('policy', {'state': 'unconfigured'}),
                             ('supportedReasoningEfforts', ['low', 'high']),
                             ('supportedReasoningEfforts', ['max', 'ultra'])]:
            value_rows = catalog(); value_rows['models'][0][field] = value
            with self.assertRaises(c.CopilotError): c._catalog(value_rows)

    def test_false_effort_or_tools_in_usage_never_prove_answer(self):
        for event in [
            {'type': 'assistant.usage', 'data': {'model': 'other'}},
            {'type': 'assistant.usage', 'data': {'model': c.MODEL, 'isAuto': True}},
            {'type': 'tool.execution_start', 'data': {}},
            {'type': 'assistant.turn_retry', 'data': {}},
            {'type': 'assistant.message', 'data': {'content': 'x', 'toolRequests': [{}]}}]:
            events = answer_events(); events.insert(1, event)
            with self.subTest(event=event['type']):
                with self.assertRaises(c.CopilotError): c._response(SimpleNamespace(next_event=lambda: events.pop(0)))


# Emitted by the native CLI between session.create / setAllowedModels and the
# prompt (2026-09-23: the copilot d image rejected these and every startup failed).
SETUP_NOTICES = ('session.model_change', 'session.tools_updated', 'session.mcp_servers_loaded',
                 'session.skills_loaded', 'session.custom_agents_updated', 'session.extensions_loaded',
                 'session.usage_info', 'session.context_changed', 'session.managed_settings_resolved')


class SetupNoticesNative(FakeNative):
    """FakeNative whose setup requests deliver events through the REAL Native.notification."""
    extra = ()

    def request(self, method, params=None):
        result = super().request(method, params)
        if method == 'session.create':
            self.session_id, self.send_started = self.sid, False
            self.event_count = self.ignored_notification_count = 0
            kinds = SETUP_NOTICES[:4] + tuple(self.extra)
        elif method == 'session.model.setAllowedModels':
            kinds = SETUP_NOTICES[4:]
        else:
            kinds = ()
        for kind in kinds:
            c.Native.notification(self, notification(kind, sid=self.sid))
        return result


class StartupSequence(unittest.TestCase):
    """prepare() with the native setup events the live image really sees."""
    setUp, tearDown, prepare = Lifecycle.setUp, Lifecycle.tearDown, Lifecycle.prepare

    def test_prepare_survives_the_real_setup_notices(self):
        with patch.object(c, 'NativeProcess', SetupNoticesNative):
            handle = self.prepare()
        native = SetupNoticesNative.instances[-1]
        self.assertEqual(native.events, [])
        self.assertEqual(native.event_count, len(SETUP_NOTICES))
        self.assertIn(('session.model.setAllowedModels', {'sessionId': native.sid, 'allowedModels': [c.MODEL]}),
                      native.calls)
        c.close(handle)

    def test_real_pre_prompt_work_during_setup_still_fails_and_is_named(self):
        class Busy(SetupNoticesNative):
            extra = ('assistant.message',)
        with patch.object(c, 'NativeProcess', Busy):
            with self.assertRaisesRegex(c.CopilotError, 'copilot_unexpected_pre_prompt_assistant_message'):
                self.prepare()


def bare_native():
    native = c.Native.__new__(c.Native)
    native.deadline = time.monotonic()+100
    native.next_renew = time.monotonic()+20
    native.renew = Mock()
    native.process = SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: 9), poll=lambda: None)
    native.selector = SimpleNamespace(select=lambda timeout: [], get_map=lambda: {'still': True})
    native.buffer, native.events = bytearray(), []
    native.total = native.index = native.event_count = 0
    native.session_id, native.send_started, native.native_stopped = 'sid', False, False
    native.clean_shutdown, native.ignored_notification_count = False, 0
    return native


def notification(kind, *, sid='sid'):
    return {'jsonrpc': '2.0', 'method': 'session.event', 'params': {
        'sessionId': sid, 'event': {'type': kind, 'data': {'content': 'old'}}}}


class Transport(unittest.TestCase):
    def test_partial_writes_emit_only_remaining_suffix(self):
        native = bare_native(); chunks = []
        def write(fd, value):
            count = min(3, len(value)); chunks.append(bytes(value[:count])); return count
        with patch.object(c.os, 'write', side_effect=write): native.write_frame(b'abcdefghij')
        self.assertEqual(b''.join(chunks), b'abcdefghij')

    def test_tick_throttles_retry_after_tolerated_renew(self):
        native = bare_native()
        native.renew = Mock(return_value=False)
        native.next_renew = time.monotonic() - 1
        native.maintain()
        self.assertEqual(native.renew.call_count, 1)
        native.maintain()
        self.assertEqual(native.renew.call_count, 1)
        now = time.monotonic()
        self.assertGreater(native.next_renew, now)
        self.assertLessEqual(native.next_renew, now + broker_renew.RETRY_SECONDS)
        native.renew.return_value = None
        native.next_renew = time.monotonic() - 1
        native.maintain()
        self.assertAlmostEqual(native.next_renew, time.monotonic() + c.RENEW_SECONDS, delta=1)

    def test_native_maintain_leaves_renew_due_when_renew_raises(self):
        native = bare_native()
        native.next_renew = due = time.monotonic() - 1
        native.renew.side_effect = c.CopilotError('copilot_hub_heartbeat_lost')
        with self.assertRaisesRegex(c.CopilotError, '^copilot_hub_heartbeat_lost$'):
            native.maintain()
        self.assertEqual(native.next_renew, due)
        self.assertEqual(native.renew.call_count, 1)
        native.renew.side_effect = None
        native.maintain()
        self.assertEqual(native.renew.call_count, 2)
        self.assertGreater(native.next_renew, due)

    def test_dead_pipe_oserror_escapes_native_maintain(self):
        native = bare_native()
        native.selector.select = lambda timeout: [(SimpleNamespace(fd=7, data='out', fileobj=object()), 1)]
        with patch.object(c.os, 'read', side_effect=OSError(32, 'private broken pipe')):
            with self.assertRaises(OSError) as caught:
                native.maintain()
        self.assertEqual(caught.exception.errno, 32)
        self.assertIn('private', str(caught.exception))

    def test_blocked_write_preserves_deadline_and_renews(self):
        native = bare_native(); native.next_renew = time.monotonic()-1
        with patch.object(c.os, 'write', side_effect=[BlockingIOError(), 3]): native.write_frame(b'abc')
        native.renew.assert_called_once()
        native.deadline = time.monotonic()-1
        with self.assertRaisesRegex(c.CopilotError, 'deadline'): native.write_frame(b'abc')

    def test_pre_prompt_activity_not_cleared_or_attributed_to_answer(self):
        native = bare_native()
        for kind in ('assistant.message', 'assistant.turn_start', 'assistant.usage'):
            with self.assertRaisesRegex(c.CopilotError, 'pre_prompt'): native.notification(notification(kind))
        native.notification(notification('session.info'))
        self.assertEqual(native.events, [])

    def test_session_setup_notices_are_not_pre_prompt_work(self):
        native = bare_native()
        for kind in SETUP_NOTICES:
            native.notification(notification(kind))
        self.assertEqual(native.events, [])

    def test_unknown_pre_prompt_event_names_its_kind(self):
        native = bare_native()
        with self.assertRaisesRegex(c.CopilotError, '^copilot_unexpected_pre_prompt_session_plan_changed$'):
            native.notification(notification('session.plan_changed'))
        with self.assertRaisesRegex(c.CopilotError, '^copilot_unexpected_pre_prompt_activity$'):
            native.notification(notification('Session.Weird Kind!'))

    def test_queued_pre_prompt_frame_blocks_send(self):
        native = bare_native()
        body = json.dumps(notification('assistant.message')).encode()
        native.buffer.extend(f'Content-Length: {len(body)}\r\n\r\n'.encode()+body)
        with patch.object(c.os, 'write') as write:
            with self.assertRaisesRegex(c.CopilotError, 'pre_prompt'):
                native.request('session.send', {'sessionId': 'sid', 'prompt': 'new'})
        write.assert_not_called(); self.assertFalse(native.send_started)

    def test_notifications_do_not_authorize_server_requests_or_other_sessions(self):
        native = bare_native()
        for value in [dict(notification('session.info'), id=8), notification('session.info', sid='other'),
                      {'jsonrpc': '2.0', 'method': 'tool.call', 'params': {}}]:
            with self.assertRaises(c.CopilotError): native.notification(value)
        native.notification({'jsonrpc': '2.0', 'method': 'gitHubTelemetry.event', 'params': {'private': 'discard'}})
        self.assertEqual(native.ignored_notification_count, 1)
        self.assertEqual(native.events, [])

    def test_duplicate_send_rejected_before_write(self):
        native = bare_native(); native.send_started = True
        with patch.object(c.os, 'write') as write:
            with self.assertRaisesRegex(c.CopilotError, 'duplicate_prompt'):
                native.request('session.send', {'sessionId': 'sid', 'prompt': 'again'})
        write.assert_not_called()

    def test_strict_id_type_and_value(self):
        for observed in (True, '1', 2):
            native = bare_native()
            native.write_frame = Mock()
            native.frame = Mock(return_value={'jsonrpc': '2.0', 'id': observed, 'result': {}})
            with self.assertRaisesRegex(c.CopilotError, 'native_request_failed'):
                native.request('account.getQuota')

    def test_partial_constructor_failure_reaps_spawned_process(self):
        process = SimpleNamespace(pid=234, stdin=Mock(), stdout=Mock(), stderr=Mock(),
                                  wait=Mock(return_value=-9), poll=Mock(return_value=-9))
        with patch.object(c.subprocess, 'Popen', return_value=process), \
             patch.object(c.selectors, 'DefaultSelector', side_effect=OSError('private')), \
             patch.object(c.os, 'killpg', create=True) as kill, \
             patch.object(c.signal, 'SIGKILL', 9, create=True):
            with self.assertRaises(c.NativeStartupStopped):
                c.Native(Path('fake'), lambda: None, time.monotonic()+10)
        kill.assert_called_once_with(234, 9)
        process.wait.assert_called_once_with(timeout=5)
        process.stdin.close.assert_called_once()


class MidTurnQuota(unittest.TestCase):
    def test_quota_signal_helper_maps_tokens_not_transient_429(self):
        secret = 'insufficient_quota for user@example.com'
        self.assertTrue(c._quota_exhausted_signal({'code': 'insufficient_quota', 'message': secret}))
        self.assertTrue(c._quota_exhausted_signal({'type': 'usage_limit'}))
        self.assertTrue(c._quota_exhausted_signal({'message': 'billing hard limit'}))
        self.assertTrue(c._quota_exhausted_signal({'message': 'credit exhausted'}))
        self.assertFalse(c._quota_exhausted_signal({'code': 429, 'message': 'rate_limit'}))
        self.assertFalse(c._quota_exhausted_signal({'code': 429, 'type': 'rate_limit'}))
        self.assertIs(c._quota_exhausted_signal(secret), True)

    def test_model_call_failure_with_quota_token_is_copilot_quota_exhausted(self):
        events = [{'type': 'assistant.turn_start', 'data': {}},
                  {'type': 'model.call_failure', 'data': {
                      'code': 'insufficient_quota',
                      'message': 'quota exceeded for user@example.com'}}]
        with self.assertRaises(c.CopilotError) as caught:
            c._response(SimpleNamespace(next_event=lambda: events.pop(0)))
        self.assertEqual(str(caught.exception), 'copilot_quota_exhausted')
        self.assertNotIn('example.com', str(caught.exception))

    def test_session_error_transient_429_stays_generic(self):
        events = [{'type': 'session.error', 'data': {'code': 429, 'message': 'rate_limit'}}]
        with self.assertRaises(c.CopilotError) as caught:
            c._response(SimpleNamespace(next_event=lambda: events.pop(0)))
        self.assertEqual(str(caught.exception), 'copilot_native_turn_failed_or_changed')
        self.assertNotIn('rate_limit', str(caught.exception))

    def test_jsonrpc_error_with_quota_token_is_copilot_quota_exhausted(self):
        native = bare_native()
        native.write_frame = Mock()
        native.frame = Mock(return_value={
            'jsonrpc': '2.0', 'id': 1,
            'error': {'code': 429, 'message': 'insufficient_quota for user@example.com'}})
        with self.assertRaises(c.CopilotError) as caught:
            native.request('account.getQuota')
        self.assertEqual(str(caught.exception), 'copilot_quota_exhausted')
        self.assertNotIn('example.com', str(caught.exception))

    def test_jsonrpc_error_plain_429_stays_generic(self):
        native = bare_native()
        native.write_frame = Mock()
        native.frame = Mock(return_value={
            'jsonrpc': '2.0', 'id': 1,
            'error': {'code': 429, 'message': 'rate_limit'}})
        with self.assertRaises(c.CopilotError) as caught:
            native.request('account.getQuota')
        self.assertEqual(str(caught.exception), 'copilot_native_request_failed')
        self.assertTrue(getattr(caught.exception, 'error_frame', False))

    def test_jsonrpc_id_mismatch_is_untagged_request_failed(self):
        native = bare_native()
        native.write_frame = Mock()
        native.frame = Mock(return_value={
            'jsonrpc': '2.0', 'id': 99, 'result': {}})
        with self.assertRaises(c.CopilotError) as caught:
            native.request('account.getQuota')
        self.assertEqual(str(caught.exception), 'copilot_native_request_failed')
        self.assertFalse(getattr(caught.exception, 'error_frame', False))

    def test_jsonrpc_missing_result_is_untagged_request_failed(self):
        native = bare_native()
        native.write_frame = Mock()
        native.frame = Mock(return_value={'jsonrpc': '2.0', 'id': 1})
        with self.assertRaises(c.CopilotError) as caught:
            native.request('account.getQuota')
        self.assertEqual(str(caught.exception), 'copilot_native_request_failed')
        self.assertFalse(getattr(caught.exception, 'error_frame', False))


class ExecuteErrorFrame(unittest.TestCase):
    """execute() outcomes must not move: error frames still fail with the same code."""
    setUp, tearDown, prepare = Lifecycle.setUp, Lifecycle.tearDown, Lifecycle.prepare

    def test_execute_error_frame_still_fails_with_native_request_failed(self):
        handle = self.prepare()
        native = handle.native
        real_request = native.request

        def boom(method, params=None):
            if method == 'account.getQuota' and native.index >= 7:
                # Second getQuota is execute()'s _fresh_metadata path.
                err = c.CopilotError('copilot_native_request_failed')
                err.error_frame = True
                native.index += 1
                native.calls.append((method, copy.deepcopy(params)))
                raise err
            return real_request(method, params)

        native.request = boom
        with self.assertRaisesRegex(c.CopilotError, '^copilot_native_request_failed$') as caught:
            c.execute(handle, 'project', time.monotonic() + 100)
        self.assertEqual(str(caught.exception), 'copilot_native_request_failed')
        self.assertTrue(handle.finished)
        self.assertNotIn('session.send', [x[0] for x in native.calls])


if __name__ == '__main__':
    unittest.main()
