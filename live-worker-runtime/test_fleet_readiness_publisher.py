"""Offline tests for advisory fleet readiness publishing (T41 / C2 controller side)."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
HUB = Path(__file__).resolve().parents[2] / 'runcrew'
SOURCE = Path('C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HUB))
sys.path.insert(0, str(SOURCE))

# The cca checkout expects a newer cloud_credential_broker (UpstreamUnavailable,
# UPSTREAM_*). The workspace runcrew hub is an older post-deploy snapshot; the
# absolute SOURCE path from the original packager is absent here. Patch the
# missing symbols so offline controller tests can import.
import agent_hub.cloud_credential_broker as _ccb  # noqa: E402
if not hasattr(_ccb, 'UpstreamUnavailable'):
    class UpstreamUnavailable(_ccb.BrokerError):
        pass
    _ccb.UpstreamUnavailable = UpstreamUnavailable
if not hasattr(_ccb, 'UPSTREAM_BUDGET_SECONDS'):
    _ccb.UPSTREAM_BUDGET_SECONDS = 8
if not hasattr(_ccb, 'UPSTREAM_HTTP_STATUSES'):
    _ccb.UPSTREAM_HTTP_STATUSES = frozenset({502, 503, 504})

from fleet_controller import Controller, digest, map_readiness_phase, execution_started  # noqa: E402
from fleet_readiness_publisher import (  # noqa: E402
    AGENT_ENTRY_KEYS, DOCUMENT_KEYS, FORBIDDEN_PAYLOAD_KEYS, FleetReadinessPublisher,
    agent_readiness_entry, build_readiness_document, document_from_slots,
    publish_after_tick, ttl_for_tick_interval,
)
from test_dynamic_broker import POLICY  # noqa: E402

PRIOR_UID = '11111111-1111-1111-1111-111111111111'
NEXT_UID = '22222222-2222-2222-2222-222222222222'
JOB_UID = '33333333-3333-3333-3333-333333333333'
PRIOR = POLICY.profile.job_name + '/executions/' + POLICY.profile.job_id + '-prior1'
NEXT = POLICY.profile.job_name + '/executions/' + POLICY.profile.job_id + '-next01'


class Store:
    def __init__(self):
        self.state = None
        self.version = 0
        self.archives = []

    def read(self):
        return deepcopy(self.state), self.version if self.state else None

    def cas(self, state, version):
        if version != (self.version if self.state else None):
            raise RuntimeError('offline CAS conflict')
        self.state = deepcopy(state)
        self.version += 1
        return self.version

    def archive(self, state, execution):
        self.archives.append((deepcopy(state), deepcopy(execution)))


class Cloud:
    def __init__(self):
        self.run_count = 0
        self.on_run = lambda: None
        self.lose_run_reply = False
        self.tasks = {}
        self.task_error = None
        self.job = {'name': POLICY.profile.job_name, 'uid': JOB_UID, 'etag': 'offline-etag',
                    'template': {'offline': 'fixed-template'}, 'latestCreatedExecution': {'name': PRIOR}}
        self.executions_by_name = {PRIOR: {'name': PRIOR, 'uid': PRIOR_UID, 'taskCount': 1,
            'template': {'serviceAccount': POLICY.caller_service_account, 'maxRetries': 0},
            'completionTime': '2026-09-22T00:00:00Z', 'reconciling': False,
            'runningCount': 0, 'succeededCount': 1}}

    def get(self, name):
        if name.endswith('/tasks') or '/tasks/' in name:
            if self.task_error:
                raise self.task_error
            if name not in self.tasks:
                raise KeyError(name)
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
        if self.lose_run_reply:
            raise OSError('offline lost launch acknowledgement')
        return {'name': 'projects/496481413971/locations/us-central1/operations/offline-operation'}

    def executions(self, job, intent):
        return [deepcopy(self.executions_by_name[NEXT])]


class Broker:
    def __init__(self):
        self.state = {'phase': 'idle', 'quarantine_reason': '', 'version': 'version-1',
                      'last_release_version': 'version-1', 'fence': 8,
                      'last_release_fence': 8, 'execution_uid': PRIOR_UID}

    def _read(self):
        return deepcopy(self.state), 'offline-version'


class Bindings:
    def __init__(self):
        self.calls = 0

    def publish(self, binding):
        self.calls += 1


class Grants:
    def __init__(self):
        self.calls = 0
        self.lose_reply = False
        self.refuse = 0
        self.published = []

    def factory(self, binding):
        return self

    def read(self):
        return self.published[-1] if self.published else None

    def publish(self, execution, uid):
        self.calls += 1
        if self.lose_reply:
            raise OSError('offline lost publication acknowledgement')
        if self.refuse:
            self.refuse -= 1
            raise RuntimeError('execution_not_authorized')
        self.published.append((execution, uid))


class FakeHubClient:
    """Records POSTs; can be told to fail."""

    def __init__(self):
        self.calls = []
        self.fail_with = None

    def __call__(self, hub_url, token, document, *, timeout=2.5):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append({
            'hub_url': hub_url,
            'token': token,
            'document': deepcopy(document),
            'timeout': timeout,
            'path': '/v1/fleet/readiness',
        })
        return None


class PhaseMappingTests(unittest.TestCase):
    """The phase mapping table: slot phase + created/started → readiness phase."""

    TABLE = [
        # (slot_phase, execution, expected_readiness)
        ('idle', None, 'idle'),
        ('idle', {'runningCount': 0}, 'idle'),
        ('binding_intent', None, 'launching'),
        ('binding_ready', None, 'launching'),
        ('launch_intent', None, 'launching'),
        ('launch_submitted', None, 'pending_start'),
        ('launch_submitted', {'uid': NEXT_UID, 'runningCount': 0}, 'starting'),
        ('launch_submitted', {'uid': NEXT_UID, 'runningCount': 1}, 'starting'),
        ('grant_intent', {'uid': NEXT_UID, 'runningCount': 0}, 'starting'),
        ('active', {'uid': NEXT_UID, 'runningCount': 0}, 'starting'),
        ('active', {'uid': NEXT_UID, 'startTime': ''}, 'starting'),
        ('active', {'uid': NEXT_UID, 'runningCount': 1}, 'ready'),
        ('active', {'uid': NEXT_UID, 'startTime': '2026-09-26T12:00:00Z'}, 'ready'),
        ('active', None, 'ready'),
        ('blocked', None, 'blocked'),
        ('blocked', {'runningCount': 1}, 'blocked'),
        ('not_a_phase', None, 'unknown'),
        ('', None, 'unknown'),
    ]

    def test_phase_mapping_table(self):
        for slot_phase, execution, expected in self.TABLE:
            with self.subTest(slot_phase=slot_phase, execution=execution):
                self.assertEqual(map_readiness_phase(slot_phase, execution), expected)

    def test_execution_started_helper(self):
        self.assertFalse(execution_started(None))
        self.assertFalse(execution_started({'runningCount': 0}))
        self.assertTrue(execution_started({'runningCount': 1}))
        self.assertTrue(execution_started({'startTime': '2026-09-26T12:00:00Z'}))


class PayloadContractTests(unittest.TestCase):
    def test_document_exact_keys_and_bounds(self):
        doc = build_readiness_document(
            {
                'codex': {
                    'phase': 'idle',
                    'since': 1000,
                    'reason_code': None,
                    'execution': None,
                },
                'claude': {
                    'phase': 'ready',
                    'since': 1001,
                    'reason_code': 'provider_quota_exhausted',
                    'execution': NEXT_UID,
                },
            },
            published_at=2000,
            ttl_seconds=180,
        )
        self.assertEqual(set(doc), DOCUMENT_KEYS)
        self.assertEqual(doc['schema'], 1)
        self.assertEqual(doc['published_at'], 2000)
        self.assertEqual(doc['ttl_seconds'], 180)
        for entry in doc['agents'].values():
            self.assertEqual(set(entry), AGENT_ENTRY_KEYS)

    def test_ttl_is_three_times_tick_capped_at_600(self):
        self.assertEqual(ttl_for_tick_interval(30), 90)
        self.assertEqual(ttl_for_tick_interval(60), 180)
        self.assertEqual(ttl_for_tick_interval(200), 600)
        self.assertEqual(ttl_for_tick_interval(300), 600)

    def test_rejects_extra_top_level_or_agent_fields_via_builder(self):
        # build_readiness_document only accepts the contract keys per agent.
        with self.assertRaises(ValueError):
            build_readiness_document(
                {'codex': {'phase': 'idle', 'since': 1, 'reason_code': None,
                           'execution': None, 'grant': 'secret'}},
                published_at=1, ttl_seconds=30,
            )

    def test_no_secret_or_grant_field_in_payload(self):
        state = {
            'phase': 'active',
            'updated_at': 1000,
            'grant': 'super-secret-grant-value',
            'grant_sha256': 'a' * 64,
            'execution': NEXT,
            'execution_uid': NEXT_UID,
            'error': 'worker_failed_credential_unreleased',
        }
        doc = document_from_slots(
            {'codex': state},
            executions={'codex': {'uid': NEXT_UID, 'runningCount': 1}},
            published_at=1000,
            ttl_seconds=90,
        )
        blob = json.dumps(doc)
        for key in ('grant', 'grant_sha256', 'super-secret-grant-value', 'token', 'password'):
            self.assertNotIn(key, blob)
        self.assertNotIn('grant', doc['agents']['codex'])
        # reason_code may mention credential_unreleased; that is a short code, not a secret.
        self.assertEqual(doc['agents']['codex']['reason_code'], 'worker_failed_credential_unreleased')
        self.assertEqual(doc['agents']['codex']['execution'], NEXT_UID)
        self.assertEqual(set(doc['agents']['codex']), AGENT_ENTRY_KEYS)
        for forbidden in FORBIDDEN_PAYLOAD_KEYS:
            self.assertNotIn(forbidden, doc)
            self.assertNotIn(forbidden, doc['agents']['codex'])

    def test_agent_entry_from_state_strips_secrets(self):
        entry = agent_readiness_entry({
            'phase': 'blocked',
            'updated_at': 50,
            'grant': 'leak',
            'error': 'launch_without_execution',
        })
        self.assertEqual(entry, {
            'phase': 'blocked',
            'since': 50,
            'reason_code': 'launch_without_execution',
            'execution': None,
        })


class PublisherBehaviourTests(unittest.TestCase):
    def test_flag_off_sends_nothing(self):
        hub = FakeHubClient()
        publisher = FleetReadinessPublisher(
            enabled=False, hub_url='http://127.0.0.1:9', token='tok', post=hub, clock=lambda: 1000,
        )
        self.assertIsNone(publisher.publish_slots({'codex': {'phase': 'idle', 'updated_at': 1}}))
        self.assertEqual(hub.calls, [])

    def test_absent_hub_url_or_token_sends_nothing(self):
        hub = FakeHubClient()
        for kwargs in (
            {'enabled': True, 'hub_url': '', 'token': 'tok'},
            {'enabled': True, 'hub_url': 'http://127.0.0.1:9', 'token': ''},
        ):
            publisher = FleetReadinessPublisher(post=hub, clock=lambda: 1000, **kwargs)
            self.assertIsNone(publisher.publish_slots({'codex': {'phase': 'idle', 'updated_at': 1}}))
        self.assertEqual(hub.calls, [])

    def test_from_env_off_by_default(self):
        hub = FakeHubClient()
        publisher = FleetReadinessPublisher.from_env(
            environ={'RUNCREW_HUB_URL': 'http://127.0.0.1:9',
                     'RUNCREW_HUB_CONTROLLER_TOKEN': 'tok'},
            post=hub, clock=lambda: 1000,
        )
        self.assertFalse(publisher.active())
        publisher.publish_slots({'codex': {'phase': 'idle', 'updated_at': 1}})
        self.assertEqual(hub.calls, [])

    def test_from_env_enabled(self):
        hub = FakeHubClient()
        publisher = FleetReadinessPublisher.from_env(
            environ={
                'RUNCREW_FLEET_READINESS_PUBLISH': '1',
                'RUNCREW_HUB_URL': 'http://127.0.0.1:9',
                'RUNCREW_HUB_CONTROLLER_TOKEN': 'tok',
                'RUNCREW_TICK_SECONDS': '60',
            },
            post=hub, clock=lambda: 1000,
        )
        self.assertTrue(publisher.active())
        doc = publisher.publish_slots({
            'codex': {'phase': 'idle', 'updated_at': 10},
            'claude': {'phase': 'blocked', 'updated_at': 11, 'error': 'launch_without_execution'},
        })
        self.assertEqual(len(hub.calls), 1)
        self.assertEqual(hub.calls[0]['document'], doc)
        self.assertEqual(doc['ttl_seconds'], 180)
        self.assertEqual(set(doc['agents']), {'codex', 'claude'})

    def test_one_post_per_tick_with_fake_hub_client(self):
        hub = FakeHubClient()
        publisher = FleetReadinessPublisher(
            enabled=True, hub_url='http://127.0.0.1:9', token='controller-token',
            tick_seconds=60, post=hub, clock=lambda: 5000,
        )
        store, cloud, broker, bindings, grants = Store(), Cloud(), Broker(), Bindings(), Grants()
        slot = {'job_uid': JOB_UID, 'template_sha256': digest(cloud.job['template']), 'enabled': True}
        controller = Controller(
            POLICY, slot, store, cloud, broker, binding_store=bindings,
            grant_factory=grants.factory, clock=lambda: 1000, readiness_publisher=publisher,
        )
        result = controller.tick()
        self.assertEqual(result['status'], 'job_running_readiness_separate')
        self.assertEqual(len(hub.calls), 1)
        doc = hub.calls[0]['document']
        self.assertEqual(set(doc), DOCUMENT_KEYS)
        self.assertEqual(doc['schema'], 1)
        self.assertIn(POLICY.profile.provider, doc['agents'])
        agent = doc['agents'][POLICY.profile.provider]
        self.assertEqual(set(agent), AGENT_ENTRY_KEYS)
        self.assertEqual(agent['phase'], 'starting')
        self.assertEqual(agent['execution'], NEXT_UID)
        blob = json.dumps(doc)
        self.assertNotIn('grant', blob)
        self.assertNotIn(store.state.get('grant') or 'no-grant-present', blob)

    def test_post_failure_leaves_tick_result_unchanged(self):
        hub = FakeHubClient()
        hub.fail_with = URLError('offline hub down')
        logs = []
        publisher = FleetReadinessPublisher(
            enabled=True, hub_url='http://127.0.0.1:9', token='tok',
            post=hub, clock=lambda: 1000, log=logs.append,
        )
        store, cloud, broker, bindings, grants = Store(), Cloud(), Broker(), Bindings(), Grants()
        slot = {'job_uid': JOB_UID, 'template_sha256': digest(cloud.job['template']), 'enabled': True}
        controller = Controller(
            POLICY, slot, store, cloud, broker, binding_store=bindings,
            grant_factory=grants.factory, clock=lambda: 1000, readiness_publisher=publisher,
        )
        result = controller.tick()
        self.assertEqual(result['status'], 'job_running_readiness_separate')
        self.assertEqual(store.state['phase'], 'active')
        self.assertEqual(cloud.run_count, 1)
        self.assertEqual(publisher.failures['count'], 1)
        self.assertEqual(len(logs), 1)
        event = json.loads(logs[0])
        self.assertEqual(event['kind'], 'runcrew_fleet_readiness_publish')
        self.assertEqual(event['status'], 'failed')
        self.assertEqual(event['reason'], 'hub_unreachable')
        self.assertNotIn('tok', logs[0])

    def test_http_error_is_counted_sanitized(self):
        hub = FakeHubClient()
        hub.fail_with = HTTPError('http://127.0.0.1:9/v1/fleet/readiness', 500, 'err', hdrs=None, fp=None)
        logs = []
        publisher = FleetReadinessPublisher(
            enabled=True, hub_url='http://127.0.0.1:9', token='secret-token',
            post=hub, log=logs.append, clock=lambda: 1,
        )
        publisher.publish_slots({'codex': {'phase': 'idle', 'updated_at': 1}})
        self.assertEqual(publisher.failures['count'], 1)
        self.assertNotIn('secret-token', logs[0])

    def test_publish_after_tick_one_document_for_all_slots(self):
        hub = FakeHubClient()
        publisher = FleetReadinessPublisher(
            enabled=True, hub_url='http://127.0.0.1:9', token='tok',
            tick_seconds=40, post=hub, clock=lambda: 9000,
        )
        # Two slot snapshots, one POST.
        doc = publish_after_tick(
            [_FakeController('codex', {'phase': 'idle', 'updated_at': 1}),
             _FakeController('claude', {'phase': 'blocked', 'updated_at': 2,
                                        'error': 'launch_without_execution'})],
            publisher,
        )
        self.assertEqual(len(hub.calls), 1)
        self.assertEqual(hub.calls[0]['document']['ttl_seconds'], 120)
        self.assertEqual(set(doc['agents']), {'codex', 'claude'})
        self.assertEqual(doc['agents']['codex']['phase'], 'idle')
        self.assertEqual(doc['agents']['claude']['phase'], 'blocked')


class _FakeController:
    def __init__(self, provider, state):
        self.policy = type('P', (), {'profile': type('F', (), {'provider': provider})()})()
        self.store = type('S', (), {'read': lambda self: (deepcopy(state), 1)})()
        self.cloud = type('C', (), {'get': lambda self, name: None})()


if __name__ == '__main__':
    unittest.main()
