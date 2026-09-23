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
SOURCE = Path(r'C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
for entry in (str(HERE), str(SOURCE)):
    if entry not in sys.path:
        sys.path.append(entry)

from providers import claude as c  # noqa: E402

TOKEN = 'PRIVATE_OFFLINE_SUBSCRIPTION_TOKEN'


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
    def __init__(self, frames, stdin):
        self.frames = frames
        self.stdin = stdin
        self.returncode = 0
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
    def __init__(self, *, overrides=None, prompt_hook=None, close_error=None, finish_error=None, idle=()):
        self.events, self.calls, self.prompts = [], [], []
        self.overrides = overrides or {}
        self.prompt_hook = prompt_hook
        self.close_error, self.finish_error = close_error, finish_error
        self.idle = list(idle)
        self.native = None
        self.environment = None
        self.factory = self._factory

    def _factory(self, environment, workspace, deadline, renew):
        owner = self
        owner.environment = dict(environment)

        class FakeNative:
            def __init__(self):
                self.renew, self.deadline, self.workspace = renew, deadline, workspace
                self.next_renew = time.monotonic() + 20
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

    def _slow_prompt(self, *, prepare_execute=None, heartbeat=None):
        """execute() -> prompt(); the answer is withheld across three renew intervals."""
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
            script = _SlowFrames(clock, ['wait', 'wait', 'wait', lifecycle, result_event(), c.rt._EOF])
            stdin = _SlowStdin()
            native = c.Native.__new__(c.Native)
            native.deadline = clock['now'] + 500
            native.renew = lambda: c._renew(handle)
            native.next_renew = clock['now'] + 20
            native.writers = []
            native.frames = SimpleNamespace(failed=threading.Event(), queue=script)
            native.process = _SlowProcess(script, stdin)
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
                    outcome.result = c.execute(handle, 'Project prompt', clock['now'] + 500)
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
