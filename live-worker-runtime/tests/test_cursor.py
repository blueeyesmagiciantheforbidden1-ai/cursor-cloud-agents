"""Offline live-Cursor lifecycle/policy tests. No native executable or network."""
import copy
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
HUB = HERE.parent / 'agent-hub'
SOURCE = Path(r'C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
for entry in (str(SOURCE / 'deploy' / 'cursor-worker'), str(HERE.parent), str(HUB), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import broker_renew  # noqa: E402
import live_loop  # noqa: E402
from agent_hub.cloud_credential_broker import BrokerError, Conflict, MutationUncertain  # noqa: E402
from agent_hub.credential_broker_service import BoundaryError  # noqa: E402
from providers import cursor as c  # noqa: E402

KEY = 'PRIVATE_OFFLINE_CURSOR_KEY'
OWNER_EMAIL = 'cursor-owner@example.invalid'
# The placeholder owner email is not the enrolled account hash. The harness
# pins ACCOUNT_REF to that email so offline status fixtures match the owner check.
c.ACCOUNT_REF = hashlib.sha256(OWNER_EMAIL.strip().lower().encode()).hexdigest()
SID = '12345678-1234-1234-1234-123456789abc'


def beat_if_due(native, *, code='cursor_lease_lost'):
    """Same due-check the real pumps use. A far next_heartbeat skips the call."""
    if time.monotonic() < getattr(native, 'next_heartbeat', 0):
        return
    renewed = native.heartbeat()
    c.need(type(renewed) is bool, code)
    native.next_heartbeat = broker_renew.next_due(renewed, c.HEARTBEAT_SECONDS)


def option(key, category, values, current):
    return {'type': 'select', 'id': key, 'category': category, 'currentValue': current,
            'options': [{'value': v} for v in values]}


def selected_options():
    return {'configOptions': [option('mode', 'mode', ['ask', 'agent', 'plan'], 'ask'),
                              option('model', 'model', [c.MODEL, 'other'], c.MODEL),
                              option('fast', 'model_config', ['false', 'true'], 'false')]}


def catalog(model=None):
    return {'models': [{'value': model or c.MODEL, 'configOptions': [option('fast', 'model_config', ['false', 'true'], 'false')]}]}


class Fixture:
    def __init__(self, *, status_email=OWNER_EMAIL, overrides=None, prompt_hook=None, close_error=None, finish_error=None):
        self.events, self.metadata_calls, self.calls = [], [], []
        self.status_email = status_email
        self.overrides = overrides or {}
        self.prompt_hook = prompt_hook
        self.close_error, self.finish_error = close_error, finish_error
        self.native = None
        self.environment = None

    def metadata_process(self, command, environment, workspace, deadline, heartbeat):
        owner = self
        owner.environment = dict(environment)
        owner.metadata_calls.append(command)

        class FakeMetadata:
            def __init__(self):
                self.heartbeat = heartbeat
                self.next_heartbeat = time.monotonic() + 3600

            def __enter__(self):
                return self

            def __exit__(self, *_):
                owner.events.append('metadata-stop')

            def completed_output(self):
                beat_if_due(self, code='cursor_lease_lost_during_metadata')
                settings = Path(environment['CURSOR_CONFIG_DIR']) / 'cli-config.json'
                if command == ('models',):
                    settings.write_text('{}', encoding='utf-8')
                    return b'human model list'
                return json.dumps({'status': 'authenticated', 'isAuthenticated': True,
                                   'userInfo': {'email': owner.status_email}}).encode()
        return FakeMetadata()

    def acp_process(self, environment, workspace, deadline, heartbeat):
        owner = self

        class FakeACP:
            def __init__(self):
                self.heartbeat, self.deadline, self.workspace = heartbeat, deadline, workspace
                self.session_id, self.prompt_sent, self.answer, self.counts = None, False, '', {}
                self.closed = False
                self.idle_hook = None
                self.idle_frame = None
                self.next_heartbeat = time.monotonic() + 3600

            def alive(self):
                return not self.closed

            def idle_pump(self):
                beat_if_due(self)
                # Same bound NativeProcess.pump enforces once stdout is still open.
                if c.time.monotonic() >= self.deadline:
                    raise c.metadata.MetadataError('native_metadata_deadline')
                if self.idle_frame is not None:
                    frame, self.idle_frame = self.idle_frame, None
                    c.review.strict_json(frame)
                if self.idle_hook:
                    self.idle_hook(self)

            def request(self, method, params):
                beat_if_due(self)
                owner.calls.append((method, copy.deepcopy(params)))
                if method in owner.overrides:
                    value = owner.overrides[method]
                    if isinstance(value, Exception):
                        raise value
                    return copy.deepcopy(value)
                if method == 'initialize':
                    return {'protocolVersion': 1}
                if method == 'cursor/list_available_models':
                    return catalog()
                if method == 'session/new':
                    self.session_id = SID
                    return {'sessionId': SID, **selected_options()}
                if method == 'session/set_config_option':
                    return selected_options()
                if method == 'session/prompt':
                    self.prompt_sent = True
                    if owner.prompt_hook:
                        return owner.prompt_hook(self, params)
                    self.answer = 'Verified project answer.'
                    self.counts = {'agent_message_chunk': 1}
                    return {'stopReason': 'end_turn'}
                raise AssertionError('Unexpected method: ' + method)

            def drain_buffered(self):
                pass

            def close(self):
                owner.events.append('stop')
                if owner.close_error:
                    raise owner.close_error
                self.closed = True

        self.native = FakeACP()
        return self.native

    def session(self, root):
        home = Path(root) / 'home'
        (home / '.cursor').mkdir(parents=True)
        key = home / '.cursor' / 'api-key'
        key.write_text(KEY + '\n', encoding='utf-8')
        session = SimpleNamespace(state='active', home=home, auth_path=key,
                                  lease=SimpleNamespace(account_ref=c.ACCOUNT_REF),
                                  broker=SimpleNamespace(assert_current=Mock(), renew=Mock(), quarantine=Mock()))

        def finish(*, native_stopped):
            assert native_stopped is True
            assert self.native is None or self.native.closed
            self.events.append('commit-release')
            if self.finish_error:
                raise self.finish_error
            session.state = 'committed'
            return 'projects/test/secrets/cursor/versions/1'

        session.finish = Mock(side_effect=finish)
        return session


class CursorAdapter(unittest.TestCase):
    def prepare(self, fixture, root, heartbeat=lambda: True):
        session = fixture.session(root)
        with patch.object(c, '_metadata_process', fixture.metadata_process), \
                patch.object(c, '_acp_process', fixture.acp_process):
            handle = c.prepare(session, heartbeat, time.monotonic() + 30)
        return session, handle

    def test_ready_preflight_verifies_owner_catalog_and_locked_settings(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            ready = handle.readiness
            self.assertTrue(ready['authenticated'] and ready['ready_for_project_prompt'])
            self.assertFalse(ready['tools_enabled'] or ready['automatic_improvement_ready'])
            self.assertEqual(ready['model'], c.MODEL)
            self.assertEqual(fixture.metadata_calls, [('models',), ('status', '--format', 'json')])
            self.assertEqual([m for m, _ in fixture.calls], ['initialize', 'cursor/list_available_models', 'session/new',
                                                           'session/set_config_option', 'session/set_config_option',
                                                           'session/set_config_option'])
            self.assertEqual([p['value'] for m, p in fixture.calls if m == 'session/set_config_option'], [c.MODEL, 'false', 'ask'])
            self.assertEqual(fixture.environment['CURSOR_API_KEY'], KEY)
            self.assertEqual(fixture.environment['CURSOR_CONFIG_DIR'], str(session.home / '.cursor'))
            settings = json.loads((session.home / '.cursor' / 'cli-config.json').read_text(encoding='utf-8'))
            self.assertEqual(settings['approvalMode'], 'allowlist')
            self.assertEqual(settings['permissions']['deny'], c.review.DENY)
            self.assertNotIn(KEY, json.dumps(ready))
            session.finish.assert_not_called()
            c.close(handle)

    def test_close_is_idempotent_and_commits_after_stop(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            version = c.close(handle)
            self.assertEqual(c.close(handle), version)
            self.assertEqual(fixture.events, ['metadata-stop', 'metadata-stop', 'stop', 'commit-release'])
            session.finish.assert_called_once_with(native_stopped=True)

    def test_execute_returns_verified_answer_after_stop_and_commit(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            result = c.execute(handle, 'Project prompt', time.monotonic() + 60)
            self.assertEqual(result['text'], 'Verified project answer.')
            self.assertEqual(result['model'], c.MODEL)
            self.assertEqual(fixture.events[-2:], ['stop', 'commit-release'])
            self.assertEqual(session.state, 'committed')
            self.assertEqual(fixture.calls[-1][0], 'session/prompt')
            self.assertEqual(fixture.calls[-1][1]['prompt'][0]['text'], 'Project prompt')
            self.assertNotIn(KEY, json.dumps(result))
            self.assertFalse(handle.readiness['ready_for_project_prompt'])

    def test_second_prompt_oversize_and_improvement_are_refused(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            with self.assertRaises(c.NativeError) as caught:
                c.execute(handle, 'x' * (c.MAX_PROMPT_BYTES + 1), time.monotonic() + 60)
            self.assertEqual(str(caught.exception), 'cursor_prompt_limit')
            self.assertNotIn('session/prompt', [m for m, _ in fixture.calls])
            with self.assertRaises(c.NativeError) as caught:
                c.execute(handle, 'again', time.monotonic() + 60)
            self.assertEqual(str(caught.exception), 'cursor_task_replay_forbidden')
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            with self.assertRaises(c.NativeError) as caught:
                c.execute(handle, 'improve', time.monotonic() + 60, task_kind='improvement')
            self.assertEqual(str(caught.exception), 'cursor_automatic_improvement_not_enabled')

    def test_wrong_owner_rejected_before_acp_session(self):
        fixture = Fixture(status_email='other@example.com')
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            with patch.object(c, '_metadata_process', fixture.metadata_process), \
                    patch.object(c, '_acp_process', fixture.acp_process):
                with self.assertRaises(Exception):
                    c.prepare(session, lambda: True, time.monotonic() + 30)
            self.assertIsNone(fixture.native)
            self.assertEqual(fixture.events, ['metadata-stop', 'metadata-stop', 'commit-release'])
            session.finish.assert_called_once_with(native_stopped=True)

    def test_missing_model_in_catalog_rejected_and_cleaned_up(self):
        fixture = Fixture(overrides={'cursor/list_available_models': catalog('other-model')})
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            with patch.object(c, '_metadata_process', fixture.metadata_process), \
                    patch.object(c, '_acp_process', fixture.acp_process):
                with self.assertRaises(Exception):
                    c.prepare(session, lambda: True, time.monotonic() + 30)
            self.assertEqual(fixture.events[-2:], ['stop', 'commit-release'])

    def test_answer_with_credential_or_incomplete_turn_never_completes(self):
        def leak(native, params):
            native.answer = 'here is ' + KEY
            return {'stopReason': 'end_turn'}

        def cancelled(native, params):
            native.answer = 'partial'
            return {'stopReason': 'cancelled'}

        for hook, code in ((leak, 'cursor_credential_in_answer'), (cancelled, 'cursor_turn_not_completed')):
            fixture = Fixture(prompt_hook=hook)
            with tempfile.TemporaryDirectory() as root:
                session, handle = self.prepare(fixture, root)
                with self.assertRaises(c.NativeError) as caught:
                    c.execute(handle, 'Project prompt', time.monotonic() + 60)
                self.assertEqual(str(caught.exception), code)
                self.assertEqual(fixture.events[-2:], ['stop', 'commit-release'])
                session.finish.assert_called_once_with(native_stopped=True)

    def test_tampered_settings_after_prepare_reject_the_answer(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            settings = session.home / '.cursor' / 'cli-config.json'
            value = json.loads(settings.read_text(encoding='utf-8'))
            value['approvalMode'] = 'unrestricted'
            settings.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(Exception):
                c.execute(handle, 'Project prompt', time.monotonic() + 60)
            self.assertEqual(fixture.events[-2:], ['stop', 'commit-release'])

    def test_lost_hub_lease_before_prompt_never_sends_prompt(self):
        fixture = Fixture()
        beats = iter([True, True, False])  # prepare checks the hub lease twice; execute once before the prompt
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root, heartbeat=lambda: next(beats))
            with self.assertRaises(c.NativeError) as caught:
                c.execute(handle, 'Project prompt', time.monotonic() + 60)
            self.assertEqual(str(caught.exception), 'cursor_hub_heartbeat_lost')
            self.assertNotIn('session/prompt', [m for m, _ in fixture.calls])

    def test_finish_failure_quarantines_and_blocks_further_close(self):
        fixture = Fixture(finish_error=RuntimeError('commit uncertain'))
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            with self.assertRaises(RuntimeError):
                c.close(handle)
            session.broker.quarantine.assert_called_once()
            with self.assertRaises(c.NativeError) as caught:
                c.close(handle)
            self.assertEqual(str(caught.exception), 'cursor_close_requires_reconciliation')

    def test_maintain_detects_dead_process_and_early_content(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            self.assertTrue(c.maintain(handle)['ready_for_project_prompt'])
            fixture.native.idle_hook = lambda native: setattr(native, 'answer', 'unsolicited')
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'cursor_content_before_prompt')
            c.close(handle)
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            fixture.native.closed = True
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'cursor_warm_process_ended')
            fixture.native.closed = False
            c.close(handle)

    def test_maintain_outlives_startup_deadline_and_vets_a_bad_idle_frame(self):
        clock = {'now': 50_000.0}

        def now():
            return clock['now']

        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root, patch.object(c.time, 'monotonic', now):
            session = fixture.session(root)
            before = now()
            with patch.object(c, '_metadata_process', fixture.metadata_process), \
                    patch.object(c, '_acp_process', fixture.acp_process):
                handle = c.prepare(session, lambda: True, before + 180)
            self.assertGreaterEqual(handle.native.deadline, before + c.WARM_SECONDS)
            self.assertLess(handle.native.deadline, before + c.WARM_SECONDS + 30)
            warm = handle.native.deadline
            clock['now'] = before + 180
            ready = c.maintain(handle)
            self.assertTrue(ready['ready_for_project_prompt'])
            self.assertEqual(handle.native.deadline, warm)
            clock['now'] = warm - 1
            self.assertTrue(c.maintain(handle)['authenticated'])
            self.assertEqual(handle.native.deadline, warm)
            fixture.native.idle_frame = b'{"jsonrpc":'
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'cursor_idle_protocol_invalid')
            self.assertIsNone(caught.exception.__cause__)
            self.assertNotIn('jsonrpc', str(caught.exception))
            self.assertTrue(handle.finished)
            self.assertIsNone(handle.native)
            self.assertEqual(fixture.events[-2:], ['stop', 'commit-release'])
            self.assertFalse(handle.readiness['authenticated'])

    def test_idle_protocol_and_renew_faults_close_with_vetted_codes(self):
        from agent_hub.cloud_credential_broker import BrokerError
        cases = (
            ('output_bound', lambda native: (_ for _ in ()).throw(
                c.metadata.MetadataError('native_metadata_output_bound')), 'cursor_idle_protocol_invalid'),
            ('frame_bound', lambda native: (_ for _ in ()).throw(
                c.metadata.MetadataError('native_metadata_frame_bound')), 'cursor_idle_protocol_invalid'),
            ('review_error', lambda native: (_ for _ in ()).throw(c.review.ReviewError('duplicate_json_key')),
             'cursor_idle_protocol_invalid'),
            ('broker_renew', lambda native: (_ for _ in ()).throw(BrokerError('lease_renew_rejected')),
             'cursor_idle_renew_failed'),
        )
        for name, hook, code in cases:
            with self.subTest(fault=name):
                fixture = Fixture()
                with tempfile.TemporaryDirectory() as root:
                    session, handle = self.prepare(fixture, root)
                    fixture.native.idle_hook = hook
                    with self.assertRaises(c.NativeError) as caught:
                        c.maintain(handle)
                    self.assertEqual(str(caught.exception), code)
                    self.assertNotIn('lease_renew', str(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(handle.finished)
                    self.assertIsNone(handle.native)
                    session.finish.assert_called_once_with(native_stopped=True)

    def test_vetted_idle_error_is_not_remapped_and_leaves_the_handle_open(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            fixture.native.idle_hook = lambda native: (_ for _ in ()).throw(c.NativeError('cursor_lease_lost'))
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'cursor_lease_lost')
            self.assertFalse(handle.finished)
            self.assertIs(handle.native, fixture.native)
            session.finish.assert_not_called()
            c.close(handle)

    def test_execute_replaces_the_warm_deadline(self):
        seen = {}

        def hook(native, params):
            seen['deadline'] = native.deadline
            native.answer = 'Verified project answer.'
            native.counts = {'agent_message_chunk': 1}
            return {'stopReason': 'end_turn'}

        fixture = Fixture(prompt_hook=hook)
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            warm = handle.native.deadline
            task_deadline = time.monotonic() + 60
            c.execute(handle, 'Project prompt', task_deadline)
            self.assertEqual(seen['deadline'], task_deadline)
            self.assertNotEqual(seen['deadline'], warm)

    def _clock_handle(self, now=0):
        state = {'now': now}

        def clock():
            return state['now']

        clock.state = state
        lease = SimpleNamespace()
        broker = SimpleNamespace(renew=Mock())
        session = SimpleNamespace(lease=lease, broker=broker)
        beats = []
        handle = c.Handle(session=session, heartbeat=lambda: beats.append(True) or True,
                          lease_clock=broker_renew.LeaseClock(c.NativeError, 'cursor', now, clock=clock))
        return handle, beats, broker, clock

    def test_renew_tolerates_transient_errors_until_window(self):
        handle, beats, broker, clock = self._clock_handle(0)
        self.assertIs(c._renew(handle), True)
        cases = ((10, MutationUncertain('broker_http_outcome_uncertain')),
                 (100, BoundaryError('transport_failed')),
                 (180, MutationUncertain('broker_http_outcome_uncertain')))
        for when, error in cases:
            with self.subTest(when=when):
                clock.state['now'] = when
                broker.renew.side_effect = error
                self.assertIs(c._renew(handle), False)
        self.assertEqual(len(beats), 4)
        clock.state['now'] = 181
        broker.renew.side_effect = MutationUncertain('x')
        with self.assertRaisesRegex(c.NativeError, '^cursor_broker_renew_failed$') as caught:
            c._renew(handle)
        self.assertIsNone(caught.exception.__cause__)

    def test_renew_rejections_are_immediate(self):
        errors = [Conflict('broker_operation_rejected'), BrokerError('broker_request_denied'),
                  BoundaryError('credential_fence_lost')]
        for error in errors:
            with self.subTest(error=str(error)):
                handle, _beats, broker, clock = self._clock_handle(0)
                anchor = handle.lease_clock.renewed_at
                broker.renew.side_effect = error
                with self.assertRaisesRegex(c.NativeError, '^cursor_broker_renew_rejected$'):
                    c._renew(handle)
                self.assertEqual(handle.lease_clock.renewed_at, anchor)
                self.assertEqual(broker.renew.call_count, 1)

    def test_tolerated_renew_still_raises_hub_heartbeat_lost(self):
        handle, _beats, broker, _clock = self._clock_handle(0)
        broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
        handle.heartbeat = lambda: False
        with self.assertRaisesRegex(c.NativeError, '^cursor_hub_heartbeat_lost$') as caught:
            c._renew(handle)
        self.assertEqual(live_loop.maintain_fault(caught.exception), 'retry')

    def test_maintain_survives_transient_failures_until_window(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            session.broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
            fixture.native.next_heartbeat = 0
            ready = c.maintain(handle)
            self.assertTrue(ready['ready_for_project_prompt'])
            self.assertFalse(handle.finished)
            self.assertIs(handle.native, fixture.native)
            handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            fixture.native.next_heartbeat = 0
            with self.assertRaisesRegex(c.NativeError, '^cursor_broker_renew_failed$') as caught:
                c.maintain(handle)
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'fail')
            self.assertFalse(handle.finished)
            self.assertIs(handle.native, fixture.native)
            c.close(handle)

    def test_maintain_backstop_without_pump(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            live = c.LiveProcess.__new__(c.LiveProcess)
            live.process = SimpleNamespace(poll=lambda: None, stdin=io.BytesIO())
            live.selector = SimpleNamespace(get_map=lambda: {})
            live.buffer = bytearray()
            live.answer = ''
            live.prompt_sent = False
            live.deadline = time.monotonic() + 100
            live.next_heartbeat = 0
            live.heartbeat = lambda: (_ for _ in ()).throw(AssertionError('empty selector must not pump'))
            handle.native = live
            handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            with self.assertRaisesRegex(c.NativeError, '^cursor_broker_renew_failed$'):
                c.maintain(handle)
            self.assertFalse(handle.finished)

    def test_execute_turn_survives_one_transient_renew(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)

            def hook(native, params):
                native.next_heartbeat = 0
                session.broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
                beat_if_due(native)
                native.answer = 'Verified project answer.'
                native.counts = {'agent_message_chunk': 1}
                return {'stopReason': 'end_turn'}

            fixture.prompt_hook = hook
            result = c.execute(handle, 'Project prompt', time.monotonic() + 60)
            self.assertEqual(result['text'], 'Verified project answer.')
            self.assertEqual(fixture.events[-2:], ['stop', 'commit-release'])
            session.broker.renew.assert_called()


class LiveProtocol(unittest.TestCase):
    """LiveProcess frame handling without a native process."""

    def process(self, *, bound=True, prompt_sent=False):
        live = c.LiveProcess.__new__(c.LiveProcess)
        live._init_state(lambda: True)
        live.buffer = bytearray()
        live.process = SimpleNamespace(stdin=io.BytesIO(), poll=lambda: None)
        live.selector = SimpleNamespace(get_map=lambda: {})
        if bound:
            live.session_id, live.stage, live.effective_selected = SID, 6, True
        live.prompt_sent = prompt_sent
        return live

    def update(self, kind, **extra):
        return {'jsonrpc': '2.0', 'method': 'session/update',
                'params': {'sessionId': SID, 'update': {'sessionUpdate': kind, **extra}}}

    def test_permission_request_is_rejected_and_aborts(self):
        live = self.process()
        with self.assertRaises(c.NativeError) as caught:
            live.notification({'jsonrpc': '2.0', 'id': 7, 'method': 'session/request_permission', 'params': {}})
        self.assertEqual(str(caught.exception), 'cursor_tool_permission_rejected')
        self.assertIn(b'reject-once', live.process.stdin.getvalue())

    def test_content_before_prompt_and_unknown_updates_abort(self):
        live = self.process()
        with self.assertRaises(c.NativeError) as caught:
            live.notification(self.update('agent_message_chunk', content={'type': 'text', 'text': 'hi'}))
        self.assertEqual(str(caught.exception), 'cursor_content_before_prompt')
        with self.assertRaises(c.NativeError) as caught:
            live.notification(self.update('tool_call', title='rm -rf'))
        self.assertEqual(str(caught.exception), 'cursor_tool_or_unknown_native_update')
        self.assertEqual(live.counts['unknown_or_tool'], 1)

    def test_answer_accumulates_only_after_prompt_and_is_bounded(self):
        live = self.process(prompt_sent=True)
        live.notification(self.update('agent_message_chunk', content={'type': 'text', 'text': 'Hello '}))
        live.notification(self.update('agent_thought_chunk', content={'type': 'text', 'text': 'thinking'}))
        live.notification(self.update('agent_message_chunk', content={'type': 'text', 'text': 'world'}))
        self.assertEqual(live.answer, 'Hello world')
        with self.assertRaises(c.NativeError) as caught:
            live.notification(self.update('agent_message_chunk', content={'type': 'text', 'text': 'x' * c.MAX_ANSWER_BYTES}))
        self.assertEqual(str(caught.exception), 'cursor_answer_excessive')

    def test_drain_rejects_content_after_turn_result_and_partial_frames(self):
        live = self.process(prompt_sent=True)
        live.buffer = bytearray(json.dumps(self.update('agent_message_chunk', content={'type': 'text', 'text': 'late'})).encode() + b'\n')
        with self.assertRaises(c.NativeError) as caught:
            live.drain_buffered()
        self.assertEqual(str(caught.exception), 'cursor_content_after_turn_result')
        live = self.process(prompt_sent=True)
        live.buffer = bytearray(b'{"jsonrpc":"2.0","method":"session/update"')
        with self.assertRaises(c.NativeError) as caught:
            live.drain_buffered()
        self.assertEqual(str(caught.exception), 'cursor_incomplete_trailing_frame')

    def test_mode_change_away_from_ask_aborts_once_selected(self):
        live = self.process()
        live.notification(self.update('current_mode_update', currentModeId='ask'))
        with self.assertRaises(c.NativeError) as caught:
            live.notification(self.update('current_mode_update', currentModeId='agent'))
        self.assertEqual(str(caught.exception), 'cursor_mode_changed')

    def test_idle_pump_processes_passive_updates_only(self):
        live = self.process()
        live.buffer = bytearray(json.dumps(self.update('session_info_update', title='t')).encode() + b'\n')
        live.idle_pump()
        self.assertEqual(live.counts, {'session_info_update': 1})
        self.assertEqual(live.answer, '')

    def test_idle_pump_deadline_and_malformed_frame(self):
        clock = {'now': 90_000.0}
        live = self.process()
        live.next_heartbeat = clock['now'] + 10_000
        live.deadline = clock['now'] + 180
        live.selector = SimpleNamespace(get_map=lambda: {1: object()}, select=lambda _timeout: [])
        with patch.object(c.time, 'monotonic', lambda: clock['now']), \
                patch.object(c.metadata.time, 'monotonic', lambda: clock['now']):
            clock['now'] += 180
            with self.assertRaises(c.metadata.MetadataError) as caught:
                live.idle_pump()
            self.assertEqual(str(caught.exception), 'native_metadata_deadline')
            live.deadline = clock['now'] + c.WARM_SECONDS
            live.idle_pump()
            live.buffer = bytearray(b'{"jsonrpc":\n')
            with self.assertRaises(json.JSONDecodeError):
                live.idle_pump()

    def test_pump_rearms_after_tolerated_failure(self):
        handle = c.Handle(session=SimpleNamespace(
            lease=SimpleNamespace(), broker=SimpleNamespace(renew=Mock())), heartbeat=lambda: True,
            lease_clock=broker_renew.LeaseClock.for_lease(SimpleNamespace(), c.NativeError, 'cursor'))
        live = c.LiveProcess.__new__(c.LiveProcess)
        live.heartbeat = lambda: c._renew(handle)
        live.deadline = time.monotonic() + 30
        live.selector = SimpleNamespace(get_map=lambda: {1: object()}, select=lambda timeout: [])
        live.next_heartbeat = 0
        handle.session.broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
        before = time.monotonic()
        live.pump()
        self.assertGreaterEqual(live.next_heartbeat, before + broker_renew.RETRY_SECONDS)
        self.assertLessEqual(live.next_heartbeat, time.monotonic() + broker_renew.RETRY_SECONDS)
        handle.session.broker.renew.side_effect = None
        live.next_heartbeat = 0
        before = time.monotonic()
        live.pump()
        self.assertAlmostEqual(live.next_heartbeat, before + c.HEARTBEAT_SECONDS, delta=1)
        live.heartbeat = lambda: None
        live.next_heartbeat = 0
        with self.assertRaisesRegex(c.NativeError, '^cursor_lease_lost$'):
            live.pump()

    def test_tolerated_broker_failure_retries_at_retry_seconds_while_hub_heartbeat_runs(self):
        state = {'now': 5_000.0}

        def clock():
            return state['now']

        beats = []
        handle = c.Handle(session=SimpleNamespace(
            lease=SimpleNamespace(), broker=SimpleNamespace(renew=Mock(
                side_effect=MutationUncertain('broker_http_outcome_uncertain')))),
            heartbeat=lambda: beats.append(True) or True,
            lease_clock=broker_renew.LeaseClock(c.NativeError, 'cursor', state['now'], clock=clock))
        live = c.LiveProcess.__new__(c.LiveProcess)
        live.heartbeat = lambda: c._renew(handle)
        live.deadline = state['now'] + 30
        live.selector = SimpleNamespace(get_map=lambda: {1: object()}, select=lambda timeout: [])
        live.next_heartbeat = state['now']
        with patch('time.monotonic', clock):
            live.pump()
            self.assertEqual(beats, [True])
            self.assertEqual(live.next_heartbeat, state['now'] + broker_renew.RETRY_SECONDS)
            # Inside the retry interval the broker is not called again.
            live.pump()
            self.assertEqual(beats, [True])
            self.assertEqual(handle.session.broker.renew.call_count, 1)
            state['now'] += broker_renew.RETRY_SECONDS
            live.pump()
        self.assertEqual(beats, [True, True])
        self.assertEqual(handle.session.broker.renew.call_count, 2)
        self.assertEqual(live.next_heartbeat, state['now'] + broker_renew.RETRY_SECONDS)

    def test_metadata_pump_tolerance(self):
        handle = c.Handle(session=SimpleNamespace(
            lease=SimpleNamespace(), broker=SimpleNamespace(renew=Mock(
                side_effect=MutationUncertain('broker_http_outcome_uncertain')))),
            heartbeat=lambda: True,
            lease_clock=broker_renew.LeaseClock.for_lease(SimpleNamespace(), c.NativeError, 'cursor'))
        with patch.object(c.metadata.NativeProcess, '__init__', lambda *args, **kwargs: None), \
                patch.object(c.metadata.NativeProcess, 'pump', lambda *args, **kwargs: None):
            native = c._metadata_process(('models',), {}, Path('.'), time.monotonic() + 30,
                                         lambda: c._renew(handle))
            native.deadline = time.monotonic() + 30
            native.next_heartbeat = 0
            before = time.monotonic()
            native.pump()
            self.assertGreaterEqual(native.next_heartbeat, before + broker_renew.RETRY_SECONDS)
            self.assertLessEqual(native.next_heartbeat, time.monotonic() + broker_renew.RETRY_SECONDS)
            handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            native.next_heartbeat = 0
            with self.assertRaisesRegex(c.NativeError, '^cursor_broker_renew_failed$'):
                native.pump()

    def test_pre_prompt_gate_is_strict(self):
        live = c.LiveProcess.__new__(c.LiveProcess)
        live.stage = 6
        live.effective_selected = True
        live.prompt_sent = False
        live.session_id = SID
        live.heartbeat = lambda: False
        live.process = SimpleNamespace(stdin=io.BytesIO())
        params = {'sessionId': SID, 'prompt': [{'type': 'text', 'text': 'Project prompt'}]}
        with self.assertRaisesRegex(c.NativeError, '^cursor_broker_renew_failed$'):
            live.request('session/prompt', params)
        self.assertEqual(live.process.stdin.getvalue(), b'')
        live.heartbeat = lambda: True
        live.stage = 6
        live.prompt_sent = False
        result = {'jsonrpc': '2.0', 'id': 7, 'result': {'stopReason': 'end_turn'}}
        live.buffer = bytearray(json.dumps(result).encode() + b'\n')
        live.process = SimpleNamespace(stdin=io.BytesIO())
        returned = live.request('session/prompt', params)
        self.assertEqual(returned['stopReason'], 'end_turn')
        self.assertIn(b'session/prompt', live.process.stdin.getvalue())


if __name__ == '__main__':
    unittest.main()
