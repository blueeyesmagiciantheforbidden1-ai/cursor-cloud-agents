import ast
import copy
import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import unittest
from types import SimpleNamespace

import live_loop
from live_loop import Worker, Settings, LiveError, task_prompt, handle_released
import provider_errors
from provider_errors import ProviderCodeError, error_code


ROOM = 'a' * 32
PROVIDERS = Path(__file__).resolve().parent / 'providers'
SPAN_FIELDS = ('trace_id', 'room_id', 'step', 'attempt_key', 'span', 'duration_ms', 'outcome', 'error_code')
DIGEST = 'sha256:' + ('ab' * 32)
TRACE = '01234567-89ab-cdef-0123-456789abcdef'


@contextmanager
def capability_env(**values):
    """Set manifest env for one assertion and restore whatever was there."""
    keys = {'CLOUD_RUN_EXECUTION', 'RUNCREW_REGION', 'RUNCREW_IMAGE_DIGEST'} | set(values)
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def arm_manifest(adapter):
    adapter.CLI_NAME = 'runcrew-live-grok'
    adapter.CLI_VERSION = '1'
    adapter.TOOLS_POLICY = 'deny_all_and_abort_on_observed_tool'
    adapter.MODEL = 'grok-4.7'
    adapter.EFFORT = 'xhigh'
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

    def test_capability_constants_repeat_an_existing_literal(self):
        # Versions and policies are published only when the module already
        # contains that exact literal. A newly invented version fails this.
        names = ('CLI_NAME', 'CLI_VERSION', 'TOOLS_POLICY')
        seen = {name: 0 for name in names}
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        for path in sorted(PROVIDERS.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            assigned = {}
            for node in tree.body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names
                        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                    assigned[node.targets[0].id] = node.value.value
            literals = [node.value for node in ast.walk(tree)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)]
            for name, value in assigned.items():
                seen[name] += 1
                self.assertGreaterEqual(literals.count(value), 2, path.name + ':' + name + '=' + value)
                filler = {
                    'runner': 'local', 'region': 'us-central1', 'cli_name': 'grok', 'cli_version': '1',
                    'workspace_mode': 'read_only', 'tools_policy': 'deny_all', 'model': 'grok-4.7',
                    'effort': 'xhigh', 'auth_alias': 'grok', 'image_digest': DIGEST,
                    'started_at': '2020-01-01T00:00:00Z',
                }
                filler[name.lower()] = value
                self.assertTrue(live_loop.capability_valid(filler, now=now), path.name + ':' + name)
        # A built image carries one provider; only the checkout carries all
        # five, and only there must every constant appear at least once.
        present = {path.stem for path in PROVIDERS.glob('*.py')}
        if {'claude', 'codex', 'copilot', 'cursor', 'grok'} <= present:
            self.assertGreaterEqual(seen['CLI_NAME'], 1)
            self.assertGreaterEqual(seen['CLI_VERSION'], 1)
            self.assertGreaterEqual(seen['TOOLS_POLICY'], 1)


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
    def setup_worker(self, trace_id=None, log=None):
        clock = Clock(); client = Client(clock); adapter = Adapter()
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, adapter,
                        object(), clock=clock, sleep=clock.sleep,
                        log=log if log is not None else (lambda record: None), trace_id=trace_id)
        return worker, client, adapter, clock

    def span_lines(self, logs):
        return [item for item in logs if isinstance(item, dict) and item.get('kind') == 'runcrew_live_span']

    def assert_hidden(self, logs, *secrets):
        blob = json.dumps(logs)
        for secret in secrets:
            self.assertNotIn(secret, blob)

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
        # again, and it is a counted failure: the room it may have leased
        # stalls, and the controller's backoff and three-strike bound apply.
        worker, client, adapter, _ = self.setup_worker(); client.fail_claim = True
        result = worker.run()
        self.assertEqual(client.claims, 1); self.assertNotIn('execute', adapter.calls)
        self.assertIn('close', adapter.calls)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'claim_response_uncertain')
        self.assertTrue(result['claim_attempted']); self.assertFalse(result['model_call_attempted'])
        self.assertEqual(worker.last_exit, 1)
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
        self.assertIn("need(time.monotonic() < handle.warm_deadline, 'warm_session_expired')", source)
        self.assertLessEqual(finalize + need + reserve, hub_codex_minimum)

    def test_idle_drains_and_releases_without_prompt(self):
        worker, client, adapter, clock = self.setup_worker(); client.empty = True
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained'); self.assertEqual(clock.now, 160)
        self.assertNotIn('drain_code', result)
        self.assertNotIn('error_code', result)
        self.assertEqual(worker.last_exit, 0)
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
        self.assertEqual(result['drain_code'], 'warm_session_expired')
        self.assertNotIn('error_code', result)
        self.assertEqual(worker.last_exit, 0)
        self.assertEqual(adapter.calls.count('maintain'), 1)
        self.assertEqual(client.claims, 0)
        self.assertNotIn('execute', adapter.calls)
        self.assertIn('close', adapter.calls)
        self.assertEqual(clock.now, 100)

    def test_unvetted_maintain_error_fails_instead_of_idling(self):
        # 2026-09-23 review: a cursor startup-deadline MetadataError (no vetted
        # code) was retried for the whole warm window; the worker reported idle,
        # never claimed, and exited 0 every hour. It must fail at once instead.
        for error in (OSError('native pipe'), ValueError('native_metadata_deadline'),
                      RuntimeError('credential_lease_not_active')):
            with self.subTest(error=type(error).__name__):
                worker, client, adapter, clock = self.setup_worker(); client.empty = True
                def maintain(handle, error=error):
                    adapter.calls.append('maintain'); raise error
                adapter.maintain = maintain
                result = worker.run()
                self.assertEqual(adapter.calls.count('maintain'), 1)
                self.assertEqual(client.claims, 0)
                self.assertEqual(result['outcome'], 'failed')
                self.assertEqual(result['error_code'], 'native_or_connection_failure')
                self.assertEqual(worker.last_exit, 1)
                self.assertIn('close', adapter.calls)

    def test_quota_exhaustion_fails_the_room_with_its_own_exit_code(self):
        # 2026-09-23: an account spend limit refused every claude call; three
        # relaunches failed three rooms as native_or_connection_failure and
        # spent the slot's three strikes in 20 minutes.
        for code in ('claude_quota_exhausted', 'included_quota_exhausted'):
            with self.subTest(code=code):
                worker, client, adapter, _ = self.setup_worker()
                def execute(handle, prompt, deadline, *, task_kind, code=code):
                    adapter.calls.append('execute'); raise CodeError(code)
                adapter.execute = execute
                result = worker.run()
                self.assertEqual(result['outcome'], 'failed')
                self.assertEqual(result['error_code'], code)
                self.assertIs(result['provider_quota_exhausted'], True)
                self.assertEqual(worker.last_exit, provider_errors.QUOTA_EXIT_CODE)
                self.assertEqual(adapter.calls.count('execute'), 1)
                self.assertEqual(len(client.completions), 1)
                completion = client.completions[0]
                self.assertEqual(completion['exit_code'], 1)
                self.assertEqual(completion['error_code'], code)
                self.assertIs(completion['model_call_attempted'], True)
                self.assertIn('usage limit is exhausted (' + code + ')', completion['output'])
                self.assertIn('grok worker', completion['output'])
        # Other failures keep exit 1 and the generic room text.
        worker, client, adapter, _ = self.setup_worker(); adapter.fail_execute = True
        result = worker.run()
        self.assertEqual(worker.last_exit, 1)
        self.assertNotIn('provider_quota_exhausted', result)
        self.assertIn('stopped before it could deliver', client.completions[0]['output'])

    def test_maintain_quota_exhausted_parks_without_claim(self):
        # Quota seen in maintain() (idle refresh) must park exit 75 and never claim.
        # live_loop already maps is_quota codes from maintain to this path; no loop fix.
        import io
        from live_loop import finish_exit
        for code in ('claude_quota_exhausted', 'grok_provider_quota_exhausted', 'included_quota_exhausted'):
            with self.subTest(code=code):
                worker, client, adapter, _ = self.setup_worker()
                client.empty = True

                def maintain(handle, code=code):
                    adapter.calls.append('maintain')
                    raise CodeError(code)

                adapter.maintain = maintain
                result = worker.run()
                self.assertEqual(result['outcome'], 'failed')
                self.assertEqual(result['error_code'], code)
                self.assertIs(result['provider_quota_exhausted'], True)
                self.assertEqual(worker.last_exit, provider_errors.QUOTA_EXIT_CODE)
                self.assertEqual(
                    finish_exit(worker.settings.agent, result, worker.last_exit, io.StringIO()),
                    provider_errors.QUOTA_EXIT_CODE)
                self.assertEqual(client.claims, 0)
                self.assertNotIn('execute', adapter.calls)
                self.assertIn('close', adapter.calls)

    def test_failed_completion_carries_structured_facts(self):
        # Before the model call: the hub may requeue safely.
        worker, client, adapter, _ = self.setup_worker()
        del client.room['purpose']
        result = worker.run()
        self.assertEqual(result['error_code'], 'purpose_missing')
        completion = client.completions[0]
        self.assertEqual(completion['error_code'], 'purpose_missing')
        self.assertIs(completion['model_call_attempted'], False)
        self.assertEqual(set(completion), {'lease_token', 'output', 'exit_code', 'error_code',
                                           'model_call_attempted', 'step'})
        # A success keeps today's payload plus the claimed step (MyHero MH-005).
        worker, client, adapter, _ = self.setup_worker()
        worker.run()
        self.assertEqual(set(client.completions[0]), {'lease_token', 'output', 'exit_code', 'step'})

    def test_sigterm_interrupt_during_execute_sends_one_worker_stopping_completion(self):
        # Demand's review of a0fd2c3: the entrypoint's SIGTERM handler raises
        # KeyboardInterrupt, which used to skip run()'s failure path entirely.
        worker, client, adapter, _ = self.setup_worker()
        def execute(handle, prompt, deadline, *, task_kind):
            adapter.calls.append('execute'); raise KeyboardInterrupt
        adapter.execute = execute
        result = worker.run()
        self.assertEqual(result['error_code'], 'worker_stopping')
        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(len(client.completions), 1)
        self.assertEqual(client.completions[0]['error_code'], 'worker_stopping')
        self.assertIs(client.completions[0]['model_call_attempted'], True)
        self.assertEqual(client.completions[0]['exit_code'], 1)

    def test_sigterm_interrupt_while_idle_is_a_clean_drain(self):
        worker, client, adapter, _ = self.setup_worker(); client.empty = True
        def maintain(handle):
            adapter.calls.append('maintain'); raise KeyboardInterrupt
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertEqual(worker.last_exit, 0)
        self.assertEqual(client.claims, 0)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertEqual(client.completions, [])

    def test_signal_never_interrupts_close_or_completion(self):
        worker, client, adapter, _ = self.setup_worker()
        seen = []
        def close(handle):
            adapter.calls.append('close')
            seen.append(('close', worker.critical, worker.on_signal()))
        adapter.close = close
        original = client.post
        def post(path, value):
            if path.endswith('/complete'):
                seen.append(('complete', worker.critical, worker.on_signal()))
            return original(path, value)
        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(seen, [('close', 1, False), ('complete', 1, False)])
        self.assertTrue(worker.stopping)
        self.assertEqual(worker.critical, 0)
        # Outside a critical section: interrupt exactly once.
        fresh, _, _, _ = self.setup_worker()
        self.assertTrue(fresh.on_signal())
        self.assertFalse(fresh.on_signal())
        self.assertTrue(fresh.stopping)

    def test_revocation_seen_during_task_setup_skips_the_completion(self):
        worker, client, adapter, _ = self.setup_worker(); client.active = False
        result = worker.run()
        self.assertEqual(result['error_code'], 'task_lease_lost')
        self.assertTrue(worker.lease_revoked)
        self.assertEqual(result['completion_delivery'], 'skipped_lease_revoked')
        self.assertEqual(client.completions, [])
        self.assertNotIn('execute', adapter.calls)

    def test_exit_line_carries_only_vetted_codes(self):
        from live_loop import exit_line
        self.assertEqual(exit_line('copilot', 1, {'error_code': 'copilot_unexpected_pre_prompt_activity'}),
                         'copilot worker exit 1: copilot_unexpected_pre_prompt_activity')
        self.assertEqual(exit_line('claude', 75, {'error_code': 'claude_quota_exhausted'}),
                         'claude worker exit 75: claude_quota_exhausted')
        for bad in ({'error_code': 'C:/secret/path token=abc'}, {'error_code': None}, {}, None, 'x'):
            with self.subTest(bad=bad):
                self.assertEqual(exit_line('grok', 1, bad), 'grok worker exit 1: unrecorded')
        self.assertEqual(exit_line('Bad Agent!', 1, {}), 'worker worker exit 1: unrecorded')

    def test_finish_exit_prints_once_only_on_failure_and_keeps_75(self):
        import io
        from live_loop import finish_exit
        for outcome in ('completed', 'idle_drained'):
            stream = io.StringIO()
            self.assertEqual(finish_exit('codex', {'outcome': outcome}, 0, stream), 0)
            self.assertEqual(stream.getvalue(), '')
        stream = io.StringIO()
        self.assertEqual(finish_exit('codex', {'outcome': 'failed', 'error_code': 'claim_response_uncertain'}, 1,
                                     stream), 1)
        self.assertEqual(stream.getvalue().splitlines(), ['codex worker exit 1: claim_response_uncertain'])
        stream = io.StringIO()
        self.assertEqual(finish_exit('claude', {'outcome': 'failed', 'error_code': 'claude_quota_exhausted'},
                                     provider_errors.QUOTA_EXIT_CODE, stream), provider_errors.QUOTA_EXIT_CODE)
        self.assertEqual(stream.getvalue().splitlines(), ['claude worker exit 75: claude_quota_exhausted'])

        class Broken:
            def write(self, _):
                raise OSError('stderr closed')

            def flush(self):
                raise OSError('stderr closed')
        self.assertEqual(finish_exit('claude', {'outcome': 'failed', 'error_code': 'claude_quota_exhausted'},
                                     provider_errors.QUOTA_EXIT_CODE, Broken()), provider_errors.QUOTA_EXIT_CODE)
        self.assertEqual(finish_exit('grok', None, None, io.StringIO()), 1)

    def test_quota_codes_are_recognised_by_suffix_only(self):
        for code in ('claude_quota_exhausted', 'included_quota_exhausted', 'grok_provider_quota_exhausted'):
            self.assertTrue(provider_errors.is_quota(code))
        for code in (None, '', 'quota_exhausted_later', 'claude_quota', 'Claude_quota_exhausted',
                     'native_quota_unavailable', 'x' * 99 + '_quota_exhausted'):
            self.assertFalse(provider_errors.is_quota(code))

    def test_maintain_retries_are_bounded(self):
        worker, client, adapter, clock = self.setup_worker(); client.empty = True
        def maintain(handle):
            adapter.calls.append('maintain'); raise CodeError('claude_hub_heartbeat_lost')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(adapter.calls.count('maintain'), 4)  # first try + MAX_MAINTAIN_RETRIES
        self.assertEqual(result['error_code'], 'claude_hub_heartbeat_lost')
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(client.claims, 0)

    def test_maintain_retry_counter_resets_after_success(self):
        worker, client, adapter, clock = self.setup_worker(); client.empty = True
        seen = {'n': 0}
        def maintain(handle):
            seen['n'] += 1; adapter.calls.append('maintain')
            if seen['n'] % 2: raise CodeError('grok_hub_heartbeat_lost')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained'); self.assertEqual(worker.last_exit, 0)

    def test_hub_blip_before_prepare_does_not_fail_the_run(self):
        # The credential is leased before run(); a failed first report used to
        # exit 1 with nothing releasing it, blocking the slot outright.
        worker, client, adapter, clock = self.setup_worker(); client.empty = True
        original = client.post; state = {'first': True}
        def post(path, value):
            if path.endswith('/report') and state['first']:
                state['first'] = False; raise OSError('hub blip')
            return original(path, value)
        client.post = post
        result = worker.run()
        self.assertIn('prepare', adapter.calls)
        self.assertEqual(result['outcome'], 'idle_drained'); self.assertEqual(worker.last_exit, 0)

    def test_idle_hub_heartbeat_codes_from_maintain_are_retried(self):
        for error in (CodeError('hub_lease_lost'),
                      CodeError('claude_hub_heartbeat_lost'), CodeError('cursor_lease_lost'),
                      CodeError('copilot_hub_heartbeat_lost')):
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

    def test_claim_sends_worker_id_and_completion_echoes_step(self):
        # MyHero MH-005: the hub records which execution held the attempt.
        worker, client, _, _ = self.setup_worker()
        client.task['step'] = client.room['step'] = 3
        worker.run()
        claims = [value for path, value in client.calls if path.endswith('/claim')]
        self.assertEqual(claims[0], {'worker_id': 'grok-live'})
        self.assertEqual(client.completions[0]['step'], 3)

    def test_worker_id_the_hub_would_refuse_is_left_out_of_the_claim(self):
        worker, client, _, _ = self.setup_worker()
        worker.settings = replace(worker.settings, worker_id='g' * 49)
        worker.run()
        claims = [value for path, value in client.calls if path.endswith('/claim')]
        self.assertEqual(claims[0], {})

    def test_repair_errors_reach_the_prompt(self):
        worker, client, _, _ = self.setup_worker()
        client.task['repair'] = {'attempt': 1, 'errors': ['invariant failed: len(batch_outcomes) != value']}
        text = task_prompt(client.task, client.room, 'grok')
        self.assertIn('rejected by the hub', text)
        self.assertIn('invariant failed: len(batch_outcomes) != value', text)
        for bad in ({'errors': 'x'}, {'errors': []}, {'errors': ['y' * 161]}, {'errors': [1]},
                    {'errors': ['z'] * 9}, 'repair'):
            with self.subTest(bad=bad):
                client.task['repair'] = bad
                text = task_prompt(client.task, client.room, 'grok')
                self.assertNotIn('rejected by the hub', text)
                self.assertNotIn('previous_attempt_rejected_by_hub', text)
        del client.task['repair']
        self.assertNotIn('previous_attempt_rejected_by_hub', task_prompt(client.task, client.room, 'grok'))

    def test_idle_drain_records_its_drain_code(self):
        # Light 25d gap: pins outcome['drain_code'] on the second idle_drained path.
        worker, client, adapter, _ = self.setup_worker(); client.empty = True
        seen = {'n': 0}
        def maintain(handle):
            adapter.calls.append('maintain'); seen['n'] += 1
            if seen['n'] == 2: raise CodeError('copilot_warm_session_lost')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertEqual(result['drain_code'], 'copilot_warm_session_lost')

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

    def test_closed_handle_maintain_fault_is_not_retried_until_the_deadline(self):
        # Copilot maintain() closes, then re-raises copilot_hub_heartbeat_lost.
        # That code is idle-retryable, so the loop used to call maintain() on
        # the dead handle until warm_seconds elapsed and then exit 0.
        worker, client, adapter, clock = self.setup_worker()
        def maintain(handle):
            adapter.calls.append('maintain')
            handle.finished = True
            raise CodeError('copilot_hub_heartbeat_lost')
        def close(handle):
            adapter.calls.append('close')
        adapter.maintain, adapter.close = maintain, close
        result = worker.run()
        self.assertEqual(adapter.calls.count('maintain'), 1)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'copilot_hub_heartbeat_lost')
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(client.claims, 0)
        self.assertNotIn('execute', adapter.calls)
        self.assertIn('close', adapter.calls)
        self.assertEqual(clock.now, 100)
        self.assertLess(clock.now - 100, 45)

    def test_closed_handle_follow_up_does_not_replace_the_original_code(self):
        # The second maintain() on a finished Copilot handle is
        # copilot_handle_not_idle. Retrying once recorded that instead of the
        # heartbeat loss that closed the session.
        worker, client, adapter, clock = self.setup_worker()
        def maintain(handle):
            adapter.calls.append('maintain')
            if getattr(handle, 'finished', False):
                raise CodeError('copilot_handle_not_idle')
            handle.finished = True
            raise CodeError('copilot_hub_heartbeat_lost')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(adapter.calls.count('maintain'), 1)
        self.assertEqual(result['error_code'], 'copilot_hub_heartbeat_lost')
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(clock.now, 100)
        self.assertEqual(client.claims, 0)

    def test_quarantined_handle_is_not_polled_and_cannot_exit_clean(self):
        worker, client, adapter, clock = self.setup_worker()
        def maintain(handle):
            adapter.calls.append('maintain')
            handle.close_failed = True
            handle.state = 'quarantined'
            raise CodeError('hub_lease_lost')
        def close(handle):
            adapter.calls.append('close')
            raise ValueError('quarantined')
        adapter.maintain, adapter.close = maintain, close
        result = worker.run()
        self.assertEqual(adapter.calls.count('maintain'), 1)
        self.assertEqual(result['outcome'], 'credential_cleanup_failed')
        self.assertEqual(worker.last_exit, 1)
        self.assertNotEqual(result['outcome'], 'idle_drained')
        self.assertEqual(client.claims, 0)
        self.assertEqual(clock.now, 100)

    def test_session_loss_after_the_adapter_closed_still_drains(self):
        # warm-session loss is a failure classification in idle_fault, then
        # the except path drains only when close has released the credential.
        worker, client, adapter, _ = self.setup_worker(); client.empty = True
        def maintain(handle):
            adapter.calls.append('maintain')
            handle.finished = True
            raise CodeError('copilot_warm_session_lost')
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertNotIn('error_code', result)
        self.assertEqual(worker.last_exit, 0)
        self.assertEqual(adapter.calls.count('maintain'), 1)
        self.assertIn('close', adapter.calls)
        self.assertEqual(client.claims, 0)

    def test_stop_during_maintain_does_not_claim(self):
        # warm_seconds still covers a task, so the short-window break does not
        # run. stopping set during maintain must still skip the claim.
        worker, client, adapter, clock = self.setup_worker()
        def maintain(handle):
            adapter.calls.append('maintain')
            worker.stopping = True
        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(client.claims, 0)
        self.assertNotIn('execute', adapter.calls)
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertNotIn('error_code', result)
        self.assertEqual(worker.last_exit, 0)
        self.assertIn('close', adapter.calls)
        self.assertEqual(clock.now, 100)

    def test_stop_during_maintain_without_credential_release_exits_unclean(self):
        worker, client, adapter, _ = self.setup_worker()
        def maintain(handle):
            adapter.calls.append('maintain')
            worker.stopping = True
        def close(handle):
            adapter.calls.append('close')
            raise ValueError('quarantined')
        adapter.maintain, adapter.close = maintain, close
        result = worker.run()
        self.assertEqual(client.claims, 0)
        self.assertEqual(result['outcome'], 'credential_cleanup_failed')
        self.assertEqual(worker.last_exit, 1)

    def test_lost_claim_does_not_hold_a_lease_or_exit_clean_without_release(self):
        # Hub lease is 45s (agent_hub.core.LEASE_SECONDS) and only a heartbeat
        # with the token extends it. A lost claim response may have assigned
        # the room, but this process never sees the token: it must not post a
        # task heartbeat, must not claim again, and must return immediately.
        # Exit 0 only if close released the credential.
        worker, client, adapter, clock = self.setup_worker(); client.fail_claim = True
        result = worker.run()
        heartbeats = [path for path, _ in client.calls if path.endswith('/heartbeat')]
        self.assertEqual(heartbeats, [])
        self.assertEqual(client.claims, 1)
        self.assertEqual(clock.now, 100)
        self.assertLess(clock.now - 100, 45)
        self.assertEqual(result['error_code'], 'claim_response_uncertain')
        self.assertEqual(worker.last_exit, 1)
        self.assertIn('close', adapter.calls)
        self.assertNotIn('execute', adapter.calls)

        worker, client, adapter, clock = self.setup_worker(); client.fail_claim = True
        def close(handle):
            adapter.calls.append('close')
            raise ValueError('quarantined')
        adapter.close = close
        result = worker.run()
        self.assertEqual(client.claims, 1)
        self.assertEqual([path for path, _ in client.calls if path.endswith('/heartbeat')], [])
        self.assertEqual(result['outcome'], 'credential_cleanup_failed')
        self.assertEqual(worker.last_exit, 1)
        self.assertNotEqual(result['outcome'], 'idle_drained')
        self.assertEqual(clock.now, 100)

    def test_offline_report_failure_does_not_hide_a_failed_release(self):
        # A missed final /report is not a credential. Exit 0 stays a clean
        # release; a failed close still exits unclean even if the report also fails.
        worker, client, adapter, _ = self.setup_worker(); client.empty = True
        original = client.post
        def post(path, value):
            if path.endswith('/report') and value.get('status') == 'offline' and value.get('last_exit_code') == 0:
                client.calls.append((path, copy.deepcopy(value)))
                raise OSError('offline report dropped')
            return original(path, value)
        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertEqual(result['offline_report'], 'unconfirmed')
        self.assertEqual(worker.last_exit, 0)
        self.assertIn('close', adapter.calls)

        worker, client, adapter, _ = self.setup_worker(); client.empty = True
        original = client.post
        def close(handle):
            adapter.calls.append('close')
            raise ValueError('quarantined')
        def post_both(path, value):
            if path.endswith('/report') and value.get('status') == 'offline' and value.get('last_exit_code') is not None:
                client.calls.append((path, copy.deepcopy(value)))
                raise OSError('offline report dropped')
            return original(path, value)
        adapter.close = close
        client.post = post_both
        result = worker.run()
        self.assertEqual(result['outcome'], 'credential_cleanup_failed')
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(result.get('offline_report'), 'unconfirmed')

    def test_handle_released_matches_provider_close_flags(self):
        self.assertFalse(handle_released(SimpleNamespace(state='ready')))
        self.assertFalse(handle_released(SimpleNamespace()))
        self.assertTrue(handle_released(SimpleNamespace(finished=True)))
        self.assertTrue(handle_released(SimpleNamespace(close_failed=True)))
        for state in ('closed', 'closing', 'quarantined'):
            self.assertTrue(handle_released(SimpleNamespace(state=state)))

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

    def _span_contract(self, spans, trace_id):
        for item in spans:
            self.assertEqual(set(item), {'kind', *SPAN_FIELDS})
            self.assertEqual(item['trace_id'], trace_id)
            self.assertIsInstance(item['duration_ms'], int)
            self.assertGreaterEqual(item['duration_ms'], 0)
            self.assertTrue(provider_errors.SAFE_CODE.fullmatch(item['outcome']))
            if item['error_code'] is not None:
                self.assertEqual(item['error_code'], item['outcome'])

    def test_spans_for_a_completed_task(self):
        logs = []
        token = 'lease-token-not-for-logs'
        prompt = 'PROMPT-SECRET-not-for-logs'
        answer = 'ANSWER-SECRET-not-for-logs'
        worker, client, adapter, clock = self.setup_worker(trace_id=TRACE, log=logs.append)
        client.task['lease_token'] = token
        client.task['prompt'] = client.room['prompt'] = prompt
        def prepare(session, heartbeat, deadline):
            adapter.calls.append('prepare')
            clock.now += 1
            return SimpleNamespace(state='ready', usage={'marker': 'SNAPSHOT-SECRET'},
                                   preflight={'quota': {'marker': 'PREFLIGHT-SECRET'}})
        def execute(handle, prompt_text, deadline, *, task_kind):
            adapter.calls.append('execute')
            clock.now += 2
            return {'text': answer, 'model': 'example', 'effort': 'max', 'usage': None}
        adapter.prepare, adapter.execute = prepare, execute
        result = worker.run()
        spans = self.span_lines(logs)
        self.assertEqual([item['span'] for item in spans],
                         ['startup', 'claim', 'task_setup', 'model_call', 'close', 'complete'])
        self.assertEqual([item['outcome'] for item in spans], ['ok', 'ok', 'ok', 'ok', 'ok', 'ok'])
        self.assertTrue(all(item['error_code'] is None for item in spans))
        self._span_contract(spans, TRACE)
        self.assertEqual(spans[0]['duration_ms'], 1000)
        self.assertEqual(spans[3]['duration_ms'], 2000)
        self.assertIsNone(spans[0]['attempt_key'])
        self.assertIsNone(spans[0]['room_id'])
        self.assertIsNone(spans[0]['step'])
        key = hashlib.sha256(token.encode()).hexdigest()[:16]
        for item in spans[1:]:
            self.assertEqual(item['attempt_key'], key)
            self.assertNotEqual(item['attempt_key'], token)
            self.assertEqual(item['room_id'], ROOM)
            self.assertEqual(item['step'], 0)
        self.assertEqual(result['spans'], [{field: item[field] for field in SPAN_FIELDS} for item in spans])
        self.assertEqual(result['outcome'], 'completed')
        self.assertIn('usage_measured_at', result)
        measured = datetime.fromisoformat(result['usage_measured_at'].replace('Z', '+00:00'))
        self.assertIsNotNone(measured.tzinfo)
        self.assertLessEqual(measured.timestamp(), datetime.now(timezone.utc).timestamp())
        self.assert_hidden(logs, token, prompt, answer, 'SNAPSHOT-SECRET', 'PREFLIGHT-SECRET')

    def test_outcome_carries_usage_rejected_when_hub_drops_usage(self):
        from urllib.error import HTTPError
        from agent_hub.worker import WorkerError
        worker, client, adapter, _ = self.setup_worker()
        def execute(handle, prompt_text, deadline, *, task_kind):
            adapter.calls.append('execute')
            return {
                'text': adapter.answer, 'model': 'example', 'effort': 'max',
                'usage': {
                    'inputTokens': 123, 'outputTokens': 12,
                    'reasoningTokens': 3, 'cachedReadTokens': 20,
                },
            }
        adapter.execute = execute
        dropped = [False]
        original = client.post
        def post(path, value):
            if (path.endswith('/report') and isinstance(value.get('usage'), list)
                    and value['usage'] and not dropped[0]):
                dropped[0] = True
                client.calls.append((path, copy.deepcopy(value)))
                raise WorkerError('Hub request failed (HTTP 400)') from HTTPError(
                    'http://hub/v1/workers/report', 400, 'Bad Request', None, None)
            return original(path, value)
        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertTrue(dropped[0])
        self.assertEqual(result['usage_rejected'], worker.usage_rejected)
        self.assertGreater(result['usage_rejected'], 0)
        self.assertTrue(all('span' in item for item in result['spans']))
        self.assertFalse(any(item.get('code') == 'usage_rejected' for item in result['spans']))

    def test_usage_rejected_ignores_identity_path_http_400(self):
        from urllib.error import HTTPError
        from agent_hub.worker import WorkerError
        worker, client, _, _ = self.setup_worker()
        worker.ready = True
        worker._last_turn_usage = {
            'inputTokens': 123, 'outputTokens': 12,
            'reasoningTokens': 3, 'cachedReadTokens': 20,
        }
        worker._last_turn_observed_at = '2026-01-01T00:00:00Z'

        def make_identity_400():
            try:
                raise WorkerError(
                    'Could not obtain the configured GCE service identity') from HTTPError(
                    'http://metadata.google.internal/computeMetadata/v1/instance/'
                    'service-accounts/default/identity', 400, 'Bad Request', None, None)
            except WorkerError as exc:
                return exc

        first = make_identity_400()

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            raise first

        client.post = post
        with self.assertRaises(WorkerError) as caught:
            worker.report(force=True)
        self.assertIs(caught.exception, first)
        self.assertEqual(worker.usage_rejected, 0)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][1]['usage'])

    def test_usage_rejected_stays_zero_when_retry_fails(self):
        from urllib.error import HTTPError
        from agent_hub.worker import WorkerError
        worker, client, _, _ = self.setup_worker()
        worker.ready = True
        worker._last_turn_usage = {
            'inputTokens': 123, 'outputTokens': 12,
            'reasoningTokens': 3, 'cachedReadTokens': 20,
        }
        worker._last_turn_observed_at = '2026-01-01T00:00:00Z'

        def make_hub400():
            try:
                raise WorkerError('Hub request failed (HTTP 400)') from HTTPError(
                    'http://hub/v1/workers/report', 400, 'Bad Request', None, None)
            except WorkerError as exc:
                return exc

        pending = [make_hub400(), make_hub400()]

        def post(path, value):
            client.calls.append((path, copy.deepcopy(value)))
            raise pending.pop(0)

        client.post = post
        with self.assertRaises(WorkerError):
            worker.report(force=True)
        self.assertEqual(worker.usage_rejected, 0)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[0][1]['usage'])
        self.assertEqual(client.calls[1][1]['usage'], [])

    def test_usage_rejected_counts_rows_only_after_accepted_retry(self):
        from urllib.error import HTTPError
        from agent_hub.worker import WorkerError
        worker, client, adapter, _ = self.setup_worker()

        def execute(handle, prompt_text, deadline, *, task_kind):
            adapter.calls.append('execute')
            return {
                'text': adapter.answer, 'model': 'example', 'effort': 'max',
                'usage': {
                    'inputTokens': 123, 'outputTokens': 12,
                    'reasoningTokens': 3, 'cachedReadTokens': 20,
                },
            }

        adapter.execute = execute
        dropped = [False]
        row_count = [0]
        original = client.post

        def post(path, value):
            if (path.endswith('/report') and isinstance(value.get('usage'), list)
                    and value['usage'] and not dropped[0]):
                dropped[0] = True
                row_count[0] = len(value['usage'])
                client.calls.append((path, copy.deepcopy(value)))
                raise WorkerError('Hub request failed (HTTP 400)') from HTTPError(
                    'http://hub/v1/workers/report', 400, 'Bad Request', None, None)
            return original(path, value)

        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(worker.usage_rejected, row_count[0])
        self.assertEqual(result['usage_rejected'], row_count[0])
        self.assertGreater(row_count[0], 0)

    def test_complete_accepts_needs_reconciliation(self):
        worker, client, adapter, _ = self.setup_worker()
        original = client.post

        def post(path, value):
            if path.endswith('/complete'):
                client.calls.append((path, copy.deepcopy(value)))
                client.completions.append(copy.deepcopy(value))
                return {'room_id': ROOM, 'status': 'needs_reconciliation'}
            return original(path, value)

        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertNotIn('completion_delivery', result)
        self.assertEqual(len(client.completions), 1)

    def test_complete_accepts_retry_scheduled(self):
        worker, client, adapter, _ = self.setup_worker()
        original = client.post

        def post(path, value):
            if path.endswith('/complete'):
                client.calls.append((path, copy.deepcopy(value)))
                client.completions.append(copy.deepcopy(value))
                return {'room_id': ROOM, 'status': 'retry_scheduled'}
            return original(path, value)

        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertNotIn('completion_delivery', result)
        self.assertEqual(len(client.completions), 1)

    def test_complete_accepts_blocked_on_provider(self):
        worker, client, adapter, _ = self.setup_worker()
        original = client.post

        def post(path, value):
            if path.endswith('/complete'):
                client.calls.append((path, copy.deepcopy(value)))
                client.completions.append(copy.deepcopy(value))
                return {'room_id': ROOM, 'status': 'blocked_on_provider'}
            return original(path, value)

        client.post = post
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertNotIn('completion_delivery', result)
        self.assertEqual(len(client.completions), 1)

    def test_cleanup_failed_clears_stale_drain_code(self):
        # Drain path sets drain_code, then finally close() fails: outcome must
        # report credential_cleanup_failed without a stale drain_code.
        worker, client, adapter, _ = self.setup_worker()
        client.empty = True

        def maintain(handle):
            adapter.calls.append('maintain')
            raise CodeError('warm_session_expired')

        def close(handle):
            adapter.calls.append('close')
            raise ValueError('quarantined')

        adapter.maintain, adapter.close = maintain, close
        result = worker.run()
        self.assertEqual(result['outcome'], 'credential_cleanup_failed')
        self.assertEqual(result['error_code'], 'credential_cleanup_failed')
        self.assertNotIn('drain_code', result)
        self.assertEqual(worker.last_exit, 1)

    def test_spans_for_a_pre_model_failure(self):
        logs = []
        token = 'lease-token-not-for-logs'
        prompt = 'PROMPT-SECRET-not-for-logs'
        worker, client, adapter, _ = self.setup_worker(trace_id=TRACE, log=logs.append)
        client.task['lease_token'] = token
        client.task['prompt'] = client.room['prompt'] = prompt
        del client.room['purpose']
        result = worker.run()
        spans = self.span_lines(logs)
        self.assertEqual([item['span'] for item in spans],
                         ['startup', 'claim', 'task_setup', 'close', 'complete'])
        self.assertEqual([item['outcome'] for item in spans],
                         ['ok', 'ok', 'purpose_missing', 'ok', 'ok'])
        self.assertEqual([item['error_code'] for item in spans],
                         [None, None, 'purpose_missing', None, None])
        self._span_contract(spans, TRACE)
        self.assertNotIn('model_call', [item['span'] for item in spans])
        self.assertEqual(result['error_code'], 'purpose_missing')
        self.assertFalse(result['model_call_attempted'])
        self.assertNotIn('usage_measured_at', result)
        self.assert_hidden(logs, token, prompt)

    def test_spans_for_a_model_failure(self):
        logs = []
        token = 'lease-token-not-for-logs'
        prompt = 'PROMPT-SECRET-not-for-logs'
        worker, client, adapter, _ = self.setup_worker(trace_id=TRACE, log=logs.append)
        client.task['lease_token'] = token
        client.task['prompt'] = client.room['prompt'] = prompt
        def execute(handle, prompt_text, deadline, *, task_kind):
            adapter.calls.append('execute')
            raise CodeError('task_deadline_out_of_bounds')
        adapter.execute = execute
        result = worker.run()
        spans = self.span_lines(logs)
        self.assertEqual([item['span'] for item in spans],
                         ['startup', 'claim', 'task_setup', 'model_call', 'close', 'complete'])
        self.assertEqual([item['outcome'] for item in spans],
                         ['ok', 'ok', 'ok', 'task_deadline_out_of_bounds', 'ok', 'ok'])
        self.assertEqual(spans[3]['error_code'], 'task_deadline_out_of_bounds')
        self._span_contract(spans, TRACE)
        self.assertEqual(result['error_code'], 'task_deadline_out_of_bounds')
        self.assertTrue(result['model_call_attempted'])
        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assert_hidden(logs, token, prompt, 'provider detail must not leak')

    def test_spans_for_an_idle_drain(self):
        logs = []
        token = 'lease-token-not-for-logs'
        prompt = 'PROMPT-SECRET-not-for-logs'
        worker, client, adapter, _ = self.setup_worker(log=logs.append)
        client.empty = True
        client.task['lease_token'] = token
        client.task['prompt'] = client.room['prompt'] = prompt
        result = worker.run()
        spans = self.span_lines(logs)
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertGreater(client.claims, 0)
        self.assertEqual([item['span'] for item in spans],
                         ['startup'] + ['claim'] * client.claims + ['close'])
        self.assertEqual([item['outcome'] for item in spans],
                         ['ok'] + ['empty'] * client.claims + ['ok'])
        self.assertTrue(all(item['error_code'] is None for item in spans))
        self.assertTrue(all(item['attempt_key'] is None and item['room_id'] is None for item in spans))
        self._span_contract(spans, None)
        self.assertNotIn('model_call', [item['span'] for item in spans])
        self.assertNotIn('task_setup', [item['span'] for item in spans])
        self.assertNotIn('complete', [item['span'] for item in spans])
        self.assertNotIn('usage_measured_at', result)
        self.assertEqual(result['spans'], [{field: item[field] for field in SPAN_FIELDS} for item in spans])
        self.assert_hidden(logs, token, prompt)
        unsafe = Worker(Settings('grok', 'grok-live'), client, adapter, object(), trace_id='not/a token')
        self.assertIsNone(unsafe.trace_id)

    def test_usage_snapshot_read_never_raises_or_is_copied(self):
        logs = []
        worker, client, adapter, _ = self.setup_worker(log=logs.append)
        class Handle:
            state = 'ready'
            @property
            def usage(self):
                raise RuntimeError('usage exploded SNAPSHOT-SECRET')
            @property
            def preflight(self):
                raise RuntimeError('preflight exploded PREFLIGHT-SECRET')
        def prepare(session, heartbeat, deadline):
            adapter.calls.append('prepare')
            return Handle()
        adapter.prepare = prepare
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertNotIn('usage_measured_at', result)
        self.assert_hidden(logs, 'SNAPSHOT-SECRET', 'PREFLIGHT-SECRET', 'exploded')

    def test_capability_manifest_is_sent_exactly_when_valid(self):
        with capability_env(RUNCREW_IMAGE_DIGEST=DIGEST):
            worker, client, adapter, _ = self.setup_worker()
            arm_manifest(adapter)
            worker.report(force=True)
            manifest = client.calls[-1][1]['capability']
            self.assertEqual(manifest, {
                'runner': 'local', 'region': 'us-central1', 'cli_name': 'runcrew-live-grok',
                'cli_version': '1', 'workspace_mode': 'read_only',
                'tools_policy': 'deny_all_and_abort_on_observed_tool', 'model': 'grok-4.7',
                'effort': 'xhigh', 'auth_alias': 'grok', 'image_digest': DIGEST,
                'started_at': live_loop._PROCESS_STARTED_AT,
            })
            self.assertEqual(set(manifest), set(live_loop._CAPABILITY_FIELDS))
            adapter.CLI_VERSION = 'not a version'
            os.environ.pop('RUNCREW_IMAGE_DIGEST', None)
            worker.report(force=True)
            self.assertEqual(client.calls[-1][1]['capability'], manifest)
        with capability_env(CLOUD_RUN_EXECUTION='runcrew-worker-grok-abcde',
                            RUNCREW_REGION='europe-west1', RUNCREW_IMAGE_DIGEST=DIGEST):
            worker, client, adapter, _ = self.setup_worker()
            arm_manifest(adapter)
            worker.report(force=True)
            manifest = client.calls[-1][1]['capability']
            self.assertEqual(manifest['runner'], 'cloud_run_job')
            self.assertEqual(manifest['region'], 'europe-west1')
            self.assertEqual(set(manifest), set(live_loop._CAPABILITY_FIELDS))

    def test_capability_manifest_is_omitted_when_any_field_is_missing_or_invalid(self):
        mutations = (
            ('missing_digest', {}, {}),
            ('blank_digest', {'RUNCREW_IMAGE_DIGEST': ''}, {}),
            ('short_digest', {'RUNCREW_IMAGE_DIGEST': 'sha256:' + 'ab' * 31}, {}),
            ('upper_digest', {'RUNCREW_IMAGE_DIGEST': 'sha256:' + 'AB' * 32}, {}),
            ('bare_digest', {'RUNCREW_IMAGE_DIGEST': 'ab' * 32}, {}),
            ('extra_digest', {'RUNCREW_IMAGE_DIGEST': DIGEST + 'aa'}, {}),
            ('bad_region', {'RUNCREW_IMAGE_DIGEST': DIGEST, 'RUNCREW_REGION': 'US-central1'}, {}),
            ('underscore_region', {'RUNCREW_IMAGE_DIGEST': DIGEST, 'RUNCREW_REGION': 'us_central1'}, {}),
            ('long_region', {'RUNCREW_IMAGE_DIGEST': DIGEST, 'RUNCREW_REGION': 'a' + 'b' * 32}, {}),
            ('missing_cli_name', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'CLI_NAME': None}),
            ('digit_cli_name', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'CLI_NAME': '1grok'}),
            ('missing_version', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'CLI_VERSION': None}),
            ('spaced_version', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'CLI_VERSION': 'v 1'}),
            ('long_version', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'CLI_VERSION': 'a' * 33}),
            ('missing_tools', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'TOOLS_POLICY': None}),
            ('digit_tools', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'TOOLS_POLICY': '1deny'}),
            ('missing_model', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'MODEL': None}),
            ('spaced_model', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'MODEL': 'grok 4'}),
            ('missing_effort', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'EFFORT': None}),
            ('long_effort', {'RUNCREW_IMAGE_DIGEST': DIGEST}, {'EFFORT': 'e' * 33}),
        )
        for label, env, changes in mutations:
            with self.subTest(label=label):
                with capability_env(**env):
                    worker, client, adapter, _ = self.setup_worker()
                    arm_manifest(adapter)
                    for key, value in changes.items():
                        if value is None:
                            delattr(adapter, key)
                        else:
                            setattr(adapter, key, value)
                    worker.report(force=True)
                    payload = client.calls[-1][1]
                    self.assertNotIn('capability', payload)
                    self.assertEqual(payload['status'], 'offline')
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        valid = {
            'runner': 'local', 'region': 'us-central1', 'cli_name': 'grok', 'cli_version': '1',
            'workspace_mode': 'read_only', 'tools_policy': 'deny_all', 'model': 'grok-4.7',
            'effort': 'xhigh', 'auth_alias': 'grok', 'image_digest': DIGEST,
            'started_at': '2020-01-01T00:00:00Z',
        }
        self.assertTrue(live_loop.capability_valid(valid, now=now))
        self.assertFalse(live_loop.capability_valid(None, now=now))
        self.assertFalse(live_loop.capability_valid({'capability': None}, now=now))
        partial = dict(valid)
        del partial['effort']
        self.assertFalse(live_loop.capability_valid(partial, now=now))
        extra = dict(valid)
        extra['note'] = 'unknown'
        self.assertFalse(live_loop.capability_valid(extra, now=now))
        for field, bad in (
            ('runner', 'vm'), ('runner', None), ('workspace_mode', 'admin'),
            ('auth_alias', 'bad alias'), ('auth_alias', 'has.dot'), ('auth_alias', 'a' * 33),
            ('started_at', '2020-01-01T00:00:00'), ('started_at', '2999-01-01T00:00:00Z'),
            ('started_at', '2020-01-01T00:00:00Z' + ('x' * 21)), ('image_digest', None),
        ):
            broken = dict(valid)
            broken[field] = bad
            self.assertFalse(live_loop.capability_valid(broken, now=now), field)
        writable = dict(valid)
        writable['workspace_mode'] = 'write'
        self.assertTrue(live_loop.capability_valid(writable, now=now))
        original = live_loop._PROCESS_STARTED_AT
        live_loop._PROCESS_STARTED_AT = '2999-01-01T00:00:00Z'
        try:
            with capability_env(RUNCREW_IMAGE_DIGEST=DIGEST):
                worker, client, adapter, _ = self.setup_worker()
                arm_manifest(adapter)
                worker.report(force=True)
                self.assertNotIn('capability', client.calls[-1][1])
        finally:
            live_loop._PROCESS_STARTED_AT = original

    def test_report_never_fails_because_of_the_manifest(self):
        class RaisingAdapter(Adapter):
            @property
            def CLI_NAME(self):
                raise RuntimeError('cli name exploded /tmp/native-secret')
        with capability_env(RUNCREW_IMAGE_DIGEST=DIGEST):
            clock = Clock(); client = Client(clock); adapter = RaisingAdapter()
            adapter.CLI_VERSION = '1'
            adapter.TOOLS_POLICY = 'deny_all_and_abort_on_observed_tool'
            adapter.MODEL = 'grok-4.7'
            adapter.EFFORT = 'xhigh'
            worker = Worker(Settings('grok', 'grok-live'), client, adapter, object(),
                            clock=clock, sleep=clock.sleep)
            worker.report(force=True)
            self.assertNotIn('capability', client.calls[-1][1])
            self.assertNotIn('native-secret', json.dumps(client.calls))
            self.assertEqual(client.calls[-1][1]['status'], 'offline')
        worker, client, adapter, _ = self.setup_worker()
        arm_manifest(adapter)
        def boom(*args, **kwargs):
            raise RuntimeError('manifest builder exploded /tmp/native-secret')
        original = live_loop.capability_manifest
        live_loop.capability_manifest = boom
        try:
            with capability_env(RUNCREW_IMAGE_DIGEST=DIGEST):
                worker.report(force=True)
        finally:
            live_loop.capability_manifest = original
        self.assertNotIn('capability', client.calls[-1][1])
        self.assertNotIn('native-secret', json.dumps(client.calls))
        self.assertEqual(client.calls[-1][1]['worker_id'], 'grok-live')

    def test_entrypoint_wires_trace_id_and_span_lines(self):
        source = (Path(__file__).resolve().parent / 'entrypoint.py').read_text(encoding='utf-8')
        self.assertIn('trace_id=broker.execution_uid', source)
        self.assertIn("value.get('kind') == 'runcrew_live_span'", source)

    def test_idle_hour_reports_refresh_quota_observed_at_within_900s(self):
        # Fake idle hour: maintain refreshes quota every 600s of fake time so
        # every /v1/workers/report carries an observed_at within 900s of send.
        epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()

        def iso(fake_now):
            return datetime.fromtimestamp(epoch + fake_now, timezone.utc).isoformat().replace('+00:00', 'Z')

        def fake_from_iso(value):
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() - epoch

        clock = Clock()
        client = Client(clock)
        client.empty = True
        adapter = Adapter()
        reports = []

        def prepare(session, heartbeat, deadline):
            adapter.calls.append('prepare')
            return SimpleNamespace(
                state='ready',
                next_quota_refresh=clock.now + 600,
                preflight={
                    'quota': {
                        'observed_at': iso(clock.now),
                        'native_included_used_percent': 50,
                        'native_usage_status': 'available',
                        'period': {'start': iso(clock.now - 1000), 'end': iso(clock.now + 100000)},
                    },
                    'same_process_account_model_quota': True,
                },
            )

        def maintain(handle):
            adapter.calls.append('maintain')
            if clock.now >= handle.next_quota_refresh:
                handle.preflight['quota'] = {
                    **handle.preflight['quota'],
                    'observed_at': iso(clock.now),
                }
                handle.next_quota_refresh = clock.now + 600

        original_post = client.post

        def post(path, value):
            if path.endswith('/report'):
                reports.append((clock.now, copy.deepcopy(value)))
            return original_post(path, value)

        adapter.prepare, adapter.maintain, client.post = prepare, maintain, post
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=3600), client, adapter,
                        object(), clock=clock, sleep=clock.sleep, log=lambda record: None)
        result = worker.run()
        self.assertEqual(result['outcome'], 'idle_drained')
        self.assertGreater(len(reports), 10)
        quota_reports = 0
        for send_at, payload in reports:
            usage = payload.get('usage') or []
            for row in usage:
                if row.get('metric') != 'quota_percent':
                    continue
                quota_reports += 1
                age = abs(send_at - fake_from_iso(row['observed_at']))
                self.assertLessEqual(age, 900, f'stale at send={send_at} observed_at={row["observed_at"]}')
        self.assertGreater(quota_reports, 0)

    def test_quota_refresh_transport_lost_drains_without_a_strike(self):
        # Optional idle quota telemetry must never fail or strike a healthy agent.
        # Model on warm_session_expired: idle_drained / exit 0 / close / no error_code.
        for code in (
            'codex_quota_refresh_transport_lost',
            'grok_quota_refresh_transport_lost',
            'copilot_quota_refresh_transport_lost',
        ):
            with self.subTest(code=code):
                worker, client, adapter, clock = self.setup_worker()
                client.empty = True

                def maintain(handle, code=code):
                    adapter.calls.append('maintain')
                    raise CodeError(code)

                adapter.maintain = maintain
                result = worker.run()
                self.assertEqual(result['outcome'], 'idle_drained')
                self.assertEqual(result['drain_code'], code)
                self.assertNotIn('error_code', result)
                self.assertEqual(worker.last_exit, 0)
                self.assertEqual(adapter.calls.count('maintain'), 1)
                self.assertEqual(client.claims, 0)
                self.assertNotIn('execute', adapter.calls)
                self.assertIn('close', adapter.calls)
                self.assertEqual(live_loop.maintain_fault(CodeError(code)), 'drain')
                self.assertIn(code, live_loop._IDLE_DRAIN_CODES)


class BrokerRenewGuardTests(unittest.TestCase):
    """Fail codes for an exhausted or rejected renew. Neither is a retry or a drain."""

    def test_both_codes_fail_for_every_provider(self):
        providers = ('grok', 'codex', 'copilot', 'claude', 'cursor')
        for provider in providers:
            for suffix in ('_broker_renew_failed', '_broker_renew_rejected'):
                code = provider + suffix
                with self.subTest(code=code):
                    error = CodeError(code)
                    self.assertEqual(live_loop.maintain_fault(error), 'fail')
                    self.assertEqual(live_loop.idle_fault(error), 'fail')
                    self.assertNotIn(code, live_loop._IDLE_RETRY_CODES)
                    self.assertNotIn(code, live_loop._IDLE_DRAIN_CODES)
                    self.assertEqual(provider_errors.error_code(error), code)

    def test_maintain_failure_claims_nothing_and_closes(self):
        worker, client, adapter, _clock = LoopTests().setup_worker()
        client.empty = True

        def maintain(handle):
            adapter.calls.append('maintain')
            raise CodeError('grok_broker_renew_failed')

        adapter.maintain = maintain
        result = worker.run()
        self.assertEqual(adapter.calls.count('maintain'), 1)
        self.assertEqual(client.claims, 0)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'grok_broker_renew_failed')
        self.assertEqual(worker.last_exit, 1)
        self.assertIn('close', adapter.calls)

    def test_lease_seconds_and_entrypoint_acquire_stamp(self):
        import broker_renew
        import dynamic_broker
        default = dynamic_broker.Policy.__dataclass_fields__['lease_seconds'].default
        self.assertEqual(broker_renew.LEASE_SECONDS, default)
        source = (Path(__file__).resolve().parent / 'entrypoint.py').read_text(encoding='utf-8')
        start = source.index('acquire_started = time.monotonic()')
        record = source.index('broker_renew.record_acquire_start(lease.lease_id, acquire_started)')
        self.assertLess(start, record)
        self.assertIn('lease = acquire_lease(broker, request_id, started=acquire_started)', source[start:record])
        body = source[source.index('def acquire_lease'):source.index('class Client')]
        self.assertEqual(body.count('broker.acquire(broker.execution, request_id)'), 2)


# runcrew agent_hub.core.HEARTBEAT_PHASES (MyHero F4). That hub answers 400 to
# any other value, so the worker must never send one.
HUB_PHASES = ('setup', 'model_call', 'finishing')


class HeartbeatPhaseTests(unittest.TestCase):
    """F4: each task heartbeat says where the worker is, for the hub's lost-worker policy.

    setup: claimed, the model call has not started. model_call: the adapter is
    executing. finishing: the model returned; close and completion delivery.
    """

    def run_worker(self, *, fail_execute=False, room_change=None):
        clock = Clock(); client = Client(clock); adapter = Adapter()
        beat = {}
        def prepare(session, heartbeat, deadline):
            adapter.calls.append('prepare'); beat['fn'] = heartbeat
            return SimpleNamespace(state='ready')
        def execute(handle, prompt, deadline, *, task_kind):
            adapter.calls.append('execute')
            self.assertTrue(beat['fn']()); self.assertTrue(beat['fn']())
            if fail_execute: raise CodeError('grok_turn_failed')
            return {'text': adapter.answer, 'model': 'example', 'effort': 'max', 'usage': None}
        def close(handle):
            adapter.calls.append('close'); beat['fn']()
        adapter.prepare, adapter.execute, adapter.close = prepare, execute, close
        client.room.update(room_change or {})
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)
        result = worker.run()
        beats = [value for path, value in client.calls if path.endswith('/heartbeat')]
        for value in beats:
            self.assertEqual(set(value), {'lease_token', 'phase'})
            self.assertEqual(value['lease_token'], 'lease')
            self.assertIn(value['phase'], HUB_PHASES)
        phases = [value['phase'] for value in beats]
        # Never backwards: a later "setup" would tell the hub no model call started.
        self.assertEqual(phases, sorted(phases, key=HUB_PHASES.index))
        return result, phases, client

    def test_phase_sequence_of_a_completed_task(self):
        result, phases, client = self.run_worker()
        self.assertEqual(result['outcome'], 'completed')
        # setup (deadline) + loop model_call before execute + two in execute + finishing.
        self.assertEqual(phases, ['setup', 'model_call', 'model_call', 'model_call', 'finishing'])
        paths = [path for path, _ in client.calls]
        last_beat = max(i for i, path in enumerate(paths) if path.endswith('/heartbeat'))
        self.assertLess(last_beat, paths.index('/v1/tasks/' + ROOM + '/complete'))

    def test_setup_only_while_the_model_call_never_started(self):
        result, phases, client = self.run_worker(room_change={'purpose': 'improvement'})
        self.assertEqual(result['error_code'], 'project_work_only')
        self.assertEqual(phases, ['setup', 'setup'])
        self.assertIs(client.completions[0]['model_call_attempted'], False)

    def test_failed_model_call_stays_model_call(self):
        result, phases, client = self.run_worker(fail_execute=True)
        self.assertEqual(result['error_code'], 'grok_turn_failed')
        self.assertEqual(phases, ['setup', 'model_call', 'model_call', 'model_call', 'model_call'])
        self.assertIs(client.completions[0]['model_call_attempted'], True)

    def test_no_phase_and_no_task_heartbeat_before_a_claim(self):
        clock = Clock(); client = Client(clock); client.empty = True
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, Adapter(),
                        object(), clock=clock, sleep=clock.sleep)
        self.assertIsNone(worker.phase)
        self.assertEqual(worker.run()['outcome'], 'idle_drained')
        self.assertEqual([path for path, _ in client.calls if path.endswith('/heartbeat')], [])

    def test_loop_model_call_heartbeat_precedes_execute(self):
        """The loop acknowledges model_call before adapter.execute is entered."""
        clock = Clock(); client = Client(clock); adapter = Adapter()
        order = []
        original = client.post

        def post(path, value):
            receipt = original(path, value)
            if path.endswith('/heartbeat') and isinstance(value, dict) and value.get('phase') == 'model_call':
                if receipt.get('active') is True:
                    order.append('heartbeat(model_call) acknowledged')
            return receipt

        def execute(handle, prompt, deadline, *, task_kind):
            order.append('execute entered')
            adapter.calls.append('execute')
            return {'text': adapter.answer, 'model': 'example', 'effort': 'max', 'usage': None}

        client.post = post
        adapter.execute = execute
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)
        self.assertEqual(worker.run()['outcome'], 'completed')
        self.assertIn('heartbeat(model_call) acknowledged', order)
        self.assertIn('execute entered', order)
        self.assertLess(order.index('heartbeat(model_call) acknowledged'),
                        order.index('execute entered'))

    def test_inactive_model_call_heartbeat_skips_execute(self):
        clock = Clock(); client = Client(clock); adapter = Adapter()
        original = client.post

        def post(path, value):
            if path.endswith('/heartbeat') and isinstance(value, dict) and value.get('phase') == 'model_call':
                client.calls.append((path, copy.deepcopy(value)))
                return {'active': False, 'deadline': client.task['deadline'], 'server_time': 700}
            return original(path, value)

        client.post = post
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)
        result = worker.run()
        self.assertNotIn('execute', adapter.calls)
        self.assertEqual(result['error_code'], 'task_lease_lost')
        self.assertTrue(worker.lease_revoked)
        self.assertEqual(result['completion_delivery'], 'skipped_lease_revoked')
        self.assertEqual(client.completions, [])
        self.assertIs(result['model_call_attempted'], False)

    def test_model_call_heartbeat_transport_error_skips_execute(self):
        clock = Clock(); client = Client(clock); adapter = Adapter()
        original = client.post

        def post(path, value):
            if path.endswith('/heartbeat') and isinstance(value, dict) and value.get('phase') == 'model_call':
                client.calls.append((path, copy.deepcopy(value)))
                raise OSError('network')
            return original(path, value)

        client.post = post
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)
        result = worker.run()
        self.assertNotIn('execute', adapter.calls)
        self.assertIs(result['model_call_attempted'], False)
        self.assertNotEqual(result.get('outcome'), 'completed')
        # What the hub receives decides pre_model vs post_model (Demand review).
        self.assertEqual(len(client.completions), 1)
        self.assertIs(client.completions[0]['model_call_attempted'], False)
        self.assertEqual(client.completions[0]['error_code'], 'native_or_connection_failure')

    def test_malformed_model_call_heartbeat_answer_skips_execute(self):
        clock = Clock(); client = Client(clock); adapter = Adapter()
        original = client.post

        def post(path, value):
            if path.endswith('/heartbeat') and isinstance(value, dict) and value.get('phase') == 'model_call':
                client.calls.append((path, copy.deepcopy(value)))
                return ['not', 'an', 'object']
            return original(path, value)

        client.post = post
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)
        result = worker.run()
        self.assertNotIn('execute', adapter.calls)
        self.assertIs(result['model_call_attempted'], False)
        self.assertEqual(len(client.completions), 1)
        self.assertIs(client.completions[0]['model_call_attempted'], False)

    def test_worker_stopping_before_phase_flip_stays_setup(self):
        clock = Clock(); client = Client(clock); adapter = Adapter()
        beat = {}

        def prepare(session, heartbeat, deadline):
            adapter.calls.append('prepare'); beat['fn'] = heartbeat
            return SimpleNamespace(state='ready')

        def close(handle):
            adapter.calls.append('close')
            if 'fn' in beat:
                beat['fn']()

        adapter.prepare, adapter.close = prepare, close
        worker = Worker(Settings('grok', 'grok-live', warm_seconds=60), client, adapter,
                        object(), clock=clock, sleep=clock.sleep)

        def get_room(room):
            worker.stopping = True
            return copy.deepcopy(client.room)

        client.get_room = get_room
        result = worker.run()
        self.assertEqual(result['error_code'], 'worker_stopping')
        self.assertIs(result['model_call_attempted'], False)
        self.assertNotIn('execute', adapter.calls)
        beats = [value for path, value in client.calls if path.endswith('/heartbeat')]
        phases = [value['phase'] for value in beats]
        self.assertTrue(phases)
        self.assertTrue(all(phase == 'setup' for phase in phases))
        self.assertNotIn('model_call', phases)

    def test_older_hub_ignores_the_phase_key(self):
        """A hub from before F4 cannot answer 400 to "phase".

        runcrew R3/R3.1 (95bb81c, agent_hub/server.py) routes a heartbeat as
        hub.heartbeat(actor, room_id, data.get('lease_token')); body() accepts
        any JSON object and reads no other key. The agent-hub on this test's
        path routes it the same way; every body the worker sends is replayed
        against it over loopback HTTP.
        """
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.request import Request, urlopen
        import agent_hub.core as agent_hub_core
        from agent_hub.core import AGENTS, Hub
        from agent_hub.server import load_tokens, make_handler
        from agent_hub.store import SQLiteStore
        # Pin: this vendored hub is pre-F4. If it gains heartbeat_phase, this
        # test would silently stop proving old-hub compatibility.
        self.assertFalse(hasattr(agent_hub_core, 'heartbeat_phase'))
        _, phases, _ = self.run_worker()
        with tempfile.TemporaryDirectory() as root:
            hub = Hub(SQLiteStore(Path(root) / 'hub.sqlite3'))
            tokens = load_tokens(json.dumps({name: 'test-' + name + '-' + 'x' * 40 for name in ('manager', *AGENTS)}))
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(hub, tokens))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                hub.create('manager', {'prompt': 'Phase compatibility', 'agents': ['grok']})
                task = hub.claim('grok')['task']
                for phase in phases:
                    body = json.dumps({'lease_token': task['lease_token'], 'phase': phase}).encode()
                    request = Request('http://127.0.0.1:%d/v1/tasks/%s/heartbeat' % (server.server_port, task['room_id']),
                                      data=body, method='POST',
                                      headers={'Content-Type': 'application/json', 'X-Hub-Token': tokens['grok']})
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIs(json.loads(response.read())['active'], True)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                closer = getattr(hub.store, 'close', None)
                if callable(closer): closer()


if __name__ == '__main__': unittest.main()
