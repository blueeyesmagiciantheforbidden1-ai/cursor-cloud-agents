import ast
import copy
import re
from pathlib import Path
import unittest
from types import SimpleNamespace

from live_loop import Worker, Settings, LiveError, task_prompt
from provider_errors import ProviderCodeError, error_code


ROOM = 'a' * 32
PROVIDERS = Path(__file__).resolve().parent / 'providers'
BUILTIN_EXCEPTIONS = {'BaseException', 'Exception', 'RuntimeError', 'ValueError', 'OSError',
                      'KeyError', 'TypeError', 'LookupError', 'ArithmeticError'}


class CodeError(ProviderCodeError, RuntimeError):
    """Stands in for a provider adapter's fixed-code error class."""


def module_constant(tree, name):
    """Value of a module-level integer constant, including tuple assignments."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            names = [t.id for t in target.elts] if isinstance(target, ast.Tuple) else [getattr(target, 'id', None)]
            values = value.elts if isinstance(value, ast.Tuple) else [value]
            if name in names:
                return values[names.index(name)].value
    raise AssertionError(name + ' not found')


def exception_classes(path):
    """{class name: base names} for every exception class defined at module level."""
    found = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.ClassDef):
            bases = [b.attr if isinstance(b, ast.Attribute) else getattr(b, 'id', None) for b in node.bases]
            if any(base in BUILTIN_EXCEPTIONS or base in found for base in bases):
                found[node.name] = bases
    return found


class ProviderErrorTests(unittest.TestCase):
    """Guards the invariant behind provider_errors, not just today's providers.

    Lives here because the image builds run this file. Provider modules need
    image-only dependencies, so the enumeration is static.
    """
    def test_every_provider_exception_opts_into_the_marker(self):
        # A new provider that forgets the marker would have its codes flattened
        # to native_or_connection_failure again; this names the class instead.
        seen = 0
        for path in sorted(PROVIDERS.glob('*.py')):
            classes = exception_classes(path)
            for name, bases in classes.items():
                if any(base in classes for base in bases):
                    continue  # inherits from a checked class in the same module
                seen += 1
                self.assertIn('ProviderCodeError', bases,
                              path.name + ':' + name + ' must inherit provider_errors.ProviderCodeError')
        self.assertGreaterEqual(seen, 1)  # a built image carries one provider; the checkout carries five

    def test_marker_keeps_original_base_and_handlers(self):
        class Value(ProviderCodeError, ValueError): pass
        self.assertIsInstance(CodeError('code'), RuntimeError)
        self.assertIsInstance(Value('code'), ValueError)
        self.assertNotIsInstance(CodeError('code'), ValueError)
        self.assertIsInstance(LiveError('code'), RuntimeError)
        self.assertEqual(error_code(CodeError('grok_period_shape')), 'grok_period_shape')
        self.assertEqual(error_code(LiveError('task_lease_lost')), 'task_lease_lost')

    def test_only_fixed_codes_cross_the_boundary(self):
        for text in ('codex_failed\n/home/worker/.codex/auth.json', 'Bad', 'x' * 101, '', '1abc', 'a-b'):
            with self.subTest(text=text):
                self.assertIsNone(error_code(CodeError(text)))
                self.assertIsNone(error_code(LiveError(text)))
        self.assertEqual(error_code(CodeError('a' * 100)), 'a' * 100)
        self.assertIsNone(error_code(RuntimeError('task_deadline_out_of_bounds')))
        self.assertIsNone(error_code(ValueError('native_not_running')))


class Clock:
    def __init__(self): self.now = 100
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class Client:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.claims = 0
        self.completions = []
        self.fail_claim = False
        self.bad_claim = False
        self.fail_completions = 0
        self.active = True
        self.empty = False
        self.task = dict(room_id=ROOM, lease_token='lease', workspace='default',
                         prompt='Help with a design.', messages=[], timeout_seconds=300,
                         deadline=1000, step=0, learning_context={})
        self.room = dict(id=ROOM, workspace='default', prompt=self.task['prompt'],
                         messages=[], status='running', step=0, purpose='project')

    def post(self, path, value):
        self.calls.append((path, copy.deepcopy(value)))
        if path.endswith('/claim'):
            self.claims += 1
            if self.fail_claim: raise OSError('network')
            if self.bad_claim: return {}
            return {'task': None if self.empty else copy.deepcopy(self.task)}
        if path.endswith('/heartbeat'):
            return {'active': self.active, 'deadline': self.task['deadline'], 'server_time': 700}
        if path.endswith('/complete'):
            self.completions.append(copy.deepcopy(value))
            if self.fail_completions:
                self.fail_completions -= 1
                raise OSError('lost acknowledgement')
            return {'room_id': ROOM, 'status': 'completed'}
        return {'accepted': True}

    def get_room(self, room): return copy.deepcopy(self.room)


class Adapter:
    def __init__(self):
        self.calls = []
        self.fail_prepare = False
        self.fail_execute = False
        self.answer = 'A useful answer.'
    def prepare(self, session, heartbeat, deadline):
        self.calls.append('prepare')
        if self.fail_prepare: raise ValueError('provider detail must not leak')
        return SimpleNamespace(state='ready')
    def maintain(self, handle): self.calls.append('maintain')
    def execute(self, handle, prompt, deadline, *, task_kind):
        self.calls.append('execute')
        if self.fail_execute: raise ValueError('provider detail must not leak')
        return {'text': self.answer, 'model': 'example', 'effort': 'max', 'usage': None}
    def close(self, handle): self.calls.append('close')


class LoopTests(unittest.TestCase):
    def setup_worker(self):
        clock = Clock(); client = Client(clock); adapter = Adapter()
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)
        return worker, client, adapter, clock

    def test_one_model_call_and_close_before_completion(self):
        worker, client, adapter, _ = self.setup_worker()
        original = client.post
        def post(path, value):
            if path.endswith('/complete'): self.assertIn('close', adapter.calls)
            return original(path, value)
        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(client.claims, 1)
        states = [v['status'] for p, v in client.calls if p.endswith('/report')]
        self.assertEqual(states[0], 'offline'); self.assertIn('idle', states)
        self.assertIn('busy', states); self.assertEqual(states[-1], 'offline')

    def test_preflight_failure_never_claims_or_reports_idle(self):
        worker, client, adapter, _ = self.setup_worker(); adapter.fail_prepare = True
        result = worker.run()
        self.assertEqual(client.claims, 0)
        self.assertFalse(result['model_call_attempted'])
        self.assertTrue(all(v['status'] == 'offline' for p, v in client.calls if p.endswith('/report')))

    def test_lost_claim_response_is_not_retried(self):
        # A lost /v1/tasks/claim may have assigned a room, so it is not polled
        # again. While still idle that ends as a clean drain, not exit 1.
        worker, client, adapter, _ = self.setup_worker(); client.fail_claim = True
        result = worker.run()
        self.assertEqual(client.claims, 1); self.assertNotIn('execute', adapter.calls)
        self.assertIn('close', adapter.calls)
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertNotIn('error_code', result)
        self.assertEqual(worker.last_exit, 0)
        self.assertNotIn('network', str(result))

    def test_completion_redelivers_identical_result_without_inference(self):
        worker, client, adapter, _ = self.setup_worker(); client.fail_completions = 2
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(len(client.completions), 3)
        self.assertTrue(all(p == client.completions[0] for p in client.completions))
        self.assertEqual(adapter.calls.count('execute'), 1)

    def test_cancelled_lease_never_starts_prompt(self):
        worker, client, adapter, _ = self.setup_worker(); client.active = False
        worker.run(); self.assertNotIn('execute', adapter.calls)

    def test_permanently_lost_success_ack_never_substitutes_failure(self):
        worker, client, adapter, _ = self.setup_worker(); client.fail_completions = 3
        result = worker.run()
        self.assertEqual(result['completion_delivery'], 'unconfirmed')
        self.assertEqual(len(client.completions), 3)
        self.assertTrue(all(p == client.completions[0] and p['exit_code'] == 0 for p in client.completions))
        self.assertEqual(adapter.calls.count('execute'), 1)

    def test_rejection_cleanup_precedes_failure_completion(self):
        worker, client, adapter, _ = self.setup_worker(); client.room['purpose'] = 'improvement'
        original = client.post
        def post(path, value):
            if path.endswith('/complete'): self.assertIn('close', adapter.calls)
            return original(path, value)
        client.post = post
        worker.run(); self.assertEqual(client.completions[0]['exit_code'], 1)

    def test_uncertain_cleanup_does_not_complete_room(self):
        worker, client, adapter, _ = self.setup_worker(); client.room['purpose'] = 'improvement'
        def close(handle): raise ValueError('quarantined')
        adapter.close = close
        result = worker.run()
        self.assertEqual(result['outcome'], 'credential_cleanup_failed')
        self.assertEqual(client.completions, [])

    def test_no_improvement_or_unapproved_workspace(self):
        for change in ({'purpose': 'improvement'}, {'workspace': 'private-server'}):
            with self.subTest(change=change):
                worker, client, adapter, _ = self.setup_worker(); client.room.update(change)
                worker.run(); self.assertNotIn('execute', adapter.calls)

    def test_missing_purpose_is_refused_with_its_own_code(self):
        # Rooms stored by hub revision 00001-clc had no purpose field; the
        # worker refuses them (fail closed) and the record now says why.
        worker, client, adapter, _ = self.setup_worker(); del client.room['purpose']
        result = worker.run()
        self.assertEqual(result['error_code'], 'purpose_missing'); self.assertNotIn('execute', adapter.calls)
        self.assertIn('(purpose_missing)', client.completions[0]['output'])
        for value in ('improvement', '', None, 'PROJECT'):
            with self.subTest(purpose=value):
                worker, client, adapter, _ = self.setup_worker(); client.room['purpose'] = value
                result = worker.run()
                self.assertEqual(result['error_code'], 'project_work_only'); self.assertNotIn('execute', adapter.calls)

    def test_hub_timeout_floor_covers_the_worker_overhead(self):
        # agent_hub.core refuses rooms below MIN_TIMEOUT_SECONDS (60) and below
        # AGENT_MIN_TIMEOUT_SECONDS['codex'] (120). Those literals live in the hub;
        # this pins them to the worker constants so neither drifts unnoticed.
        hub_minimum, hub_codex_minimum = 60, 120
        reserve = Settings('grok', 'grok-live').completion_reserve
        overhead = 30  # claim, heartbeat, room read and prompt build round-trips
        self.assertLessEqual(reserve + 5 + overhead, hub_minimum)
        if not (PROVIDERS / 'codex.py').is_file():
            self.skipTest('codex adapter not in this image')
        source = (PROVIDERS / 'codex.py').read_text(encoding='utf-8')
        finalize = module_constant(ast.parse(source), 'FINALIZE_RESERVE')
        need = int(re.search(r'EXECUTE_WARM_FLOOR = FINALIZE_RESERVE \+ (\d+)', source).group(1))
        self.assertIn('need(EXECUTE_WARM_FLOOR <= remaining <= 900', source)
        self.assertIn('warm_deadline - time.monotonic() >= EXECUTE_WARM_FLOOR', source)
        self.assertLessEqual(finalize + need + reserve, hub_codex_minimum)

    def test_idle_drains_and_releases_without_prompt(self):
        worker, client, adapter, clock = self.setup_worker(); client.empty = True
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained'); self.assertEqual(clock.now, 160)
        self.assertNotIn('execute', adapter.calls); self.assertIn('close', adapter.calls)

    def test_warm_window_expiry_drains_without_a_failure(self):
        # Codex maintain() raises warm_session_expired when its 3600s deadline
        # passes. That deadline starts inside prepare(), before this loop's
        # warm_seconds window, so the raise used to fail the execution.
        worker, client, adapter, clock = self.setup_worker(); client.empty = True
        def maintain(handle):
            adapter.calls.append('maintain')
            raise CodeError('warm_session_expired')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertNotIn('error_code', result)
        self.assertEqual(worker.last_exit, 0)
        self.assertEqual(adapter.calls.count('maintain'), 1)
        self.assertEqual(client.claims, 0)
        self.assertNotIn('execute', adapter.calls)
        self.assertIn('close', adapter.calls)
        self.assertEqual(clock.now, 100)

    def test_idle_transient_maintain_errors_are_retried(self):
        for error in (OSError('transient native pipe'), CodeError('hub_lease_lost'),
                      CodeError('claude_hub_heartbeat_lost'), CodeError('cursor_lease_lost')):
            with self.subTest(error=str(error)):
                worker, client, adapter, clock = self.setup_worker(); client.empty = True
                pending = [error]
                def maintain(handle, pending=pending):
                    adapter.calls.append('maintain')
                    if pending:
                        raise pending.pop()
                adapter.maintain = maintain
                result = worker.run()
                self.assertEqual(result['outcome'], 'idle_drained')
                self.assertNotIn('error_code', result)
                self.assertNotIn('transient native pipe', str(result))
                self.assertGreater(adapter.calls.count('maintain'), 1)
                self.assertEqual(worker.last_exit, 0)
                self.assertEqual(clock.now, 160)

    def test_idle_report_http_error_is_retried_until_the_window_ends(self):
        worker, client, adapter, clock = self.setup_worker(); client.empty = True
        faults = [3]
        original = client.post
        def post(path, value):
            if path.endswith('/report') and value.get('status') == 'idle' and faults[0]:
                faults[0] -= 1
                client.calls.append((path, copy.deepcopy(value)))
                raise OSError('transient hub socket')
            return original(path, value)
        client.post = post
        result = worker.run()
        self.assertEqual(faults[0], 0)
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertNotIn('error_code', result)
        self.assertNotIn('transient hub socket', str(result))
        self.assertGreater(client.claims, 0)
        self.assertEqual(worker.last_exit, 0)
        self.assertEqual(clock.now, 160)

    def test_vetted_idle_errors_still_fail(self):
        worker, client, adapter, _ = self.setup_worker()
        def maintain(handle):
            adapter.calls.append('maintain')
            raise CodeError('claude_warm_process_ended')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'claude_warm_process_ended')
        self.assertEqual(adapter.calls.count('maintain'), 1)
        self.assertEqual(client.claims, 0)
        self.assertEqual(worker.last_exit, 1)

        worker, client, adapter, _ = self.setup_worker(); client.bad_claim = True
        result = worker.run()
        self.assertEqual(result['error_code'], 'claim_response_invalid')
        self.assertEqual(client.claims, 1)
        self.assertNotIn('execute', adapter.calls)

        worker, client, adapter, _ = self.setup_worker()
        original = client.post
        def post(path, value):
            if path.endswith('/report') and value.get('status') == 'idle':
                client.calls.append((path, copy.deepcopy(value)))
                return {'accepted': False}
            return original(path, value)
        client.post = post
        result = worker.run()
        self.assertEqual(result['error_code'], 'heartbeat_not_acknowledged')
        self.assertEqual(client.claims, 0)

    def test_claim_waits_out_a_warm_window_that_cannot_host_a_task(self):
        # codex-qmhhc claimed in the last ~21s of the warm window and died
        # before the model call. Below the task budget the loop drains idle.
        clock = Clock(); client = Client(clock); adapter = Adapter()
        adapter.EXECUTE_WARM_FLOOR = 75
        def prepare(session, heartbeat, deadline):
            adapter.calls.append('prepare')
            return SimpleNamespace(state='ready', warm_deadline=clock.now + 20)
        adapter.prepare = prepare
        worker = Worker(Settings('codex', 'codex-live', warm_seconds=3600), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertNotIn('error_code', result)
        self.assertFalse(result['model_call_attempted'])
        self.assertEqual(client.claims, 0)
        self.assertNotIn('execute', adapter.calls)
        self.assertIn('close', adapter.calls)
        self.assertEqual(worker.last_exit, 0)
        self.assertEqual(clock.now, 100)

    def test_claim_proceeds_when_the_warm_window_covers_the_task(self):
        clock = Clock(); client = Client(clock); adapter = Adapter()
        adapter.EXECUTE_WARM_FLOOR = 75
        def prepare(session, heartbeat, deadline):
            adapter.calls.append('prepare')
            return SimpleNamespace(state='ready', warm_deadline=clock.now + 3600)
        adapter.prepare = prepare
        worker = Worker(Settings('codex', 'codex-live', warm_seconds=3600), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(client.claims, 1)
        self.assertEqual(adapter.calls.count('execute'), 1)

    def test_room_read_transport_error_retries_then_uses_a_fixed_code(self):
        worker, client, adapter, _ = self.setup_worker()
        faults = [1]
        original = client.get_room
        def get_room(room):
            if faults[0]:
                faults[0] -= 1
                raise OSError('transient room read')
            return original(room)
        client.get_room = get_room
        result = worker.run()
        self.assertEqual(faults[0], 0)
        self.assertEqual(result['outcome'], 'completed')
        self.assertNotIn('transient room read', str(result))
        self.assertEqual(adapter.calls.count('execute'), 1)

        worker, client, adapter, _ = self.setup_worker()
        def get_room_down(room):
            raise OSError('transient room read')
        client.get_room = get_room_down
        result = worker.run()
        self.assertEqual(result['error_code'], 'task_setup_unavailable')
        self.assertFalse(result['model_call_attempted'])
        self.assertTrue(result['claim_attempted'])
        self.assertEqual(client.claims, 1)
        self.assertNotIn('execute', adapter.calls)
        self.assertNotIn('transient room read', str(result) + str(client.completions))
        self.assertIn('(task_setup_unavailable)', client.completions[0]['output'])

    def test_report_error_after_claim_still_fails(self):
        worker, client, adapter, _ = self.setup_worker()
        original = client.post
        def post(path, value):
            if path.endswith('/report') and value.get('status') == 'busy':
                client.calls.append((path, copy.deepcopy(value)))
                raise OSError('transient hub socket')
            return original(path, value)
        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'task_setup_unavailable')
        self.assertFalse(result['model_call_attempted'])
        self.assertNotIn('execute', adapter.calls)
        self.assertNotIn('transient hub socket', str(result) + str(client.completions))
        self.assertIn('(task_setup_unavailable)', client.completions[0]['output'])
        self.assertEqual(worker.last_exit, 1)

    def test_provider_error_is_never_retried_or_leaked(self):
        worker, client, adapter, _ = self.setup_worker(); adapter.fail_execute = True
        result = worker.run()
        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertNotIn('provider detail', str(result) + str(client.completions))

    def test_vetted_provider_code_is_recorded_and_delivered(self):
        worker, client, adapter, _ = self.setup_worker()
        def execute(handle, prompt, deadline, *, task_kind): raise CodeError('task_deadline_out_of_bounds')
        adapter.execute = execute
        result = worker.run()
        self.assertEqual(result['error_code'], 'task_deadline_out_of_bounds')
        self.assertEqual(client.completions[0]['exit_code'], 1)
        self.assertIn('(task_deadline_out_of_bounds)', client.completions[0]['output'])

    def test_unvetted_provider_text_stays_generic(self):
        for text in ('codex_failed\n/home/worker/.codex/auth.json', 'Bad Code', 'x' * 101, ''):
            with self.subTest(text=text):
                worker, client, adapter, _ = self.setup_worker()
                def execute(handle, prompt, deadline, *, task_kind): raise CodeError(text)
                adapter.execute = execute
                result = worker.run()
                self.assertEqual(result['error_code'], 'native_or_connection_failure')
                self.assertNotIn('auth.json', str(result) + str(client.completions))
                self.assertIn('(native_or_connection_failure)', client.completions[0]['output'])

    def test_all_previous_agent_text_preserved_as_context(self):
        worker, client, _, _ = self.setup_worker()
        messages = [{'agent': p, 'text': p + ' context', 'exit_code': 0} for p in ('codex', 'claude', 'cursor', 'copilot')]
        client.task['messages'] = client.room['messages'] = messages
        text = task_prompt(client.task, client.room, 'grok')
        for m in messages: self.assertIn(m['text'], text)

    def test_insufficient_server_deadline_never_calls_provider(self):
        worker, client, adapter, _ = self.setup_worker(); client.task['deadline'] = 710
        worker.run(); self.assertNotIn('execute', adapter.calls)

    def test_idle_warm_session_loss_drains_without_a_task_or_model_call(self):
        worker, client, adapter, _ = self.setup_worker(); client.empty = True
        seen = {'n': 0}
        def maintain(handle):
            adapter.calls.append('maintain'); seen['n'] += 1
            if seen['n'] == 2: raise CodeError('copilot_warm_session_lost')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertNotIn('error_code', result)
        self.assertFalse(result['model_call_attempted'])
        self.assertEqual(worker.last_exit, 0)
        self.assertNotIn('execute', adapter.calls)
        self.assertIn('close', adapter.calls)
        self.assertEqual(client.completions, [])
        self.assertGreaterEqual(client.claims, 1)

    def test_warm_session_loss_after_a_model_call_stays_a_failure(self):
        worker, client, adapter, _ = self.setup_worker()
        def execute(handle, prompt, deadline, *, task_kind):
            adapter.calls.append('execute')
            raise CodeError('copilot_warm_session_lost')
        adapter.execute = execute
        result = worker.run()
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'copilot_warm_session_lost')
        self.assertTrue(result['model_call_attempted'])
        self.assertEqual(client.completions[0]['exit_code'], 1)
        self.assertEqual(worker.last_exit, 1)

    def test_idle_maintain_failure_other_than_session_loss_stays_a_failure(self):
        worker, client, adapter, _ = self.setup_worker(); client.empty = True
        def maintain(handle):
            adapter.calls.append('maintain')
            raise CodeError('copilot_tools_forbidden')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'copilot_tools_forbidden')
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(client.completions, [])
        self.assertNotIn('execute', adapter.calls)

    def test_idle_session_loss_without_credential_release_stays_failed(self):
        worker, client, adapter, _ = self.setup_worker(); client.empty = True
        def maintain(handle):
            adapter.calls.append('maintain')
            raise CodeError('copilot_warm_session_lost')
        def close(handle):
            adapter.calls.append('close')
            raise ValueError('quarantined')
        adapter.maintain, adapter.close = maintain, close
        result = worker.run()
        self.assertEqual(result['outcome'], 'credential_cleanup_failed')
        self.assertEqual(client.completions, [])
        self.assertEqual(worker.last_exit, 1)


if __name__ == '__main__': unittest.main()
