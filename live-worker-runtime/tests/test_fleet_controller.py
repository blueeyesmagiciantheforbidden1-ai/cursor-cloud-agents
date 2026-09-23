"""Offline adversarial scheduler-delivery checks; no provider/cloud calls."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(SOURCE))

from fleet_controller import Controller, ControllerError, digest
from agent_hub.credential_broker_service import BoundaryError
from test_dynamic_broker import POLICY

PRIOR_UID = '11111111-1111-1111-1111-111111111111'
NEXT_UID = '22222222-2222-2222-2222-222222222222'
JOB_UID = '33333333-3333-3333-3333-333333333333'
PRIOR = POLICY.profile.job_name + '/executions/' + POLICY.profile.job_id + '-prior1'
NEXT = POLICY.profile.job_name + '/executions/' + POLICY.profile.job_id + '-next01'


class Store:
    def __init__(self): self.state = None; self.version = 0; self.archives = []
    def read(self): return deepcopy(self.state), self.version if self.state else None
    def cas(self, state, version):
        if version != (self.version if self.state else None): raise RuntimeError('offline CAS conflict')
        self.state = deepcopy(state); self.version += 1
        return self.version
    def archive(self, state, execution): self.archives.append((deepcopy(state), deepcopy(execution)))


class Cloud:
    def __init__(self):
        self.run_count = 0; self.on_run = lambda: None; self.lose_run_reply = False
        self.job = {'name': POLICY.profile.job_name, 'uid': JOB_UID, 'etag': 'offline-etag',
                    'template': {'offline': 'fixed-template'}, 'latestCreatedExecution': {'name': PRIOR}}
        self.executions_by_name = {PRIOR: {'name': PRIOR, 'uid': PRIOR_UID, 'taskCount': 1,
            'template': {'serviceAccount': POLICY.caller_service_account, 'maxRetries': 0},
            'completionTime': '2026-09-22T00:00:00Z', 'reconciling': False,
            'runningCount': 0, 'succeededCount': 1}}
    def get(self, name): return deepcopy(self.job if name == self.job['name'] else self.executions_by_name[name])
    def run(self, name, request):
        self.run_count += 1
        env = request['overrides']['containerOverrides'][0]['env']
        self.executions_by_name[NEXT] = {'name': NEXT, 'uid': NEXT_UID, 'taskCount': 1,
            'template': {'serviceAccount': POLICY.caller_service_account, 'maxRetries': 0,
                         'containers': [{'env': deepcopy(env)}]}}
        self.job['latestCreatedExecution'] = {'name': NEXT}
        self.on_run()
        if self.lose_run_reply: raise OSError('offline lost launch acknowledgement')
        return {'name': 'projects/496481413971/locations/us-central1/operations/offline-operation'}
    def executions(self, job, intent): return [deepcopy(self.executions_by_name[NEXT])]


class Broker:
    def __init__(self):
        self.state = {'phase': 'idle', 'quarantine_reason': '', 'version': 'version-1',
                      'last_release_version': 'version-1', 'fence': 8,
                      'last_release_fence': 8, 'execution_uid': PRIOR_UID}
    def _read(self): return deepcopy(self.state), 'offline-version'


class Bindings:
    def __init__(self): self.calls = 0
    def publish(self, binding): self.calls += 1


class Grants:
    def __init__(self): self.calls = 0; self.lose_reply = False; self.refuse = 0; self.published = []
    def factory(self, binding): return self
    def read(self): return self.published[-1] if self.published else None
    def publish(self, execution, uid):
        self.calls += 1
        if self.lose_reply: raise OSError('offline lost publication acknowledgement')
        if self.refuse:
            self.refuse -= 1; raise BoundaryError('execution_not_authorized')
        self.published.append((execution, uid))


class FleetReviewTests(unittest.TestCase):
    def make(self):
        store, cloud, broker, bindings, grants = Store(), Cloud(), Broker(), Bindings(), Grants()
        slot = {'job_uid': JOB_UID, 'template_sha256': digest(cloud.job['template']), 'enabled': True}
        controller = Controller(POLICY, slot, store, cloud, broker, binding_store=bindings,
                                grant_factory=grants.factory, clock=lambda: 1000)
        return controller, store, cloud, broker, bindings, grants

    def test_nested_scheduler_delivery_cannot_launch_twice(self):
        controller, store, cloud, _, bindings, grants = self.make()
        nested = []
        cloud.on_run = lambda: nested.append(controller.tick())
        first = controller.tick()
        self.assertEqual(first['status'], 'job_running_readiness_separate')
        self.assertEqual(first['worker_id'], 'grok-live-' + NEXT_UID.replace('-', ''))
        self.assertEqual(nested[0]['status'], 'launch_intent')
        self.assertEqual((bindings.calls, cloud.run_count, grants.calls), (1, 1, 1))
        self.assertIsNone(store.state['grant'])

    def test_lost_launch_acknowledgement_never_launches_again(self):
        controller, store, cloud, _, _, grants = self.make(); cloud.lose_run_reply = True
        with self.assertRaises(OSError): controller.tick()
        self.assertEqual(store.state['phase'], 'launch_intent')
        self.assertEqual(controller.tick()['status'], 'launch_intent')
        self.assertEqual(cloud.run_count, 1); self.assertEqual(grants.calls, 0)

    def test_lost_exact_grant_publication_does_not_replay(self):
        controller, store, cloud, _, _, grants = self.make(); grants.lose_reply = True
        with self.assertRaises(OSError): controller.tick()
        self.assertEqual(controller.tick()['status'], 'grant_intent')
        self.assertEqual(cloud.run_count, 1); self.assertEqual(grants.calls, 1)

    def test_failed_terminal_job_blocks_replenishment(self):
        controller, store, cloud, _, _, _ = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(cloud.run_count, 1)

    def blocked(self):
        """A slot stopped by a failed terminal execution whose credential was released."""
        controller, store, cloud, broker, bindings, grants = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        self.assertEqual(controller.tick()['status'], 'blocked')
        broker.state.update(execution_uid=NEXT_UID)
        return controller, store, cloud, broker, bindings, grants

    def test_reset_returns_blocked_slot_to_idle_and_next_tick_replaces(self):
        controller, store, cloud, _, _, _ = self.blocked()
        result = controller.reset()
        self.assertEqual(result, {'status': 'idle', 'cleared': 'worker_failed_no_restart_loop', 'from_phase': 'blocked', 'generation': 1})
        self.assertEqual(store.state['phase'], 'idle'); self.assertNotIn('error', store.state)
        self.assertEqual(store.state['previous_uid'], NEXT_UID)
        self.assertEqual(cloud.run_count, 1)  # reset itself launches nothing
        # The durable launch interval still applies after a reset.
        self.assertEqual(controller.tick()['status'], 'replacement_cooldown'); self.assertEqual(cloud.run_count, 1)
        controller.clock = lambda: 1100
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(cloud.run_count, 2); self.assertEqual(store.state['generation'], 2)

    def test_reset_reconciles_slot_stuck_in_grant_intent(self):
        # A lost grant publication leaves the slot at grant_intent forever and
        # the worker dies at bootstrap without a grant. The broker's release
        # record still names the PREVIOUS execution, because this one never
        # held the credential; the slot must not deadlock on that.
        controller, store, cloud, broker, _, grants = self.make(); grants.lose_reply = True
        with self.assertRaises(OSError): controller.tick()
        self.assertEqual(store.state['phase'], 'grant_intent')
        with self.assertRaises(ControllerError): controller.reset()  # execution still running
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        self.assertEqual(broker.state['execution_uid'], PRIOR_UID)
        result = controller.reset()
        self.assertEqual(result['status'], 'idle'); self.assertEqual(result['from_phase'], 'grant_intent')
        self.assertEqual(store.state['phase'], 'idle'); self.assertEqual(cloud.run_count, 1)
        grants.lose_reply = False; controller.clock = lambda: 1100
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(cloud.run_count, 2)

    def test_never_bound_rule_needs_an_unpublished_grant_and_a_failed_execution(self):
        controller, store, cloud, broker, _, grants = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        # The grant WAS published, so this execution held the credential: the
        # broker record must name it, and the mismatch is a real refusal.
        self.assertEqual(len(grants.published), 1)
        self.assertEqual(controller.tick()['status'], 'blocked')
        with self.assertRaises(ControllerError) as caught: controller.reset()
        self.assertEqual(str(caught.exception), 'credential_release_execution_mismatch')

    def test_execution_names_are_stored_and_published_in_canonical_form(self):
        controller, store, cloud, _, _, grants = self.make()
        original_run = cloud.run
        def run(name, request):
            result = original_run(name, request)
            cloud.executions_by_name[NEXT]['name'] = NEXT.replace('projects/496481413971/', 'projects/project-0c6d31fa-509e-4116-a2c/', 1)
            return result
        cloud.run = run
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(store.state['execution'], NEXT)
        self.assertEqual(grants.published, [(NEXT, NEXT_UID)])

    def test_refused_grant_publication_parks_the_slot_for_reconciliation(self):
        controller, store, _, _, _, grants = self.make(); grants.refuse = 1
        with self.assertRaises(BoundaryError): controller.tick()
        self.assertEqual(grants.calls, 1); self.assertEqual(store.state['phase'], 'grant_intent')

    def test_never_bound_recovers_a_cancelled_launch_and_an_expired_binding(self):
        # Cancelled before binding never held the credential either, and the
        # operator may reconcile hours later, after the binding's expires_at.
        controller, store, cloud, broker, _, grants = self.make(); grants.lose_reply = True
        with self.assertRaises(OSError): controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, cancelledCount=1, failedCount=0, succeededCount=0)
        store.state['expires_at'] = 1000 - 10000
        self.assertEqual(controller.reset()['status'], 'idle')
        self.assertEqual(broker.state['execution_uid'], PRIOR_UID)

    def test_never_bound_requires_this_slots_own_failed_execution(self):
        for change in ({'succeededCount': 1, 'failedCount': 0}, {'uid': '44444444-4444-4444-4444-444444444444'}):
            with self.subTest(change=change):
                controller, store, cloud, _, _, grants = self.make(); grants.lose_reply = True
                with self.assertRaises(OSError): controller.tick()
                cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
                    reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
                cloud.executions_by_name[NEXT].update(change)
                with self.assertRaises(ControllerError): controller.reset()
                self.assertEqual(store.state['phase'], 'grant_intent')

    def test_reset_refuses_unless_credential_cleanly_released(self):
        for change in ({'phase': 'quarantined'}, {'quarantine_reason': 'provider_refresh_uncertain'},
                       {'fence': 9}, {'execution_uid': PRIOR_UID}):
            with self.subTest(change=change):
                controller, store, cloud, broker, _, _ = self.blocked(); broker.state.update(change)
                with self.assertRaises(ControllerError): controller.reset()
                self.assertEqual(store.state['phase'], 'blocked'); self.assertEqual(cloud.run_count, 1)

    def test_reset_refuses_running_or_unblocked_slots(self):
        controller, store, cloud, _, _, _ = self.make(); controller.tick()
        with self.assertRaises(ControllerError) as caught: controller.reset()
        self.assertEqual(str(caught.exception), 'slot_not_stopped'); self.assertEqual(store.state['phase'], 'active')
        controller, store, cloud, _, _, _ = self.blocked()
        cloud.executions_by_name[NEXT].update(runningCount=1, completionTime='')
        with self.assertRaises(ControllerError): controller.reset()
        self.assertEqual(store.state['phase'], 'blocked'); self.assertEqual(cloud.run_count, 1)

    def test_success_with_unreleased_credentials_cannot_replenish(self):
        controller, _, cloud, broker, _, _ = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, succeededCount=1)
        broker.state.update(phase='quarantined', execution_uid=NEXT_UID)
        with self.assertRaises(ControllerError): controller.tick()
        self.assertEqual(cloud.run_count, 1)

    def test_success_cannot_use_previous_execution_release(self):
        controller, _, cloud, broker, _, _ = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, succeededCount=1)
        self.assertEqual(broker.state['execution_uid'], PRIOR_UID)
        with self.assertRaises(ControllerError): controller.tick()
        self.assertEqual(cloud.run_count, 1)

    def test_uncertain_cas_before_launch_cannot_trigger_launch(self):
        controller, store, cloud, _, _, _ = self.make()
        original = store.cas
        def cas(state, version):
            result = original(state, version)
            if state['phase'] == 'launch_intent': raise OSError('offline CAS acknowledgement lost')
            return result
        store.cas = cas
        with self.assertRaises(OSError): controller.tick()
        self.assertEqual(controller.tick()['status'], 'launch_intent')
        self.assertEqual(cloud.run_count, 0)

    def test_changed_terminal_uid_cannot_use_release(self):
        controller, _, cloud, broker, _, _ = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(uid=PRIOR_UID,
            completionTime='2026-09-22T00:01:00Z', reconciling=False,
            runningCount=0, succeededCount=1)
        broker.state['execution_uid'] = PRIOR_UID
        with self.assertRaises(ControllerError): controller.tick()
        self.assertEqual(cloud.run_count, 1)

    def test_fast_clean_success_waits_for_durable_launch_interval(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:00:01Z',
            reconciling=False, runningCount=0, succeededCount=1)
        broker.state.update(execution_uid=NEXT_UID)
        self.assertEqual(controller.tick()['status'], 'replacement_cooldown')
        self.assertEqual(store.state['next_launch_at'], 1060)
        self.assertEqual(cloud.run_count, 1)
        # A different scheduler instance reads the same durable bound.
        clone = Controller(controller.policy, controller.slot, store, cloud, broker,
            binding_store=controller.bindings, grant_factory=controller.grant_factory,
            clock=lambda: 1059)
        self.assertEqual(clone.tick()['status'], 'replacement_cooldown')
        self.assertEqual(cloud.run_count, 1)

    def test_initial_unknown_prior_execution_does_not_bootstrap(self):
        controller, _, cloud, _, bindings, _ = self.make()
        cloud.job.pop('latestCreatedExecution')
        with self.assertRaises(ControllerError): controller.tick()
        self.assertEqual((bindings.calls, cloud.run_count), (0, 0))

    def test_project_id_resource_names_are_accepted(self):
        # Cloud Run echoes projects/<id>/... while profiles build projects/<number>/... (seen live 2026-09-22).
        controller, _, cloud, _, _, _ = self.make()
        id_form = lambda n: n.replace('projects/496481413971/', 'projects/project-0c6d31fa-509e-4116-a2c/', 1)
        cloud.job['name'] = id_form(POLICY.profile.job_name)
        cloud.executions_by_name[PRIOR]['name'] = id_form(PRIOR)
        original = cloud.get
        cloud.get = lambda name: original(name) if name in cloud.executions_by_name else deepcopy(cloud.job)
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')


class GrantStoreNameFormTests(unittest.TestCase):
    def test_record_accepts_either_echo_form_and_stores_the_canonical_one(self):
        from types import SimpleNamespace
        from agent_hub.credential_broker_service import BoundaryError, ExecutionGrantStore
        binding = POLICY.binding('a' * 64, 2000)
        store = ExecutionGrantStore(binding, rest=SimpleNamespace(), clock=lambda: 1000)
        id_form = NEXT.replace('projects/496481413971/', 'projects/project-0c6d31fa-509e-4116-a2c/', 1)
        self.assertEqual(store._record(id_form, NEXT_UID)['execution'], NEXT)
        self.assertEqual(store._record(NEXT, NEXT_UID)['execution'], NEXT)
        with self.assertRaises(BoundaryError):
            store._record('projects/496481413971/locations/us-central1/jobs/other-job/executions/other-job-abcde', NEXT_UID)


if __name__ == '__main__': unittest.main()
