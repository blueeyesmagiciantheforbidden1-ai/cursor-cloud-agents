"""Offline live-Claude lifecycle/policy tests. No native executable or network."""
import copy
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

HERE = Path(__file__).resolve().parents[1]
HUB = HERE.parent / 'agent-hub'
SOURCE = Path(r'C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
for entry in (str(SOURCE), str(HUB), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import broker_renew  # noqa: E402
import live_loop  # noqa: E402
from agent_hub.cloud_credential_broker import BrokerError, Conflict, MutationUncertain  # noqa: E402
from agent_hub.credential_broker_service import BoundaryError  # noqa: E402
from providers import claude as c  # noqa: E402

TOKEN = 'PRIVATE_OFFLINE_SUBSCRIPTION_TOKEN'


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


class _SlowStdin:
    def __init__(self):
        self.chunks = []
        self.closed = False

    def write(self, payload):
        self.chunks.append(bytes(payload))
        return len(payload)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _SlowFrames:
    """Withhold the answer for one renew interval per 'wait' item."""

    def __init__(self, clock, items):
        self.failed = threading.Event()
        self.clock = clock
        self.items = list(items)

    def get(self, timeout=None):
        if not self.items:
            raise queue.Empty
        item = self.items.pop(0)
        if item == 'wait':
            self.clock['now'] += 21
            raise queue.Empty
        return item

    def empty(self):
        return not self.items


class _SlowProcess:
    def __init__(self, frames, stdin, returncode=0):
        self.frames = frames
        self.stdin = stdin
        self.returncode = returncode
        self.pid = 1

    def poll(self):
        return None if self.frames.items else 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def result_event(**changes):
    value = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Verified project answer.',
             'modelUsage': {c.MODEL: {'inputTokens': 10}}, 'usage': {'input_tokens': 10, 'output_tokens': 5},
             'total_cost_usd': 0.01}
    value.update(changes)
    return value


class Fixture:
    def __init__(self, *, overrides=None, prompt_hook=None, close_error=None, finish_error=None, idle=(),
                 renew_due=False):
        self.events, self.calls, self.prompts = [], [], []
        self.overrides = overrides or {}
        self.prompt_hook = prompt_hook
        self.close_error, self.finish_error = close_error, finish_error
        self.idle = list(idle)
        self.renew_due = renew_due
        self.native = None
        self.environment = None
        self.factory = self._factory

    def _factory(self, environment, workspace, deadline, renew):
        owner = self
        owner.environment = dict(environment)

        class FakeNative:
            def __init__(self):
                self.renew, self.deadline, self.workspace = renew, deadline, workspace
                self.next_renew = 0 if owner.renew_due else time.monotonic() + 20
                self.closed = False
                self.frames = SimpleNamespace(failed=threading.Event())
                self.process = SimpleNamespace(poll=lambda: 0 if self.closed else None)

            def control(self, subtype):
                owner.calls.append(subtype)
                if subtype in owner.overrides:
                    value = owner.overrides[subtype]
                    if isinstance(value, Exception):
                        raise value
                    return copy.deepcopy(value)
                if subtype == 'initialize':
                    return {'account': {'apiProvider': 'firstParty', 'tokenSource': 'CLAUDE_CODE_OAUTH_TOKEN',
                                        'subscriptionType': 'max'}}
                if subtype == 'get_settings':
                    return {'effective': {}, 'sources': [], 'applied': {'model': c.MODEL, 'effort': c.EFFORT,
                                                                       'ultracode': False}}
                raise AssertionError('Unexpected control: ' + subtype)

            def idle_events(self):
                events, owner.idle = owner.idle, []
                return events

            def tick(self):
                c.Native.tick(self)

            def prompt(self, text):
                owner.prompts.append(text)
                if owner.prompt_hook:
                    return owner.prompt_hook(self, text)
                return result_event()

            def close(self):
                owner.events.append('stop')
                if owner.close_error:
                    raise owner.close_error
                self.closed = True

        self.native = FakeNative()
        return self.native

    def session(self, root):
        home = Path(root) / 'home'
        (home / '.claude').mkdir(parents=True)
        token = home / '.claude' / 'oauth-token'
        token.write_text(TOKEN + '\n', encoding='utf-8')
        session = SimpleNamespace(state='active', home=home, auth_path=token,
                                  lease=SimpleNamespace(account_ref=c.ACCOUNT_REF),
                                  broker=SimpleNamespace(assert_current=Mock(), renew=Mock(), quarantine=Mock()))

        def finish(*, native_stopped):
            assert native_stopped is True
            assert self.native is None or self.native.closed
            self.events.append('commit-release')
            if self.finish_error:
                raise self.finish_error
            session.state = 'committed'
            return 'projects/test/secrets/claude/versions/1'

        session.finish = Mock(side_effect=finish)
        return session


class ClaudeAdapter(unittest.TestCase):
    def prepare(self, fixture, root, heartbeat=lambda: True):
        session = fixture.session(root)
        with patch.object(c, 'NativeProcess', fixture.factory), \
                patch.object(c, 'launch_context_error', lambda *a: None):
            handle = c.prepare(session, heartbeat, time.monotonic() + 30)
        return session, handle

    def test_ready_preflight_verifies_route_model_and_effort_before_any_task(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            ready = handle.readiness
            self.assertTrue(ready['authenticated'])
            self.assertTrue(ready['ready_for_project_prompt'])
            self.assertFalse(ready['tools_enabled'])
            self.assertFalse(ready['automatic_improvement_ready'])
            self.assertEqual(ready['preflight']['quota']['native_usage_status'], 'unavailable')
            self.assertEqual(fixture.calls, ['initialize', 'get_settings'])
            self.assertEqual(fixture.environment['CLAUDE_CODE_OAUTH_TOKEN'], TOKEN)
            self.assertEqual(fixture.environment['HOME'], str(c.auth.CLAUDE_HOME))
            self.assertNotIn('ANTHROPIC_API_KEY', fixture.environment)
            self.assertNotIn(TOKEN, json.dumps(ready))
            session.finish.assert_not_called()
            c.close(handle)

    def test_close_is_idempotent_and_commits_after_stop(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            version = c.close(handle)
            self.assertEqual(c.close(handle), version)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            session.finish.assert_called_once_with(native_stopped=True)
            self.assertFalse(handle.readiness['authenticated'])

    def test_execute_returns_verified_answer_after_stop_and_commit(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            result = c.execute(handle, 'Project prompt', time.monotonic() + 60)
            self.assertEqual(result['text'], 'Verified project answer.')
            self.assertEqual(result['model'], c.MODEL)
            self.assertEqual(result['effort'], c.EFFORT)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            self.assertEqual(session.state, 'committed')
            self.assertEqual(result['credential_writeback'], 'committed')
            self.assertFalse(result['usage']['actual_account_charge_verified'])
            self.assertEqual(result['usage']['usage'], {'input_tokens': 10, 'output_tokens': 5})
            self.assertEqual(fixture.prompts, ['Project prompt'])
            self.assertNotIn(TOKEN, json.dumps(result))
            self.assertFalse(handle.readiness['ready_for_project_prompt'])

    def test_second_prompt_oversize_and_improvement_are_refused(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            with self.assertRaises(c.NativeError) as caught:
                c.execute(handle, 'x' * (c.MAX_PROMPT_BYTES + 1), time.monotonic() + 60)
            self.assertEqual(str(caught.exception), 'claude_prompt_limit')
            self.assertEqual(fixture.prompts, [])
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            with self.assertRaises(c.NativeError) as caught:
                c.execute(handle, 'again', time.monotonic() + 60)
            self.assertEqual(str(caught.exception), 'claude_task_replay_forbidden')
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            with self.assertRaises(c.NativeError) as caught:
                c.execute(handle, 'improve yourself', time.monotonic() + 60, task_kind='improvement')
            self.assertEqual(str(caught.exception), 'claude_automatic_improvement_not_enabled')
            self.assertEqual(fixture.prompts, [])

    def test_wrong_owner_or_route_is_rejected_and_cleaned_up(self):
        for account in ({'apiProvider': 'firstParty', 'tokenSource': 'CLAUDE_CODE_OAUTH_TOKEN',
                         'subscriptionType': 'max', 'email': 'other@example.com'},
                        {'apiProvider': 'firstParty', 'tokenSource': 'CLAUDE_CODE_OAUTH_TOKEN',
                         'subscriptionType': 'team'},
                        {'apiProvider': 'firstParty', 'tokenSource': 'ANTHROPIC_API_KEY'}):
            fixture = Fixture(overrides={'initialize': {'account': account}})
            with tempfile.TemporaryDirectory() as root, self.assertRaises(Exception):
                self.prepare(fixture, root)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
            self.assertEqual(fixture.prompts, [])

    def test_settings_outside_clean_profile_or_wrong_effort_rejected(self):
        for settings in ({'effective': {}, 'sources': [], 'applied': {'model': c.MODEL, 'effort': 'high', 'ultracode': False}},
                         {'effective': {'model': 'x'}, 'sources': ['user'], 'applied': {'model': c.MODEL, 'effort': c.EFFORT, 'ultracode': False}},
                         {'effective': {}, 'sources': [], 'applied': {'model': 'claude-other-1', 'effort': c.EFFORT, 'ultracode': False}}):
            fixture = Fixture(overrides={'get_settings': settings})
            with tempfile.TemporaryDirectory() as root, self.assertRaises(Exception):
                self.prepare(fixture, root)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_api_key_override_in_environment_is_blocked_before_native(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            with patch.object(c, 'NativeProcess', fixture.factory), \
                    patch.object(c, 'launch_context_error', lambda *a: None), \
                    patch.object(c.auth, 'claude_environment', lambda token: {'HOME': '/home/worker', 'ANTHROPIC_API_KEY': 'x',
                                                                          'CLAUDE_CODE_OAUTH_TOKEN': token}):
                with self.assertRaises(Exception):
                    c.prepare(session, lambda: True, time.monotonic() + 30)
            self.assertEqual(fixture.prompts, [])
            session.finish.assert_called_once()

    def test_launch_context_error_blocks_before_native_start(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            with patch.object(c, 'NativeProcess', fixture.factory), \
                    patch.object(c, 'launch_context_error', lambda *a: 'unapproved_command'):
                with self.assertRaises(c.NativeError) as caught:
                    c.prepare(session, lambda: True, time.monotonic() + 30)
            self.assertEqual(str(caught.exception), 'claude_unapproved_command')
            self.assertIsNone(fixture.native)
            session.finish.assert_called_once_with(native_stopped=True)

    def test_error_result_or_other_model_never_completes_but_still_commits(self):
        for hook in (lambda native, text: result_event(is_error=True, subtype='error_during_execution'),
                     lambda native, text: result_event(modelUsage={'claude-other-1': {}}),
                     lambda native, text: result_event(result='x' * (c.MAX_ANSWER_BYTES + 1))):
            fixture = Fixture(prompt_hook=hook)
            with tempfile.TemporaryDirectory() as root:
                session, handle = self.prepare(fixture, root)
                with self.assertRaises(c.NativeError):
                    c.execute(handle, 'Project prompt', time.monotonic() + 60)
                self.assertEqual(fixture.events, ['stop', 'commit-release'])
                session.finish.assert_called_once_with(native_stopped=True)
                self.assertTrue(handle.attempted)

    def test_lost_hub_lease_before_prompt_never_sends_prompt(self):
        fixture = Fixture()
        beats = iter([True, False])  # prepare checks the hub lease once; execute checks it before the prompt
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root, heartbeat=lambda: next(beats))
            with self.assertRaises(c.NativeError) as caught:
                c.execute(handle, 'Project prompt', time.monotonic() + 60)
            self.assertEqual(str(caught.exception), 'claude_hub_heartbeat_lost')
            self.assertEqual(fixture.prompts, [])
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_finish_failure_quarantines_and_blocks_further_close(self):
        fixture = Fixture(finish_error=RuntimeError('commit uncertain'))
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            with self.assertRaises(RuntimeError):
                c.close(handle)
            session.broker.quarantine.assert_called_once()
            self.assertTrue(handle.close_failed)
            with self.assertRaises(c.NativeError) as caught:
                c.close(handle)
            self.assertEqual(str(caught.exception), 'claude_close_requires_reconciliation')

    def test_maintain_detects_dead_process_and_unexpected_idle_output(self):
        fixture = Fixture(idle=[{'type': 'keep_alive'}])
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            self.assertTrue(c.maintain(handle)['ready_for_project_prompt'])
            fixture.idle = [{'type': 'assistant', 'message': {}}]
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'claude_unexpected_idle_message')
            c.close(handle)
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            fixture.native.closed = True
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'claude_warm_process_ended')
            fixture.native.closed = False
            c.close(handle)

    def test_maintain_renews_lease_after_window(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            fixture.native.next_renew = time.monotonic() - 1
            c.maintain(handle)
            session.broker.renew.assert_called_once_with(session.lease)
            self.assertGreater(fixture.native.next_renew, time.monotonic())
            c.maintain(handle)
            session.broker.renew.assert_called_once_with(session.lease)
            c.close(handle)

    def test_maintain_renew_failure_stays_due_with_a_vetted_code(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            fixture.native.next_renew = time.monotonic() - 1
            due = fixture.native.next_renew
            session.broker.renew.side_effect = RuntimeError('connection reset by peer')
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'claude_lease_renew_failed')
            self.assertNotIn('peer', str(caught.exception))
            self.assertEqual(fixture.native.next_renew, due)
            session.broker.renew.side_effect = RuntimeError('credential_lease_not_active')
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'credential_lease_not_active')
            self.assertEqual(fixture.native.next_renew, due)
            handle.heartbeat = lambda: False
            session.broker.renew.side_effect = None
            with self.assertRaises(c.NativeError) as caught:
                c.maintain(handle)
            self.assertEqual(str(caught.exception), 'claude_hub_heartbeat_lost')
            self.assertEqual(fixture.native.next_renew, due)
            c.close(handle)

    def _slow_prompt(self, *, prepare_execute=None, heartbeat=None, waits=3, answer=None, returncode=0):
        """execute() -> prompt(); the answer is withheld across `waits` renew intervals."""
        fixture = Fixture()
        clock = {'now': 10_000.0}
        command = '11111111-1111-4111-8111-111111111111'
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            if heartbeat is not None:
                handle.heartbeat = heartbeat
            lifecycle = {'type': 'command_lifecycle', 'command_uuid': command, 'state': 'completed',
                         'uuid': '33333333-3333-4333-8333-333333333333',
                         'session_id': '22222222-2222-4222-8222-222222222222'}
            items = ['wait'] * waits + [lifecycle if item == 'lifecycle' else item for item in
                                        (answer if answer is not None else ['lifecycle', result_event()])]
            script = _SlowFrames(clock, items + [c.rt._EOF])
            stdin = _SlowStdin()
            native = c.Native.__new__(c.Native)
            native.deadline = clock['now'] + 21 * waits + 500
            native.renew = lambda: c._renew(handle)
            native.next_renew = clock['now'] + 20
            native.writers = []
            native.frames = SimpleNamespace(failed=threading.Event(), queue=script)
            native.process = _SlowProcess(script, stdin, returncode)
            native.closed = False

            def close():
                native.closed = True
                fixture.events.append('stop')

            native.close = close
            handle.native = native
            fixture.native = native
            if prepare_execute is not None:
                prepare_execute(native, clock, session)
            outcome = SimpleNamespace(session=session, handle=handle, native=native, stdin=stdin,
                                      clock=clock, events=fixture.events, result=None, error=None)
            with patch.object(c.time, 'monotonic', lambda: clock['now']), \
                    patch.object(c, 'uuid4', return_value=command):
                try:
                    outcome.result = c.execute(handle, 'Project prompt', native.deadline)
                except c.NativeError as error:
                    outcome.error = error
            return outcome

    def test_slow_prompt_renews_lease_across_intervals(self):
        when = []

        def prepare(native, clock, session):
            def renew(lease):
                self.assertLessEqual(native.next_renew, clock['now'])
                when.append(clock['now'])
            session.broker.renew.side_effect = renew

        outcome = self._slow_prompt(prepare_execute=prepare)
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.result['text'], 'Verified project answer.')
        self.assertEqual(when, [10_021.0, 10_042.0, 10_063.0])
        self.assertEqual(outcome.native.next_renew, when[-1] + 20)
        self.assertEqual(outcome.session.broker.renew.call_count, 3)
        self.assertEqual(len(outcome.stdin.chunks), 1)
        self.assertIn(b'Project prompt', outcome.stdin.chunks[0])
        self.assertEqual(outcome.events, ['stop', 'commit-release'])
        self.assertEqual(outcome.session.state, 'committed')

    def test_turn_longer_than_the_credential_lease_keeps_renewing(self):
        # A 300 s turn outlives the 240 s broker lease and the 45 s hub task
        # lease many times over; every 20 s window must renew both.
        when, beats = [], {'n': 0}

        def prepare(native, clock, session):
            def renew(lease):
                self.assertLessEqual(native.next_renew, clock['now'])
                when.append(clock['now'])
            session.broker.renew.side_effect = renew

        def beat():
            beats['n'] += 1
            return True

        outcome = self._slow_prompt(prepare_execute=prepare, heartbeat=beat, waits=15)
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.result['text'], 'Verified project answer.')
        self.assertEqual(len(when), 15)
        self.assertGreater(when[-1] - 10_000.0, 300)
        self.assertTrue(all(b - a <= 21 for a, b in zip([10_000.0] + when, when)))
        # One heartbeat before the prompt, then one per renewal.
        self.assertEqual(beats['n'], 1 + 15)
        self.assertEqual(outcome.events, ['stop', 'commit-release'])
        self.assertEqual(outcome.session.state, 'committed')

    def test_account_limit_is_reported_as_quota_exhausted(self):
        spend = 'You have hit your monthly spend limit for account user@example.com, resets 1pm'
        cases = {
            'result text': [result_event(is_error=True, result=spend)],
            'billing error': [{'type': 'assistant', 'error': 'billing_error', 'message': {}},
                              result_event(is_error=True, result='API Error')],
            'rate limit plus usage-limit text': [{'type': 'assistant', 'error': 'rate_limit', 'message': {}},
                                                 result_event(is_error=True, result='Claude AI usage limit reached|1')],
            'rejected rate limit event': [{'type': 'rate_limit_event',
                                           'rate_limit_info': {'status': 'rejected', 'resetsAt': 1}},
                                          result_event(is_error=True, result='API Error')],
            'billing error outranks server error': [
                {'type': 'assistant', 'error': 'server_error', 'message': {}},
                {'type': 'assistant', 'error': 'billing_error', 'message': {}},
                result_event(is_error=True, subtype='error_during_execution', result='x')],
        }
        for name, answer in cases.items():
            for returncode in (0, 1):
                with self.subTest(name, returncode=returncode):
                    outcome = self._slow_prompt(waits=0, answer=answer, returncode=returncode)
                    self.assertIsInstance(outcome.error, c.NativeError)
                    self.assertEqual(str(outcome.error), 'claude_quota_exhausted')
                    self.assertNotIn('example.com', str(outcome.error))
                    self.assertEqual(outcome.events, ['stop', 'commit-release'])
                    self.assertTrue(outcome.handle.attempted)

    def test_account_limit_before_result_still_names_the_cause(self):
        # The CLI can exit after the refusal without writing a result frame.
        refused = [{'type': 'assistant', 'error': 'billing_error', 'message': {}}]
        outcome = self._slow_prompt(waits=0, answer=refused, returncode=1)
        self.assertEqual(str(outcome.error), 'claude_quota_exhausted')

    def test_other_provider_errors_keep_distinct_codes(self):
        auth = self._slow_prompt(waits=0, answer=[{'type': 'assistant', 'error': 'authentication_failed',
                                                   'message': {}},
                                                  result_event(is_error=True, result='Invalid API key')])
        self.assertEqual(str(auth.error), 'claude_authentication_failed')
        odd = self._slow_prompt(waits=0, answer=[{'type': 'assistant', 'error': 'something new', 'message': {}},
                                                 result_event(is_error=True, result='failed')])
        self.assertEqual(str(odd.error), 'claude_provider_error')
        server = self._slow_prompt(waits=0, returncode=1, answer=[
            {'type': 'assistant', 'error': 'server_error', 'message': {}},
            result_event(is_error=True, result='API Error: 500')])
        self.assertEqual(str(server.error), 'claude_provider_server_error')
        self.assertFalse(c.provider_errors.is_quota(str(server.error)))
        # A bare 429 is not proof of an account limit: its own code, a normal strike.
        for returncode in (0, 1):
            limited = self._slow_prompt(waits=0, returncode=returncode, answer=[
                {'type': 'assistant', 'error': 'rate_limit', 'message': {}},
                result_event(is_error=True, result='API Error: 429 rate_limit_error')])
            self.assertEqual(str(limited.error), 'claude_rate_limited')
            self.assertFalse(c.provider_errors.is_quota(str(limited.error)))
        plain = self._slow_prompt(waits=0, answer=['lifecycle', result_event(is_error=True, result='Something broke')])
        self.assertEqual(str(plain.error), 'claude_task_not_successful')
        crash = self._slow_prompt(waits=0, returncode=1)
        self.assertEqual(str(crash.error), 'claude_exit_unsuccessful')
        # Lifecycle failures keep their fixed code instead of the loop's generic one.
        unfinished = self._slow_prompt(waits=0, answer=[result_event()])
        self.assertIsInstance(unfinished.error, c.NativeError)
        self.assertEqual(str(unfinished.error), 'claude_command_lifecycle_incomplete_outcome_uncertain')
        # A limit warning on a turn that still succeeds is not a failure.
        warned = self._slow_prompt(waits=0, answer=[{'type': 'rate_limit_event',
                                                     'rate_limit_info': {'status': 'allowed_warning'}},
                                                    'lifecycle', result_event()])
        self.assertIsNone(warned.error)
        self.assertEqual(warned.result['text'], 'Verified project answer.')

    def test_slow_prompt_renew_failure_keeps_schedule_and_vetted_code(self):
        calls = {'n': 0, 'due': None}

        def prepare(native, clock, session):
            def renew(lease):
                calls['n'] += 1
                calls['due'] = native.next_renew
                if calls['n'] >= 2:
                    raise RuntimeError('raw diagnostics /tmp/secret')
            session.broker.renew.side_effect = renew

        outcome = self._slow_prompt(prepare_execute=prepare)
        self.assertIsInstance(outcome.error, c.NativeError)
        self.assertEqual(str(outcome.error), 'claude_lease_renew_failed')
        self.assertNotIn('secret', str(outcome.error))
        self.assertEqual(calls['n'], 2)
        self.assertEqual(outcome.native.next_renew, calls['due'])
        self.assertEqual(outcome.events, ['stop', 'commit-release'])
        self.assertEqual(outcome.session.state, 'committed')
        self.assertTrue(outcome.handle.attempted)

        beats = {'n': 0}

        def beat():
            beats['n'] += 1
            return beats['n'] == 1

        heart = self._slow_prompt(heartbeat=beat)
        self.assertEqual(str(heart.error), 'claude_hub_heartbeat_lost')
        self.assertEqual(heart.native.next_renew, 10_020.0)
        self.assertEqual(heart.session.broker.renew.call_count, 1)
        self.assertEqual(heart.session.state, 'committed')

        def install_safe_code(native, clock, session):
            session.broker.renew.side_effect = RuntimeError('credential_lease_not_active')

        safe = self._slow_prompt(prepare_execute=install_safe_code)
        self.assertEqual(str(safe.error), 'credential_lease_not_active')
        self.assertEqual(safe.native.next_renew, 10_020.0)

    def test_transient_idle_renew_failure_keeps_session_and_retries(self):
        errors = [MutationUncertain('broker_http_outcome_uncertain'), BoundaryError('transport_failed'),
                  BoundaryError('metadata_identity_unavailable')]
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
                    ready = c.maintain(handle)
                    self.assertTrue(ready['ready_for_project_prompt'])
                    self.assertEqual(fixture.events, [])
                    session.finish.assert_not_called()
                    session.broker.quarantine.assert_not_called()
                    self.assertEqual(beats, [True])
                    self.assertEqual(handle.lease_clock.renewed_at, anchor)
                    self.assertEqual(fixture.native.next_renew, clock() + broker_renew.RETRY_SECONDS)
                    clock.advance()
                    session.broker.renew.side_effect = None
                    fixture.native.next_renew = clock() - 1
                    c.maintain(handle)
                    self.assertFalse(handle.lease_clock.degraded)
                    # A confirmed renew stamps the clock at the start of the attempt.
                    # That reading can equal the previous anchor when the clock did not move.
                    self.assertGreaterEqual(handle.lease_clock.renewed_at, anchor)
                    self.assertEqual(handle.lease_clock.renewed_at, clock())
                    self.assertEqual(fixture.native.next_renew, clock() + 20)
                    c.close(handle)

    def test_renew_failure_past_window_raises_claude_broker_renew_failed(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            session.broker.renew.side_effect = MutationUncertain('private detail')
            fixture.native.next_renew = time.monotonic() - 1
            with self.assertRaisesRegex(c.NativeError, '^claude_broker_renew_failed$') as caught:
                c.maintain(handle)
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'fail')
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertIsNone(caught.exception.__cause__)
            self.assertIs(handle.native, fixture.native)
            self.assertFalse(fixture.native.closed)
            c.close(handle)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_conflict_and_denied_are_rejected_at_once(self):
        errors = [Conflict('broker_operation_rejected'), BrokerError('broker_request_denied'),
                  BrokerError('credential_lease_not_active')]
        for error in errors:
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as root:
                fixture = Fixture()
                session, handle = self.prepare(fixture, root)
                due = fixture.native.next_renew = time.monotonic() - 1
                session.broker.renew.side_effect = error
                with self.assertRaisesRegex(c.NativeError, '^claude_broker_renew_rejected$') as caught:
                    c.maintain(handle)
                self.assertEqual(session.broker.renew.call_count, 1)
                self.assertEqual(fixture.native.next_renew, due)
                self.assertIsNone(caught.exception.__cause__)
                c.close(handle)

    def test_tolerated_failure_still_heartbeats(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            fixture.native.next_renew = time.monotonic() - 1
            session.broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
            handle.heartbeat = lambda: False
            with self.assertRaisesRegex(c.NativeError, '^claude_hub_heartbeat_lost$') as caught:
                c.maintain(handle)
            self.assertEqual(live_loop.maintain_fault(caught.exception), 'retry')
            c.close(handle)

    def test_tick_during_prompt_tolerates_one_blip(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            beats = []
            handle.heartbeat = lambda: beats.append(True) or True

            def hook(native, text):
                native.next_renew = time.monotonic() - 1
                session.broker.renew.side_effect = MutationUncertain('broker_http_outcome_uncertain')
                native.tick()
                return result_event()

            fixture.prompt_hook = hook
            result = c.execute(handle, 'Project prompt', time.monotonic() + 30)
            self.assertEqual(result['text'], 'Verified project answer.')
            self.assertEqual(fixture.prompts, ['Project prompt'])
            # execute heartbeats before the prompt, then tick heartbeats on the blip.
            self.assertEqual(beats, [True, True])
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)

            def stale(native, text):
                handle.lease_clock.renewed_at = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
                native.next_renew = 0
                session.broker.renew.side_effect = MutationUncertain('x')
                native.tick()
                return result_event()

            fixture.prompt_hook = stale
            with self.assertRaisesRegex(c.NativeError, '^claude_broker_renew_failed$'):
                c.execute(handle, 'Project prompt', time.monotonic() + 30)
            self.assertEqual(fixture.events, ['stop', 'commit-release'])

    def test_degraded_lease_gets_strict_renew_before_prompt(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            session.broker.renew.side_effect = MutationUncertain('x')
            fixture.native.next_renew = time.monotonic() - 1
            c.maintain(handle)
            with self.assertRaisesRegex(c.NativeError, '^claude_broker_renew_failed$'):
                c.execute(handle, 'Project prompt', time.monotonic() + 30)
            self.assertEqual(fixture.prompts, [])
            self.assertEqual(fixture.events, ['stop', 'commit-release'])
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            session.broker.renew.side_effect = MutationUncertain('x')
            fixture.native.next_renew = time.monotonic() - 1
            c.maintain(handle)
            session.broker.renew.side_effect = None
            result = c.execute(handle, 'Project prompt', time.monotonic() + 30)
            self.assertEqual(result['text'], 'Verified project answer.')
            self.assertEqual(fixture.prompts, ['Project prompt'])
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session, handle = self.prepare(fixture, root)
            before = session.broker.renew.call_count
            c.execute(handle, 'Project prompt', time.monotonic() + 30)
            self.assertEqual(session.broker.renew.call_count, before)

    def test_prepare_anchors_lease_clock_without_renewing(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            before = time.monotonic()
            session, handle = self.prepare(fixture, root)
            after = time.monotonic()
            self.assertGreaterEqual(handle.lease_clock.renewed_at, before)
            self.assertLessEqual(handle.lease_clock.renewed_at, after)
            session.broker.renew.assert_not_called()
            c.close(handle)
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            session.lease.lease_id = 'c' * 32
            t0 = time.monotonic() - 5
            with patch.dict(broker_renew._acquire_started, {'c' * 32: t0}), \
                    patch.object(c, 'NativeProcess', fixture.factory), \
                    patch.object(c, 'launch_context_error', lambda *a: None):
                handle = c.prepare(session, lambda: True, time.monotonic() + 30)
            self.assertEqual(handle.lease_clock.renewed_at, t0)
            session.broker.renew.assert_not_called()
            c.close(handle)

    def test_worker_survives_idle_blips_and_fails_after_window(self):
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

        pending = [MutationUncertain('broker_http_outcome_uncertain'), BoundaryError('transport_failed')]

        def effect(lease):
            if pending:
                raise pending.pop(0)

        fixture = Fixture(renew_due=True)
        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            session.broker.renew.side_effect = effect
            client = IdleClient()
            with patch.object(c, 'NativeProcess', fixture.factory), \
                    patch.object(c, 'launch_context_error', lambda *a: None), \
                    patch('time.monotonic', now):
                worker = live_loop.Worker(live_loop.Settings('claude', 'claude-live', warm_seconds=60),
                                          client, c, session, clock=now, sleep=sleep, log=lambda record: None)
                result = worker.run()
            self.assertEqual(result['outcome'], 'idle_drained')
            self.assertEqual(worker.last_exit, 0)
        fixture = Fixture(renew_due=True)
        maintains = {'n': 0}
        real = c.maintain

        def counting(handle):
            maintains['n'] += 1
            return real(handle)

        with tempfile.TemporaryDirectory() as root:
            session = fixture.session(root)
            session.lease.lease_id = 'd' * 32
            session.broker.renew.side_effect = MutationUncertain('x')
            old = time.monotonic() - broker_renew.TOLERANCE_SECONDS - 1
            client = IdleClient()
            state['now'] = origin
            with patch.dict(broker_renew._acquire_started, {'d' * 32: old}), \
                    patch.object(c, 'NativeProcess', fixture.factory), \
                    patch.object(c, 'launch_context_error', lambda *a: None), \
                    patch.object(c, 'maintain', counting), \
                    patch('time.monotonic', now):
                worker = live_loop.Worker(live_loop.Settings('claude', 'claude-live', warm_seconds=60),
                                          client, c, session, clock=now, sleep=sleep, log=lambda record: None)
                result = worker.run()
            self.assertEqual(result['outcome'], 'failed')
            self.assertEqual(result['error_code'], 'claude_broker_renew_failed')
            self.assertEqual(worker.last_exit, 1)
            self.assertEqual(maintains['n'], 1)
            self.assertEqual(client.claims, 0)

    def test_command_matches_the_audited_subscription_profile(self):
        argv = c.command().argv
        self.assertEqual(argv[0], c.EXECUTABLE)
        for flag in ('-p', '--safe-mode', '--restricted', '--strict-mcp-config'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--model') + 1], c.MODEL)
        self.assertEqual(argv[argv.index('--effort') + 1], c.EFFORT)
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Grep,Glob')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dontAsk')
        self.assertTrue(c.auth._command_matches(c.command(), execution_mode='read_only'))


if __name__ == '__main__':
    unittest.main()
