"""Cross-check: the broker's abort-to-idle is a clean release the controller strikes on.

Builds the credential document through the REAL cloud_credential_broker abort path
(an access stall after the lease write) and ticks the REAL fleet_controller on it:
idle_credential() passes, the failed execution is a normal strike with backoff,
and the next launch follows the backoff with no manual step. Negative control: the
same document quarantined blocks the slot as worker_failed_credential_unreleased.
Written by Cursor on Demand; composition instead of inheritance so the controller
suite is not re-run here. Offline only.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
HUB = Path(__file__).resolve().parents[2] / 'agent-hub'
for entry in (str(HUB / 'tests'), str(HUB), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from agent_hub import cloud_credential_broker as cb  # noqa: E402
from fleet_controller import ControllerError  # noqa: E402
# Import the modules, not the TestCase classes, so unittest does not collect
# and re-run those suites from this file.
import test_cloud_credential_broker as broker_tests  # noqa: E402
from tests import test_fleet_controller as fleet_tests  # noqa: E402

NEXT, NEXT_UID = fleet_tests.NEXT, fleet_tests.NEXT_UID


class AbortIdleControllerCrosscheck(unittest.TestCase):
    def setUp(self):
        self.fleet = fleet_tests.FleetReviewTests('setUp')

    def aborted_idle_doc(self):
        """The credential record exactly as the broker's access-stall abort leaves it."""
        fixture = broker_tests.BrokerTests()
        fixture.setUp()
        fixture._stall_rest('access')
        with self.assertRaisesRegex(cb.UpstreamUnavailable, '^credential_acquisition_upstream_unavailable$'):
            fixture.acquire()
        fixture._assert_clean_release()
        doc = deepcopy(fixture.wire.state)
        self.assertEqual(doc['phase'], 'idle')
        self.assertEqual(doc['quarantine_reason'], '')
        self.assertEqual(doc['version'], doc['last_release_version'])
        self.assertEqual(doc['fence'], doc['last_release_fence'])
        self.assertEqual(doc['last_release_id'], '1' * 32)
        self.assertEqual(doc['execution_uid'], fixture.wire.execution['uid'])
        return doc

    def slot_with(self, doc):
        """Launch one worker, then bind the broker to that document for its execution."""
        controller, store, cloud, broker, _, _ = self.fleet.make()
        controller.tick()
        released = deepcopy(doc)
        released['execution_uid'] = NEXT_UID
        broker.state = released
        self.fleet.fail_current(cloud, broker)
        return controller, store, cloud, broker

    def test_abort_idle_release_passes_idle_credential_and_strikes(self):
        controller, store, cloud, broker = self.slot_with(self.aborted_idle_doc())
        self.assertEqual(controller.idle_credential(cloud.executions_by_name[NEXT]), broker.state)
        result = controller.tick()
        self.assertEqual(result['status'], 'replacement_after_failure')
        self.assertEqual(result['consecutive_failures'], 1)
        self.assertEqual(result['next_launch_at'], 1000 + 120)
        self.assertNotEqual(store.state.get('error'), 'worker_failed_credential_unreleased')
        self.assertEqual(store.state['phase'], 'idle')
        # The next launch comes after the backoff, with no manual reset.
        self.assertEqual(controller.tick()['status'], 'replacement_cooldown')
        self.assertEqual(cloud.run_count, 1)
        controller.clock = lambda: 1200
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(cloud.run_count, 2)

    def test_the_same_doc_quarantined_blocks_the_slot(self):
        doc = self.aborted_idle_doc()
        doc['phase'] = 'quarantined'
        doc['quarantine_reason'] = 'credential_read_failed'
        controller, store, cloud, _ = self.slot_with(doc)
        with self.assertRaises(ControllerError) as caught:
            controller.idle_credential(cloud.executions_by_name[NEXT])
        self.assertEqual(str(caught.exception), 'credential_not_cleanly_released')
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(store.state['error'], 'worker_failed_credential_unreleased')
        self.assertEqual(cloud.run_count, 1)


if __name__ == '__main__':
    unittest.main()
