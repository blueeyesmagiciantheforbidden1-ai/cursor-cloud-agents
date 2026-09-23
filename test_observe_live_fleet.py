"""Offline checks for observe_live_fleet's pure summarizers; no cloud calls."""
import unittest

import observe_live_fleet as m


class ObserverSummaries(unittest.TestCase):
    def test_ticks_are_oldest_first_and_only_tick_rows(self):
        rows = [
            {'timestamp': '2026-09-22T18:29:41.1Z', 'jsonPayload': {'kind': 'runcrew_fleet_tick', 'workers': {
                'claude': {'status': 'controller_attention_required'}, 'grok': {'status': 'idle', 'generation': 0}}}},
            {'timestamp': '2026-09-22T18:28:40.1Z', 'textPayload': 'Starting new instance.'},
            {'timestamp': '2026-09-22T18:27:39.1Z', 'jsonPayload': {'kind': 'runcrew_fleet_tick', 'workers': {
                'claude': {'status': 'job_running_readiness_separate', 'execution': 'x', 'worker_id': 'hidden'}}}},
        ]
        summary = m.summarize_ticks(rows)
        self.assertEqual([s['at'] for s in summary], ['2026-09-22T18:27:39', '2026-09-22T18:29:41'])
        self.assertEqual(summary[0]['workers']['claude'], {'status': 'job_running_readiness_separate', 'execution': 'x'})
        self.assertFalse(m.parked(summary))
        self.assertTrue(m.parked(summary[-1:]) is False)  # grok is idle in the newest tick
        summary[-1]['workers']['grok']['status'] = 'controller_attention_required'
        self.assertTrue(m.parked(summary))
        self.assertTrue(m.parked([]))

    def test_executions_filtered_by_day(self):
        rows = [
            {'metadata': {'name': 'runcrew-worker-grok-blueeyes-bqcwf', 'creationTimestamp': '2026-09-22T00:09:39Z'},
             'status': {'succeededCount': 1}},
            {'metadata': {'name': 'runcrew-worker-grok-blueeyes-old01', 'creationTimestamp': '2026-09-21T23:50:38Z'},
             'status': {'failedCount': 1}},
        ]
        today = m.summarize_executions(rows, '2026-09-22')
        self.assertEqual([e['name'] for e in today], ['runcrew-worker-grok-blueeyes-bqcwf'])
        self.assertEqual(today[0], {'name': 'runcrew-worker-grok-blueeyes-bqcwf', 'created': '2026-09-22T00:09:39',
                                    'running': 0, 'succeeded': 1, 'failed': 0})
        self.assertEqual(m.summarize_executions(rows, '2026-09-23'), [])


if __name__ == '__main__':
    unittest.main()
