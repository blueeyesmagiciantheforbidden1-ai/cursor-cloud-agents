import json
import math
from pathlib import Path
import tempfile
import unittest

from agent_hub.core import Hub, HubError
from agent_hub.store import SQLiteStore
from agent_hub.telemetry import iso, report_worker, status_snapshot


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-telemetry-test-')
        self.root = Path(self.temp.name).resolve()
        self.now = 1789929000.0
        self.hub = Hub(SQLiteStore(self.root/'test.sqlite3'), lambda: self.now)

    def tearDown(self):
        self.assertEqual(self.root, Path(self.temp.name).resolve())
        self.temp.cleanup()

    def measure(self, **changes):
        return {'scope':'account','metric':'credits','status':'available','source':'provider_cli',
                'used':95,'limit':100,'remaining':5,'observed_at':iso(self.now),**changes}

    def test_never_connected_is_unconfigured_and_unknown_balance_is_not_zero(self):
        snapshot = status_snapshot(self.hub)
        self.assertEqual(len(snapshot['agents']),5)
        self.assertTrue(all(row['status']=='unconfigured' for row in snapshot['agents']))
        self.assertTrue(all(row['used'] is None and row['remaining'] is None for row in snapshot['usage']))
        self.assertEqual(snapshot['alerts'],[])

    def test_worker_expires_and_stopped_worker_is_offline_immediately(self):
        report_worker(self.hub,'codex',{'status':'idle'})
        self.assertEqual(status_snapshot(self.hub)['agents'][0]['status'],'idle')
        self.now += 91
        snapshot = status_snapshot(self.hub)
        self.assertEqual(snapshot['agents'][0]['status'],'offline')
        self.assertEqual(snapshot['alerts'][0]['kind'],'worker_offline')
        report_worker(self.hub,'codex',{'status':'offline'})
        self.assertEqual(status_snapshot(self.hub)['agents'][0]['status'],'offline')

    def test_worker_identity_comes_from_authentication_not_payload(self):
        report_worker(self.hub,'codex',{'agent_id':'claude','worker_id':'one','status':'idle'})
        snapshot = status_snapshot(self.hub)
        self.assertTrue(snapshot['agents'][0]['configured'])
        self.assertFalse(snapshot['agents'][1]['configured'])
        with self.assertRaises(HubError):
            report_worker(self.hub,'manager',{'status':'idle'})
        room = self.hub.create('manager',{'prompt':'private','agents':['claude']})
        with self.assertRaises(HubError):
            report_worker(self.hub,'codex',{'status':'busy','current_room_id':room['id']})

    def test_session_usage_cannot_masquerade_as_account_balance(self):
        report_worker(self.hub,'codex',{'usage':[self.measure(scope='session')]})
        snapshot = status_snapshot(self.hub)
        measured = [row for row in snapshot['usage'] if row['provider']=='codex']
        self.assertEqual(len(measured),2)
        self.assertEqual(next(row for row in measured if row['scope']=='account')['status'],'unavailable')
        self.assertFalse(any(row['kind']=='usage_low' for row in snapshot['alerts']))

    def test_usage_staleness_does_not_falsely_refresh_from_worker_heartbeat(self):
        original = self.measure()
        report_worker(self.hub,'codex',{'status':'idle','usage':[original]})
        self.assertTrue(any(row['kind']=='usage_low' for row in status_snapshot(self.hub)['alerts']))
        self.now += 901
        report_worker(self.hub,'codex',{'status':'idle','usage':[original]})
        snapshot = status_snapshot(self.hub)
        measured = next(row for row in snapshot['usage'] if row['provider']=='codex')
        self.assertEqual(measured['status'],'stale')
        self.assertEqual(measured['remaining'],5)
        self.assertFalse(any(row['kind']=='usage_low' for row in snapshot['alerts']))

    def test_malformed_provider_metrics_are_rejected(self):
        for changes in ({'used':math.nan},{'used':True},{'remaining':101},
                        {'observed_at':iso(self.now+120)},{'observed_at':None},
                        {'source':'paste-secret-here'},{'error':[]}):
            with self.subTest(changes=changes), self.assertRaises(HubError):
                report_worker(self.hub,'codex',{'usage':[self.measure(**changes)]})

    def test_status_omits_task_contents_and_lease(self):
        room = self.hub.create('manager',{'prompt':'private task contents'})
        task = self.hub.claim('codex')['task']
        report_worker(self.hub,'codex',{'status':'busy','current_room_id':room['id']})
        snapshot = status_snapshot(self.hub)
        rendered = json.dumps(snapshot)
        self.assertNotIn('private task contents',rendered)
        self.assertNotIn(task['lease_token'],rendered)
        self.assertEqual(snapshot['rooms'][0]['total_steps'],4)
        self.assertEqual(snapshot['rooms'][0]['current_agent'],'codex')

    def test_multiple_workers_do_not_turn_live_peer_offline(self):
        report_worker(self.hub,'codex',{'worker_id':'old','status':'idle'})
        self.now += 91
        report_worker(self.hub,'codex',{'worker_id':'new','status':'idle'})
        row = status_snapshot(self.hub)['agents'][0]
        self.assertEqual(row['status'],'idle')
        self.assertEqual({item['status'] for item in row['workers']},{'idle','offline'})


if __name__ == '__main__':
    unittest.main()
