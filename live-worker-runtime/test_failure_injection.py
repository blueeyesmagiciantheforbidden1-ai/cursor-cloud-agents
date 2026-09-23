"""Audit acceptance: interrupt / uncertain-delivery failure injection for live_loop.

Alpha criterion: interrupt a worker before execution, during execution, and after
producing a result but before acknowledgment. Verify one accepted result per
logical step and no duplicated external action.

Standard library only. Fake hub + adapter; no network. Does not modify live_loop.
"""
from __future__ import annotations

import copy
import hashlib
import io
import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

from live_loop import Worker, Settings, LiveError
import provider_errors
from provider_errors import ProviderCodeError


ROOM = 'b' * 32
# Distinctive secrets: must never appear in logs, stdout, or stderr.
LEASE = 'inj-lease-tok-aabbccddee1122'
PROMPT = 'UNIQUE_PROMPT_TEXT_FOR_INJECTION_TEST'
ANSWER = 'UNIQUE_ANSWER_TEXT_FOR_INJECTION_TEST'


class CodeError(ProviderCodeError, RuntimeError):
    """Stands in for a provider adapter's fixed-code error class."""


class Clock:
    def __init__(self):
        self.now = 100

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeHub:
    """In-memory hub. Dedupes /complete by lease_token hash (one accept per lease)."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.claims = 0
        self.completions = []  # every POST attempt (including lost acks)
        self.accepted_completions = []  # deduped accepts only
        self._accepted_lease_hashes = set()
        self.fail_claim = False
        self.claim_raises_after_lease = False
        self.fail_completions = 0
        self.active = True
        self.empty = False
        self.duplicate_claim_returns_same_task = False
        self._leased = False
        self.task = dict(
            room_id=ROOM, lease_token=LEASE, workspace='default',
            prompt=PROMPT, messages=[], timeout_seconds=300,
            deadline=1000, step=0, learning_context={},
        )
        self.room = dict(
            id=ROOM, workspace='default', prompt=PROMPT,
            messages=[], status='running', step=0, purpose='project',
        )

    def post(self, path, value):
        self.calls.append((path, copy.deepcopy(value)))
        if path.endswith('/claim'):
            self.claims += 1
            if self.claim_raises_after_lease:
                # Hub leased the room; the HTTP reply is lost.
                self._leased = True
                raise OSError('claim reply lost after lease')
            if self.fail_claim:
                raise OSError('network')
            if self.empty and not self.duplicate_claim_returns_same_task:
                return {'task': None}
            if self._leased and not self.duplicate_claim_returns_same_task:
                # Already leased once in this test unless redelivery is requested.
                if self.room.get('status') != 'running':
                    return {'task': None}
            self._leased = True
            return {'task': copy.deepcopy(self.task)}
        if path.endswith('/heartbeat'):
            return {
                'active': self.active,
                'deadline': self.task['deadline'],
                'server_time': 700,
            }
        if path.endswith('/complete'):
            self.completions.append(copy.deepcopy(value))
            if self.fail_completions:
                self.fail_completions -= 1
                raise OSError('lost acknowledgement')
            lease_hash = hashlib.sha256(value.get('lease_token', '').encode()).hexdigest()
            if lease_hash not in self._accepted_lease_hashes:
                self._accepted_lease_hashes.add(lease_hash)
                self.accepted_completions.append(copy.deepcopy(value))
                self.room['status'] = 'completed' if value.get('exit_code') == 0 else 'failed'
            return {'room_id': ROOM, 'status': self.room['status']}
        return {'accepted': True}

    def get_room(self, room):
        return copy.deepcopy(self.room)


class FakeAdapter:
    def __init__(self, worker_ref=None, heartbeat_ref=None):
        self.calls = []
        self.answer = ANSWER
        self.fail_execute = False
        self.execute_hook = None  # optional callable(handle, prompt, deadline, heartbeat)
        self.close_hook = None
        self._heartbeat = None
        self._worker_ref = worker_ref

    def prepare(self, session, heartbeat, deadline):
        self.calls.append('prepare')
        self._heartbeat = heartbeat
        return SimpleNamespace(state='ready')

    def maintain(self, handle):
        self.calls.append('maintain')

    def execute(self, handle, prompt, deadline, *, task_kind):
        self.calls.append('execute')
        if self.execute_hook:
            return self.execute_hook(handle, prompt, deadline, self._heartbeat)
        if self.fail_execute:
            raise ValueError('provider detail must not leak')
        return {'text': self.answer, 'model': 'example', 'effort': 'max', 'usage': None}

    def close(self, handle):
        self.calls.append('close')
        if self.close_hook:
            self.close_hook(handle)


class FailureInjectionTests(unittest.TestCase):
    """Cases a–g: audit acceptance for live_loop interrupt and delivery paths."""

    def setUp(self):
        self.log_records = []
        self._logging_records = []

    def _log(self, record):
        self.log_records.append(copy.deepcopy(record))

    def setup_worker(self, *, warm_seconds=60):
        clock = Clock()
        hub = FakeHub(clock)
        adapter = FakeAdapter()
        worker = Worker(
            Settings('grok', 'grok-live', warm_seconds=warm_seconds),
            hub, adapter, object(),
            clock=clock, sleep=clock.sleep, log=self._log,
        )
        adapter._worker_ref = worker
        return worker, hub, adapter, clock

    def run_captured(self, worker):
        """Run worker while capturing stdout, stderr, and logging module output."""
        handler = logging.Handler()
        handler.setLevel(logging.DEBUG)
        captured = self._logging_records

        class ListHandler(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        list_handler = ListHandler()
        list_handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(list_handler)
        root.setLevel(logging.DEBUG)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                result = worker.run()
        finally:
            root.removeHandler(list_handler)
            root.setLevel(old_level)
        return result, out.getvalue(), err.getvalue()

    def assert_no_secret_leak(self, *blobs):
        combined = '\n'.join(str(b) for b in blobs)
        for secret in (LEASE, PROMPT, ANSWER):
            self.assertNotIn(secret, combined)

    def assert_common(self, worker, hub, adapter, result, *,
                      execute=None, completions=None, accepted=None,
                      closes=None, last_exit=None, outcome=None, **outcome_fields):
        if execute is not None:
            self.assertEqual(adapter.calls.count('execute'), execute)
        if completions is not None:
            self.assertEqual(len(hub.completions), completions)
        if accepted is not None:
            self.assertEqual(len(hub.accepted_completions), accepted)
        if closes is not None:
            self.assertEqual(adapter.calls.count('close'), closes)
        if last_exit is not None:
            self.assertEqual(worker.last_exit, last_exit)
        if outcome is not None:
            self.assertEqual(result.get('outcome'), outcome)
        for key, value in outcome_fields.items():
            self.assertEqual(result.get(key), value, msg=key)

    # --- (a) Claim reply lost after hub leased ---------------------------------

    def test_a_claim_reply_lost_after_lease(self):
        """Case a: POST raises after hub leased -> claim_response_uncertain."""
        worker, hub, adapter, _ = self.setup_worker()
        hub.claim_raises_after_lease = True
        result, out, err = self.run_captured(worker)

        self.assert_common(
            worker, hub, adapter, result,
            execute=0, completions=0, accepted=0, closes=1, last_exit=1,
            outcome='failed', error_code='claim_response_uncertain',
            model_call_attempted=False, claim_attempted=True,
        )
        self.assertEqual(hub.claims, 1)
        self.assertTrue(hub._leased)
        # No second claim.
        self.assertEqual(hub.claims, 1)
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (b) SIGTERM / worker.stopping at three points -------------------------

    def test_b_stop_before_claim(self):
        """Case b: stopping before claim -> no model call; close always runs."""
        worker, hub, adapter, _ = self.setup_worker()

        def maintain(handle):
            adapter.calls.append('maintain')
            worker.stopping = True

        adapter.maintain = maintain
        result, out, err = self.run_captured(worker)

        self.assert_common(
            worker, hub, adapter, result,
            execute=0, completions=0, accepted=0, closes=1, last_exit=0,
            outcome='idle_drained', model_call_attempted=False,
        )
        self.assertEqual(hub.claims, 0)
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    def test_b_stop_between_claim_and_execute(self):
        """Case b: stopping after claim, before execute.

        Audit: a claimed room gets exactly one completion with structured
        error_code and model_call_attempted; at most one model call; close runs.
        """
        worker, hub, adapter, _ = self.setup_worker()
        original_get = hub.get_room

        def get_room(room):
            # Claim has returned; prepare is in progress. SIGTERM arrives here.
            worker.stopping = True
            return original_get(room)

        hub.get_room = get_room
        result, out, err = self.run_captured(worker)

        # BUG: live_loop.py:331-334 never rechecks self.stopping after assigning
        # self.task / during _prepare_task, so SIGTERM between claim and execute
        # still runs the model call and may complete successfully (exit 0) instead
        # of emitting one structured failure completion for the claimed room.
        # BUG: live_loop.py:314 only guards the pre-claim break; heartbeat()
        # (live_loop.py:171-172) returns False when stopping but checked_deadline
        # posts the task heartbeat directly and ignores stopping.
        self.assertLessEqual(adapter.calls.count('execute'), 1)
        self.assertGreaterEqual(adapter.calls.count('close'), 1)
        if hub._leased:
            # Desired: exactly one failure completion with structured facts.
            # Keep asserting the audit criterion so a fix turns this green.
            self.assertEqual(
                len(hub.accepted_completions), 1,
                # BUG: claimed room may complete successfully or not at all
                # instead of one structured interrupt completion.
            )
            if hub.completions:
                completion = hub.completions[0]
                self.assertIn('error_code', completion)
                self.assertIn('model_call_attempted', completion)
                self.assertEqual(completion.get('exit_code'), 1)
        self.assert_no_secret_leak(out, err, self.log_records, self._logging_records)
        # Intentionally still assert audit outcome (may fail until live_loop checks stopping).
        self.assertNotEqual(
            result.get('outcome'), 'completed',
            # BUG: live_loop.py:331-343 proceeds to execute+complete after stop
        )

    def test_b_stop_during_execute(self):
        """Case b: stopping mid-execute via heartbeat seeing worker.stopping."""
        worker, hub, adapter, _ = self.setup_worker()

        def execute_hook(handle, prompt, deadline, heartbeat):
            worker.stopping = True
            # Cooperative adapter: poll the loop heartbeat (entrypoint SIGTERM).
            if heartbeat is not None and heartbeat() is False:
                raise LiveError('task_lease_lost')
            return {'text': ANSWER, 'model': 'example', 'effort': 'max', 'usage': None}

        adapter.execute_hook = execute_hook
        result, out, err = self.run_captured(worker)

        self.assertLessEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(adapter.calls.count('close'), 1)
        # Claimed room: exactly one completion with structured error facts.
        self.assertEqual(len(hub.completions), 1)
        self.assertEqual(len(hub.accepted_completions), 1)
        completion = hub.completions[0]
        self.assertEqual(completion['error_code'], 'task_lease_lost')
        self.assertIs(completion['model_call_attempted'], True)
        self.assertEqual(completion['exit_code'], 1)
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['error_code'], 'task_lease_lost')
        self.assertTrue(result['model_call_attempted'])
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (c) Heartbeat inactive mid-execute (lease revoked) --------------------

    def test_c_lease_revoked_mid_execute_no_completion(self):
        """Case c: heartbeat inactive mid-execute -> no completion, no further model call.

        Audit: do not complete a lease the hub revoked.
        """
        worker, hub, adapter, _ = self.setup_worker()

        def execute_hook(handle, prompt, deadline, heartbeat):
            hub.active = False
            if heartbeat is not None and heartbeat() is False:
                # Adapter surfaces the revoked lease; loop must not complete it.
                raise LiveError('task_lease_lost')
            raise AssertionError('heartbeat should have reported inactive')

        adapter.execute_hook = execute_hook
        result, out, err = self.run_captured(worker)

        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(result['error_code'], 'task_lease_lost')
        self.assertTrue(result['model_call_attempted'])
        # Desired audit criterion: no completion for a revoked lease.
        # BUG: live_loop.py:389-400 still calls complete() after cleanup when
        # self.task is set and cleaned, including task_lease_lost — so a
        # revoked lease can receive a failure completion.
        self.assertEqual(
            len(hub.completions), 0,
            # BUG: live_loop.py:389-400 sends completion after lease revoke
        )
        self.assertEqual(len(hub.accepted_completions), 0)
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (d) Completion ack lost (retry identical / then uncertain) ------------

    def test_d_completion_ack_lost_once_then_ok_deduped(self):
        """Case d: one lost ack then OK -> identical payload retried; hub accepts one."""
        worker, hub, adapter, _ = self.setup_worker()
        hub.fail_completions = 1
        result, out, err = self.run_captured(worker)

        self.assert_common(
            worker, hub, adapter, result,
            execute=1, completions=2, accepted=1, closes=1, last_exit=0,
            outcome='completed', model_call_attempted=True,
        )
        self.assertTrue(all(p == hub.completions[0] for p in hub.completions))
        self.assertEqual(hub.completions[0]['lease_token'], LEASE)
        self.assertEqual(hub.completions[0]['exit_code'], 0)
        self.assertEqual(hub.completions[0]['output'], ANSWER)
        # Payload frozen on the worker.
        self.assertEqual(worker.completion_payload, hub.completions[0])
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    def test_d_completion_ack_lost_three_times_uncertain(self):
        """Case d: three lost acks -> completion_delivery_uncertain; payload unchanged."""
        worker, hub, adapter, _ = self.setup_worker()
        hub.fail_completions = 3
        result, out, err = self.run_captured(worker)

        self.assert_common(
            worker, hub, adapter, result,
            execute=1, completions=3, accepted=0, closes=1, last_exit=1,
            error_code='completion_delivery_uncertain',
            model_call_attempted=True,
        )
        self.assertEqual(result.get('completion_delivery'), 'unconfirmed')
        self.assertTrue(all(p == hub.completions[0] and p['exit_code'] == 0 for p in hub.completions))
        self.assertEqual(worker.completion_payload, hub.completions[0])
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (e) Duplicate task delivery -------------------------------------------

    def test_e_duplicate_delivery_second_refused_without_model_call(self):
        """Case e: same task delivered twice -> one execute; second refused, no model call."""
        worker1, hub, adapter1, clock = self.setup_worker()
        result1, out1, err1 = self.run_captured(worker1)
        self.assert_common(
            worker1, hub, adapter1, result1,
            execute=1, completions=1, accepted=1, closes=1, last_exit=0,
            outcome='completed',
        )

        # Hub redelivers the same lease even though the room is no longer running.
        hub.duplicate_claim_returns_same_task = True
        hub._leased = False
        self.assertEqual(hub.room['status'], 'completed')

        adapter2 = FakeAdapter()
        logs2 = []
        worker2 = Worker(
            Settings('grok', 'grok-live', warm_seconds=60),
            hub, adapter2, object(),
            clock=clock, sleep=clock.sleep, log=lambda r: logs2.append(copy.deepcopy(r)),
        )
        result2, out2, err2 = self.run_captured(worker2)

        self.assertEqual(adapter1.calls.count('execute'), 1)
        self.assertEqual(adapter2.calls.count('execute'), 0)
        self.assertEqual(len(hub.accepted_completions), 1)
        self.assertFalse(result2.get('model_call_attempted'))
        self.assertGreaterEqual(adapter2.calls.count('close'), 1)
        self.assertEqual(result2.get('error_code'), 'room_step_changed')
        self.assertEqual(worker2.last_exit, 1)
        self.assert_no_secret_leak(result1, result2, out1, err1, out2, err2,
                                   self.log_records, logs2, self._logging_records)

    # --- (f) close() raises after successful execute ---------------------------

    def test_f_close_raises_after_execute_no_completion(self):
        """Case f: adapter close() raises after success -> credential_cleanup_failed, no completion."""
        worker, hub, adapter, _ = self.setup_worker()
        close_attempts = {'n': 0}

        def close_hook(handle):
            close_attempts['n'] += 1
            raise ValueError('quarantined')

        adapter.close_hook = close_hook
        result, out, err = self.run_captured(worker)

        self.assert_common(
            worker, hub, adapter, result,
            execute=1, completions=0, accepted=0, last_exit=1,
            outcome='credential_cleanup_failed', error_code='credential_cleanup_failed',
            model_call_attempted=True,
        )
        self.assertGreaterEqual(adapter.calls.count('close'), 1)
        self.assertEqual(hub.completions, [])
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (g) quota_exhausted mid-execute ---------------------------------------

    def test_g_quota_exhausted_mid_execute(self):
        """Case g: ProviderCodeError claude_quota_exhausted -> exit 75, one completion."""
        worker, hub, adapter, _ = self.setup_worker()

        def execute_hook(handle, prompt, deadline, heartbeat):
            raise CodeError('claude_quota_exhausted')

        adapter.execute_hook = execute_hook
        result, out, err = self.run_captured(worker)

        self.assert_common(
            worker, hub, adapter, result,
            execute=1, completions=1, accepted=1, closes=1,
            last_exit=provider_errors.QUOTA_EXIT_CODE,
            outcome='failed', error_code='claude_quota_exhausted',
            model_call_attempted=True,
        )
        self.assertIs(result.get('provider_quota_exhausted'), True)
        completion = hub.completions[0]
        self.assertEqual(completion['exit_code'], 1)
        self.assertEqual(completion['error_code'], 'claude_quota_exhausted')
        self.assertIs(completion['model_call_attempted'], True)
        self.assertNotIn(ANSWER, completion['output'])
        self.assertNotIn(PROMPT, completion['output'])
        self.assertNotIn(LEASE, completion['output'])
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)


class RealSigtermInjection(unittest.TestCase):
    """Cases h1–h6: production SIGTERM via Worker.on_signal (entrypoint.py:109-114).

    Entrypoint stop handler: `if worker.on_signal(): raise KeyboardInterrupt`.
    on_signal always sets stopping; returns True (interrupt) at most once and
    never while worker.critical is non-zero. run() catches KeyboardInterrupt
    as worker_stopping and takes the normal close-then-complete path; a stop
    before any claim is idle_drained, exit 0.
    """

    def setUp(self):
        self.log_records = []
        self._logging_records = []

    def _log(self, record):
        self.log_records.append(copy.deepcopy(record))

    def setup_worker(self, *, warm_seconds=60):
        clock = Clock()
        hub = FakeHub(clock)
        adapter = FakeAdapter()
        worker = Worker(
            Settings('grok', 'grok-live', warm_seconds=warm_seconds),
            hub, adapter, object(),
            clock=clock, sleep=clock.sleep, log=self._log,
        )
        adapter._worker_ref = worker
        return worker, hub, adapter, clock

    def run_captured(self, worker):
        """Run worker while capturing stdout, stderr, and logging module output."""
        captured = self._logging_records

        class ListHandler(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        list_handler = ListHandler()
        list_handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(list_handler)
        root.setLevel(logging.DEBUG)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                result = worker.run()
        finally:
            root.removeHandler(list_handler)
            root.setLevel(old_level)
        return result, out.getvalue(), err.getvalue()

    def assert_no_secret_leak(self, *blobs):
        combined = '\n'.join(str(b) for b in blobs)
        for secret in (LEASE, PROMPT, ANSWER):
            self.assertNotIn(secret, combined)

    def _sigterm(self, worker):
        """Match entrypoint.py:109-114 exactly."""
        if worker.on_signal():
            raise KeyboardInterrupt

    # --- (h1) SIGTERM during maintain, before claim ----------------------------

    def test_h1_sigterm_during_maintain_before_claim(self):
        """h1: SIGTERM in maintain() before claim -> idle_drained, exit 0."""
        worker, hub, adapter, _ = self.setup_worker()

        def maintain(handle):
            adapter.calls.append('maintain')
            self._sigterm(worker)

        adapter.maintain = maintain
        result, out, err = self.run_captured(worker)

        self.assertEqual(result.get('outcome'), 'idle_drained')
        self.assertEqual(worker.last_exit, 0)
        self.assertEqual(hub.claims, 0)
        self.assertEqual(adapter.calls.count('execute'), 0)
        self.assertEqual(len(hub.completions), 0)
        self.assertEqual(len(hub.accepted_completions), 0)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertTrue(worker.cleaned)
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (h2) SIGTERM inside get_room after claim ------------------------------

    def test_h2_sigterm_inside_get_room_after_claim(self):
        """h2: SIGTERM in get_room during _prepare_task after claim.

        Exactly one completion: worker_stopping, model_call_attempted False,
        exit_code 1; execute 0; close once.
        """
        worker, hub, adapter, _ = self.setup_worker()

        def get_room(room):
            # Claim returned; prepare is in progress. Production SIGTERM lands here.
            self._sigterm(worker)
            return copy.deepcopy(hub.room)

        hub.get_room = get_room
        result, out, err = self.run_captured(worker)

        self.assertEqual(hub.claims, 1)
        self.assertTrue(hub._leased)
        self.assertEqual(adapter.calls.count('execute'), 0)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(len(hub.completions), 1)
        self.assertEqual(len(hub.accepted_completions), 1)
        completion = hub.completions[0]
        self.assertEqual(completion['error_code'], 'worker_stopping')
        self.assertIs(completion['model_call_attempted'], False)
        self.assertEqual(completion['exit_code'], 1)
        self.assertEqual(result.get('error_code'), 'worker_stopping')
        self.assertIs(result.get('model_call_attempted'), False)
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (h3) SIGTERM inside adapter.execute -----------------------------------

    def test_h3_sigterm_inside_execute(self):
        """h3: SIGTERM mid-execute -> one worker_stopping completion, model attempted."""
        worker, hub, adapter, _ = self.setup_worker()

        def execute_hook(handle, prompt, deadline, heartbeat):
            self._sigterm(worker)

        adapter.execute_hook = execute_hook
        result, out, err = self.run_captured(worker)

        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertTrue(worker.model_call_attempted)
        self.assertEqual(worker.last_exit, 1)
        self.assertEqual(len(hub.completions), 1)
        self.assertEqual(len(hub.accepted_completions), 1)
        completion = hub.completions[0]
        self.assertEqual(completion['error_code'], 'worker_stopping')
        self.assertIs(completion['model_call_attempted'], True)
        self.assertEqual(completion['exit_code'], 1)
        self.assertEqual(result.get('error_code'), 'worker_stopping')
        self.assertTrue(result.get('model_call_attempted'))
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (h4) SIGTERM inside adapter.close after success -----------------------

    def test_h4_sigterm_inside_close_after_success(self):
        """h4: SIGTERM inside close() after success -> on_signal False (critical).

        Close finishes under _critical; cleaned True; exactly one completion
        (successful answer — stop honoured after critical sections).
        """
        worker, hub, adapter, _ = self.setup_worker()
        close_interrupted = []

        def close_hook(handle):
            # Production: SIGTERM during close; critical section refuses interrupt.
            try:
                self._sigterm(worker)
                close_interrupted.append(False)
            except KeyboardInterrupt:
                close_interrupted.append(True)
                raise

        adapter.close_hook = close_hook
        result, out, err = self.run_captured(worker)

        self.assertEqual(close_interrupted, [False])
        self.assertTrue(worker.stopping)
        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertTrue(worker.cleaned)
        self.assertEqual(len(hub.completions), 1)
        self.assertEqual(len(hub.accepted_completions), 1)
        completion = hub.completions[0]
        # Stop was deferred; success path still posts the answer.
        self.assertEqual(completion.get('exit_code'), 0)
        self.assertEqual(completion.get('output'), ANSWER)
        self.assertNotIn('error_code', completion)
        self.assertEqual(result.get('outcome'), 'completed')
        self.assertEqual(worker.last_exit, 0)
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (h5) SIGTERM inside the completion POST -------------------------------

    def test_h5_sigterm_inside_completion_post(self):
        """h5: SIGTERM inside /complete -> on_signal False; completion once."""
        worker, hub, adapter, _ = self.setup_worker()
        complete_interrupted = []
        original_post = hub.post

        def post(path, value):
            if path.endswith('/complete'):
                try:
                    self._sigterm(worker)
                    complete_interrupted.append(False)
                except KeyboardInterrupt:
                    complete_interrupted.append(True)
                    raise
            return original_post(path, value)

        hub.post = post
        result, out, err = self.run_captured(worker)

        self.assertEqual(complete_interrupted, [False])
        self.assertTrue(worker.stopping)
        self.assertEqual(len(hub.completions), 1)
        self.assertEqual(len(hub.accepted_completions), 1)
        self.assertEqual(hub.completions[0].get('exit_code'), 0)
        self.assertEqual(result.get('outcome'), 'completed')
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertTrue(worker.cleaned)
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)

    # --- (h6) two SIGTERMs: execute then close ---------------------------------

    def test_h6_two_sigterms_execute_then_close(self):
        """h6: SIGTERM in execute then again in close -> one interrupt, one completion."""
        worker, hub, adapter, _ = self.setup_worker()
        signal_results = []

        def execute_hook(handle, prompt, deadline, heartbeat):
            try:
                self._sigterm(worker)
                signal_results.append(('execute', False))
            except KeyboardInterrupt:
                signal_results.append(('execute', True))
                raise

        def close_hook(handle):
            try:
                self._sigterm(worker)
                signal_results.append(('close', False))
            except KeyboardInterrupt:
                signal_results.append(('close', True))
                raise

        adapter.execute_hook = execute_hook
        adapter.close_hook = close_hook
        result, out, err = self.run_captured(worker)

        self.assertEqual(signal_results, [('execute', True), ('close', False)])
        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertTrue(worker.cleaned)
        self.assertEqual(len(hub.completions), 1)
        self.assertEqual(len(hub.accepted_completions), 1)
        completion = hub.completions[0]
        self.assertEqual(completion['error_code'], 'worker_stopping')
        self.assertIs(completion['model_call_attempted'], True)
        self.assertEqual(completion['exit_code'], 1)
        self.assertEqual(result.get('error_code'), 'worker_stopping')
        self.assert_no_secret_leak(result, out, err, self.log_records, self._logging_records)


if __name__ == '__main__':
    unittest.main()
