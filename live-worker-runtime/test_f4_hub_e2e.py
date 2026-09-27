"""End-to-end: live_loop.Worker against the integrated F4 hub over loopback HTTP.

Skipped only when F4_HUB_PATH is unset or empty. When it is set, a missing or
pre-F4 hub, or agent_hub already loaded from elsewhere, is an import error.
The configured F4 hub is put first on sys.path before importing live_loop and
stays first for the rest of the process. Leave F4_HUB_PATH unset for a full
discover run; run these contract tests in their own process with it set.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

F4_HUB_PATH = os.environ.get('F4_HUB_PATH')
_SKIP_REASON = None
Hub = HEARTBEAT_PHASES = LEASE_SECONDS = None
SQLiteStore = ThreadingHTTPServer = make_handler = None

if not F4_HUB_PATH:
    _SKIP_REASON = 'F4_HUB_PATH is unset; set it to the F4 agent-hub source tree'
else:
    hub_root = Path(F4_HUB_PATH).resolve()
    if not (hub_root / 'agent_hub').is_dir():
        raise ImportError('F4_HUB_PATH does not contain agent_hub/: %r' % (F4_HUB_PATH,))
    sys.path.insert(0, str(hub_root))

import live_loop
from live_loop import Settings, Worker

if F4_HUB_PATH:
    import agent_hub.core as _core

    # A prior test module may have cached a different hub before this import.
    _package_root = Path(os.path.normcase(str((hub_root / 'agent_hub').resolve())))
    for _module_name in ('agent_hub', 'agent_hub.core', 'agent_hub.worker'):
        _loaded_file = getattr(sys.modules.get(_module_name), '__file__', None)
        _loaded_path = (
            Path(os.path.normcase(str(Path(_loaded_file).resolve())))
            if _loaded_file else None
        )
        if _loaded_path is None or not _loaded_path.is_relative_to(_package_root):
            raise ImportError(
                'agent_hub was not loaded from F4_HUB_PATH %s: %s.__file__=%r\n'
                'Run python -m unittest test_f4_hub_e2e in its own process, '
                'or unset F4_HUB_PATH so the module skips.'
                % (hub_root, _module_name, _loaded_file)
            )

    if not hasattr(_core, 'HEARTBEAT_PHASES'):
        raise ImportError(
            'agent_hub at F4_HUB_PATH is not F4 (missing HEARTBEAT_PHASES); '
            'refusing to run against the vendored pre-F4 hub'
        )
    from agent_hub.core import HEARTBEAT_PHASES, Hub, LEASE_SECONDS
    from agent_hub.server import ThreadingHTTPServer, make_handler
    from agent_hub.store import SQLiteStore
    from agent_hub.worker import Config, HubClient


class LoopbackClient:
    """Minimal HubClient stand-in: POST/GET over loopback, no Google identity."""

    def __init__(self, base, token, agent):
        self.base = base.rstrip('/')
        self.token = token
        self.agent = agent
        self.opener = build_opener(ProxyHandler({}))

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'X-Hub-Token': self.token,
            'X-Hub-Agent': self.agent,
        }

    def post(self, path, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        request = Request(self.base + path, data=body, headers=self._headers(), method='POST')
        try:
            with self.opener.open(request, timeout=10) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            raise OSError('Hub request failed (HTTP %s): %s' % (exc.code, detail)) from exc
        except URLError as exc:
            raise OSError('Hub transport error') from exc

    def get_room(self, room_id):
        request = Request(
            self.base + '/v1/rooms/' + room_id,
            headers={'X-Hub-Token': self.token, 'X-Hub-Agent': self.agent},
            method='GET',
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            raise OSError('Hub request failed (HTTP %s): %s' % (exc.code, detail)) from exc


class DieAdapter:
    """Fake provider. die_at='setup'|'execute' leaves no completion (close fails).

    Real adapters beat phase='finishing' inside execute (via
    broker_renew.finishing_beat) before their own close; the loop's close() is
    then a no-op on a finished handle. This fake does the same on the success
    path so the hub records last_phase=finishing.
    """

    def __init__(self, die_at=None):
        self.die_at = die_at
        self.calls = []
        self.answer = 'A useful e2e answer.'
        self._heartbeat = None

    def prepare(self, session, heartbeat, deadline):
        self.calls.append('prepare')
        self._heartbeat = heartbeat
        return SimpleNamespace(state='ready')

    def maintain(self, handle):
        self.calls.append('maintain')

    def execute(self, handle, prompt, deadline, *, task_kind):
        self.calls.append('execute')
        if self.die_at == 'execute':
            raise RuntimeError('simulated worker death inside execute')
        import broker_renew
        broker_renew.finishing_beat(self._heartbeat)
        return {'text': self.answer, 'model': 'example', 'effort': 'max', 'usage': None}

    def close(self, handle):
        self.calls.append('close')
        if self.die_at in ('setup', 'execute'):
            raise RuntimeError('simulated worker death during credential close')


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON or 'skipped')
class F4HubE2ETests(unittest.TestCase):
    """Real Worker + F4 hub HTTP. Controllable hub clock; no model, no WAN."""

    def setUp(self):
        self.assertIn('setup', HEARTBEAT_PHASES)
        self.temp = tempfile.TemporaryDirectory(prefix='f4-hub-e2e-')
        self.root = Path(self.temp.name).resolve()
        self.now = 1_800_000_000.0
        self.hub = Hub(SQLiteStore(self.root / 'hub.sqlite3'), lambda: self.now)
        self.tokens = {
            name: name + '-' + ('x' * 40)
            for name in ('manager', 'codex', 'claude', 'cursor', 'copilot', 'grok')
        }
        self.server = ThreadingHTTPServer(
            ('127.0.0.1', 0), make_handler(self.hub, self.tokens),
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port
        self.agent = 'grok'
        self.client = LoopbackClient(self.base, self.tokens[self.agent], self.agent)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        closer = getattr(self.hub.store, 'close', None)
        if callable(closer):
            closer()
        self.assertEqual(Path(self.temp.name).resolve(), self.root)
        self.temp.cleanup()

    def create_room(self, **kwargs):
        data = {
            'prompt': 'E2E lost-worker collaboration',
            'agents': [self.agent],
            'timeout_seconds': 120,
            'purpose': 'project',
        }
        data.update(kwargs)
        return self.hub.create('manager', data)

    def raw(self, room_id):
        return self.hub.store.get(room_id)

    def seen(self, room_id):
        return self.hub.get('manager', room_id)

    def expire(self, room_id):
        self.now += LEASE_SECONDS + 1
        return self.seen(room_id)

    def run_worker(self, *, die_at=None, client=None, adapter=None):
        client = client or self.client
        adapter = adapter or DieAdapter(die_at=die_at)
        if die_at == 'setup':
            original_get = client.get_room

            def get_room(room_id):
                # Claim + setup heartbeat already happened inside checked_deadline.
                raise RuntimeError('simulated worker death during setup')

            client.get_room = get_room
            # Keep a reference so tear-down/debug can still see the original.
            adapter._original_get = original_get
        worker = Worker(
            Settings(self.agent, 'grok-e2e', warm_seconds=60),
            client, adapter, object(),
        )
        return worker.run(), adapter

    def test_1_dies_during_setup_auto_read_only(self):
        """EXPECTED (auto, read_only, death before model_call beat):
        After lease expiry: status=retry_scheduled, recovery_audit rule=setup_phase,
        audit last_phase=setup, attempt_records[-1].last_phase=setup.
        """
        room = self.create_room(recovery='auto')
        result, adapter = self.run_worker(die_at='setup')
        self.assertNotIn('execute', adapter.calls)
        self.assertNotEqual(result.get('outcome'), 'completed')
        self.assertFalse(result.get('model_call_attempted'))
        raw = self.raw(room['id'])
        self.assertEqual(raw['status'], 'running')
        self.assertEqual(raw['lease'].get('last_phase'), 'setup')
        seen = self.expire(room['id'])
        self.assertEqual(seen['status'], 'retry_scheduled')
        audit = seen['recovery_audit'][-1]
        self.assertEqual(audit['rule'], 'setup_phase')
        self.assertEqual(audit['last_phase'], 'setup')
        self.assertEqual(audit['decision'], 'retry')
        record = seen['attempt_records'][-1]
        self.assertEqual(record.get('last_phase'), 'setup')

    def test_2_dies_during_execute_auto_read_only_then_limit(self):
        """EXPECTED (auto, read_only, death after acknowledged model_call, in execute):
        First loss: retry_scheduled, rule=read_only_once, last_phase=model_call.
        Second loss on the same step: needs_reconciliation, rule=auto_retry_limit.
        """
        room = self.create_room(recovery='auto')
        result, adapter = self.run_worker(die_at='execute')
        self.assertIn('execute', adapter.calls)
        self.assertTrue(result.get('model_call_attempted'))
        raw = self.raw(room['id'])
        self.assertEqual(raw['lease'].get('last_phase'), 'model_call')
        seen = self.expire(room['id'])
        self.assertEqual(seen['status'], 'retry_scheduled')
        audit = seen['recovery_audit'][-1]
        self.assertEqual((audit['rule'], audit['last_phase'], audit['decision']),
                         ('read_only_once', 'model_call', 'retry'))
        self.assertEqual(seen['attempt_records'][-1].get('last_phase'), 'model_call')

        # Fresh client/adapter for the automatic retry of the same step.
        self.client = LoopbackClient(self.base, self.tokens[self.agent], self.agent)
        result2, adapter2 = self.run_worker(die_at='execute')
        self.assertIn('execute', adapter2.calls)
        seen2 = self.expire(room['id'])
        self.assertEqual(seen2['status'], 'needs_reconciliation')
        audit2 = seen2['recovery_audit'][-1]
        self.assertEqual(audit2['rule'], 'auto_retry_limit')
        self.assertEqual(audit2['decision'], 'reconcile')
        self.assertEqual(audit2['last_phase'], 'model_call')

    def test_3_dies_during_execute_write_manual(self):
        """EXPECTED (write room, manual recovery):
        After lease expiry: needs_reconciliation; never auto-retried (no retry_scheduled,
        recovery_audit empty because recovery is manual).
        """
        room = self.create_room(workspace_mode='write', recovery='manual')
        result, adapter = self.run_worker(die_at='execute')
        self.assertIn('execute', adapter.calls)
        self.assertTrue(result.get('model_call_attempted'))
        seen = self.expire(room['id'])
        self.assertEqual(seen['status'], 'needs_reconciliation')
        self.assertEqual(seen.get('failure_reason'), 'lease')
        self.assertEqual(seen.get('recovery_audit'), [])
        self.assertNotEqual(seen['status'], 'retry_scheduled')
        self.assertEqual(seen['attempt_records'][-1].get('last_phase'), 'model_call')

    def test_3b_dies_during_execute_write_auto_reconciles(self):
        """EXPECTED (write room, recovery auto: the hub allows auto up to risk 1):
        a loss after the acknowledged model_call beat may have left side effects,
        so the policy reconciles (rule write_side_effects) and never retries.
        """
        room = self.create_room(workspace_mode='write', recovery='auto')
        result, adapter = self.run_worker(die_at='execute')
        self.assertIn('execute', adapter.calls)
        self.assertTrue(result.get('model_call_attempted'))
        seen = self.expire(room['id'])
        self.assertEqual(seen['status'], 'needs_reconciliation')
        audit = seen['recovery_audit'][-1]
        self.assertEqual((audit['rule'], audit['decision'], audit['last_phase']),
                         ('write_side_effects', 'reconcile', 'model_call'))

    def test_4_model_call_beat_lands_transport_error_skips_execute(self):
        """EXPECTED (both confirming model_call beats reach the hub; the worker sees a
        transport error each time, so its one retry is used up): execute never runs; completion has model_call_attempted False; hub classifies
        the attempt pre_model and requeues (retry_scheduled); not post_model /
        needs_reconciliation.
        """
        room = self.create_room(recovery='auto')
        client = LoopbackClient(self.base, self.tokens[self.agent], self.agent)
        original = client.post
        model_call_beats = {'n': 0}

        def post(path, value):
            if (path.endswith('/heartbeat') and isinstance(value, dict)
                    and value.get('phase') == 'model_call'):
                model_call_beats['n'] += 1
                if model_call_beats['n'] <= 2:
                    # Land the beat at the hub, then fail the worker's transport
                    # (twice: the worker retries the beat once).
                    original(path, value)
                    raise OSError('simulated transport loss after hub accepted model_call')
            return original(path, value)

        client.post = post
        adapter = DieAdapter()
        result, _ = self.run_worker(client=client, adapter=adapter)
        self.assertNotIn('execute', adapter.calls)
        self.assertIs(result.get('model_call_attempted'), False)
        seen = self.seen(room['id'])
        self.assertEqual(seen['status'], 'retry_scheduled')
        self.assertNotEqual(seen['status'], 'needs_reconciliation')
        self.assertNotEqual(seen.get('failure_reason'), 'post_model')
        record = seen['attempt_records'][-1]
        self.assertEqual(record.get('failure_class'), 'pre_model')
        self.assertIs(record.get('model_call_attempted'), False)
        # The beat did land before the transport error.
        raw_after_beat_path = self.raw(room['id'])
        # After pre_model requeue the lease is gone; phase was recorded on the attempt.
        self.assertEqual(record.get('last_phase'), 'model_call')
        self.assertIsNone(raw_after_beat_path.get('lease'))

    def test_4b_lost_ack_then_retry_succeeds_runs_execute_once(self):
        """EXPECTED (the first model_call beat lands but its ack is lost; the one
        retry is acknowledged): execute runs exactly once, the room completes,
        and the attempt succeeded with last_phase finishing (never back to setup).
        """
        room = self.create_room(recovery='auto')
        client = LoopbackClient(self.base, self.tokens[self.agent], self.agent)
        original = client.post
        beats = {'n': 0}

        def post(path, value):
            if (path.endswith('/heartbeat') and isinstance(value, dict)
                    and value.get('phase') == 'model_call'):
                beats['n'] += 1
                if beats['n'] == 1:
                    original(path, value)
                    raise OSError('simulated lost ack after hub accepted model_call')
            return original(path, value)

        client.post = post
        result, adapter = self.run_worker(client=client, die_at=None)
        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(result.get('outcome'), 'completed')
        seen = self.seen(room['id'])
        self.assertEqual(seen['status'], 'completed')
        record = seen['attempt_records'][-1]
        self.assertEqual(record.get('outcome'), 'succeeded')
        self.assertEqual(record.get('last_phase'), 'finishing')

    def test_6_cancel_mid_call_stops_within_one_tick(self):
        import provider_errors

        TICK_SECONDS = 20
        case = self
        room = self.create_room()
        beats = []
        cancelled_at = []
        stopped_at = []

        class RecordingClient(HubClient):
            def __init__(self):
                super().__init__(Config(case.base, case.agent, case.tokens[case.agent],
                                        'F4_E2E_TOKEN', {}))
                self.opener = build_opener(ProxyHandler({}))
                self.paths = []
                self.reader = LoopbackClient(case.base, case.tokens[case.agent], case.agent)

            def get_room(self, room_id):
                return self.reader.get_room(room_id)

            def post(self, path, value):
                self.paths.append(path)
                return super().post(path, value)

        class TickError(provider_errors.ProviderCodeError, RuntimeError):
            pass

        class TickAdapter(DieAdapter):
            def execute(self, handle, prompt, deadline, *, task_kind):
                self.calls.append('execute')
                for tick in range(10):
                    case.now += TICK_SECONDS
                    active = self._heartbeat()
                    beats.append(active)
                    if active is not True:
                        stopped_at.append(case.now)
                        raise TickError('grok_hub_heartbeat_lost')
                    if tick == 1:
                        LoopbackClient(case.base, case.tokens['manager'], 'manager').post(
                            '/v1/rooms/' + room['id'] + '/cancel', {})
                        cancelled_at.append(case.now)
                raise AssertionError('cancel did not stop the call')

        client = RecordingClient()
        result, adapter = self.run_worker(client=client, adapter=TickAdapter())
        self.assertEqual(beats, [True, True, False])
        self.assertEqual(len(cancelled_at), 1)
        self.assertEqual(len(stopped_at), 1)
        self.assertLessEqual(stopped_at[0] - cancelled_at[0], TICK_SECONDS)
        self.assertEqual(result['error_code'], 'grok_hub_heartbeat_lost')
        self.assertEqual(result['completion_delivery'], 'skipped_lease_revoked')
        self.assertIs(result['model_call_attempted'], True)
        self.assertFalse(any(path.endswith('/complete') for path in client.paths))
        self.assertEqual(adapter.calls.count('close'), 1)
        seen = self.seen(room['id'])
        self.assertEqual(seen['status'], 'cancelled')
        self.assertEqual(seen['messages'], [])
        raw = self.raw(room['id'])
        self.assertIsNone(raw['lease'])
        self.assertFalse(raw.get('rejected_outputs'))
        self.assertFalse(raw.get('completion_rejections'))

    def recording_client(self, *, without_reason=False):
        from agent_hub.worker import LeaseLost
        case = self

        class RecordingClient(HubClient):
            def __init__(self):
                super().__init__(Config(case.base, case.agent, case.tokens[case.agent],
                                        'F4_E2E_TOKEN', {}))
                self.opener = build_opener(ProxyHandler({}))
                self.paths = []
                self.lease_reasons = []
                self.reader = LoopbackClient(case.base, case.tokens[case.agent], case.agent)

            def get_room(self, room_id):
                return self.reader.get_room(room_id)

            def post(self, path, value):
                self.paths.append((path, dict(value)))
                try:
                    return super().post(path, value)
                except LeaseLost as error:
                    self.lease_reasons.append(getattr(error, 'reason', None))
                    raise

        client = RecordingClient()
        if without_reason:
            opener = client.opener

            class OldHubOpener:
                def open(self, request, **kwargs):
                    try:
                        return opener.open(request, **kwargs)
                    except HTTPError as error:
                        if error.code != 409:
                            raise
                        # Simulate the old hub's wire response. The real new
                        # HubClient must parse this absent reason as None.
                        with error:
                            body = json.loads(error.read().decode('utf-8'))
                        body.pop('reason', None)
                        raise HTTPError(error.url, error.code, error.msg, error.headers,
                                        io.BytesIO(json.dumps(body).encode('utf-8'))) from error

            client.opener = OldHubOpener()
        return client

    def run_expired_mid_call(self, room, *, client=None):
        import provider_errors
        case = self
        beats = []

        class TickError(provider_errors.ProviderCodeError, RuntimeError):
            pass

        class ExpiryAdapter(DieAdapter):
            def execute(self, handle, prompt, deadline, *, task_kind):
                self.calls.append('execute')
                case.now += 20
                active = self._heartbeat()
                beats.append(active)
                if active is not True:
                    raise AssertionError('first heartbeat did not renew the lease')
                # Renewal expires at t0+65; this tick is t0+66, before t0+120.
                case.now += LEASE_SECONDS + 1
                active = self._heartbeat()
                beats.append(active)
                if active is not True:
                    raise TickError('grok_hub_heartbeat_lost')
                raise AssertionError('lease expiry did not stop the call')

        client = client or self.recording_client()
        result, adapter = self.run_worker(client=client, adapter=ExpiryAdapter())
        self.assertEqual(beats, [True, False])
        self.assertEqual(result['error_code'], 'grok_hub_heartbeat_lost')
        self.assertIs(result['model_call_attempted'], True)
        self.assertEqual(adapter.calls.count('execute'), 1)
        self.assertEqual(adapter.calls.count('close'), 1)
        return result, client

    def test_7_expired_lease_mid_call_completes_one_failure(self):
        room = self.create_room(recovery='manual')
        result, client = self.run_expired_mid_call(room)
        self.assertNotIn('completion_delivery', result)
        self.assertTrue(client.lease_reasons)
        self.assertEqual(set(client.lease_reasons), {'lease_expired_completable'})
        completions = [value for path, value in client.paths if path.endswith('/complete')]
        self.assertEqual(len(completions), 1)
        completion = completions[0]
        self.assertEqual(completion['exit_code'], 1)
        self.assertEqual(completion['error_code'], 'grok_hub_heartbeat_lost')
        self.assertIs(completion['model_call_attempted'], True)
        raw = self.raw(room['id'])
        self.assertEqual(raw['status'], 'needs_reconciliation')
        self.assertEqual(raw['failure_reason'], 'post_model')
        self.assertIsNone(raw['lease'])
        heartbeat_bodies = [value for path, value in client.paths
                            if path.endswith('/heartbeat')]
        self.assertTrue(heartbeat_bodies)
        token = heartbeat_bodies[0]['lease_token']
        self.assertTrue(all(value['lease_token'] == token for value in heartbeat_bodies))
        self.assertEqual(completion['lease_token'], token)
        output = ('The cloud worker stopped before it could deliver a verified answer '
                  '(grok_hub_heartbeat_lost). It did not automatically repeat the model request.')
        self.assertEqual(completion['output'], output)
        record = raw['attempt_records'][-1]
        self.assertEqual(record['outcome'], 'failed')
        self.assertEqual(record['failure_class'], 'post_model')
        self.assertEqual(record['last_phase'], 'model_call')
        self.assertEqual(record['exit_code'], 1)
        self.assertEqual(record['error_code'], 'grok_hub_heartbeat_lost')
        self.assertIs(record['model_call_attempted'], True)
        self.assertIn('expired_first', record)
        self.assertEqual(len(raw['rejected_outputs']), 1)
        self.assertEqual(raw['rejected_outputs'][0]['exit_code'], 1)
        self.assertEqual(raw['rejected_outputs'][0]['step'], 0)
        self.assertEqual(raw['rejected_outputs'][0]['full_text'], output)
        self.assertEqual(raw['messages'], [])

        manager = LoopbackClient(self.base, self.tokens['manager'], 'manager')
        with self.assertRaises(OSError) as refused:
            manager.post('/v1/rooms/' + room['id'] + '/retry', {})
        self.assertEqual(refused.exception.__cause__.code, 409)
        self.assertIn('post_model output cannot be retried unless force is true', str(refused.exception))
        manager.post('/v1/rooms/' + room['id'] + '/retry', {'force': True})
        self.assertTrue(self.raw(room['id'])['retry_audit'][-1]['force'])

    def test_7b_old_hub_409_without_reason_keeps_the_skip(self):
        room = self.create_room(recovery='manual')
        client = self.recording_client(without_reason=True)
        result, client = self.run_expired_mid_call(room, client=client)
        self.assertEqual(result['completion_delivery'], 'skipped_lease_revoked')
        self.assertTrue(client.lease_reasons)
        self.assertEqual(set(client.lease_reasons), {None})
        self.assertFalse(any(path.endswith('/complete') for path, _ in client.paths))
        raw = self.raw(room['id'])
        self.assertEqual(raw['status'], 'needs_reconciliation')
        self.assertEqual(raw['failure_reason'], 'lease')
        self.assertIsNotNone(raw['lease'])
        record = raw['attempt_records'][-1]
        self.assertEqual(record['outcome'], 'lease_expired')
        self.assertEqual(record['failure_class'], 'lease_expired')
        self.assertEqual(record['last_phase'], 'model_call')
        for key in ('exit_code', 'error_code', 'model_call_attempted'):
            self.assertNotIn(key, record)
        self.assertFalse(raw.get('rejected_outputs'))

    def test_7c_expired_pre_execute_lease_completes_one_pre_model_failure(self):
        room = self.create_room(recovery='manual')
        client = self.recording_client()
        original = client.post
        expired = False

        def post(path, value):
            nonlocal expired
            if not expired and path.endswith('/heartbeat') and value.get('phase') == 'model_call':
                expired = True
                self.now += LEASE_SECONDS + 1
            return original(path, value)

        client.post = post
        result, adapter = self.run_worker(client=client)
        self.assertNotIn('execute', adapter.calls)
        self.assertEqual(adapter.calls.count('close'), 1)
        self.assertEqual(result['error_code'], 'task_lease_lost')
        self.assertIs(result['model_call_attempted'], False)
        self.assertNotIn('completion_delivery', result)
        self.assertTrue(client.lease_reasons)
        self.assertEqual(set(client.lease_reasons), {'lease_expired_completable'})
        completions = [value for path, value in client.paths if path.endswith('/complete')]
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]['exit_code'], 1)
        self.assertEqual(completions[0]['error_code'], 'task_lease_lost')
        self.assertIs(completions[0]['model_call_attempted'], False)
        raw = self.raw(room['id'])
        self.assertEqual(raw['status'], 'retry_scheduled')
        self.assertIsNone(raw['lease'])
        self.assertEqual(len(raw['attempt_records']), 1)
        record = raw['attempt_records'][-1]
        self.assertEqual(record['failure_class'], 'pre_model')
        self.assertEqual(record['last_phase'], 'setup')
        self.assertEqual(record['exit_code'], 1)
        self.assertEqual(record['error_code'], 'task_lease_lost')
        self.assertIs(record['model_call_attempted'], False)
        self.assertIn('expired_first', record)

    def test_7d_completion_409_after_completable_heartbeat_is_final(self):
        room = self.create_room(recovery='manual')
        client = self.recording_client()
        original = client.post

        def post(path, value):
            if path.endswith('/complete'):
                self.hub.cancel('manager', room['id'])
            return original(path, value)

        client.post = post
        result, client = self.run_expired_mid_call(room, client=client)
        self.assertEqual(result['completion_delivery'], 'refused')
        self.assertIn('lease_expired_completable', client.lease_reasons)
        self.assertEqual(client.lease_reasons[-1], 'lease_inactive')
        completions = [value for path, value in client.paths if path.endswith('/complete')]
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]['exit_code'], 1)
        self.assertEqual(completions[0]['error_code'], 'grok_hub_heartbeat_lost')
        self.assertIs(completions[0]['model_call_attempted'], True)
        raw = self.raw(room['id'])
        self.assertEqual(raw['status'], 'cancelled')
        self.assertIsNone(raw['lease'])
        self.assertFalse(raw.get('completed_leases'))
        self.assertFalse(raw.get('rejected_outputs'))
        self.assertEqual(raw['messages'], [])

    def test_7_lost_complete_reply_next_agent_claimed_counts_as_delivered(self):
        room = self.create_room(agents=[self.agent, 'cursor'], recovery='auto')
        client = LoopbackClient(self.base, self.tokens[self.agent], self.agent)
        original = client.post
        completions = []

        def post(path, value):
            if path.endswith('/complete'):
                completions.append(path)
                if len(completions) == 1:
                    original(path, value)
                    task = self.hub.claim('cursor', room['id'])
                    self.assertIsNotNone(task['task'])
                    raise OSError('simulated lost reply after the hub stored the completion')
            return original(path, value)

        client.post = post
        worker = Worker(
            Settings(self.agent, 'grok-e2e', warm_seconds=60),
            client, DieAdapter(), object(), sleep=lambda seconds: None,
        )
        result = worker.run()
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(worker.last_exit, 0)
        self.assertEqual(len(completions), 2)
        self.assertNotIn('completion_delivery', result)
        raw = self.raw(room['id'])
        self.assertEqual(raw['status'], 'running')
        self.assertEqual(len(raw['messages']), 1)
        self.assertEqual(raw['messages'][0]['agent'], 'grok')
        self.assertEqual(raw['messages'][0]['text'], 'A useful e2e answer.')
        self.assertEqual(len(raw['completed_leases']), 1)
        self.assertEqual(raw['completed_leases'][0]['agent'], 'grok')
        self.assertEqual(raw['lease']['agent'], 'cursor')

    def test_5_happy_path_finishing(self):
        """EXPECTED (normal completion):
        Room advances (completed for a one-agent one-round room); the attempt
        record's last_phase is finishing.
        """
        room = self.create_room(recovery='auto')
        result, adapter = self.run_worker(die_at=None)
        self.assertEqual(result.get('outcome'), 'completed')
        self.assertIn('execute', adapter.calls)
        seen = self.seen(room['id'])
        self.assertEqual(seen['status'], 'completed')
        record = seen['attempt_records'][-1]
        self.assertEqual(record.get('last_phase'), 'finishing')
        self.assertEqual(record.get('outcome'), 'succeeded')

    def test_6_realistic_adapter_finishing_beats_against_hub(self):
        """Adapter + loop both post finishing; slow broker close still completes."""
        import broker_renew
        case = self
        room = self.create_room(recovery='auto')
        finishing = []

        class RecordingClient(LoopbackClient):
            def post(self, path, value):
                if path.endswith('/heartbeat') and isinstance(value, dict):
                    if value.get('phase') == 'finishing':
                        finishing.append('adapter' if 'close' not in adapter.calls else 'loop')
                return super().post(path, value)

        class RealisticAdapter(DieAdapter):
            def execute(self, handle, prompt, deadline, *, task_kind):
                self.calls.append('execute')
                case.now += 20
                broker_renew.finishing_beat(self._heartbeat)
                case.now += 36
                return {'text': self.answer, 'model': 'example', 'effort': 'max', 'usage': None}

            def close(self, handle):
                self.calls.append('close')

        adapter = RealisticAdapter()
        client = RecordingClient(self.base, self.tokens[self.agent], self.agent)

        def sleep(seconds):
            case.now += seconds

        worker = Worker(
            Settings(self.agent, 'grok-e2e', warm_seconds=60),
            client, adapter, object(),
            clock=lambda: case.now, sleep=sleep,
        )
        result = worker.run()
        self.assertEqual(result.get('outcome'), 'completed')
        seen = self.seen(room['id'])
        self.assertEqual(seen['status'], 'completed')
        record = seen['attempt_records'][-1]
        self.assertEqual(record.get('last_phase'), 'finishing')
        self.assertEqual(record.get('outcome'), 'succeeded')
        self.assertEqual(seen.get('recovery_audit'), [])
        self.assertIn('adapter', finishing)
        self.assertIn('loop', finishing)

    def test_7_loss_after_answer_records_finishing(self):
        """A loss after finishing_beat is visible as last_phase=finishing."""
        import broker_renew
        case = self
        room = self.create_room(recovery='auto')

        class LostAfterAnswer(DieAdapter):
            def execute(self, handle, prompt, deadline, *, task_kind):
                self.calls.append('execute')
                broker_renew.finishing_beat(self._heartbeat)
                raise RuntimeError('simulated worker death after answer')

            def close(self, handle):
                self.calls.append('close')
                raise RuntimeError('simulated worker death during credential close')

        worker = Worker(
            Settings(self.agent, 'grok-e2e', warm_seconds=60),
            self.client, LostAfterAnswer(), object(),
            clock=lambda: case.now, sleep=lambda seconds: setattr(case, 'now', case.now + seconds),
        )
        result = worker.run()
        self.assertNotEqual(result.get('outcome'), 'completed')
        seen = self.expire(room['id'])
        audit = seen['recovery_audit'][-1]
        self.assertEqual(audit['last_phase'], 'finishing')


if __name__ == '__main__':
    unittest.main()
