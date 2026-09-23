import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_hub.collector import LocalCollector, bounded_text, read_json
from agent_hub.dashboard import Monitor, StatusError, public_snapshot


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'hub'
        (self.root / 'collaboration').mkdir(parents=True)
        (self.root / 'runtime').mkdir()
        self.now = 2_000_000_000
        self.collector = LocalCollector(self.root, home=self.root / 'home', clock=lambda: self.now, interval=0)

    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_missing_or_malformed_receipts_never_claim_relay_success(self):
        for value in ({}, {'worker_exit_code': 0, 'room': None}, {'room': []}):
            self.write('collaboration/live-pilot-result.json', value)
            with patch('agent_hub.collector.process_counts', return_value={}):
                snapshot = self.collector.snapshot()
            self.assertEqual(snapshot['milestones'][0]['status'], 'in_progress')
            self.assertEqual(snapshot['activity'], [])

    def test_acknowledgement_or_empty_file_does_not_claim_review(self):
        (self.root / 'CLAUDE-FOLLOWUP.md').write_text('CLAUDE-002 acknowledged', encoding='utf-8')
        (self.root.parent / 'grok-architecture-review.md').write_text('', encoding='utf-8')
        self.assertEqual(self.collector.activity(), [])

    def test_read_bounds_reject_oversized_and_invalid_data(self):
        path = self.root / 'CLAUDE-FOLLOWUP.md'
        path.write_bytes(b'x' * 64_001)
        self.assertIsNone(bounded_text(path, 64_000))
        self.assertEqual(read_json(path), {})
        self.assertEqual(self.collector.activity(), [])

    def test_quota_handles_malformed_windows_and_expired_reset(self):
        for value in (None, {}, 'bad', [None, {'used_percent': float('nan')}]):
            self.write('runtime/codex-usage.json', {'windows': value})
            self.assertEqual(self.collector.quota(), [])
        data = {'observed_at': self.now-100, 'windows': [{'used_percent': 9, 'window_minutes': 10080, 'resets_at': self.now+10}]}
        self.write('runtime/codex-usage.json', data)
        row = self.collector.quota()[0]
        self.assertEqual(row['remaining'], 91)
        self.assertEqual(row['status'], 'available')
        self.now += 11
        self.assertEqual(self.collector.quota()[0]['status'], 'stale')

    def test_running_process_is_evidence_without_worker_auth(self):
        with patch('agent_hub.collector.process_counts', return_value={'claude.exe': 3}):
            rows = self.collector.inventory('2026-09-20T00:00:00Z')
        claude = next(row for row in rows if row['agent_id'] == 'claude')
        self.assertTrue(claude['installed'])
        self.assertEqual(claude['process_count'], 3)
        self.assertNotIn('auth_status', claude)
        self.assertNotIn('configured', claude)

    def test_cloud_failure_does_not_invoke_local_observation_callback(self):
        counter = []
        def unavailable():
            raise StatusError('unreachable')
        def enrich(data):
            counter.append(True)
            return {**data, 'machine': {'ram_percent': len(counter)}, 'secret': 'DO_NOT_RENDER'}
        monitor = Monitor(unavailable, enrich=enrich, cache_seconds=0)
        first, second = monitor.snapshot(), monitor.snapshot()
        self.assertFalse(second['dashboard']['hub_reachable'])
        self.assertEqual(counter, [])
        for result in (first, second):
            for name in ('machine', 'inventory', 'activity', 'milestones', 'collector'):
                self.assertNotIn(name, result)
        self.assertNotIn('DO_NOT_RENDER', json.dumps(second))

    def test_projection_drops_all_local_observation_fields(self):
        value = {'inventory': [{'agent_id': 'codex', 'process_count': 1, 'command_line': 'SECRET', 'token': 'SECRET'}],
                 'activity': [{'id': 'x', 'raw_output': 'SECRET'}], 'machine': {'hostname': 'SECRET', 'ram_percent': 40},
                 'milestones': [{'id': 'local', 'status': 'complete'}], 'collector': {'source': 'local-machine'}}
        result = public_snapshot(value)
        self.assertNotIn('SECRET', json.dumps(result))
        for name in ('machine', 'inventory', 'activity', 'milestones', 'collector'):
            self.assertNotIn(name, result)


if __name__ == '__main__':
    unittest.main()
