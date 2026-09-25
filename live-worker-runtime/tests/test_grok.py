"""Offline live-Grok lifecycle/policy tests. No native executable or network."""
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
HUB = Path(__file__).resolve().parents[2] / 'agent-hub'
for entry in (str(HERE), str(HUB)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import broker_renew
import live_loop
import provider_errors
from agent_hub.cloud_credential_broker import BrokerError, Conflict, MutationUncertain
from agent_hub.credential_broker_service import BoundaryError
from providers import grok as g
from providers import _grok_protocol as wire


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


def stamp(offset):
    return datetime.fromtimestamp(time.time()+offset, timezone.utc).isoformat()


def billing():
    return {'config': {'onDemandCap': {}, 'prepaidBalance': {}, 'onDemandUsed': {},
                       'currentPeriod': {'start': stamp(-60), 'end': stamp(3600)}},
            'subscription_tier': 'SuperGrok Heavy', 'on_demand_enabled': None}


def catalog():
    return {'result': {'currentModelId': g.MODEL, 'availableModels': [{
        'modelId': g.MODEL, '_meta': {'supportsReasoningEffort': True,
                                    'reasoningEfforts': ['xhigh', 'high', 'medium', 'low']}}]}}


class Fixture:
    def __init__(self, *, overrides=None, task_hook=None, close_error=None, finish_error=None,
                 pump=False, prompt_renews=0, renew_due=False):
        self.events, self.calls = [], []
        self.overrides = overrides or {}
        self.task_hook = task_hook
        self.close_error, self.finish_error = close_error, finish_error
        self.pump, self.prompt_renews, self.renew_due = pump, prompt_renews, renew_due
        self.native = None
        self.factory = self._factory

    def _factory(self, home, renew, deadline):
        owner = self
        class FakeNative:
            def __init__(self):
                self.renew, self.deadline = renew, deadline
                self.next_renew = 0 if owner.renew_due else time.monotonic()+20
                self.notifications, self.internal_ack_counts = [], {'skills-reload': 1}
                self.closed = False
                self.process = SimpleNamespace(poll=lambda: 0 if self.closed else None)
            def request(self, method, params):
                if time.monotonic() >= self.deadline:
                    raise g.NativeError('native_deadline')
                if owner.pump and method == 'session/prompt' and owner.prompt_renews:
                    owner.calls.append((method, copy.deepcopy(params)))
                    for _ in range(owner.prompt_renews):
                        self.next_renew = 0
                        self.next_renew = broker_renew.next_due(self.renew(), 20)
                else:
                    if owner.pump and time.monotonic() >= self.next_renew:
                        self.next_renew = broker_renew.next_due(self.renew(), 20)
                    owner.calls.append((method, copy.deepcopy(params)))
                if method in owner.overrides:
                    value = owner.overrides[method]
                    if isinstance(value, Exception):
                        raise value
                    return copy.deepcopy(value() if callable(value) else value)
                if method == 'initialize':
                    return {'authMethods': [{'id': 'cached_token'}]}
                if method == 'authenticate':
                    return {}
                if method == 'x.ai/auth/info':
                    return {'email': g.OWNER, 'methodId': 'cached_token'}
                if method == 'x.ai/models/list':
                    return catalog()
                if method == 'x.ai/billing':
                    return billing()
                if method == 'x.ai/auto-topup-rule':
                    return {'rule': None}
                if method == 'session/new':
                    return {'sessionId': 'private-native-session', 'models': {
                        'currentModelId': g.MODEL, 'availableModels': [{
                            'modelId': g.MODEL, '_meta': {'reasoningEffort': g.EFFORT}}]}}
                if method == 'session/prompt':
                    nonce, sid = params['_meta']['promptId'], params['sessionId']
                    result = {'stopReason': 'end_turn', '_meta': {'modelId': g.MODEL,
                        'sessionId': sid, 'promptId': nonce, 'requestId': nonce,
                        'inputTokens': 123, 'outputTokens': 12, 'reasoningTokens': 3, 'cachedReadTokens': 20}}
                    self.notifications.append({'method': 'session/update', 'params': {
                        'sessionId': sid, '_meta': {'promptId': nonce}, 'update': {
                            'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Verified project answer.'}}}})
                    if owner.task_hook:
                        return owner.task_hook(self, params, result)
                    return result
                raise AssertionError('Unexpected method: '+method)
            def close(self):
                owner.events.append('stop')
                if owner.close_error:
                    raise owner.close_error
                self.closed = True
        self.native = FakeNative()
        return self.native

    def session(self, root):
        home = Path(root)/'home'
        (home/'.grok').mkdir(parents=True)
        (home/'.grok'/'auth.json').write_text('PRIVATE_OFFLINE_AUTH', encoding='utf-8')
        session = SimpleNamespace(state='active', home=home,
            lease=SimpleNamespace(account_ref=g.ACCOUNT_REF),
            broker=SimpleNamespace(assert_current=Mock(), renew=Mock(), quarantine=Mock()))
        def finish(*, native_stopped):
            assert native_stopped is True
            assert self.native is None or self.native.closed
            self.events.append('commit-release')
            if self.finish_error:
                raise self.finish_error
            session.state = 'committed'
            return 'projects/test/secrets/grok/versions/2'
        session.finish = Mock(side_effect=finish)
        return session


class GrokAdapter(unittest.TestCase):
    def prepare(self, fixture, root):
        session = fixture.session(root)
        with patch.object(g, 'NativeProcess', fixture.factory):
            handle = g.prepare(session, lambda: True, time.monotonic()+30)
        return session, handle

    def test_ready_with_unknown_native_usage_is_explicitly_project_prompt_only(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            ready = handle.readiness
            self.assertTrue(ready['authenticated'])
            self.assertTrue(ready['ready_for_project_prompt'])
            self.assertFalse(ready['automatic_improvement_ready'])
            self.assertFalse(ready['tools_enabled'])
            self.assertFalse(ready['full_coding_ready'])
            quota = ready['preflight']['quota']
            self.assertIsNone(quota['native_included_used_percent'])
            self.assertEqual(quota['native_usage_status'], 'unavailable')
            self.assertFalse(ready['preflight']['same_process_account_model_quota'])
            self.assertNotIn('browser', json.dumps(ready))
            self.assertEqual([name for name, _ in fixture.calls], ['initialize', 'authenticate',
                'x.ai/auth/info', 'x.ai/models/list', 'x.ai/billing', 'x.ai/auto-topup-rule', 'session/new'])
            self.assertEqual(fixture.calls[-1][1]['_meta']['reasoningEffort'], 'xhigh')
            self.assertNotIn('bufferingSettings', json.dumps(fixture.calls[0][1]))
            session.finish.assert_not_called()
            g.close(handle)

    def test_close_is_idempotent_and_commits_after_stop(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            version = g.close(handle)
            self.assertEqual(g.close(handle), version)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            session.finish.assert_called_once_with(native_stopped=True)
            self.assertFalse(handle.readiness['ready_for_project_prompt'])
            self.assertFalse(handle.readiness['authenticated'])

    def test_idle_maintain_renews_and_heartbeat_failure_aborts(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            fixture.native.next_renew = time.monotonic()-1
            self.assertTrue(g.maintain(handle)['ready_for_project_prompt'])
            session.broker.renew.assert_called_once_with(session.lease)
            fixture.native.next_renew = time.monotonic()-1
            handle.heartbeat = lambda: False
            with self.assertRaisesRegex(g.NativeError, 'heartbeat_lost'):
                g.maintain(handle)
            g.close(handle)

    def test_dead_idle_process_is_not_reported_as_ready(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            fixture.native.closed = True
            with self.assertRaisesRegex(g.NativeError, 'warm_process_ended'):
                g.maintain(handle)
            g.close(handle)

    def test_one_real_prompt_is_correlated_and_committed_before_result(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            result = g.execute(handle, 'Use all prior agent context; do not truncate.', time.monotonic()+30)
            self.assertEqual(result['text'], 'Verified project answer.')
            self.assertEqual(result['review_sha256'], hashlib.sha256(result['text'].encode()).hexdigest())
            self.assertEqual(result['native_internal_reload_acks'], {'skills-reload': 1})
            self.assertEqual(result['usage']['inputTokens'], 123)
            self.assertTrue(result['native_stopped'])
            self.assertEqual(result['credential_writeback'], 'committed')
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            self.assertEqual(sum(method == 'x.ai/billing' for method, _ in fixture.calls), 2)
            prompts = [params for method, params in fixture.calls if method == 'session/prompt']
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]['prompt'][0]['text'], 'Use all prior agent context; do not truncate.')
            self.assertNotIn('private-native-session', json.dumps(result))
            self.assertNotIn('PRIVATE_OFFLINE_AUTH', json.dumps(result))
            with self.assertRaisesRegex(g.NativeError, 'task_replay_forbidden'):
                g.execute(handle, 'Do not run this second task.', time.monotonic()+30)
            self.assertEqual(len([x for x in fixture.calls if x[0] == 'session/prompt']), 1)
            g.close(handle)
            session.finish.assert_called_once()

    def test_improvement_is_rejected_without_prompt_and_releases_cleanly(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            with self.assertRaisesRegex(g.NativeError, 'automatic_improvement_not_enabled'):
                g.execute(handle, 'Improve automatically.', time.monotonic()+30, task_kind='improvement')
            self.assertFalse(any(name == 'session/prompt' for name, _ in fixture.calls))
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_changed_spending_controls_are_rechecked_before_prompt(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            bad = billing(); bad['config']['prepaidBalance'] = {'val': 1}
            fixture.overrides['x.ai/billing'] = bad
            with self.assertRaisesRegex(g.NativeError, 'native_zero_spend_required'):
                g.execute(handle, 'Project.', time.monotonic()+30)
            self.assertFalse(any(name == 'session/prompt' for name, _ in fixture.calls))
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_wrong_final_prompt_identity_stops_without_replay(self):
        def wrong(native, params, result):
            result['_meta']['requestId'] = 'different'
            return result
        fixture = Fixture(task_hook=wrong)
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            with self.assertRaisesRegex(g.NativeError, 'answer_identity_mismatch'):
                g.execute(handle, 'Project.', time.monotonic()+30)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            self.assertEqual(sum(name == 'session/prompt' for name, _ in fixture.calls), 1)
            with self.assertRaises(g.NativeError):
                g.execute(handle, 'No replay.', time.monotonic()+30)

    def test_replay_untagged_and_other_session_chunks_are_excluded(self):
        def mixed(native, params, result):
            correct = copy.deepcopy(native.notifications[0])
            untagged = copy.deepcopy(correct); untagged['params'].pop('_meta')
            replay = copy.deepcopy(correct); replay['params']['_meta']['isReplay'] = True
            wrong_session = copy.deepcopy(correct); wrong_session['params']['sessionId'] = 'other'
            for row in (untagged, replay, wrong_session):
                row['params']['update']['content']['text'] = 'MUST_NOT_ACCEPT'
            native.notifications = [untagged, replay, wrong_session, correct]
            return result
        fixture = Fixture(task_hook=mixed)
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            self.assertEqual(g.execute(handle, 'Project.', time.monotonic()+30)['text'], 'Verified project answer.')

    def test_no_correlated_text_is_a_failure(self):
        def no_text(native, params, result):
            native.notifications[0]['params']['_meta']['isReplay'] = True
            return result
        fixture = Fixture(task_hook=no_text)
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            with self.assertRaisesRegex(g.NativeError, 'correlated_answer_missing'):
                g.execute(handle, 'Project.', time.monotonic()+30)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_full_prompt_and_long_answer_are_not_commissioning_word_limited(self):
        answer = 'Project response with many useful details. '*200
        def long_answer(native, params, result):
            native.notifications[0]['params']['update']['content']['text'] = answer
            return result
        fixture = Fixture(task_hook=long_answer)
        prompt = 'p'*200000
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            result = g.execute(handle, prompt, time.monotonic()+30)
            self.assertEqual(result['text'], answer.strip())
            self.assertEqual([p for m, p in fixture.calls if m == 'session/prompt'][0]['prompt'][0]['text'], prompt)

    def test_prompt_and_answer_byte_limits_are_enforced_without_truncation(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            with self.assertRaisesRegex(g.NativeError, 'prompt_limit'):
                g.execute(handle, 'é'*100001, time.monotonic()+30)
            self.assertFalse(any(name == 'session/prompt' for name, _ in fixture.calls))
        def too_large(native, params, result):
            native.notifications[0]['params']['update']['content']['text'] = 'x'*15001
            return result
        fixture = Fixture(task_hook=too_large)
        with tempfile.TemporaryDirectory() as root:
            _, handle = self.prepare(fixture, root)
            with self.assertRaisesRegex(g.NativeError, 'missing_or_large'):
                g.execute(handle, 'Project.', time.monotonic()+30)

    def test_stop_failure_quarantines_without_commit_or_cleanup_replay(self):
        fixture = Fixture(close_error=g.NativeError('synthetic_stop_failure'))
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            with self.assertRaisesRegex(g.NativeError, 'synthetic_stop_failure'):
                g.close(handle)
            session.finish.assert_not_called()
            session.broker.quarantine.assert_called_once()
            with self.assertRaisesRegex(g.NativeError, 'close_requires_reconciliation'):
                g.close(handle)
            self.assertEqual(fixture.events, ['stop'])

    def test_commit_failure_is_not_retried_by_execute_or_close(self):
        fixture = Fixture(finish_error=g.NativeError('synthetic_writeback_uncertain'))
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            with self.assertRaisesRegex(g.NativeError, 'synthetic_writeback_uncertain'):
                g.execute(handle, 'Project.', time.monotonic()+30)
            session.finish.assert_called_once()
            with self.assertRaisesRegex(g.NativeError, 'close_requires_reconciliation'):
                g.close(handle)
            session.finish.assert_called_once()

    def test_constructor_unknown_stop_never_commits(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            with patch.object(g, 'NativeProcess', side_effect=wire.NativeStopUncertain('synthetic')):
                with self.assertRaisesRegex(g.NativeError, 'native_stop_unconfirmed'):
                    g.prepare(session, lambda: True, time.monotonic()+30)
            session.finish.assert_not_called()
            session.broker.quarantine.assert_called_once()

    def test_confirmed_constructor_cleanup_can_release(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            with patch.object(g, 'NativeProcess', side_effect=wire.NativeStartupStopped('synthetic')):
                with self.assertRaises(wire.NativeStartupStopped):
                    g.prepare(session, lambda: True, time.monotonic()+30)
            session.finish.assert_called_once_with(native_stopped=True)

    def test_unknown_effort_owner_api_route_and_selected_effort_fail_before_ready(self):
        invalid_catalog = catalog(); invalid_catalog['result']['availableModels'][0]['_meta']['reasoningEfforts'].append('future-super')
        invalid_session = {'sessionId': 's', 'models': {'currentModelId': g.MODEL,
            'availableModels': [{'modelId': g.MODEL, '_meta': {'reasoningEffort': 'high'}}]}}
        cases = [
            {'x.ai/models/list': invalid_catalog},
            {'x.ai/auth/info': {'methodId': 'cached_token', 'email': 'another@example.com'}},
            {'initialize': {'authMethods': [{'id': 'cached_token'}, {'id': 'xai.api_key'}]}},
            {'session/new': invalid_session},
        ]
        for overrides in cases:
            with self.subTest(overrides=list(overrides)), tempfile.TemporaryDirectory() as root:
                fixture = Fixture(overrides=overrides); session = fixture.session(root)
                with patch.object(g, 'NativeProcess', fixture.factory), self.assertRaises(g.NativeError):
                    g.prepare(session, lambda: True, time.monotonic()+30)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])
                self.assertFalse(any(name == 'session/prompt' for name, _ in fixture.calls))


    def _beats(self):
        seen = []

        def beat():
            seen.append(True)
            return True

        return seen, beat

    def test_transient_idle_renew_failure_keeps_session_and_retries(self):
        errors = [MutationUncertain('broker_http_outcome_uncertain'), BoundaryError('transport_failed'),
                  BoundaryError('transport_limit'), BoundaryError('metadata_identity_unavailable')]
        for error in errors:
            with self.subTest(error=str(error)):
                clock = StepClock()
                with patch('time.monotonic', clock), tempfile.TemporaryDirectory() as root:
                    fixture = Fixture()
                    session, handle = self.prepare(fixture, root)
                    beats = []
                    handle.heartbeat = lambda: beats.append(True) or True
                    anchor = bind_lease_clock(handle, clock)
                    session.broker.renew.reset_mock()
                    session.broker.renew.side_effect = error
                    fixture.native.next_renew = clock() - 1
                    ready = g.maintain(handle)
                    self.assertTrue(ready['ready_for_project_prompt'])
                    self.assertEqual(fixture.events, [])
                    session.finish.assert_not_called()
                    session.broker.quarantine.assert_not_called()
                    self.assertEqual(beats, [True])
                    self.assertEqual(handle.lease_clock.renewed_at, anchor)
                    self.assertTrue(handle.lease_clock.degraded)
                    self.assertEqual(fixture.native.next_renew, clock() + broker_renew.RETRY_SECONDS)
                    clock.advance()
                    session.broker.renew.side_effect = None
                    fixture.native.next_renew = clock() - 1
                    g.maintain(handle)
                    self.assertEqual(session.broker.renew.call_count, 2)
                    self.assertFalse(handle.lease_clock.degraded)
                    # A confirmed renew stamps the clock at the start of the attempt.
                    # That reading can equal the previous anchor when the clock did not move.
                    self.assertGreaterEqual(handle.lease_clock.renewed_at, anchor)
                    self.assertEqual(handle.lease_clock.renewed_at, clock())
                    self.assertEqual(fixture.native.next_renew, clock() + 20)
                    g.close(handle)

    def test_renew_rejections_fail_idle_at_once(self):
        errors = [Conflict('broker_operation_rejected'), BrokerError('broker_request_denied'),
                  BoundaryError('credential_fence_lost'), BoundaryError('request_invalid')]
        for error in errors:
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as root:
                fixture = Fixture()
                session, handle = self.prepare(fixture, root)
                session.broker.renew.side_effect = error
                fixture.native.next_renew = time.monotonic() - 1
                with self.assertRaisesRegex(g.NativeError, '^grok_broker_renew_rejected$') as caught:
                    g.maintain(handle)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn(str(error), str(caught.exception))
                self.assertEqual(live_loop.maintain_fault(caught.exception), 'fail')
                self.assertEqual(fixture.events, [])
                self.assertEqual(session.broker.renew.call_count, 1)
                g.close(handle)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_renew_failure_past_window_fails_with_vetted_code(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            session.broker.renew.side_effect = MutationUncertain('private detail')
            fixture.native.next_renew = time.monotonic() - 1
            with self.assertRaisesRegex(g.NativeError, '^grok_broker_renew_failed$') as caught:
                g.maintain(handle)
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(provider_errors.error_code(caught.exception), 'grok_broker_renew_failed')
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'fail')
            self.assertNotIn('grok_broker_renew_failed', live_loop._IDLE_RETRY_CODES)
            self.assertNotIn('grok_broker_renew_failed', live_loop._IDLE_DRAIN_CODES)
            self.assertEqual(fixture.events, [])
            g.close(handle)

    def test_tolerated_renew_failure_still_heartbeats(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            session.broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
            fixture.native.next_renew = due = time.monotonic() - 1
            handle.heartbeat = lambda: False
            with self.assertRaisesRegex(g.NativeError, '^grok_hub_heartbeat_lost$') as caught:
                g.maintain(handle)
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'retry')
            self.assertEqual(fixture.native.next_renew, due)
            self.assertIsNone(fixture.native.process.poll())
            g.close(handle)

    def test_prepare_anchors_lease_clock(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            before = time.monotonic()
            session, handle = self.prepare(fixture, root)
            after = time.monotonic()
            self.assertGreaterEqual(handle.lease_clock.renewed_at, before)
            self.assertLessEqual(handle.lease_clock.renewed_at, after)
            session.broker.renew.assert_not_called()
            g.close(handle)
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            session.lease.lease_id = 'x' * 32
            t0 = time.monotonic() - 5
            with patch.dict(broker_renew._acquire_started, {'x' * 32: t0}):
                with patch.object(g, 'NativeProcess', fixture.factory):
                    handle = g.prepare(session, lambda: True, time.monotonic() + 30)
            self.assertEqual(handle.lease_clock.renewed_at, t0)
            session.broker.renew.assert_not_called()
            g.close(handle)

    def test_transient_renew_failure_during_turn_keeps_the_turn(self):
        fixture = Fixture(pump=True, prompt_renews=3)
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            seen = []
            handle.heartbeat = lambda: seen.append(handle.lease_clock.degraded) or True
            session.broker.renew.side_effect = [None, MutationUncertain('x'), None]
            result = g.execute(handle, 'Project.', time.monotonic() + 30)
            self.assertEqual(result['text'], 'Verified project answer.')
            self.assertEqual(sum(name == 'session/prompt' for name, _ in fixture.calls), 1)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            self.assertIn(True, seen)

    def test_renew_failure_past_window_during_turn_fails_with_vetted_code(self):
        fixture = Fixture(pump=True, prompt_renews=1)
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            session.broker.renew.side_effect = MutationUncertain('private detail')
            with self.assertRaisesRegex(g.NativeError, '^grok_broker_renew_failed$'):
                g.execute(handle, 'Project.', time.monotonic() + 30)
            self.assertEqual(sum(name == 'session/prompt' for name, _ in fixture.calls), 1)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            with self.assertRaisesRegex(g.NativeError, 'task_replay_forbidden'):
                g.execute(handle, 'No second prompt.', time.monotonic() + 30)
            self.assertEqual(sum(name == 'session/prompt' for name, _ in fixture.calls), 1)

    def test_conflict_during_turn_is_rejected_at_once(self):
        fixture = Fixture(pump=True, prompt_renews=1)
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            session.broker.renew.side_effect = Conflict('broker_operation_rejected')
            with self.assertRaisesRegex(g.NativeError, '^grok_broker_renew_rejected$'):
                g.execute(handle, 'Project.', time.monotonic() + 30)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_degraded_lease_gets_strict_renew_before_prompt(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            session.broker.renew.side_effect = MutationUncertain('x')
            fixture.native.next_renew = time.monotonic() - 1
            g.maintain(handle)
            self.assertTrue(handle.lease_clock.degraded)
            session.broker.renew.side_effect = None
            before = session.broker.renew.call_count
            result = g.execute(handle, 'Project.', time.monotonic() + 30)
            self.assertEqual(result['text'], 'Verified project answer.')
            self.assertEqual(session.broker.renew.call_count, before + 1)
            self.assertEqual(sum(name == 'session/prompt' for name, _ in fixture.calls), 1)
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            session.broker.renew.side_effect = MutationUncertain('x')
            fixture.native.next_renew = time.monotonic() - 1
            g.maintain(handle)
            with self.assertRaisesRegex(g.NativeError, '^grok_broker_renew_failed$'):
                g.execute(handle, 'Project.', time.monotonic() + 30)
            self.assertFalse(any(name == 'session/prompt' for name, _ in fixture.calls))
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            before = session.broker.renew.call_count
            g.execute(handle, 'Project.', time.monotonic() + 30)
            self.assertEqual(session.broker.renew.call_count, before)

    def test_startup_pump_tolerates_then_fails_past_window(self):
        fixture = Fixture(pump=True, renew_due=True)
        state = {'n': 0}

        def effect(lease):
            state['n'] += 1
            if state['n'] == 1:
                raise MutationUncertain('broker_http_outcome_uncertain')

        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            session.broker.renew.side_effect = effect
            with patch.object(g, 'NativeProcess', fixture.factory):
                handle = g.prepare(session, lambda: True, time.monotonic() + 30)
            self.assertTrue(handle.readiness['ready_for_project_prompt'])
            g.close(handle)
        fixture = Fixture(pump=True, renew_due=True)
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            session.lease.lease_id = 'y' * 32
            session.broker.renew.side_effect = MutationUncertain('x')
            old = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            with patch.dict(broker_renew._acquire_started, {'y' * 32: old}):
                with patch.object(g, 'NativeProcess', fixture.factory):
                    with self.assertRaisesRegex(g.NativeError, '^grok_broker_renew_failed$'):
                        g.prepare(session, lambda: True, time.monotonic() + 30)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_worker_survives_one_idle_blip_and_fails_after_window(self):
        origin = time.monotonic()
        state = {'now': origin}

        def now():
            return state['now']

        def sleep(seconds):
            state['now'] += seconds

        class IdleClient:
            def __init__(self):
                self.claims = 0

            def post(self, path, value):
                if str(path).endswith('/claim'):
                    self.claims += 1
                    return {'task': None}
                return {'accepted': True, 'active': True, 'deadline': 10 ** 12, 'server_time': 1}

            def get_room(self, room_id):
                raise AssertionError(room_id)

        calls = {'n': 0}

        def effect(lease):
            calls['n'] += 1
            if calls['n'] == 1:
                raise MutationUncertain('broker_http_outcome_uncertain')

        fixture = Fixture(renew_due=True)
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            session.broker.renew.side_effect = effect
            client = IdleClient()
            with patch.object(g, 'NativeProcess', fixture.factory), patch('time.monotonic', now):
                worker = live_loop.Worker(live_loop.Settings('grok', 'grok-live', warm_seconds=60),
                                          client, g, session, clock=now, sleep=sleep, log=lambda record: None)
                result = worker.run()
            self.assertEqual(result['outcome'], 'idle_drained')
            self.assertEqual(worker.last_exit, 0)
        fixture = Fixture(renew_due=True)
        maintains = {'n': 0}
        real = g.maintain

        def counting(handle):
            maintains['n'] += 1
            return real(handle)

        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            session.lease.lease_id = 'z' * 32
            session.broker.renew.side_effect = MutationUncertain('x')
            old = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            client = IdleClient()
            state['now'] = origin
            with patch.dict(broker_renew._acquire_started, {'z' * 32: old}), \
                    patch.object(g, 'NativeProcess', fixture.factory), \
                    patch.object(g, 'maintain', counting), \
                    patch('time.monotonic', now):
                worker = live_loop.Worker(live_loop.Settings('grok', 'grok-live', warm_seconds=60),
                                          client, g, session, clock=now, sleep=sleep, log=lambda record: None)
                result = worker.run()
            self.assertEqual(result['outcome'], 'failed')
            self.assertEqual(result['error_code'], 'grok_broker_renew_failed')
            self.assertEqual(worker.last_exit, 1)
            self.assertEqual(maintains['n'], 1)
            self.assertEqual(client.claims, 0)

    def test_quota_refresh_fires_after_quota_refresh_seconds_not_before(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                billing_calls = sum(1 for m, _ in fixture.calls if m == 'x.ai/billing')
                g.maintain(handle)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), billing_calls)
                clock.advance(g.QUOTA_REFRESH_SECONDS - 1)
                g.maintain(handle)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), billing_calls)
                clock.advance(1)
                old_deadline = handle.native.deadline
                g.maintain(handle)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), billing_calls + 1)
                self.assertEqual(handle.native.deadline, old_deadline)
                g.close(handle)

    def test_quota_refresh_success_replaces_observed_at(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                prepare_deadline = handle.native.deadline
                clock.advance(prepare_deadline - clock.now + 1)
                self.assertGreaterEqual(clock.now, prepare_deadline)
                data = billing()
                data['config']['creditUsagePercent'] = 40
                fixture.overrides['x.ai/billing'] = data
                handle.next_quota_refresh = clock.now
                old_deadline = handle.native.deadline
                with patch.object(g, '_utc', return_value='2099-06-01T12:00:00+00:00'):
                    g.maintain(handle)
                self.assertEqual(handle.preflight['quota']['observed_at'], '2099-06-01T12:00:00+00:00')
                self.assertEqual(handle.preflight['quota']['native_included_used_percent'], 40)
                self.assertEqual(handle.next_quota_refresh, clock.now + g.QUOTA_REFRESH_SECONDS)
                self.assertEqual(handle.native.deadline, old_deadline)
                g.close(handle)

    def test_quota_refresh_validation_failure_keeps_old_row_and_retries_at_120s(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                old = copy.deepcopy(handle.preflight['quota'])
                fixture.overrides['x.ai/billing'] = {'broken': True}
                handle.next_quota_refresh = clock.now
                before = sum(1 for m, _ in fixture.calls if m == 'x.ai/billing')
                readiness = g.maintain(handle)
                self.assertTrue(readiness['ready_for_project_prompt'])
                self.assertEqual(handle.preflight['quota'], old)
                self.assertEqual(handle.next_quota_refresh, clock.now + g.QUOTA_RETRY_SECONDS)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), before + 1)
                clock.advance(g.QUOTA_RETRY_SECONDS - 1)
                g.maintain(handle)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), before + 1)
                clock.advance(1)
                g.maintain(handle)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), before + 2)
                self.assertEqual(handle.preflight['quota'], old)
                g.close(handle)

    def test_quota_refresh_exhausted_raises_quota_park(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                old = copy.deepcopy(handle.preflight['quota'])
                bad = billing()
                bad['config']['creditUsagePercent'] = 100
                fixture.overrides['x.ai/billing'] = bad
                handle.next_quota_refresh = clock.now
                with self.assertRaisesRegex(g.NativeError, '^grok_quota_exhausted$') as caught:
                    g.maintain(handle)
                self.assertEqual(provider_errors.error_code(caught.exception), 'grok_quota_exhausted')
                self.assertTrue(provider_errors.is_quota(provider_errors.error_code(caught.exception)))
                self.assertEqual(handle.preflight['quota'], old)
                g.close(handle)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_quota_refresh_error_frame_exhausted_raises_quota_park(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                old = copy.deepcopy(handle.preflight['quota'])
                fixture.overrides['x.ai/billing'] = g.NativeError('grok_quota_exhausted')
                handle.next_quota_refresh = clock.now
                with self.assertRaisesRegex(g.NativeError, '^grok_quota_exhausted$'):
                    g.maintain(handle)
                self.assertEqual(handle.preflight['quota'], old)
                g.close(handle)

    def test_quota_refresh_error_frame_keeps_old_row_and_retries_at_120s(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                old = copy.deepcopy(handle.preflight['quota'])
                # Intact error frame via rpc_error_code (negative JSON-RPC code).
                negative = wire.rpc_error_code({'code': -32603, 'message': 'x'})
                self.assertEqual(negative, 'native_rpc_-32603')
                fixture.overrides['x.ai/billing'] = g.NativeError(negative)
                handle.next_quota_refresh = clock.now
                before = sum(1 for m, _ in fixture.calls if m == 'x.ai/billing')
                readiness = g.maintain(handle)
                self.assertTrue(readiness['ready_for_project_prompt'])
                self.assertEqual(handle.preflight['quota'], old)
                self.assertEqual(handle.next_quota_refresh, clock.now + g.QUOTA_RETRY_SECONDS)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), before + 1)
                clock.advance(g.QUOTA_RETRY_SECONDS - 1)
                g.maintain(handle)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), before + 1)
                clock.advance(1)
                # Positive rpc code also swallows; native_rpc_failed likewise.
                fixture.overrides['x.ai/billing'] = g.NativeError('native_rpc_429')
                g.maintain(handle)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), before + 2)
                self.assertEqual(handle.preflight['quota'], old)
                self.assertEqual(handle.next_quota_refresh, clock.now + g.QUOTA_RETRY_SECONDS)
                clock.advance(g.QUOTA_RETRY_SECONDS)
                fixture.overrides['x.ai/billing'] = g.NativeError('native_rpc_failed')
                g.maintain(handle)
                self.assertEqual(sum(1 for m, _ in fixture.calls if m == 'x.ai/billing'), before + 3)
                self.assertEqual(handle.preflight['quota'], old)
                g.close(handle)

    def test_quota_refresh_swallowed_codes_cover_negative_positive_and_failed(self):
        negative = wire.rpc_error_code({'code': -32603, 'message': 'x'})
        self.assertTrue(g._grok_quota_refresh_swallowed(g.NativeError(negative)))
        self.assertTrue(g._grok_quota_refresh_swallowed(g.NativeError('native_rpc_429')))
        self.assertTrue(g._grok_quota_refresh_swallowed(g.NativeError('native_rpc_failed')))
        self.assertFalse(g._grok_quota_refresh_swallowed(g.NativeError('grok_quota_exhausted')))
        self.assertFalse(g._grok_quota_refresh_swallowed(g.NativeError('native_tool_observed')))

    def test_quota_refresh_passthrough_renew_heartbeat_and_tool(self):
        cases = [
            ('grok_broker_renew_rejected', 'fail'),
            ('grok_hub_heartbeat_lost', 'retry'),
            ('native_tool_observed', 'fail'),
        ]
        for code, fault in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                clock = StepClock()
                fixture = Fixture()
                with patch('time.monotonic', clock):
                    _, handle = self.prepare(fixture, root)
                    clock.advance(handle.native.deadline - clock.now + 1)
                    fixture.overrides['x.ai/billing'] = g.NativeError(code)
                    handle.next_quota_refresh = clock.now
                    with self.assertRaisesRegex(g.NativeError, '^' + code + '$') as caught:
                        g.maintain(handle)
                    self.assertEqual(str(caught.exception), code)
                    self.assertEqual(live_loop.maintain_fault(caught.exception), fault)
                    g.close(handle)

    def test_quota_refresh_owner_mismatch_raises_and_close_skips_commit(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                session, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                fixture.overrides['x.ai/auth/info'] = {
                    'methodId': 'cached_token', 'email': 'another@example.com'}
                handle.next_quota_refresh = clock.now
                with self.assertRaisesRegex(g.NativeError, '^grok_owner_mismatch$'):
                    g.maintain(handle)
                self.assertFalse(handle.preflight['account']['native_owner_verified'])
                with self.assertRaisesRegex(g.NativeError, 'owner_unverified_at_close|close_requires_reconciliation'):
                    g.close(handle)
                session.finish.assert_not_called()
                session.broker.quarantine.assert_called()
                self.assertEqual(fixture.events, ['stop'])

    def test_quota_refresh_auth_info_missing_email_keeps_verified_and_commits(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                fixture.overrides['x.ai/auth/info'] = {'methodId': 'cached_token'}
                handle.next_quota_refresh = clock.now
                readiness = g.maintain(handle)
                self.assertTrue(readiness['ready_for_project_prompt'])
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                self.assertEqual(handle.next_quota_refresh, clock.now + g.QUOTA_RETRY_SECONDS)
                g.close(handle)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_quota_refresh_auth_info_wrong_method_id_with_owner_email_keeps_verified(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                fixture.overrides['x.ai/auth/info'] = {
                    'methodId': 'other_method', 'email': g.OWNER}
                handle.next_quota_refresh = clock.now
                readiness = g.maintain(handle)
                self.assertTrue(readiness['ready_for_project_prompt'])
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                self.assertEqual(handle.next_quota_refresh, clock.now + g.QUOTA_RETRY_SECONDS)
                g.close(handle)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_quota_refresh_auth_info_user_blocked_parks_keeps_verified_and_commits(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                fixture.overrides['x.ai/auth/info'] = {
                    'methodId': 'cached_token', 'email': g.OWNER,
                    'userBlockedReason': 'abuse'}
                handle.next_quota_refresh = clock.now
                with self.assertRaisesRegex(g.NativeError, '^grok_account_blocked_quota_exhausted$') as caught:
                    g.maintain(handle)
                code = provider_errors.error_code(caught.exception)
                self.assertTrue(provider_errors.is_quota(code))
                self.assertEqual(live_loop.maintain_fault(caught.exception), 'fail')
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                g.close(handle)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_quota_refresh_auth_info_error_frame_keeps_owner_verified(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                negative = wire.rpc_error_code({'code': -32603, 'message': 'x'})
                self.assertEqual(negative, 'native_rpc_-32603')
                fixture.overrides['x.ai/auth/info'] = g.NativeError(negative)
                handle.next_quota_refresh = clock.now
                readiness = g.maintain(handle)
                self.assertTrue(readiness['ready_for_project_prompt'])
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                self.assertEqual(handle.next_quota_refresh, clock.now + g.QUOTA_RETRY_SECONDS)
                g.close(handle)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_quota_refresh_auth_info_deadline_drains_keeps_owner_verified(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                fixture.overrides['x.ai/auth/info'] = g.NativeError('native_deadline')
                handle.next_quota_refresh = clock.now
                with self.assertRaisesRegex(g.NativeError, '^grok_quota_refresh_transport_lost$') as caught:
                    g.maintain(handle)
                self.assertEqual(live_loop.maintain_fault(caught.exception), 'drain')
                self.assertTrue(handle.preflight['account']['native_owner_verified'])
                g.close(handle)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_quota_refresh_transport_failure_propagates_from_maintain(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                fixture.overrides['x.ai/billing'] = g.NativeError('unexpected_response')
                handle.next_quota_refresh = clock.now
                with self.assertRaisesRegex(g.NativeError, '^grok_quota_refresh_transport_lost$') as caught:
                    g.maintain(handle)
                self.assertEqual(str(caught.exception), 'grok_quota_refresh_transport_lost')
                self.assertEqual(provider_errors.error_code(caught.exception),
                                 'grok_quota_refresh_transport_lost')
                self.assertEqual(live_loop.maintain_fault(caught.exception), 'drain')
                g.close(handle)

    def test_quota_refresh_deadline_is_transport_lost_drain(self):
        clock = StepClock()
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            with patch('time.monotonic', clock):
                _, handle = self.prepare(fixture, root)
                clock.advance(handle.native.deadline - clock.now + 1)
                fixture.overrides['x.ai/billing'] = g.NativeError('native_deadline')
                handle.next_quota_refresh = clock.now
                with self.assertRaisesRegex(g.NativeError, '^grok_quota_refresh_transport_lost$') as caught:
                    g.maintain(handle)
                self.assertEqual(live_loop.maintain_fault(caught.exception), 'drain')
                g.close(handle)

    def test_worker_idle_quota_refresh_schema_failure_keeps_observed_at(self):
        origin = 1_000_000.0
        state = {'now': origin}

        def now():
            return state['now']

        def sleep(seconds):
            state['now'] += seconds

        class IdleClient:
            def __init__(self):
                self.claims = 0

            def post(self, path, value):
                if str(path).endswith('/claim'):
                    self.claims += 1
                    return {'task': None}
                return {'accepted': True, 'active': True, 'deadline': 10 ** 12, 'server_time': 1}

            def get_room(self, room_id):
                raise AssertionError(room_id)

        fixture = Fixture()
        captured = {}
        real_prepare = g.prepare

        def arm(session, heartbeat, deadline):
            handle = real_prepare(session, heartbeat, deadline)
            captured['observed_at'] = handle.preflight['quota']['observed_at']
            captured['handle'] = handle
            fixture.overrides['x.ai/billing'] = {'broken': True}
            handle.next_quota_refresh = now()
            return handle

        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            client = IdleClient()
            with patch.object(g, 'NativeProcess', fixture.factory), \
                    patch.object(g, 'prepare', arm), \
                    patch('time.monotonic', now):
                worker = live_loop.Worker(
                    live_loop.Settings('grok', 'grok-live', warm_seconds=300, poll_seconds=10),
                    client, g, session, clock=now, sleep=sleep, log=lambda record: None)
                result = worker.run()
            self.assertEqual(result['outcome'], 'idle_drained')
            self.assertEqual(worker.last_exit, 0)
            self.assertNotIn('error_code', result)
            handle = captured['handle']
            self.assertEqual(handle.preflight['quota']['observed_at'], captured['observed_at'])
            billing_after_prepare = sum(1 for m, _ in fixture.calls if m == 'x.ai/billing')
            # prepare (1) + due refresh + retry at +QUOTA_RETRY_SECONDS
            self.assertGreaterEqual(billing_after_prepare, 3)
            # Last swallowed refresh scheduled another retry 120s out from its tick.
            self.assertGreaterEqual(handle.next_quota_refresh, origin + g.QUOTA_RETRY_SECONDS)

    def test_worker_idle_quota_refresh_exhaustion_parks_without_claim(self):
        origin = 1_000_000.0
        state = {'now': origin}

        def now():
            return state['now']

        def sleep(seconds):
            state['now'] += seconds

        class IdleClient:
            def __init__(self):
                self.claims = 0

            def post(self, path, value):
                if str(path).endswith('/claim'):
                    self.claims += 1
                    return {'task': None}
                return {'accepted': True, 'active': True, 'deadline': 10 ** 12, 'server_time': 1}

            def get_room(self, room_id):
                raise AssertionError(room_id)

        fixture = Fixture()
        real_prepare = g.prepare

        def arm(session, heartbeat, deadline):
            handle = real_prepare(session, heartbeat, deadline)
            bad = billing()
            bad['config']['creditUsagePercent'] = 100
            fixture.overrides['x.ai/billing'] = bad
            handle.next_quota_refresh = now()
            return handle

        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            client = IdleClient()
            with patch.object(g, 'NativeProcess', fixture.factory), \
                    patch.object(g, 'prepare', arm), \
                    patch('time.monotonic', now):
                worker = live_loop.Worker(
                    live_loop.Settings('grok', 'grok-live', warm_seconds=60, poll_seconds=10),
                    client, g, session, clock=now, sleep=sleep, log=lambda record: None)
                result = worker.run()
            self.assertEqual(result['outcome'], 'failed')
            self.assertEqual(result['error_code'], 'grok_quota_exhausted')
            self.assertTrue(result.get('provider_quota_exhausted'))
            self.assertEqual(worker.last_exit, provider_errors.QUOTA_EXIT_CODE)
            self.assertEqual(client.claims, 0)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            session.finish.assert_called_once_with(native_stopped=True)


class BillingPolicy(unittest.TestCase):
    def test_native_omissions_are_unavailable_and_cent_empty_is_zero(self):
        result = g._billing(billing(), {'rule': {}})
        self.assertIsNone(result['native_included_used_percent'])
        self.assertEqual(result['on_demand_cap_cents'], 0)
        self.assertFalse(result['auto_topup_enabled'])

    def test_percentage_and_legacy_native_usage(self):
        data = billing(); data['config']['creditUsagePercent'] = 25
        self.assertEqual(g._billing(data, {})['native_included_used_percent'], 25)
        data = billing(); data['config'].update(monthlyLimit={'val': '100'}, used={'val': 20})
        result = g._billing(data, {})
        self.assertEqual(result['native_included_used_percent'], 20)
        self.assertEqual(result['usage_source'], 'native_legacy_cents')

    def test_unknown_or_paid_controls_and_invalid_usage_are_rejected(self):
        cases = []
        for key, value in [('onDemandCap', None), ('onDemandCap', {'val': 1}),
                           ('prepaidBalance', None), ('prepaidBalance', {'val': True}),
                           ('creditUsagePercent', True), ('creditUsagePercent', float('nan')),
                           ('creditUsagePercent', 100), ('monthlyLimit', {'val': 0})]:
            data = billing(); data['config'][key] = value; cases.append((data, {'rule': None}))
        for topup in ({'rule': {'enabled': True}}, {'rule': {'enabled': 'false'}}, {'raw': {}}):
            cases.append((billing(), topup))
        expired = billing(); expired['config']['currentPeriod']['end'] = stamp(-1); cases.append((expired, {}))
        tier = billing(); tier['subscription_tier'] = 'Free'; cases.append((tier, {}))
        for data, topup in cases:
            with self.subTest(data=data, topup=topup), self.assertRaises(g.NativeError):
                g._billing(data, topup)


class TransportSmoke(unittest.TestCase):
    def transport(self, frames):
        process = SimpleNamespace(pid=424242, stdin=io.BytesIO(),
            stdout=io.BytesIO(b''.join(json.dumps(row).encode()+b'\n' for row in frames)), wait=Mock(return_value=0))
        return process

    def test_exact_watcher_ack_is_counted_before_strict_matching_reply(self):
        process = self.transport([
            {'jsonrpc': '2.0', 'id': 'skills-reload', 'result': {'result': {'reloaded': 1}}},
            {'jsonrpc': '2.0', 'id': '1', 'result': {'email': 'fixture'}}])
        with patch.object(wire.subprocess, 'Popen', return_value=process), patch.object(wire.os, 'killpg', create=True), \
                patch.object(wire.signal, 'SIGKILL', 9, create=True):
            native = wire.Native(Path('/offline'), lambda: None, time.monotonic()+5)
            self.assertEqual(native.request('x.ai/auth/info', {}), {'email': 'fixture'})
            self.assertEqual(native.internal_ack_counts, {'skills-reload': 1})
            native.close()

    def test_unrelated_string_id_is_not_swallowed(self):
        process = self.transport([{'jsonrpc': '2.0', 'id': 'unrelated', 'result': {}}])
        with patch.object(wire.subprocess, 'Popen', return_value=process), patch.object(wire.os, 'killpg', create=True), \
                patch.object(wire.signal, 'SIGKILL', 9, create=True):
            native = wire.Native(Path('/offline'), lambda: None, time.monotonic()+5)
            with self.assertRaisesRegex(g.NativeError, 'unexpected_response'):
                native.request('x.ai/auth/info', {})
            native.close()

    def test_rpc_quota_token_is_grok_quota_exhausted(self):
        secret = 'insufficient_quota for user@example.com'
        process = self.transport([{
            'jsonrpc': '2.0', 'id': '1',
            'error': {'code': 429, 'message': secret}}])
        with patch.object(wire.subprocess, 'Popen', return_value=process), patch.object(wire.os, 'killpg', create=True), \
                patch.object(wire.signal, 'SIGKILL', 9, create=True):
            native = wire.Native(Path('/offline'), lambda: None, time.monotonic()+5)
            with self.assertRaises(g.NativeError) as caught:
                native.request('x.ai/auth/info', {})
            self.assertEqual(str(caught.exception), 'grok_quota_exhausted')
            self.assertNotIn('example.com', str(caught.exception))
            native.close()

    def test_rpc_plain_429_stays_native_rpc_code(self):
        process = self.transport([{
            'jsonrpc': '2.0', 'id': '1',
            'error': {'code': 429, 'message': 'rate_limit'}}])
        with patch.object(wire.subprocess, 'Popen', return_value=process), patch.object(wire.os, 'killpg', create=True), \
                patch.object(wire.signal, 'SIGKILL', 9, create=True):
            native = wire.Native(Path('/offline'), lambda: None, time.monotonic()+5)
            with self.assertRaises(g.NativeError) as caught:
                native.request('x.ai/auth/info', {})
            self.assertEqual(str(caught.exception), 'native_rpc_429')
            self.assertNotEqual(str(caught.exception), 'grok_quota_exhausted')
            native.close()

    def test_quota_signal_helper_maps_tokens_not_transient_429(self):
        self.assertTrue(wire.quota_exhausted_signal({'code': 'usage_limit'}))
        self.assertTrue(wire.quota_exhausted_signal({'message': 'quota_exceeded'}))
        self.assertTrue(wire.quota_exhausted_signal({'message': 'billing'}))
        self.assertTrue(wire.quota_exhausted_signal({'message': 'credit'}))
        self.assertFalse(wire.quota_exhausted_signal({'code': 429, 'message': 'rate_limit'}))
        self.assertEqual(wire.rpc_error_code({'code': 429, 'message': 'insufficient_quota'}),
                         'grok_quota_exhausted')
        self.assertEqual(wire.rpc_error_code({'code': 429, 'message': 'rate_limit'}), 'native_rpc_429')

    def test_pump_rearms_after_renew_outcome(self):
        def run(renew, after):
            process = self.transport([{'jsonrpc': '2.0', 'id': '1', 'result': {'email': 'fixture'}}])
            with patch.object(wire.subprocess, 'Popen', return_value=process), \
                    patch.object(wire.os, 'killpg', create=True), \
                    patch.object(wire.signal, 'SIGKILL', 9, create=True):
                native = wire.Native(Path('/offline'), renew, time.monotonic() + 5)
                native.next_renew = time.monotonic() - 1
                try:
                    native.request('x.ai/auth/info', {})
                except g.NativeError as error:
                    after(native, error)
                else:
                    after(native, None)
                native.close()

        recorded = {}

        def tolerated():
            recorded['t'] = time.monotonic()
            return False

        def check_retry(native, error):
            self.assertIsNone(error)
            now = time.monotonic()
            self.assertGreater(native.next_renew, now)
            self.assertLessEqual(native.next_renew, now + broker_renew.RETRY_SECONDS)
            self.assertGreaterEqual(native.next_renew, recorded['t'] + broker_renew.RETRY_SECONDS)

        run(tolerated, check_retry)

        def check_cadence(native, error):
            self.assertIsNone(error)
            self.assertAlmostEqual(native.next_renew, time.monotonic() + 20, delta=1)

        run(lambda: True, check_cadence)
        run(lambda: None, check_cadence)

        due = {}

        def boom():
            raise wire.NativeError('grok_hub_heartbeat_lost')

        def run_raise():
            process = self.transport([{'jsonrpc': '2.0', 'id': '1', 'result': {'email': 'fixture'}}])
            with patch.object(wire.subprocess, 'Popen', return_value=process), \
                    patch.object(wire.os, 'killpg', create=True), \
                    patch.object(wire.signal, 'SIGKILL', 9, create=True):
                native = wire.Native(Path('/offline'), boom, time.monotonic() + 5)
                due['value'] = native.next_renew = time.monotonic() - 1
                with self.assertRaisesRegex(g.NativeError, '^grok_hub_heartbeat_lost$'):
                    native.request('x.ai/auth/info', {})
                self.assertEqual(native.next_renew, due['value'])
                native.close()

        run_raise()


if __name__ == '__main__':
    unittest.main()
