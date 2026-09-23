"""Offline synthetic observation feed tests; no provider or cloud calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_hub.collector import LocalCollector
from agent_hub.core import Hub, HubError
from agent_hub.observations import MAX_OBSERVATION_BYTES, enrich_status, report_observation
from agent_hub.store import FirestoreStore, SQLiteStore
from agent_hub.telemetry import iso, status_snapshot


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='runcrew-synthetic-observations-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1789929000.0
        self.hub = Hub(SQLiteStore(self.root / 'hub.sqlite3'), lambda: self.now)

    def sample(self):
        at = iso(self.now)
        return {'machine_id': 'synthetic-windows',
            'inventory': [{'agent_id': 'cursor', 'label': 'Cursor', 'installed': True, 'process_count': 3,
                'runtime_status': 'running', 'checked_at': at, 'note': 'Local app only'}],
            'machine': {'status': 'ok', 'observed_at': at, 'platform': 'Windows', 'logical_cpus': 8,
                'ram_total_gb': 16.0, 'ram_used_gb': 8.0, 'ram_percent': 50, 'disk_free_gb': 123.0, 'cpu_percent': 20},
            'activity': [{'id': 'cursor-sdk', 'agent_id': 'cursor', 'status': 'completed', 'title': 'Review complete',
                'detail': 'Synthetic receipt', 'observed_at': at, 'source': 'saved collaboration receipt'}],
            'milestones': [{'id': 'cloud', 'title': 'Google Cloud with login', 'status': 'in_progress', 'detail': 'Synthetic'}],
            'collector': {'status': 'ok', 'observed_at': at, 'interval_seconds': 15, 'source': 'local-machine'},
            'local_usage': [
                {'provider': 'codex', 'scope': 'account', 'metric': 'quota_percent', 'label': 'Weekly allowance',
                 'used': 80, 'remaining': 20, 'limit': 100, 'unit': '%', 'window_minutes': 10080,
                 'resets_at': iso(self.now + 3600), 'observed_at': at, 'source': 'provider_api', 'status': 'available', 'error': None},
                {'provider': 'codex', 'scope': 'account', 'metric': 'credits', 'label': 'Extra credits', 'used': None,
                 'remaining': 10, 'limit': None, 'unit': 'credits', 'observed_at': at,
                 'source': 'provider_api', 'status': 'available', 'error': None}]}

    def snapshot(self):
        return enrich_status(self.hub, status_snapshot(self.hub))

    def test_no_observation_preserves_status_without_fabricated_local_machine(self):
        initial = status_snapshot(self.hub)
        self.assertIs(enrich_status(self.hub, initial), initial)
        self.assertNotIn('machine', initial)

    def test_store_survives_restart_and_merges_without_claiming_worker_connection(self):
        receipt = report_observation(self.hub, self.sample())
        self.assertEqual(receipt, {'accepted': True, 'received_at': iso(self.now), 'scope': 'single_machine'})
        restarted = Hub(SQLiteStore(self.root / 'hub.sqlite3'), lambda: self.now)
        snapshot = enrich_status(restarted, status_snapshot(restarted))
        self.assertEqual(snapshot['inventory'][0]['process_count'], 3)
        self.assertEqual(snapshot['collector']['machine_id'], 'synthetic-windows')
        self.assertEqual(snapshot['collector']['scope'], 'single_machine')
        self.assertTrue(all(row['status'] == 'unconfigured' for row in snapshot['agents']))
        self.assertEqual(len([row for row in snapshot['usage'] if row['provider'] == 'codex' and row['metric'] == 'credits']), 1)
        self.assertEqual(next(row for row in snapshot['usage'] if row['provider'] == 'codex' and row['metric'] == 'credits')['remaining'], 10)

    def test_exact_collector_snapshot_schema_is_accepted(self):
        collector = LocalCollector(self.root, home=self.root, clock=lambda: self.now)
        sample = self.sample()
        with patch.object(collector, 'inventory', return_value=sample['inventory']), \
             patch.object(collector, 'machine', return_value=sample['machine']), \
             patch.object(collector, 'activity', return_value=sample['activity']), \
             patch.object(collector, 'quota', return_value=sample['local_usage']):
            report_observation(self.hub, {'machine_id': 'synthetic', **collector.snapshot()})
        self.assertEqual(len(self.snapshot()['milestones']), 6)

    def test_collector_expires_at_90_seconds_but_provider_usage_has_its_own_clock(self):
        report_observation(self.hub, self.sample())
        self.now += 90
        self.assertEqual(self.snapshot()['collector']['status'], 'ok')
        self.now += 1
        result = self.snapshot()
        self.assertEqual(result['collector']['status'], 'stale')
        self.assertEqual(result['machine']['status'], 'stale')
        self.assertEqual(result['inventory'][0]['runtime_status'], 'stale')
        self.assertTrue(any(row['kind'] == 'collector_stale' for row in result['alerts']))
        quota = next(row for row in result['usage'] if row['metric'] == 'quota_percent')
        self.assertEqual(quota['status'], 'available')

    def test_new_heartbeat_does_not_refresh_old_usage_or_stale_sample(self):
        sample = self.sample()
        original = sample['local_usage'][0]['observed_at']
        report_observation(self.hub, sample)
        self.now += 901
        report_observation(self.hub, sample)
        self.assertEqual(self.snapshot()['collector']['status'], 'stale')
        sample['collector']['observed_at'] = iso(self.now)
        report_observation(self.hub, sample)
        result = self.snapshot()
        self.assertEqual(result['collector']['status'], 'ok')
        self.assertEqual(result['machine']['status'], 'stale')
        quota = next(row for row in result['usage'] if row['metric'] == 'quota_percent')
        self.assertEqual(quota['status'], 'stale')
        self.assertEqual(quota['observed_at'], original)

    def test_window_reset_stales_quota_without_staling_credits(self):
        sample = self.sample()
        sample['local_usage'][0]['resets_at'] = iso(self.now + 20)
        report_observation(self.hub, sample)
        self.now += 20
        result = self.snapshot()
        self.assertEqual(next(row for row in result['usage'] if row['metric'] == 'quota_percent')['status'], 'stale')
        self.assertEqual(next(row for row in result['usage'] if row['provider'] == 'codex' and row['metric'] == 'credits')['status'], 'available')

    def test_free_display_text_is_regenerated_and_unknown_fields_rejected(self):
        sample = self.sample()
        sentinel = 'SYNTHETIC_DO_NOT_ECHO_RAW_PROMPT_OR_CREDENTIAL'
        sample['inventory'][0].update(label=sentinel, note=sentinel)
        sample['activity'][0].update(title=sentinel, detail=sentinel)
        sample['milestones'][0].update(title=sentinel, detail=sentinel)
        sample['local_usage'][0]['label'] = sentinel
        report_observation(self.hub, sample)
        self.assertNotIn(sentinel, json.dumps(self.hub.store.get_observation()))
        self.assertNotIn(sentinel, json.dumps(self.snapshot()))
        for location in (sample, sample['machine'], sample['activity'][0], sample['local_usage'][0]):
            location['prompt'] = sentinel
            with self.assertRaises(HubError):
                report_observation(self.hub, sample)
            del location['prompt']

    def test_invalid_types_bounds_duplicates_and_future_times_are_rejected(self):
        edits = [lambda x: x.update(machine_id='../private'),
                 lambda x: x['machine'].update(cpu_percent=True),
                 lambda x: x['machine'].update(ram_percent=101),
                 lambda x: x['machine'].update(ram_total_gb=math_inf),
                 lambda x: x['collector'].update(observed_at=iso(self.now + 61)),
                 lambda x: x['inventory'][0].update(runtime_status=[]),
                 lambda x: x['local_usage'][0].update(remaining=101),
                 lambda x: x['local_usage'][0].update(source='manual'),
                 lambda x: x['inventory'].append(deepcopy(x['inventory'][0])),
                 lambda x: x['local_usage'].append(deepcopy(x['local_usage'][0])),
                 lambda x: x['activity'][0].update(detail='x' * MAX_OBSERVATION_BYTES)]
        math_inf = float('inf')
        for edit in edits:
            sample = self.sample()
            edit(sample)
            with self.subTest(edit=edit), self.assertRaises(HubError):
                report_observation(self.hub, sample)
        self.assertIsNone(self.hub.store.get_observation())

    def test_single_latest_machine_and_no_mutation_of_input_status(self):
        first = self.sample()
        report_observation(self.hub, first)
        second = self.sample()
        second['machine_id'] = 'synthetic-replacement'
        second['machine']['ram_percent'] = 95
        report_observation(self.hub, second)
        initial = status_snapshot(self.hub)
        saved = deepcopy(initial)
        result = enrich_status(self.hub, initial)
        self.assertEqual(initial, saved)
        self.assertEqual(result['collector']['machine_id'], 'synthetic-replacement')
        self.assertTrue(any(row['kind'] == 'capacity' for row in result['alerts']))
        self.assertEqual(self.hub.store.get_observation()['data']['machine_id'], 'synthetic-replacement')

    def test_newer_worker_usage_is_not_replaced_with_older_local_value(self):
        report_observation(self.hub, self.sample())
        status = status_snapshot(self.hub)
        value = next(row for row in status['usage'] if row['provider'] == 'codex')
        value.update(observed_at=iso(self.now + 1), status='available', remaining=7, source='provider_api')
        result = enrich_status(self.hub, status)
        measured = next(row for row in result['usage'] if row['provider'] == 'codex' and row['metric'] == 'credits')
        self.assertEqual(measured['remaining'], 7)

    def test_firestore_adapter_uses_only_one_dedicated_document(self):
        class FakeDocument:
            def __init__(self):
                self.data = None
            def set(self, value):
                self.data = deepcopy(value)
            def get(self):
                return SimpleNamespace(exists=self.data is not None, to_dict=lambda: deepcopy(self.data))
        document = FakeDocument()
        class FakeCollection:
            def document(inner, key):
                self.assertEqual(key, 'latest')
                return document
        store = FirestoreStore.__new__(FirestoreStore)
        store.observations = FakeCollection()
        self.assertIsNone(store.get_observation())
        hub = Hub(store, lambda: self.now)
        report_observation(hub, self.sample())
        self.assertEqual(store.get_observation()['received_at'], self.now)
        self.assertEqual(store.get_observation()['data']['machine_id'], 'synthetic-windows')


if __name__ == '__main__':
    unittest.main()
