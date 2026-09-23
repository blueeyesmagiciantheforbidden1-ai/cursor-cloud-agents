import concurrent.futures
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_hub.core import AGENTS, MAX_ATTEMPTS, MAX_HISTORY_BYTES, Hub, HubError, history_size
from agent_hub.server import ThreadingHTTPServer, load_tokens, make_handler
from agent_hub.store import FirestoreStore, SQLiteStore


class HubFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'hub.sqlite3'
        self.now = 1000.0
        self.hub = Hub(SQLiteStore(self.path), lambda: self.now)

    def tearDown(self):
        # tempfile owns this exact path; validate containment before recursive cleanup.
        self.assertTrue(self.path.resolve().is_relative_to(Path(self.temp.name).resolve()))
        closer = getattr(self.hub.store, 'close', None)
        if callable(closer):
            closer()
        self.temp.cleanup()

    def room(self, **kwargs):
        return self.hub.create('manager', {'prompt': 'Review the sample project', **kwargs})

    def finish(self, agent, task, output='Reviewed', code=0):
        return self.hub.complete(agent, task['room_id'], dict(lease_token=task['lease_token'], output=output, exit_code=code))


class CoreTests(HubFixture, unittest.TestCase):
    def test_model_improvement_waits_for_authoritative_usage_accounting(self):
        with self.assertRaisesRegex(HubError, 'usage accounting') as caught:
            self.room(purpose='improvement')
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.hub.list('manager'), [])
        with self.assertRaises(HubError):
            self.room(purpose=['project'])
        ordinary = self.room(purpose='project')
        self.assertEqual(ordinary['purpose'], 'project')
        self.assertEqual(self.hub.claim('codex')['task']['room_id'], ordinary['id'])

    def test_all_four_agents_share_prior_results_and_stop(self):
        room = self.room(rounds=2)
        for index, agent in enumerate(['codex', 'claude', 'cursor', 'copilot'] * 2):
            task = self.hub.claim(agent)['task']
            self.assertEqual(len(task['messages']), index)
            self.assertEqual(task['room_id'], room['id'])
            self.finish(agent, task, f'{agent} result {index}')
        final = self.hub.get('manager', room['id'])
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(len(final['messages']), 8)
        self.assertIsNone(self.hub.claim('codex')['task'])
        self.assertNotIn('lease', final)

    def test_parallel_claim_has_exactly_one_winner(self):
        self.room()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.hub.claim('codex'), range(8)))
        self.assertEqual(sum(item['task'] is not None for item in claims), 1)

    def test_exact_room_claim_does_not_take_unrelated_queued_work(self):
        ordinary = self.room(agents=['codex'])
        diagnostic = self.room(agents=['codex'], workspace='release-verification')
        task = self.hub.claim('codex', diagnostic['id'])['task']
        self.assertEqual(task['room_id'], diagnostic['id'])
        self.assertEqual(self.hub.get('manager', ordinary['id'])['status'], 'queued')
        self.assertIsNone(self.hub.claim('codex', diagnostic['id'])['task'])
        with self.assertRaises(HubError):
            self.hub.claim('claude', ordinary['id'])

    def test_reserved_release_workspace_requires_exact_room_claim(self):
        diagnostic = self.room(agents=['codex', 'claude'], workspace='release_test_probe')
        self.assertIsNone(diagnostic['next_agent'])
        # This is also the unchanged legacy worker query shape.
        self.assertEqual(self.hub.store.list(agent='codex'), [])
        ordinary = self.room(agents=['codex'])
        task = self.hub.claim('codex')['task']
        self.assertEqual(task['room_id'], ordinary['id'])
        self.assertIsNone(self.hub.claim('codex')['task'])
        probe = self.hub.claim('codex', diagnostic['id'])['task']
        self.finish('codex', probe, 'SYNTHETIC RELEASE TEST')
        self.assertIsNone(self.hub.get('manager', diagnostic['id'])['next_agent'])
        self.assertEqual(self.hub.store.list(agent='claude'), [])
        self.assertIsNone(self.hub.claim('claude')['task'])
        self.assertEqual(self.hub.claim('claude', diagnostic['id'])['task']['room_id'], diagnostic['id'])

    def test_duplicate_completion_does_not_advance_twice(self):
        room = self.room()
        task = self.hub.claim('codex')['task']
        self.finish('codex', task)
        self.finish('codex', task)
        final = self.hub.get('manager', room['id'])
        self.assertEqual(final['step'], 1)
        self.assertEqual(len(final['messages']), 1)

    def test_expired_lease_stalls_and_rejects_old_worker_after_retry(self):
        room = self.room()
        old = self.hub.claim('codex')['task']
        self.now += 46
        self.assertIsNone(self.hub.claim('codex')['task'])
        self.assertEqual(self.hub.get('manager', room['id'])['status'], 'stalled')
        self.hub.retry('manager', room['id'])
        new = self.hub.claim('codex')['task']
        with self.assertRaises(HubError):
            self.finish('codex', old)
        self.finish('codex', new)

    def test_absolute_deadline_cannot_be_extended_by_heartbeat(self):
        room = self.room(timeout_seconds=30)
        task = self.hub.claim('codex')['task']
        self.assertEqual(task['deadline'], self.now + 30)
        self.assertEqual(task['server_time'], self.now)
        self.now += 20
        heartbeat = self.hub.heartbeat('codex', room['id'], task['lease_token'])
        self.assertEqual(heartbeat['deadline'], task['deadline'])
        self.assertEqual(heartbeat['server_time'], self.now)
        self.assertEqual(heartbeat['lease_expires_at'], task['deadline'])
        self.now += 11
        with self.assertRaises(HubError):
            self.hub.heartbeat('codex', room['id'], task['lease_token'])
        self.assertEqual(self.hub.get('manager', room['id'])['status'], 'stalled')

    def test_cancel_invalidates_active_execution(self):
        room = self.room()
        task = self.hub.claim('codex')['task']
        self.hub.cancel('manager', room['id'])
        with self.assertRaises(HubError):
            self.hub.heartbeat('codex', room['id'], task['lease_token'])
        with self.assertRaises(HubError):
            self.finish('codex', task)

    def test_failed_agent_does_not_send_error_as_success_to_next_agent(self):
        room = self.room()
        task = self.hub.claim('codex')['task']
        self.finish('codex', task, 'Agent not authenticated', 1)
        self.assertEqual(self.hub.get('manager', room['id'])['status'], 'failed')
        self.assertIsNone(self.hub.claim('claude')['task'])

    def test_wrong_agent_cannot_complete_or_start_jobs(self):
        room = self.room(agents=['codex'])
        task = self.hub.claim('codex')['task']
        with self.assertRaises(HubError):
            self.finish('claude', task)
        with self.assertRaises(HubError):
            self.hub.get('claude', room['id'])
        with self.assertRaises(HubError):
            self.hub.create('codex', {'prompt': 'Run more agents'})

    def test_queue_survives_server_restart(self):
        room = self.room()
        restarted = Hub(SQLiteStore(self.path), lambda: self.now)
        self.assertEqual(restarted.claim('codex')['task']['room_id'], room['id'])

    def test_unrelated_backlog_does_not_hide_oldest_queued_room(self):
        oldest = self.room(agents=['codex'])
        template = self.hub.store.get(oldest['id'])
        # Populate a realistic large backlog in one transaction rather than 1000 fsyncs.
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                for index in range(1000):
                    room = dict(template, id=f'{index:032x}', agents=['claude'],
                                next_agent='claude', created_at=self.now + index + 1)
                    connection.execute('INSERT INTO rooms VALUES (?, ?)', (room['id'], json.dumps(room)))
        finally:
            connection.close()
        self.now += 1001
        newer = self.room(agents=['codex'])
        first = self.hub.claim('codex')['task']
        self.assertEqual(first['room_id'], oldest['id'])
        running = self.hub.store.get(oldest['id'])
        self.assertIsNone(running['next_agent'])
        self.assertEqual(running['status'], 'running')
        self.assertEqual([room['id'] for room in self.hub.store.list(agent='codex')], [newer['id']])
        self.assertEqual(self.hub.claim('codex')['task']['room_id'], newer['id'])

    def test_prompt_and_output_limits_count_utf8_bytes(self):
        emoji = chr(0x1f4a1)
        room = self.room(prompt=emoji * 2000, agents=['codex'])
        with self.assertRaisesRegex(HubError, '8000 UTF-8 bytes'):
            self.room(prompt=emoji * 2001)
        task = self.hub.claim('codex')['task']
        with self.assertRaises(HubError) as oversized:
            self.finish('codex', task, emoji * 4001)
        self.assertEqual(oversized.exception.status, 413)
        self.assertEqual(self.hub.get('manager', room['id'])['status'], 'running')
        self.finish('codex', task, emoji * 4000)
        self.assertEqual(self.hub.get('manager', room['id'])['messages'][0]['text'], emoji * 4000)

    def test_invalid_unicode_is_rejected_without_losing_active_task(self):
        with self.assertRaisesRegex(HubError, 'valid Unicode'):
            self.room(prompt='invalid ' + chr(0xd800))
        self.room(agents=['codex'])
        task = self.hub.claim('codex')['task']
        with self.assertRaisesRegex(HubError, 'valid Unicode'):
            self.finish('codex', task, chr(0xd800))
        self.assertEqual(self.finish('codex', task)['status'], 'completed')

    def test_history_budget_fails_explicitly_without_truncating_or_advancing(self):
        room = self.room()
        first = self.hub.claim('codex')['task']
        # JSON escaping matters: this valid 16KB result occupies roughly 96KB on the wire.
        self.finish('codex', first, chr(0) * 16000)
        second = self.hub.claim('claude')['task']
        result = self.finish('claude', second, chr(0) * 16000)
        self.assertEqual(result['status'], 'failed')
        final = self.hub.get('manager', room['id'])
        self.assertTrue(final['history_exhausted'])
        self.assertIn('result was not stored', final['failure_reason'])
        self.assertEqual(final['step'], 1)
        self.assertEqual(len(final['messages']), 1)
        self.assertEqual(final['messages'][0]['text'], chr(0) * 16000)
        self.assertLessEqual(history_size(final['messages']), MAX_HISTORY_BYTES)
        self.assertEqual(self.finish('claude', second, chr(0) * 16000), result)
        self.assertIsNone(self.hub.claim('cursor')['task'])
        with self.assertRaisesRegex(HubError, 'History byte limit'):
            self.hub.retry('manager', room['id'])

    def test_failed_retries_have_a_total_attempt_budget(self):
        room = self.room(agents=['codex'])
        for index in range(MAX_ATTEMPTS):
            task = self.hub.claim('codex')['task']
            self.assertIsNotNone(task)
            self.finish('codex', task, f'failure {index}', 1)
            if index + 1 < MAX_ATTEMPTS:
                self.hub.retry('manager', room['id'])
        with self.assertRaises(HubError) as exhausted:
            self.hub.retry('manager', room['id'])
        self.assertEqual(exhausted.exception.status, 409)
        final = self.hub.store.get(room['id'])
        self.assertEqual(final['attempts'], MAX_ATTEMPTS)
        self.assertEqual(len(final['messages']), MAX_ATTEMPTS)
        self.assertEqual(len(final['completed_leases']), MAX_ATTEMPTS)
        self.assertIsNone(self.hub.claim('codex')['task'])

    def test_expired_attempts_count_even_without_completions(self):
        room = self.room(agents=['codex'])
        for index in range(MAX_ATTEMPTS):
            self.assertIsNotNone(self.hub.claim('codex')['task'])
            self.now += 46
            self.assertEqual(self.hub.get('manager', room['id'])['status'], 'stalled')
            if index + 1 < MAX_ATTEMPTS:
                self.hub.retry('manager', room['id'])
        with self.assertRaisesRegex(HubError, 'Attempt limit'):
            self.hub.retry('manager', room['id'])
        final = self.hub.store.get(room['id'])
        self.assertEqual(final['attempts'], MAX_ATTEMPTS)
        self.assertEqual(final['messages'], [])

    def test_sqlite_reads_and_duplicate_results_do_not_write(self):
        room = self.room()
        observer = sqlite3.connect(self.path)
        try:
            version = observer.execute('PRAGMA data_version').fetchone()[0]
            self.hub.get('manager', room['id'])
            self.hub.list('manager')
            self.assertEqual(observer.execute('PRAGMA data_version').fetchone()[0], version)
            task = self.hub.claim('codex')['task']
            self.assertNotEqual(observer.execute('PRAGMA data_version').fetchone()[0], version)
            self.finish('codex', task)
            version = observer.execute('PRAGMA data_version').fetchone()[0]
            self.finish('codex', task)
            self.assertEqual(observer.execute('PRAGMA data_version').fetchone()[0], version)
        finally:
            observer.close()

    def test_firestore_mutation_only_writes_changed_documents(self):
        original = {'messages': [{'text': 'original'}]}
        writes = []
        reference = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(
            exists=True, to_dict=lambda: copy.deepcopy(original)))
        transaction = SimpleNamespace(set=lambda ref, room: writes.append(copy.deepcopy(room)))
        store = FirestoreStore.__new__(FirestoreStore)
        store.collection = SimpleNamespace(document=lambda room_id: reference)
        store.client = SimpleNamespace(transaction=lambda: transaction)
        store.firestore = SimpleNamespace(transactional=lambda function: function)
        self.assertEqual(store.mutate('room', lambda room: room['messages'][0]['text']), 'original')
        self.assertEqual(writes, [])
        store.mutate('room', lambda room: room['messages'][0].update(text='changed'))
        self.assertEqual(writes, [{'messages': [{'text': 'changed'}]}])

    def test_rejects_paths_as_workspace_and_unbounded_rounds(self):
        for bad in ({'workspace': '../secrets'}, {'rounds': 999}, {'agents': ['unknown']}, {'agents': ['codex', 'codex']}):
            with self.assertRaises(HubError):
                self.room(**bad)


class HTTPTests(HubFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tokens = {name: name + '-' + 'x' * 40 for name in ('manager', 'codex', 'claude', 'cursor', 'copilot')}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.hub, self.tokens))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def call(self, path, data=None, actor=None, claimed=None, raw=False):
        headers = {'Content-Type': 'application/json'}
        if actor:
            headers['X-Hub-Token'] = self.tokens[actor]
        if claimed:
            headers['X-Hub-Agent'] = claimed
        body = json.dumps(data).encode() if data is not None else None
        req = Request(self.base + path, data=body, headers=headers)
        with urlopen(req, timeout=5) as response:
            payload = response.read()
            return payload if raw else json.loads(payload)

    def test_http_auth_and_identity_isolation(self):
        self.assertEqual(self.call('/healthz')['status'], 'ok')
        with self.assertRaises(HTTPError) as denied:
            self.call('/v1/rooms')
        self.assertEqual(denied.exception.code, 401)
        with self.assertRaises(HTTPError) as mismatch:
            self.call('/v1/tasks/claim', {}, actor='codex', claimed='claude')
        self.assertEqual(mismatch.exception.code, 403)

    def test_http_live_roundtrip(self):
        room = self.call('/v1/rooms', {'prompt': 'Test collaboration'}, actor='manager')
        for agent in ('codex', 'claude', 'cursor', 'copilot'):
            task = self.call('/v1/tasks/claim', {}, actor=agent)['task']
            self.call(f"/v1/tasks/{room['id']}/heartbeat", {'lease_token': task['lease_token']}, actor=agent)
            self.call(f"/v1/tasks/{room['id']}/complete", dict(lease_token=task['lease_token'], exit_code=0, output=agent + ' reviewed'), actor=agent)
        result = self.call('/v1/rooms/' + room['id'], actor='manager')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([item['agent'] for item in result['messages']], ['codex', 'claude', 'cursor', 'copilot'])

    def test_http_exact_room_claim_is_explicit_and_authenticated(self):
        ordinary = self.room(agents=['codex'])
        diagnostic = self.room(agents=['codex'])
        task = self.call('/v1/rooms/' + diagnostic['id'] + '/claim', {}, actor='codex')['task']
        self.assertEqual(task['room_id'], diagnostic['id'])
        self.assertEqual(self.hub.get('manager', ordinary['id'])['status'], 'queued')
        with self.assertRaises(HTTPError):
            self.call('/v1/rooms/' + diagnostic['id'] + '/claim', {}, actor='claude')

    def test_no_lease_token_in_room_response(self):
        room = self.room()
        task = self.hub.claim('codex')['task']
        response = self.call('/v1/rooms/' + room['id'], actor='claude')
        self.assertNotIn(task['lease_token'], json.dumps(response))

    def test_full_unicode_history_claim_fits_worker_response_limit(self):
        room = self.room(prompt=chr(0) * 8000, rounds=3)
        output = chr(0x1f4a1) * 4000
        for index in range(10):
            agent = room['agents'][index % len(room['agents'])]
            self.finish(agent, self.hub.claim(agent)['task'], output)
        payload = self.call('/v1/tasks/claim', {}, actor='cursor', raw=True)
        self.assertLess(len(payload), 256 * 1024)
        self.assertIn(chr(0x1f4a1).encode('utf-8'), payload)
        task = json.loads(payload)['task']
        self.assertEqual(len(task['messages']), 10)
        self.assertEqual(task['prompt'], chr(0) * 8000)
        self.assertEqual(self.finish('cursor', task, output)['status'], 'failed')
        final = self.hub.store.get(room['id'])
        self.assertLessEqual(history_size(final['messages']), MAX_HISTORY_BYTES)
        self.assertLess(len(json.dumps(final, ensure_ascii=False).encode('utf-8')), 1024 * 1024)

    def test_http_rejects_non_ascii_token_cleanly(self):
        request = Request(self.base + '/v1/rooms', headers={'X-Hub-Token': chr(0xe9) * 40})
        with self.assertRaises(HTTPError) as denied:
            urlopen(request, timeout=5)
        self.assertEqual(denied.exception.code, 401)

    def test_token_configuration_rejects_non_ascii_or_unbounded_values(self):
        self.assertEqual(load_tokens(json.dumps(self.tokens)), self.tokens)
        for bad in (chr(0xe9) * 40, 'x' * 257, 'x' * 31 + ' '):
            with self.assertRaises(ValueError):
                load_tokens(json.dumps({**self.tokens, 'manager': bad}))

    def mcp(self, actor, method, params=None, rpc_id=1):
        return self.call('/mcp', {'jsonrpc': '2.0', 'id': rpc_id, 'method': method, 'params': params or {}}, actor=actor)

    def test_mcp_lets_all_four_agents_talk(self):
        listed = self.mcp('cursor', 'tools/list')
        names = [tool['name'] for tool in listed['result']['tools']]
        self.assertIn('hub_claim', names)
        started = self.mcp('manager', 'tools/call', {
            'name': 'hub_start',
            'arguments': {'prompt': 'Each agent posts a one-line check-in'},
        })
        room = started['result']['structuredContent']
        speakers = []
        for agent in ('codex', 'claude', 'cursor', 'copilot'):
            claimed = self.mcp(agent, 'tools/call', {'name': 'hub_claim', 'arguments': {}})
            task = claimed['result']['structuredContent']['task']
            finished = self.mcp(agent, 'tools/call', {
                'name': 'hub_complete',
                'arguments': {
                    'room_id': task['room_id'],
                    'lease_token': task['lease_token'],
                    'output': agent + ' checking in through the shared hub',
                    'exit_code': 0,
                },
            })
            speakers.append(agent)
            self.assertEqual(finished['result']['structuredContent']['room_id'], room['id'])
        final = self.mcp('manager', 'tools/call', {'name': 'hub_get', 'arguments': {'room_id': room['id']}})
        room = final['result']['structuredContent']
        self.assertEqual(room['status'], 'completed')
        self.assertEqual([item['agent'] for item in room['messages']], speakers)


if __name__ == '__main__':
    unittest.main()
