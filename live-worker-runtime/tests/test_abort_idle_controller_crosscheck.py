"""Cross-check: broker R1 abort-to-idle is a clean release the controller can strike on.

Demand asked Alpha for this: 47d367c asserts the released fields but never runs the
controller on them. This file proves end-to-end that after the broker's abort-to-idle,
the REAL fleet_controller sees a clean release and takes a normal strike (no manual
recovery). Offline only; no provider/cloud calls. Shell may be unavailable — Claude
runs the suite.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
HUB = Path(__file__).resolve().parents[2] / 'agent-hub'
for entry in (str(HERE), str(HUB), str(HUB / 'tests')):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from agent_hub import cloud_credential_broker as cb
from fleet_controller import ControllerError
from test_cloud_credential_broker import BrokerTests
from test_fleet_controller import FleetReviewTests, NEXT, NEXT_UID

# test_fleet_controller may prepend a machine-local SOURCE; keep workspace hubs first.
sys.path.insert(0, str(HUB))
sys.path.insert(0, str(HERE))


class AbortIdleControllerCrosscheck(FleetReviewTests):
    def aborted_idle_doc(self):
        """Build the credential record exactly as the broker's R1 access-stall abort leaves it."""
        fixture = BrokerTests()
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

    def _slot_with_aborted_idle(self, doc):
        """Launch one worker, then bind the broker to the abort-idle release for that execution."""
        controller, store, cloud, broker, bindings, grants = self.make()
        controller.tick()
        released = deepcopy(doc)
        released['execution_uid'] = NEXT_UID
        broker.state = released
        self.fail_current(cloud, broker)
        return controller, store, cloud, broker, bindings, grants

    def test_abort_idle_release_passes_idle_credential_and_strikes(self):
        doc = self.aborted_idle_doc()
        controller, store, cloud, broker, _, _ = self._slot_with_aborted_idle(doc)

        # Controller's clean-release gate accepts the broker's abort document as-is.
        self.assertEqual(controller.idle_credential(cloud.executions_by_name[NEXT]), broker.state)
        self.assertEqual(controller.idle_credential()['phase'], 'idle')

        result = controller.tick()
        self.assertEqual(result['status'], 'replacement_after_failure')
        self.assertEqual(result['consecutive_failures'], 1)
        self.assertIn('next_launch_at', result)
        self.assertEqual(result['next_launch_at'], 1000 + 120)
        self.assertNotEqual(result['status'], 'blocked')
        self.assertNotEqual(store.state.get('error'), 'worker_failed_credential_unreleased')
        self.assertEqual(store.state['phase'], 'idle')
        self.assertEqual(store.state['next_launch_at'], 1000 + 120)
        self.assertEqual(store.state['consecutive_failures'], 1)

        # Next launch only after backoff — no manual reset.
        self.assertEqual(controller.tick()['status'], 'replacement_cooldown')
        self.assertEqual(cloud.run_count, 1)
        controller.clock = lambda: 1200
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(cloud.run_count, 2)

    def test_abort_idle_doc_quarantined_blocks_without_strike(self):
        doc = self.aborted_idle_doc()
        doc['phase'] = 'quarantined'
        doc['quarantine_reason'] = 'credential_read_failed'
        controller, store, cloud, broker, _, _ = self._slot_with_aborted_idle(doc)

        with self.assertRaises(ControllerError) as caught:
            controller.idle_credential(cloud.executions_by_name[NEXT])
        self.assertEqual(str(caught.exception), 'credential_not_cleanly_released')

        result = controller.tick()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(store.state['error'], 'worker_failed_credential_unreleased')
        self.assertEqual(store.state['phase'], 'blocked')
        self.assertEqual(cloud.run_count, 1)
        self.assertEqual(controller.tick()['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
