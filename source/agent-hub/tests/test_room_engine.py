"""Claim, lease, and complete behavior that used to drop a room."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub.capture import apply_capture_limit, completion_body
from agent_hub.core import LEASE_SECONDS, MAX_OUTPUT, Hub, HubError
from agent_hub.store import SQLiteStore


class Clock:
    def __init__(self):
        self.now = 1_700_000_000

    def __call__(self):
        return self.now


class RoomEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp.name) / 'rooms.sqlite3')
        self.clock = Clock()
        self.hub = Hub(self.store, self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def room(self, **extra):
        data = {'prompt': 'Review the patch.', 'agents': ['codex', 'claude'], 'timeout_seconds': 30}
        data.update(extra)
        return self.hub.create('manager', data)

    def test_late_complete_keeps_output_after_deadline(self):
        created = self.room()
        task = self.hub.claim('codex')['task']
        self.clock.now = task['deadline']
        result = self.hub.complete('codex', task['room_id'], {
            'lease_token': task['lease_token'], 'output': 'ship it', 'exit_code': 0,
        })
        self.assertEqual(result['status'], 'queued')
        saved = self.hub.get('manager', created['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertEqual(saved['next_agent'], 'claude')
        self.assertEqual(saved['messages'][0]['text'], 'ship it')
        self.assertEqual(saved['messages'][0]['exit_code'], 0)

    def test_late_complete_after_lease_expiry_still_stores_the_result(self):
        created = self.room()
        task = self.hub.claim('codex')['task']
        self.clock.now = task['lease_expires_at'] + 5
        self.hub.complete('codex', task['room_id'], {
            'lease_token': task['lease_token'], 'output': 'finished late', 'exit_code': 0,
        })
        saved = self.hub.get('manager', created['id'])
        self.assertEqual(saved['messages'][0]['text'], 'finished late')
        self.assertEqual(saved['next_agent'], 'claude')

    def test_heartbeat_still_expires_and_a_stale_room_rejects_complete(self):
        created = self.room()
        task = self.hub.claim('codex')['task']
        self.clock.now = task['lease_expires_at']
        with self.assertRaises(HubError) as raised:
            self.hub.heartbeat('codex', task['room_id'], task['lease_token'])
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.hub.get('manager', created['id'])['status'], 'stalled')
        with self.assertRaises(HubError) as rejected:
            self.hub.complete('codex', task['room_id'], {
                'lease_token': task['lease_token'], 'output': 'too late', 'exit_code': 0,
            })
        self.assertEqual(rejected.exception.status, 409)
        self.assertEqual(self.hub.get('manager', created['id'])['messages'], [])

    def test_lost_claim_returns_the_same_lease(self):
        created = self.room()
        first = self.hub.claim('codex')['task']
        again = self.hub.claim('codex')['task']
        self.assertEqual(again['lease_token'], first['lease_token'])
        self.assertEqual(again['room_id'], first['room_id'])
        saved = self.hub.get('manager', created['id'])
        self.assertEqual(saved['status'], 'running')
        self.assertEqual(saved['attempts'], 1)
        self.assertIsNone(self.hub.claim('claude')['task'])
        self.hub.complete('codex', again['room_id'], {
            'lease_token': again['lease_token'], 'output': 'recovered', 'exit_code': 0,
        })
        self.assertEqual(self.hub.get('manager', created['id'])['messages'][0]['text'], 'recovered')

    def test_exact_room_reclaim_and_expired_lease_is_not_revived(self):
        created = self.room()
        first = self.hub.claim('codex', created['id'])['task']
        again = self.hub.claim('codex', created['id'])['task']
        self.assertEqual(again['lease_token'], first['lease_token'])
        self.clock.now = first['lease_expires_at']
        self.assertIsNone(self.hub.claim('codex')['task'])
        self.assertEqual(self.hub.get('manager', created['id'])['status'], 'stalled')
        self.hub.retry('manager', created['id'])
        renewed = self.hub.claim('codex')['task']
        self.assertNotEqual(renewed['lease_token'], first['lease_token'])
        self.assertEqual(self.hub.get('manager', created['id'])['attempts'], 2)
        with self.assertRaises(HubError):
            self.hub.complete('codex', created['id'], {
                'lease_token': first['lease_token'], 'output': 'old', 'exit_code': 0,
            })
        self.hub.complete('codex', created['id'], {
            'lease_token': renewed['lease_token'], 'output': 'new', 'exit_code': 0,
        })

    def test_truncated_success_does_not_fail_the_room(self):
        created = self.room()
        task = self.hub.claim('codex')['task']
        raw = '[Earlier output truncated]\n' + ('x' * 1000)
        text = 'y' * (MAX_OUTPUT + 50)
        body = completion_body(task['lease_token'], 0, raw, text)
        self.assertEqual(body['exit_code'], 0)
        self.assertLessEqual(len(body['output'].encode('utf-8')), MAX_OUTPUT)
        self.assertIn('Capture truncation', body['output'])
        self.hub.complete('codex', task['room_id'], body)
        saved = self.hub.get('manager', created['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertEqual(saved['next_agent'], 'claude')
        self.assertEqual(saved['messages'][0]['exit_code'], 0)
        follow = self.hub.claim('claude')['task']
        self.assertEqual(follow['room_id'], created['id'])

    def test_real_nonzero_exit_still_fails_the_room(self):
        created = self.room()
        task = self.hub.claim('codex')['task']
        kept, text = apply_capture_limit(1, 'traceback', 'traceback')
        self.assertEqual(kept, 1)
        self.hub.complete('codex', task['room_id'], completion_body(task['lease_token'], 1, 'traceback', text))
        self.assertEqual(self.hub.get('manager', created['id'])['status'], 'failed')
        self.assertIsNone(self.hub.claim('claude')['task'])

    def test_duplicate_complete_is_idempotent(self):
        created = self.room(agents=['codex'])
        task = self.hub.claim('codex')['task']
        payload = {'lease_token': task['lease_token'], 'output': 'done', 'exit_code': 0}
        first = self.hub.complete('codex', task['room_id'], payload)
        second = self.hub.complete('codex', task['room_id'], payload)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(len(self.hub.get('manager', created['id'])['messages']), 1)

    def test_lease_window_constant(self):
        self.assertEqual(LEASE_SECONDS, 45)


if __name__ == '__main__':
    unittest.main()
