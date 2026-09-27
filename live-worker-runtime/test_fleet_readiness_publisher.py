"""Offline tests for advisory fleet readiness publishing (T41 / T65 / C2 controller side)."""
from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import ast
from dataclasses import asdict
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
SOURCE = Path('C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SOURCE))

import fleet_readiness_publisher as frp  # noqa: E402
from fleet_controller import Controller, digest, map_readiness_phase, execution_started  # noqa: E402
from fleet_readiness_publisher import (  # noqa: E402
    AGENT_ENTRY_KEYS, DOCUMENT_KEYS, FORBIDDEN_PAYLOAD_KEYS, EXECUTION_ID, MAX_REASON_CODE,
    REASON_CODE, FleetReadinessPublisher, NoRedirect, agent_readiness_entry,
    build_readiness_document, document_from_slots, publish_after_tick,
    ttl_for_tick_interval,
)
from test_dynamic_broker import POLICY  # noqa: E402

# Both cca/agent-hub and runcrew ship package name agent_hub; putting runcrew
# first breaks worker imports (UpstreamUnavailable etc.). Import by loading the
# runcrew module file under an isolated package name.
HUB_VALIDATE_SOURCE = ''
HUB_VALIDATE_ROOT = None
HUB_STUB_AGENTS = None


def _load_hub_validate_document():
    global HUB_VALIDATE_SOURCE, HUB_VALIDATE_ROOT, HUB_STUB_AGENTS
    import importlib.util
    import types
    # Same convention as test_workspace_runner: RUNCREW_AGENT_HUB is runcrew's
    # source/agent-hub; a sibling runcrew/ is the offline tarball layout.
    roots = [os.environ.get('RUNCREW_AGENT_HUB'), Path(__file__).resolve().parents[2] / 'runcrew']
    runcrew_ah = next((Path(r) / 'agent_hub' for r in roots
                       if r and (Path(r) / 'agent_hub' / 'fleet_readiness.py').is_file()), None)
    if runcrew_ah is None:
        return None
    pkg_name = '_t65_runcrew_agent_hub'
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(runcrew_ah)]
        sys.modules[pkg_name] = pkg
    # Minimal core stub: fleet_readiness only needs AGENTS and HubError.
    core_name = pkg_name + '.core'
    if core_name not in sys.modules:
        core = types.ModuleType(core_name)

        class HubError(Exception):
            def __init__(self, message, status=400):
                Exception.__init__(self, message)
                self.status = status

        core.HubError = HubError
        core.AGENTS = ('codex', 'claude', 'cursor', 'copilot', 'grok')
        sys.modules[core_name] = core
    HUB_VALIDATE_ROOT = runcrew_ah
    HUB_STUB_AGENTS = sys.modules[core_name].AGENTS
    fr_path = runcrew_ah / 'fleet_readiness.py'
    fr_name = pkg_name + '.fleet_readiness'
    spec = importlib.util.spec_from_file_location(fr_name, fr_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fr_name] = mod
    spec.loader.exec_module(mod)
    HUB_VALIDATE_SOURCE = (
        'importlib load of runcrew/agent_hub/fleet_readiness.py under isolated package '
        '_t65_runcrew_agent_hub (cca and runcrew both use agent_hub)'
    )
    return mod.validate_document


hub_validate_document = _load_hub_validate_document()

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
        # unknown execution (failed cloud.get) must not become ready
        ('active', None, 'unknown'),
        # completed / failed must never map to ready
        ('active', {'uid': NEXT_UID, 'runningCount': 0, 'completionTime': '2026-09-26T12:00:00Z',
                    'succeededCount': 1}, 'unknown'),
        ('active', {'uid': NEXT_UID, 'runningCount': 0, 'startTime': '2026-09-26T11:00:00Z',
                    'completionTime': '2026-09-26T12:00:00Z', 'succeededCount': 1}, 'unknown'),
        ('active', {'uid': NEXT_UID, 'runningCount': 0, 'failedCount': 1}, 'unknown'),
        ('active', {'uid': NEXT_UID, 'runningCount': 1, 'failedCount': 1}, 'unknown'),
        ('blocked', None, 'blocked'),
        ('blocked', {'runningCount': 1}, 'blocked'),
        ('not_a_phase', None, 'unknown'),
        ('', None, 'unknown'),
    ]

    def test_phase_mapping_table(self):
        for slot_phase, execution, expected in self.TABLE:
            with self.subTest(slot_phase=slot_phase, execution=execution):
                self.assertEqual(map_readiness_phase(slot_phase, execution), expected)

    def test_unknown_completed_failed_never_ready(self):
        self.assertNotEqual(map_readiness_phase('active', None), 'ready')
        self.assertEqual(map_readiness_phase('active', None), 'unknown')
        completed = {'uid': NEXT_UID, 'completionTime': 't', 'runningCount': 0, 'succeededCount': 1}
        self.assertNotEqual(map_readiness_phase('active', completed), 'ready')
        failed = {'uid': NEXT_UID, 'failedCount': 1, 'runningCount': 0}
        self.assertNotEqual(map_readiness_phase('active', failed), 'ready')

    def test_execution_started_helper(self):
        self.assertFalse(execution_started(None))
        self.assertFalse(execution_started({'runningCount': 0}))
        self.assertTrue(execution_started({'runningCount': 1}))
        self.assertTrue(execution_started({'startTime': '2026-09-26T12:00:00Z'}))


class PayloadContractTests(unittest.TestCase):
    def test_rejects_keys_outside_hub_contract(self):
        entry = agent_readiness_entry({'phase': 'idle', 'updated_at': 1})
        for key in ('codex_ryan', 'gemini'):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, '^agent_key_invalid$'):
                    build_readiness_document({key: entry}, published_at=1, ttl_seconds=30)

    def test_hub_agents_cover_policy_providers(self):
        from agent_hub.cloud_credential_broker import _PROVIDERS
        self.assertEqual(set(frp.HUB_AGENTS), _PROVIDERS)

    def test_document_exact_keys_and_bounds(self):
        doc = build_readiness_document(
            {
                'codex': {
                    'phase': 'idle',
                    'since': 1000,
                    'reason_code': 'ok',
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
        with self.assertRaises(ValueError):
            build_readiness_document(
                {'codex': {'phase': 'idle', 'since': 1, 'reason_code': 'ok',
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

    def test_reason_code_null_becomes_ok(self):
        entry = agent_readiness_entry({'phase': 'idle', 'updated_at': 1})
        self.assertEqual(entry['reason_code'], 'ok')
        self.assertIsInstance(entry['reason_code'], str)
        self.assertTrue(REASON_CODE.fullmatch(entry['reason_code']))

    def test_reason_code_boundary_64_accepted_65_refused(self):
        ok = 'x' * 64
        self.assertEqual(len(ok), 64)
        doc = build_readiness_document(
            {'codex': {'phase': 'idle', 'since': 1, 'reason_code': ok, 'execution': None}},
            published_at=1, ttl_seconds=30,
        )
        self.assertEqual(doc['agents']['codex']['reason_code'], ok)
        too_long = 'x' * 65
        self.assertEqual(len(too_long), 65)
        with self.assertRaises(ValueError):
            build_readiness_document(
                {'codex': {'phase': 'idle', 'since': 1, 'reason_code': too_long, 'execution': None}},
                published_at=1, ttl_seconds=30,
            )
        self.assertEqual(MAX_REASON_CODE, 64)

    def test_execution_id_with_slash_refused(self):
        self.assertIsNone(EXECUTION_ID.fullmatch('a/b'))
        with self.assertRaises(ValueError):
            build_readiness_document(
                {'codex': {'phase': 'idle', 'since': 1, 'reason_code': 'ok',
                           'execution': 'projects/x/executions/y'}},
                published_at=1, ttl_seconds=30,
            )


class PublisherBehaviourTests(unittest.TestCase):
    def test_slot_agent_key_is_provider(self):
        self.assertEqual(frp.slot_agent_key(
            _FakeController('codex', {'phase': 'idle'}, profile='blueeyes')), 'codex')

    def test_collision_skips_stores_and_post(self):
        hub, logs = FakeHubClient(), []
        publisher = FleetReadinessPublisher(
            enabled=True, hub_url='http://127.0.0.1:9', token='tok',
            post=hub, log=logs.append, clock=lambda: 1000,
        )
        controllers = [_FakeController('codex', {'phase': 'idle'}, profile=profile)
                       for profile in ('ryan', 'blueeyes')]
        for controller in controllers:
            controller.store.read = Mock(return_value=({'phase': 'idle'}, 1))
        result = publish_after_tick(controllers, publisher)
        self.assertIsNone(result)
        self.assertEqual(hub.calls, [])
        self.assertEqual(len(logs), 1)
        self.assertEqual(json.loads(logs[0])['reason'], 'agent_key_collision')
        self.assertEqual(publisher.failures['count'], 1)
        for controller in controllers:
            controller.store.read.assert_not_called()

    def test_runtime_refuses_duplicate_providers(self):
        from agent_hub.credential_broker_service import BrokerError
        from agent_hub.cloud_credential_broker import ProfileConfig
        from cloud_runtime import Runtime
        from test_dynamic_broker import ENDPOINT
        profiles = [
            ProfileConfig('codex', 'ryan', '1'*64, '2'*64,
                          'runcrew-credential-codex-ryan', 'runcrew-worker-codex-ryan'),
            ProfileConfig('codex', 'blueeyes', '3'*64, '4'*64,
                          'runcrew-credential-codex-blueeyes', 'runcrew-worker-codex-blueeyes'),
        ]
        config = {
            'schema_version': 1, 'audience': ENDPOINT,
            'policies': [
                {'profile': asdict(profile), 'caller_subject': subject,
                 'caller_service_account': 'runcrew-worker-codex@project-0c6d31fa-509e-4116-a2c.iam.gserviceaccount.com',
                 'lease_seconds': 240}
                for profile, subject in zip(profiles, ('123456789', '987654321'))
            ],
            'slots': {'codex': {'job_uid': JOB_UID, 'template_sha256': '0'*64, 'enabled': True}},
        }
        with self.assertRaises(BrokerError) as raised:
            Runtime(config)
        self.assertEqual(str(raised.exception), 'controller_slots_invalid')

    def test_http_status_codes_are_fixed_and_sanitized(self):
        url, token = 'http://127.0.0.1:9/v1/fleet/readiness', 'secret-token'
        for code, expected in (
            (400, 'hub_http_400'), (401, 'hub_http_401'), (403, 'hub_http_403'),
            (404, 'hub_http_404'), (409, 'hub_http_409'), (413, 'hub_http_413'),
            (500, 'hub_http_5xx'), (503, 'hub_http_5xx'), (599, 'hub_http_5xx'),
            (418, 'hub_http_other'), (600, 'hub_http_other'),
            ('400', 'hub_http_other'), (None, 'hub_http_other'), (400.0, 'hub_http_other'),
        ):
            with self.subTest(code=code):
                hub, logs = FakeHubClient(), []
                hub.fail_with = HTTPError(url, code, 'x', hdrs=None, fp=None)
                publisher = FleetReadinessPublisher(
                    enabled=True, hub_url='http://127.0.0.1:9', token=token,
                    post=hub, log=logs.append, clock=lambda: 1,
                )
                self.assertIsNone(publisher.publish_slots({'codex': {'phase': 'idle'}}))
                self.assertEqual(publisher.failures['count'], 1)
                self.assertEqual(len(logs), 1)
                self.assertEqual(json.loads(logs[0])['reason'], expected)
                self.assertTrue(frp.SAFE_CODE.fullmatch(expected))
                for line in logs:
                    self.assertNotIn(token, line)
                    self.assertNotIn(url, line)

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
        self.assertEqual(doc['agents']['codex']['reason_code'], 'ok')

    def test_controller_tick_does_not_publish(self):
        """Per-controller publish removed; hub replaces the whole document."""
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
        self.assertEqual(hub.calls, [])

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
        # Publish is fleet-level; a failed publish_after_tick must not affect tick.
        publish_after_tick([controller], publisher)
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
        doc = publish_after_tick(
            [_FakeController('codex', {'phase': 'idle', 'updated_at': 1}, profile='ryan'),
             _FakeController('claude', {'phase': 'blocked', 'updated_at': 2,
                                        'error': 'launch_without_execution'}, profile='ryan')],
            publisher,
        )
        self.assertEqual(len(hub.calls), 1)
        self.assertEqual(hub.calls[0]['document']['ttl_seconds'], 120)
        self.assertEqual(set(doc['agents']), {'codex', 'claude'})
        self.assertEqual(doc['agents']['codex']['phase'], 'idle')
        self.assertEqual(doc['agents']['claude']['phase'], 'blocked')

    def test_runtime_tick_one_post_three_slots_keyed_by_provider(self):
        hub = FakeHubClient()
        publisher = FleetReadinessPublisher(
            enabled=True, hub_url='http://127.0.0.1:9', token='tok',
            tick_seconds=60, post=hub, clock=lambda: 7000,
        )
        controllers = [
            _FakeController('codex', {'phase': 'idle', 'updated_at': 1}, profile='ryan'),
            _FakeController('claude', {'phase': 'idle', 'updated_at': 2}, profile='blueeyes'),
            _FakeController('grok', {'phase': 'blocked', 'updated_at': 3,
                                     'error': 'launch_without_execution'}, profile='ryan'),
        ]
        from cloud_runtime import Runtime
        runtime = Runtime.__new__(Runtime)
        runtime.lock = threading.Lock()
        runtime.controllers = controllers
        runtime.readiness_publisher = publisher
        result = runtime.tick()
        self.assertEqual(len(hub.calls), 1)
        doc = hub.calls[0]['document']
        expected = {'codex', 'claude', 'grok'}
        self.assertEqual(set(doc['agents']), expected)
        self.assertEqual(result['codex']['status'], 'ok')
        self.assertEqual(result['claude']['status'], 'ok')
        self.assertEqual(result['grok']['status'], 'ok')


@unittest.skipIf(hub_validate_document is None,
                 'runcrew agent_hub not found: set RUNCREW_AGENT_HUB to runcrew source/agent-hub')
class RealHttpPublishTests(unittest.TestCase):
    """End-to-end POST against a real local HTTP server (no fake post)."""

    def setUp(self):
        self.hits = []
        self.statuses = []
        self.force_status = None
        self.redirect_targets = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def send_response(self, code, message=None):
                parent.statuses.append(code)
                super().send_response(code, message)

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(length)
                agent = self.headers.get('X-Hub-Agent')
                parent.hits.append({
                    'path': self.path,
                    'agent': agent,
                    'token': self.headers.get('X-Hub-Token'),
                    'body': body,
                })
                if parent.force_status is not None:
                    self.send_response(parent.force_status)
                    self.end_headers()
                    return
                if self.path == '/redirect-source':
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:%d/v1/fleet/readiness' % parent.server.server_port)
                    self.end_headers()
                    return
                if self.path != '/v1/fleet/readiness':
                    self.send_response(404)
                    self.end_headers()
                    return
                if agent != 'fleet':
                    self.send_response(403)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"error":"agent_mismatch"}')
                    return
                try:
                    data = json.loads(body.decode('utf-8'))
                    hub_validate_document(data, time.time())
                except Exception as error:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': str(error)}).encode())
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def do_GET(self):
                parent.redirect_targets.append(self.path)
                self.send_response(200)
                self.end_headers()

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.base = 'http://127.0.0.1:%d' % self.port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_hub_agents_mirror_core(self):
        tree = ast.parse((HUB_VALIDATE_ROOT / 'core.py').read_text(encoding='utf-8'))
        fleet = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'FLEET'
                             for target in node.targets))
        self.assertEqual(fleet, frp.HUB_AGENTS)
        self.assertEqual(fleet, HUB_STUB_AGENTS)

    def _tick_runtime(self, logs):
        from cloud_runtime import Runtime
        publisher = FleetReadinessPublisher(
            enabled=True, hub_url=self.base, token='fleet-token-for-local-test',
            tick_seconds=60, clock=lambda: int(time.time()), log=logs.append,
        )
        runtime = Runtime.__new__(Runtime)
        runtime.lock = threading.Lock()
        runtime.controllers = [
            _FakeController(provider, {'phase': 'idle', 'updated_at': int(time.time())},
                            profile=profile)
            for provider, profile in (('codex', 'ryan'), ('claude', 'blueeyes'), ('grok', 'ryan'))
        ]
        runtime.readiness_publisher = publisher
        runtime.tick()
        return publisher

    def test_real_publish_succeeds_end_to_end(self):
        self.assertTrue(HUB_VALIDATE_SOURCE.startswith('import'))
        logs = []
        publisher = self._tick_runtime(logs)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(self.hits[0]['agent'], 'fleet')
        self.assertEqual(self.hits[0]['path'], '/v1/fleet/readiness')
        self.assertEqual(self.statuses, [200])
        self.assertEqual(publisher.failures['count'], 0)
        self.assertEqual(logs, [])
        body = json.loads(self.hits[0]['body'].decode())
        self.assertEqual(set(body['agents']), {'codex', 'claude', 'grok'})

    def test_real_http_conflict_is_logged(self):
        self.force_status = 409
        logs = []
        publisher = self._tick_runtime(logs)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(self.hits[0]['path'], '/v1/fleet/readiness')
        self.assertEqual(self.statuses, [409])
        self.assertEqual(publisher.failures['count'], 1)
        self.assertEqual(len(logs), 1)
        self.assertEqual(json.loads(logs[0])['reason'], 'hub_http_409')
        self.assertNotIn('fleet-token-for-local-test', logs[0])
        self.assertNotIn(self.base, logs[0])

    def test_redirect_not_followed(self):
        # POST to a path that 302s; NoRedirect must not follow to /v1/fleet/readiness.
        document = build_readiness_document(
            {'codex': {'phase': 'idle', 'since': int(time.time()), 'reason_code': 'ok',
                       'execution': None}},
            published_at=int(time.time()),
            ttl_seconds=30,
        )
        body = json.dumps(document, separators=(',', ':')).encode()
        request = Request(
            self.base + '/redirect-source',
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-Hub-Token': 'fleet-token-for-local-test',
                'X-Hub-Agent': 'fleet',
            },
            method='POST',
        )
        opener = build_opener(ProxyHandler({}), NoRedirect())
        hits_before = len(self.hits)
        try:
            with opener.open(request, timeout=2.5) as response:
                self.assertEqual(response.status, 302)
        except HTTPError as error:
            self.assertEqual(error.code, 302)
        readiness_hits = [h for h in self.hits[hits_before:] if h['path'] == '/v1/fleet/readiness']
        self.assertEqual(readiness_hits, [])
        source_hits = [h for h in self.hits[hits_before:] if h['path'] == '/redirect-source']
        self.assertEqual(len(source_hits), 1)


class _FakeController:
    def __init__(self, provider, state, profile='ryan'):
        self.policy = type('P', (), {
            'profile': type('F', (), {'provider': provider, 'profile': profile})(),
        })()
        self.store = type('S', (), {'read': lambda self: (deepcopy(state), 1)})()
        self.cloud = type('C', (), {'get': lambda self, name: None})()

    def tick(self):
        return {'status': 'ok'}


if __name__ == '__main__':
    unittest.main()
