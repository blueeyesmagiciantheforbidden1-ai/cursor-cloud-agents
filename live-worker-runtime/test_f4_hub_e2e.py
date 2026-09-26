"""End-to-end: live_loop.Worker against the integrated F4 hub over loopback HTTP.

Skipped unless F4_HUB_PATH points at a directory that contains the F4 agent_hub
package (with HEARTBEAT_PHASES). That path is put first on sys.path before any
agent_hub import so the vendored pre-F4 hub on PYTHONPATH cannot win.
"""
from __future__ import annotations

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

import live_loop
from live_loop import Settings, Worker

F4_HUB_PATH = os.environ.get('F4_HUB_PATH')
_SKIP_REASON = None
Hub = HEARTBEAT_PHASES = LEASE_SECONDS = None
SQLiteStore = ThreadingHTTPServer = make_handler = None

if not F4_HUB_PATH:
    _SKIP_REASON = 'F4_HUB_PATH is unset; set it to the F4 agent-hub source tree'
elif not (Path(F4_HUB_PATH) / 'agent_hub').is_dir():
    _SKIP_REASON = 'F4_HUB_PATH does not contain agent_hub/: %r' % (F4_HUB_PATH,)
else:
    # F4 hub must win over any vendored agent_hub already on PYTHONPATH.
    sys.path.insert(0, str(Path(F4_HUB_PATH).resolve()))
    import agent_hub.core as _core
    if not hasattr(_core, 'HEARTBEAT_PHASES'):
        _SKIP_REASON = (
            'agent_hub at F4_HUB_PATH is not F4 (missing HEARTBEAT_PHASES); '
            'refusing to run against the vendored pre-F4 hub'
        )
    else:
        from agent_hub.core import HEARTBEAT_PHASES, Hub, LEASE_SECONDS
        from agent_hub.server import ThreadingHTTPServer, make_handler
        from agent_hub.store import SQLiteStore


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

    Real adapters call the prepare() heartbeat during close once phase is
    finishing (see HeartbeatPhaseTests); this fake does the same so the hub
    records last_phase=finishing on a successful run.
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
        return {'text': self.answer, 'model': 'example', 'effort': 'max', 'usage': None}

    def close(self, handle):
        self.calls.append('close')
        if self.die_at in ('setup', 'execute'):
            raise RuntimeError('simulated worker death during credential close')
        if self._heartbeat is not None:
            self._heartbeat()


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
        """EXPECTED (write room, manual recovery; auto not used at write risk here):
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

    def test_4_model_call_beat_lands_transport_error_skips_execute(self):
        """EXPECTED (confirming model_call beat reaches hub; worker sees transport error):
        execute never runs; completion has model_call_attempted False; hub classifies
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
                if model_call_beats['n'] == 1:
                    # Land the beat at the hub, then fail the worker's transport.
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


if __name__ == '__main__':
    unittest.main()
