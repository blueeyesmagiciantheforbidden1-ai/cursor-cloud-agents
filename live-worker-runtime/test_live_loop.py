import ast
import copy
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
        self.assertGreaterEqual(seen, 5)

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
        worker, client, adapter, _ = self.setup_worker(); client.fail_claim = True
        worker.run()
        self.assertEqual(client.claims, 1); self.assertNotIn('execute', adapter.calls)
        self.assertIn('close', adapter.calls)

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

    def test_idle_drains_and_releases_without_prompt(self):
        worker, client, adapter, clock = self.setup_worker(); client.empty = True
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained'); self.assertEqual(clock.now, 160)
        self.assertNotIn('execute', adapter.calls); self.assertIn('close', adapter.calls)

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


if __name__ == '__main__': unittest.main()
