"""Offline adversarial scheduler-delivery checks; no provider/cloud calls."""
from copy import deepcopy
from dataclasses import asdict, replace
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
        self.tasks = {}; self.task_error = None
        self.job = {'name': POLICY.profile.job_name, 'uid': JOB_UID, 'etag': 'offline-etag',
                    'template': {'offline': 'fixed-template'}, 'latestCreatedExecution': {'name': PRIOR}}
        self.executions_by_name = {PRIOR: {'name': PRIOR, 'uid': PRIOR_UID, 'taskCount': 1,
            'template': {'serviceAccount': POLICY.caller_service_account, 'maxRetries': 0},
            'completionTime': '2026-09-22T00:00:00Z', 'reconciling': False,
            'runningCount': 0, 'succeededCount': 1}}
    def get(self, name):
        if name.endswith('/tasks') or '/tasks/' in name:
            if self.task_error: raise self.task_error
            if name not in self.tasks: raise KeyError(name)
            return deepcopy(self.tasks[name])
        return deepcopy(self.job if name == self.job['name'] else self.executions_by_name[name])
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
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        broker.state.update(phase='leased', execution_uid=NEXT_UID)  # failed while holding it
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(cloud.run_count, 1)

    def fail_current(self, cloud, broker, uid=NEXT_UID):
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        broker.state.update(execution_uid=uid)  # the failed worker released cleanly

    def test_failure_after_clean_release_is_replaced_with_backoff(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        self.fail_current(cloud, broker)
        result = controller.tick()
        self.assertEqual(result['status'], 'replacement_after_failure')
        self.assertEqual(result['consecutive_failures'], 1)
        self.assertEqual(store.state['phase'], 'idle'); self.assertEqual(store.state['next_launch_at'], 1000 + 120)
        self.assertEqual(controller.tick()['status'], 'replacement_cooldown'); self.assertEqual(cloud.run_count, 1)
        controller.clock = lambda: 1200
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(cloud.run_count, 2)

    def test_failure_count_survives_a_relaunch_and_backoff_doubles(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        self.fail_current(cloud, broker)
        self.assertEqual(controller.tick()['consecutive_failures'], 1)
        controller.clock = lambda: 1200
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(store.state['consecutive_failures'], 1)
        self.fail_current(cloud, broker)
        result = controller.tick()
        self.assertEqual(result['consecutive_failures'], 2)
        self.assertEqual(store.state['next_launch_at'], 1200 + 240)

    def test_third_consecutive_failure_stops_the_slot(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        store.state['consecutive_failures'] = 2
        self.fail_current(cloud, broker)
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(store.state['error'], 'worker_failed_no_restart_loop')
        self.assertEqual(store.state['consecutive_failures'], 3); self.assertEqual(cloud.run_count, 1)

    def test_reset_after_three_strikes_grants_a_fresh_budget(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        store.state['consecutive_failures'] = 2
        self.fail_current(cloud, broker)
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(controller.reset()['status'], 'idle')
        self.assertEqual(store.state['consecutive_failures'], 0)

    def test_success_resets_the_failure_count(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        store.state['consecutive_failures'] = 2
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, succeededCount=1)
        broker.state.update(execution_uid=NEXT_UID)
        controller.tick()
        self.assertEqual(store.state['consecutive_failures'], 0)

    def test_failure_without_clean_release_stops_the_slot(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        self.fail_current(cloud, broker)
        broker.state.update(phase='leased')  # the failed worker still holds it
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(store.state['error'], 'worker_failed_credential_unreleased')
        self.assertEqual(cloud.run_count, 1)

    def blocked(self):
        """A slot stopped by a failure while holding the credential; the broker then releases."""
        controller, store, cloud, broker, bindings, grants = self.make(); controller.tick()
        broker.state.update(phase='leased', execution_uid=NEXT_UID)
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        self.assertEqual(controller.tick()['status'], 'blocked')
        broker.state.update(phase='idle', execution_uid=NEXT_UID)
        return controller, store, cloud, broker, bindings, grants

    def test_reset_returns_blocked_slot_to_idle_and_next_tick_replaces(self):
        controller, store, cloud, _, _, _ = self.blocked()
        result = controller.reset()
        self.assertEqual(result, {'status': 'idle', 'cleared': 'worker_failed_credential_unreleased', 'from_phase': 'blocked', 'generation': 1})
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

    def test_failure_before_acquiring_the_credential_is_retried(self):
        # The grant was published but the worker failed before acquiring: the
        # broker record still names the previous holder, so it never held it.
        controller, store, cloud, broker, _, grants = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        self.assertEqual(len(grants.published), 1); self.assertEqual(broker.state['execution_uid'], PRIOR_UID)
        self.assertEqual(controller.tick()['status'], 'replacement_after_failure')
        controller.clock = lambda: 1200
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')

    def test_release_naming_a_third_execution_is_refused(self):
        controller, store, cloud, broker, _, grants = self.make(); controller.tick()
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        broker.state.update(execution_uid='55555555-5555-5555-5555-555555555555')
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
                       {'fence': 9}, {'execution_uid': '55555555-5555-5555-5555-555555555555'}):
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

    def idled(self):
        """One clean launch, then back to idle while the launch interval still holds."""
        controller, store, cloud, broker, bindings, grants = self.make()
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:00:01Z',
            reconciling=False, runningCount=0, succeededCount=1)
        broker.state.update(execution_uid=NEXT_UID)
        self.assertEqual(controller.tick()['status'], 'replacement_cooldown')
        self.assertEqual(store.state['phase'], 'idle')
        return controller, store, cloud, broker, bindings, grants

    def roll_template(self, controller, cloud, template=None):
        template = {'offline': 'rolled-template'} if template is None else template
        cloud.job['template'] = template
        controller.slot['template_sha256'] = digest(template)
        return template

    def test_template_only_change_on_idle_rekeys_and_launches(self):
        controller, store, cloud, _, _, _ = self.idled()
        previous = store.state['slot_template_sha256']
        self.roll_template(controller, cloud)
        self.assertNotEqual(controller.config_sha, store.state['config_sha256'])
        self.assertEqual(previous, digest({'offline': 'fixed-template'}))
        controller.clock = lambda: 2000
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(store.state['config_sha256'], controller.config_sha)
        self.assertEqual(store.state['slot_template_sha256'], digest(cloud.job['template']))
        self.assertEqual(store.state['policy_sha256'], digest(asdict(controller.policy)))
        self.assertEqual(cloud.run_count, 2)

    def test_policy_or_job_uid_change_is_not_a_rekey(self):
        cases = (
            ('policy', lambda controller: setattr(controller, 'policy', replace(controller.policy, lease_seconds=180))),
            ('job_uid', lambda controller: controller.slot.update(job_uid='44444444-4444-4444-4444-444444444444')),
        )
        for name, change in cases:
            with self.subTest(change=name):
                controller, store, cloud, _, _, _ = self.idled()
                bound = store.state['config_sha256']
                change(controller)
                controller.clock = lambda: 2000
                with self.assertRaises(ControllerError) as caught:
                    controller.tick()
                self.assertEqual(str(caught.exception), 'controller_config_changed')
                self.assertEqual(store.state['config_sha256'], bound)
                self.assertEqual(store.state['phase'], 'idle')
                self.assertEqual(cloud.run_count, 1)

    def test_template_change_while_active_waits_until_terminal(self):
        controller, store, cloud, broker, _, _ = self.make()
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        bound = store.state['config_sha256']
        self.roll_template(controller, cloud)
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        self.assertEqual(store.state['phase'], 'active')
        self.assertEqual(store.state['config_sha256'], bound)
        self.assertEqual(cloud.run_count, 1)
        # Still running: a second delivery does not adopt the digest either.
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:00:01Z',
            reconciling=False, runningCount=0, succeededCount=1)
        broker.state.update(execution_uid=NEXT_UID)
        controller.clock = lambda: 2000
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(store.state['config_sha256'], controller.config_sha)
        self.assertEqual(store.state['slot_template_sha256'], digest(cloud.job['template']))
        self.assertEqual(cloud.run_count, 2)

    def test_template_change_mid_launch_is_refused(self):
        controller, store, cloud, _, _, grants = self.make()
        grants.lose_reply = True
        with self.assertRaises(OSError):
            controller.tick()
        self.assertEqual(store.state['phase'], 'grant_intent')
        bound = store.state['config_sha256']
        self.roll_template(controller, cloud)
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        self.assertEqual(store.state['phase'], 'grant_intent')
        self.assertEqual(store.state['config_sha256'], bound)
        with self.assertRaises(ControllerError) as caught:
            controller.reset()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        self.assertEqual(store.state['config_sha256'], bound)
        self.assertEqual(cloud.run_count, 1)

    def test_blocked_template_change_rekeys_without_launching(self):
        controller, store, cloud, _, _, _ = self.blocked()
        self.roll_template(controller, cloud)
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(store.state['phase'], 'blocked')
        self.assertEqual(store.state['config_sha256'], controller.config_sha)
        self.assertEqual(cloud.run_count, 1)
        self.assertEqual(controller.reset()['status'], 'idle')
        self.assertEqual(store.state['config_sha256'], controller.config_sha)
        controller.clock = lambda: 2000
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(cloud.run_count, 2)

    def _legacy_state(self, controller, store):
        """State shape written before policy_sha256 and the slot fields were stored."""
        store.state = {'schema_version': 1, 'config_sha256': controller.config_sha, 'phase': 'idle',
                       'generation': 0, 'updated_at': 1}
        store.version = 1

    def test_legacy_state_rekeys_from_the_job_template_then_launches(self):
        # Slot digest moves first. The live job still has the previous template,
        # which is the only proof a config_sha256-only document has.
        controller, store, cloud, _, _, _ = self.make()
        self._legacy_state(controller, store)
        self.assertNotIn('slot_template_sha256', store.state)
        new_template = {'offline': 'rolled-template'}
        controller.slot['template_sha256'] = digest(new_template)
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'job_template_changed')
        self.assertEqual(store.state['config_sha256'], controller.config_sha)
        self.assertEqual(store.state['slot_template_sha256'], digest(new_template))
        self.assertEqual(store.state['policy_sha256'], digest(asdict(controller.policy)))
        self.assertEqual(cloud.run_count, 0)
        # The re-key already committed. The image update is what the next tick launches.
        cloud.job['template'] = new_template
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(digest(cloud.job['template']), controller.slot['template_sha256'])
        self.assertEqual(cloud.run_count, 1)

    def test_legacy_state_records_the_previous_template_before_the_digest_changes(self):
        # Image rolled, fleet.json not yet. One tick persists the slot's current
        # (previous) template, then refuses the job. Recording the new digest
        # after that is a normal re-key and launches.
        controller, store, cloud, _, _, _ = self.make()
        self._legacy_state(controller, store)
        previous = controller.slot['template_sha256']
        bound = controller.config_sha
        cloud.job['template'] = {'offline': 'rolled-template'}
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'job_template_changed')
        self.assertEqual(store.state['slot_template_sha256'], previous)
        self.assertEqual(store.state['config_sha256'], bound)
        self.assertEqual(store.state['policy_sha256'], digest(asdict(POLICY)))
        self.assertEqual(cloud.run_count, 0)
        controller.slot['template_sha256'] = digest(cloud.job['template'])
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(store.state['config_sha256'], controller.config_sha)
        self.assertEqual(store.state['slot_template_sha256'], digest(cloud.job['template']))
        self.assertEqual(cloud.run_count, 1)

    def _strip_binding(self, store):
        for key in ('policy_sha256', 'slot_job_uid', 'slot_enabled', 'slot_template_sha256'):
            store.state.pop(key, None)

    def test_legacy_policy_and_template_change_is_refused_while_job_has_previous_template(self):
        # The live job still shows the previous template, so a template-only
        # re-key would succeed. A policy edit in the same change must not.
        controller, store, cloud, _, _, _ = self.make()
        self._legacy_state(controller, store)
        before, version = deepcopy(store.state), store.version
        controller.policy = replace(controller.policy, lease_seconds=180)
        controller.slot['template_sha256'] = digest({'offline': 'rolled-template'})
        self.assertEqual(digest(cloud.job['template']), digest({'offline': 'fixed-template'}))
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        self.assertEqual(store.state, before)
        self.assertEqual(store.version, version)
        self.assertEqual(store.archives, [])
        self.assertEqual(cloud.run_count, 0)

    def test_job_uid_and_template_change_is_refused_without_split_fields(self):
        # Legacy document, and a new-format document with the split fields
        # removed, while the job still has the previous template.
        cases = ('legacy', 'stripped')
        for name in cases:
            with self.subTest(shape=name):
                controller, store, cloud, _, _, _ = self.make()
                if name == 'legacy':
                    self._legacy_state(controller, store)
                else:
                    self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
                    cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:00:01Z',
                        reconciling=False, runningCount=0, succeededCount=1)
                    controller.broker.state.update(execution_uid=NEXT_UID)
                    self.assertEqual(controller.tick()['status'], 'replacement_cooldown')
                    self._strip_binding(store)
                before, version = deepcopy(store.state), store.version
                archives, runs = deepcopy(store.archives), cloud.run_count
                controller.slot['template_sha256'] = digest({'offline': 'rolled-template'})
                controller.slot['job_uid'] = '44444444-4444-4444-4444-444444444444'
                self.assertEqual(digest(cloud.job['template']), digest({'offline': 'fixed-template'}))
                with self.assertRaises(ControllerError) as caught:
                    controller.tick()
                self.assertEqual(str(caught.exception), 'controller_config_changed')
                self.assertEqual(store.state, before)
                self.assertEqual(store.version, version)
                self.assertEqual(store.archives, archives)
                self.assertEqual(cloud.run_count, runs)

    def test_policy_change_on_terminal_active_does_not_archive_or_write(self):
        controller, store, cloud, broker, _, _ = self.make()
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:00:01Z',
            reconciling=False, runningCount=0, succeededCount=1)
        broker.state.update(execution_uid=NEXT_UID)
        controller.policy = replace(controller.policy, lease_seconds=180)
        before, version = deepcopy(store.state), store.version
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        self.assertEqual(store.state, before)
        self.assertEqual(store.state['phase'], 'active')
        self.assertEqual(store.version, version)
        self.assertEqual(store.archives, [])
        self.assertEqual(cloud.run_count, 1)

    def test_reset_refuses_a_policy_change(self):
        controller, store, cloud, _, _, _ = self.blocked()
        before, version = deepcopy(store.state), store.version
        controller.policy = replace(controller.policy, lease_seconds=180)
        with self.assertRaises(ControllerError) as caught:
            controller.reset()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        self.assertEqual(store.state, before)
        self.assertEqual(store.version, version)
        self.assertEqual(store.archives, [])
        self.assertEqual(cloud.run_count, 1)

    def test_active_match_does_not_rewrite_until_the_drain(self):
        # A legacy active document keeps its bytes while the execution runs.
        # The drain archives that receipt, then the idle pass of the same tick
        # records the split fields.
        controller, store, cloud, broker, _, _ = self.make()
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self._strip_binding(store)
        before, version = deepcopy(store.state), store.version
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(store.state, before)
        self.assertEqual(store.version, version)
        self.assertEqual(store.archives, [])
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:00:01Z',
            reconciling=False, runningCount=0, succeededCount=1)
        broker.state.update(execution_uid=NEXT_UID)
        self.assertEqual(controller.tick()['status'], 'replacement_cooldown')
        self.assertEqual(store.state['phase'], 'idle')
        self.assertEqual(store.state['policy_sha256'], digest(asdict(controller.policy)))
        self.assertEqual(store.state['slot_template_sha256'], controller.slot['template_sha256'])
        self.assertEqual(len(store.archives), 1)
        archived, _ = store.archives[0]
        self.assertEqual(archived['phase'], 'active')
        self.assertNotIn('policy_sha256', archived)

    def test_legacy_policy_change_and_unprovable_template_change_are_refused(self):
        controller, store, cloud, _, _, _ = self.make()
        self._legacy_state(controller, store)
        before = deepcopy(store.state)
        controller.policy = replace(controller.policy, lease_seconds=180)
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        self.assertEqual(store.state, before)
        self.assertEqual(cloud.run_count, 0)
        # Both the image and the slot digest already moved, and nothing in the
        # document remembers the previous template: indistinguishable from a
        # policy edit, so it stays refused.
        controller, store, cloud, _, _, _ = self.make()
        self._legacy_state(controller, store)
        before = deepcopy(store.state)
        self.roll_template(controller, cloud)
        with self.assertRaises(ControllerError) as caught:
            controller.tick()
        self.assertEqual(str(caught.exception), 'controller_config_changed')
        self.assertEqual(store.state, before)
        self.assertEqual(cloud.run_count, 0)


    def park_quota(self, cloud, broker, code=75, tasks=None):
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
            reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
        broker.state.update(execution_uid=NEXT_UID)
        if tasks is None:
            tasks = {'tasks': [{'name': NEXT + '/tasks/task0', 'lastAttemptResult': {'exitCode': code}}]}
        cloud.task_error = None
        cloud.tasks = {NEXT + '/tasks': tasks}

    def test_quota_exit_parks_without_burning_strikes_and_backs_off(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        store.state['consecutive_failures'] = 2
        self.park_quota(cloud, broker)
        result = controller.tick()
        self.assertEqual(result['status'], 'provider_quota_parked')
        self.assertEqual(result['quota_parks'], 1)
        self.assertEqual(result['next_launch_at'], 1000 + 3600)
        self.assertEqual(store.state['consecutive_failures'], 2)
        self.assertEqual(store.state['phase'], 'idle')
        self.assertEqual(store.state['error'], 'provider_quota_exhausted')
        self.assertEqual(cloud.run_count, 1)
        # Still inside the park: same status, no relaunch.
        again = controller.tick()
        self.assertEqual(again['status'], 'provider_quota_parked')
        self.assertEqual(again['next_launch_at'], 1000 + 3600)
        self.assertEqual(cloud.run_count, 1)
        # 1h, 2h, 4h, 4h. Each park waits out the previous delay, relaunches, and fails quota again.
        delays = (7200, 14400, 14400)
        clock = 1000 + 3600
        for index, delay in enumerate(delays, start=2):
            controller.clock = lambda now=clock: now
            self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
            self.park_quota(cloud, broker)
            result = controller.tick()
            self.assertEqual(result['status'], 'provider_quota_parked')
            self.assertEqual(result['quota_parks'], index)
            self.assertEqual(result['next_launch_at'], clock + delay)
            self.assertEqual(store.state['consecutive_failures'], 2)
            self.assertEqual(cloud.run_count, index)
            clock = result['next_launch_at']

    def test_quota_then_success_clears_the_park(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        self.park_quota(cloud, broker)
        self.assertEqual(controller.tick()['quota_parks'], 1)
        controller.clock = lambda: 1000 + 3600
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T02:00:00Z',
            reconciling=False, runningCount=0, succeededCount=1, failedCount=0)
        broker.state.update(execution_uid=NEXT_UID)
        controller.tick()
        self.assertEqual(store.state['quota_parks'], 0)
        self.assertNotIn('error', store.state)
        self.assertEqual(store.state['consecutive_failures'], 0)

    def test_quota_exit_with_unreleased_credential_blocks(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        self.park_quota(cloud, broker)
        broker.state.update(phase='leased')
        self.assertEqual(controller.tick()['status'], 'blocked')
        self.assertEqual(store.state['error'], 'worker_failed_credential_unreleased')
        self.assertEqual(store.state.get('quota_parks', 0), 0)
        self.assertEqual(store.state['consecutive_failures'], 1)
        self.assertEqual(cloud.run_count, 1)

    def test_unproved_task_exit_keeps_the_strike_path(self):
        cases = {
            'raises': {'error': OSError('offline tasks denied')},
            'two tasks': {'tasks': {'tasks': [
                {'lastAttemptResult': {'exitCode': 75}}, {'lastAttemptResult': {'exitCode': 75}}]}},
            'missing exitCode': {'tasks': {'tasks': [{'lastAttemptResult': {}}]}},
            'exit 1': {'tasks': {'tasks': [{'lastAttemptResult': {'exitCode': 1}}]}},
        }
        for name, setup in cases.items():
            with self.subTest(case=name):
                controller, store, cloud, broker, _, _ = self.make(); controller.tick()
                cloud.executions_by_name[NEXT].update(completionTime='2026-09-22T00:01:00Z',
                    reconciling=False, runningCount=0, failedCount=1, succeededCount=0)
                broker.state.update(execution_uid=NEXT_UID)
                cloud.task_error = setup.get('error')
                cloud.tasks = {} if 'tasks' not in setup else {NEXT + '/tasks': setup['tasks']}
                result = controller.tick()
                self.assertEqual(result['status'], 'replacement_after_failure')
                self.assertEqual(result['consecutive_failures'], 1)
                self.assertEqual(store.state['next_launch_at'], 1000 + 120)
                self.assertNotIn('quota_parks', store.state)
                self.assertNotIn('error', store.state)
                self.assertEqual(cloud.run_count, 1)

    def test_reset_on_a_parked_slot_clears_the_park(self):
        controller, store, cloud, broker, _, _ = self.make(); controller.tick()
        self.park_quota(cloud, broker)
        self.assertEqual(controller.tick()['status'], 'provider_quota_parked')
        result = controller.reset()
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(result['cleared'], 'provider_quota_exhausted')
        self.assertEqual(store.state['quota_parks'], 0)
        self.assertNotIn('error', store.state)
        self.assertNotIn('next_launch_at', store.state)
        self.assertEqual(store.state['consecutive_failures'], 0)
        # The hour has not elapsed; the park is gone, so the next tick launches.
        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')
        self.assertEqual(cloud.run_count, 2)


class CloudTaskNameTests(unittest.TestCase):
    def test_name_pattern_allows_tasks_and_refuses_anything_wider(self):
        from agent_hub.credential_broker_service import BoundaryError
        from cloud_runtime import Google
        google = Google(POLICY)
        seen = []
        google.request = lambda host, path, method='GET', body=None: seen.append((host, path)) or {'tasks': []}
        execution = POLICY.profile.job_name + '/executions/' + POLICY.profile.job_id + '-abcde'
        allowed = [
            POLICY.profile.job_name,
            execution,
            execution + '/tasks',
            execution + '/tasks/task-0',
            'projects/496481413971/locations/us-central1/operations/offline-operation',
            'projects/project-0c6d31fa-509e-4116-a2c/locations/us-central1/jobs/runcrew-worker-grok-blueeyes/executions/runcrew-worker-grok-blueeyes-abcde/tasks/task-0',
        ]
        for name in allowed:
            with self.subTest(allowed=name):
                seen.clear()
                self.assertEqual(google.get(name), {'tasks': []})
                self.assertEqual(seen, [('run.googleapis.com', '/v2/' + name)])
        refused = [
            execution + '/tasks/task-0/logs',
            execution + '/tasks/task-0/extra',
            execution + '/tasks/task-0/task-1',
            execution + '/tasks/',
            execution + '/containers',
            POLICY.profile.job_name + '/tasks',
            POLICY.profile.job_name + '/executions',
            execution + '/tasks/task-0?pageSize=1',
            'projects/other/locations/us-central1/jobs/job/executions/exec/tasks',
            execution + '/tasks/../secrets',
        ]
        for name in refused:
            with self.subTest(refused=name):
                with self.assertRaises(BoundaryError) as caught:
                    google.get(name)
                self.assertEqual(str(caught.exception), 'cloud_resource_not_allowed')


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
