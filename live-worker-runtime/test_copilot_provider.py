"""Offline lifecycle, admission and transport tests; no provider/Cloud calls."""
import copy
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from providers import copilot as c


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
        self.late = []
        self.instances.append(self)

    def request(self, method, params=None):
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
        if self.fail_maintain: raise c.CopilotError('copilot_native_deadline_expired')
        if self.process.poll() is not None: raise c.CopilotError('copilot_warm_process_ended')

    def next_event(self): return self.events.pop(0)

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

    def test_heartbeat_or_idle_failure_closes_without_prompt(self):
        handle = self.prepare(); native = handle.native
        native.fail_maintain = True
        with self.assertRaisesRegex(c.CopilotError, 'warm_session_lost'): c.maintain(handle)
        self.assertTrue(handle.finished)
        self.assertNotIn('session.send', [x[0] for x in native.calls])

    def test_idle_deadline_does_not_slide(self):
        handle = self.prepare(); original = handle.idle_deadline
        c.maintain(handle); self.assertEqual(handle.idle_deadline, original)
        handle.idle_deadline = time.monotonic()-1
        with self.assertRaises(c.CopilotError): c.maintain(handle)

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


if __name__ == '__main__':
    unittest.main()
