"""Advisory fleet status documents and controller publish hooks (MH-008 C2)."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(SOURCE))

from fleet_controller import Controller, ControllerError, digest
from fleet_status import (STATUS_KEYS, FakeStatusPublisher, FirestoreStatusPublisher,
                          status_document, status_reason)
from test_dynamic_broker import POLICY
from tests.test_fleet_controller import Broker, Cloud, Grants, Bindings, Store, JOB_UID, NEXT, NEXT_UID


NOW = 1000


class StatusDocumentTests(unittest.TestCase):
    def doc(self, state, now=NOW, provider='grok'):
        return status_document(state, now, provider)

    def test_every_phase_maps_to_the_right_reason(self):
        cases = (
            ({'phase': 'active'}, 'ready_or_running'),
            ({'phase': 'binding_intent'}, 'provisioning'),
            ({'phase': 'binding_ready'}, 'provisioning'),
            ({'phase': 'launch_intent'}, 'provisioning'),
            ({'phase': 'launch_submitted'}, 'provisioning'),
            ({'phase': 'grant_intent'}, 'provisioning'),
            ({'phase': 'blocked'}, 'blocked'),
            ({'slot_enabled': False}, 'disabled'),
            ({'phase': 'idle'}, 'idle_launching'),
            ({'phase': 'something_else'}, 'unknown'),
            (None, 'unknown'),
        )
        for state, reason in cases:
            with self.subTest(state=state, reason=reason):
                self.assertEqual(status_reason(state, NOW), reason)
                document = self.doc(state if state is not None else {})
                self.assertEqual(document['reason'], reason if state is not None else 'unknown')

    def test_backoff_parked_and_idle_launching(self):
        backoff = {'phase': 'idle', 'consecutive_failures': 1, 'next_launch_at': NOW + 120}
        self.assertEqual(status_reason(backoff, NOW), 'backoff')
        self.assertEqual(status_reason(backoff, NOW + 120), 'idle_launching')

        parked = {'phase': 'idle', 'error': 'provider_quota_exhausted',
                  'consecutive_failures': 2, 'next_launch_at': NOW + 3600}
        self.assertEqual(status_reason(parked, NOW), 'parked')
        self.assertEqual(status_reason(parked, NOW + 3600), 'idle_launching')

        # Clean durable launch interval: waiting, but not a strike and not a park.
        cooldown = {'phase': 'idle', 'consecutive_failures': 0, 'next_launch_at': NOW + 60}
        self.assertEqual(status_reason(cooldown, NOW), 'unknown')
        self.assertEqual(status_reason(cooldown, NOW + 60), 'idle_launching')

        absent = {'phase': 'idle', 'consecutive_failures': 1}
        self.assertEqual(status_reason(absent, NOW), 'idle_launching')

    def test_document_shape_and_secret_whitelist(self):
        dirty = {
            'phase': 'active',
            'grant': 'secret-grant-token',
            'grant_sha256': 'a' * 64,
            'intent': 'deadbeef',
            'execution': 'projects/x/locations/y/jobs/z/executions/z-abcde',
            'execution_uid': NEXT_UID,
            'operation': 'projects/x/locations/y/operations/op',
            'token': 'leak',
            'receipt': {'anything': True},
            'consecutive_failures': 2,
            'next_launch_at': NOW + 1,
            'error': 'worker_failed_no_restart_loop',
        }
        document = self.doc(dirty)
        self.assertEqual(set(document), STATUS_KEYS)
        self.assertEqual(document, {
            'schema': 1,
            'provider': 'grok',
            'phase': 'active',
            'reason': 'ready_or_running',
            'next_launch_at': NOW + 1,
            'consecutive_failures': 2,
            'published_at': NOW,
        })
        for forbidden in ('grant', 'grant_sha256', 'intent', 'execution', 'execution_uid',
                          'operation', 'token', 'receipt', 'error'):
            self.assertNotIn(forbidden, document)

    def test_firestore_publisher_is_a_named_stub(self):
        with self.assertRaises(NotImplementedError):
            FirestoreStatusPublisher().publish('grok', self.doc({'phase': 'idle'}))
        self.assertIn('runcrew_fleet_status', FirestoreStatusPublisher.__doc__)


class ControllerPublishTests(unittest.TestCase):
    def make(self, publisher=None, enabled=True):
        store, cloud, broker, bindings, grants = Store(), Cloud(), Broker(), Bindings(), Grants()
        slot = {'job_uid': JOB_UID, 'template_sha256': digest(cloud.job['template']), 'enabled': enabled}
        controller = Controller(POLICY, slot, store, cloud, broker, binding_store=bindings,
                                grant_factory=grants.factory, clock=lambda: NOW,
                                status_publisher=publisher)
        return controller, store, cloud, broker, bindings, grants

    def test_publish_once_per_tick_on_success_paths(self):
        publisher = FakeStatusPublisher()
        controller, store, cloud, broker, _, _ = self.make(publisher)
        result = controller.tick()
        self.assertEqual(result['status'], 'job_running_readiness_separate')
        self.assertEqual(len(publisher.published), 1)
        provider, document = publisher.published[0]
        self.assertEqual(provider, 'grok')
        self.assertEqual(document['reason'], 'ready_or_running')
        self.assertEqual(document['phase'], 'active')
        self.assertEqual(set(document), STATUS_KEYS)

        # Mid-execution return path.
        again = controller.tick()
        self.assertEqual(again['status'], 'job_running_readiness_separate')
        self.assertEqual(len(publisher.published), 2)
        self.assertEqual(publisher.published[1][1]['reason'], 'ready_or_running')

        # Blocked return path.
        broker.state.update(phase='leased', execution_uid=NEXT_UID)
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        blocked = controller.tick()
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(len(publisher.published), 3)
        self.assertEqual(publisher.published[2][1]['reason'], 'blocked')
        self.assertEqual(publisher.published[2][1]['phase'], 'blocked')

        # Early read-only blocked return.
        controller.tick()
        self.assertEqual(len(publisher.published), 4)
        self.assertEqual(publisher.published[3][1]['reason'], 'blocked')

    def test_publish_on_disabled_and_controller_error(self):
        publisher = FakeStatusPublisher()
        controller, _, _, _, _, _ = self.make(publisher, enabled=False)
        self.assertEqual(controller.tick()['status'], 'disabled')
        self.assertEqual(len(publisher.published), 1)
        self.assertEqual(publisher.published[0][1]['reason'], 'disabled')

        publisher = FakeStatusPublisher()
        controller, _, cloud, _, _, _ = self.make(publisher)
        cloud.job.pop('latestCreatedExecution')
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'prior_execution_required')
        self.assertEqual(len(publisher.published), 1)
        self.assertEqual(publisher.published[0][1]['reason'], 'idle_launching')
        self.assertEqual(publisher.published[0][1]['phase'], 'idle')

    def test_publish_on_backoff_and_park_return_paths(self):
        publisher = FakeStatusPublisher()
        controller, store, cloud, broker, _, _ = self.make(publisher)
        controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        broker.state.update(execution_uid=NEXT_UID)
        result = controller.tick()
        self.assertEqual(result['status'], 'replacement_after_failure')
        self.assertEqual(publisher.published[-1][1]['reason'], 'backoff')
        cooldown = controller.tick()
        self.assertEqual(cooldown['status'], 'replacement_cooldown')
        self.assertEqual(publisher.published[-1][1]['reason'], 'backoff')

        publisher = FakeStatusPublisher()
        controller, store, cloud, broker, _, _ = self.make(publisher)
        controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        broker.state.update(execution_uid=NEXT_UID)
        cloud.tasks = {NEXT + '/tasks': {'tasks': [
            {'name': NEXT + '/tasks/task0', 'lastAttemptResult': {'exitCode': 75}}]}}
        parked = controller.tick()
        self.assertEqual(parked['status'], 'provider_quota_parked')
        self.assertEqual(publisher.published[-1][1]['reason'], 'parked')

    def test_failing_publisher_leaves_tick_results_unchanged(self):
        publisher = FakeStatusPublisher()
        publisher.fail_next = True
        controller, store, cloud, _, _, _ = self.make(publisher)
        result = controller.tick()
        self.assertEqual(result['status'], 'job_running_readiness_separate')
        self.assertEqual(store.state['phase'], 'active')
        self.assertEqual(controller.status_publish_failures, 1)
        self.assertEqual(publisher.published, [])
        self.assertEqual(cloud.run_count, 1)

        # A later tick still works and publishes.
        again = controller.tick()
        self.assertEqual(again['status'], 'job_running_readiness_separate')
        self.assertEqual(len(publisher.published), 1)
        self.assertEqual(controller.status_publish_failures, 1)

    def test_no_publisher_matches_today_byte_for_byte(self):
        # Same scenario with and without a publisher: tick results and durable
        # store fields must match. grant/intent are random per launch, so scrub
        # those (they are never part of the status document either).
        def scrub(state):
            cleaned = deepcopy(state)
            for key in ('intent', 'grant', 'grant_sha256'):
                cleaned.pop(key, None)
            return cleaned

        def run(publisher=None):
            controller, store, cloud, broker, _, _ = self.make(publisher)
            first = controller.tick()
            cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
                reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
            broker.state.update(execution_uid=NEXT_UID)
            second = controller.tick()
            third = controller.tick()
            return first, second, third, scrub(store.state), cloud.run_count, len(store.archives)

        without = run(None)
        with_pub = run(FakeStatusPublisher())
        self.assertEqual(without, with_pub)

    def test_publish_on_intent_phase_early_return(self):
        publisher = FakeStatusPublisher()
        controller, store, cloud, _, _, grants = self.make(publisher)
        cloud.lose_run_reply = True
        with self.assertRaises(OSError):
            controller.tick()
        self.assertEqual(store.state['phase'], 'launch_intent')
        # Exception path still publishes the state the tick ended in.
        self.assertEqual(len(publisher.published), 1)
        self.assertEqual(publisher.published[0][1]['reason'], 'provisioning')
        self.assertEqual(publisher.published[0][1]['phase'], 'launch_intent')

        result = controller.tick()
        self.assertEqual(result['status'], 'launch_intent')
        self.assertEqual(len(publisher.published), 2)
        self.assertEqual(publisher.published[1][1]['reason'], 'provisioning')


if __name__ == '__main__':
    unittest.main()
