from dataclasses import asdict
import hashlib
import unittest
from unittest.mock import patch

from agent_hub import credential_broker_service as base
from agent_hub.cloud_credential_broker import (ProfileConfig, MutationUncertain, UpstreamUnavailable,
                                              Conflict, BrokerError, UPSTREAM_BUDGET_SECONDS)
from dynamic_broker import Policy, BindingStore, LiveBrokerService, parse_config


PROFILE = ProfileConfig('grok', 'blueeyes', '1' * 64, '2' * 64,
                        'runcrew-credential-grok-blueeyes', 'runcrew-worker-grok-blueeyes')
POLICY = Policy(PROFILE, '123456789', 'runcrew-worker-grok@project-0c6d31fa-509e-4116-a2c.iam.gserviceaccount.com')
GRANT = 'g' * 43
DIGEST = hashlib.sha256(GRANT.encode()).hexdigest()
BINDING = POLICY.binding(DIGEST, 2000)
ENDPOINT = 'https://runcrew-live-broker-496481413971.us-central1.run.app'
EXEC = PROFILE.job_name + '/executions/runcrew-worker-grok-blueeyes-abcde'
UID = 'c4be8773-b4d1-493d-aec0-f7071a827414'


class MemoryStore(BindingStore):
    def __init__(self, policy=POLICY):
        super().__init__(policy, rest=object(), clock=lambda: 1000)
        self.documents = {}; self.writes = 0; self.lost_reply = False
    def exchange(self, method, path, value=None):
        if method == 'GET': return self.documents.get(path)
        self.writes += 1
        update = value['writes'][0]['update']; key = update['name']
        if key in self.documents: raise MutationUncertain('exists')
        self.documents[key] = update
        if self.lost_reply: raise MutationUncertain('lost_reply')
        return {'writeResults': [{'updateTime': 'timestamp'}]}


class FakeBroker:
    def __init__(self, *args, **kwargs): self.terminal = False; self.uid = UID
    def _execution_status(self, execution):
        return {'uid': self.uid, 'completionTime': 'timestamp' if self.terminal else None,
                'template': {'serviceAccount': POLICY.caller_service_account}}


class DynamicTests(unittest.TestCase):
    def service(self):
        store = MemoryStore(); store.publish(BINDING)
        active = base.ActiveBinding(BINDING, EXEC, UID)
        grant_store = type('GrantStore', (), {'read': lambda self: active})
        service = LiveBrokerService(ENDPOINT, (POLICY,),
            authenticator=lambda token: {'subject': POLICY.caller_subject,
                                         'service_account': POLICY.caller_service_account},
            broker_factory=FakeBroker, binding_store_factory=lambda p: store,
            grant_store_factory=lambda b: grant_store(), clock=lambda: 1000)
        return service, store

    def test_good_exact_execution_bootstrap(self):
        service, _ = self.service()
        self.assertEqual(service.dispatch('bootstrap', 'token', GRANT, {}),
                         {'ready': True, 'execution': EXEC, 'execution_uid': UID})

    def test_wrong_caller_rejected_before_store(self):
        service, store = self.service()
        service.authenticator = lambda token: {'subject': '999999999', 'service_account': POLICY.caller_service_account}
        store.read = lambda d: self.fail('unauthorized caller caused lookup')
        with self.assertRaises(base.BoundaryError): service.dispatch('bootstrap', 'token', GRANT, {})

    def test_unknown_grant_rejected(self):
        service, _ = self.service()
        with self.assertRaises(base.BoundaryError): service.dispatch('bootstrap', 'token', 'z' * 43, {})

    def test_rebound_profile_rejected(self):
        service, store = self.service(); document = store.documents[store.name(DIGEST)]
        value = base.decode(document['fields']['binding_json']['stringValue'].encode())
        value['profile']['account_ref'] = '3' * 64
        document['fields']['binding_json']['stringValue'] = base.encode(value).decode()
        with self.assertRaises(base.BoundaryError): service.dispatch('bootstrap', 'token', GRANT, {})

    def test_expired_grant_rejected(self):
        service, _ = self.service(); service.clock = lambda: 2000
        with self.assertRaises(base.BoundaryError): service.dispatch('bootstrap', 'token', GRANT, {})

    def test_terminal_or_reused_execution_rejected(self):
        for field, value in (('terminal', True), ('uid', 'different')):
            with self.subTest(field=field):
                service, _ = self.service(); setattr(service.brokers[POLICY], field, value)
                with self.assertRaises(base.BoundaryError): service.dispatch('bootstrap', 'token', GRANT, {})

    def test_uncertain_binding_write_resolved_by_exact_read_only(self):
        store = MemoryStore(); store.lost_reply = True
        self.assertEqual(store.publish(BINDING), BINDING); self.assertEqual(store.writes, 1)

    def test_binding_record_is_immutable(self):
        store = MemoryStore(); store.publish(BINDING)
        with self.assertRaises(MutationUncertain): store.publish(POLICY.binding(DIGEST, 2100))
        self.assertEqual(store.read(DIGEST), BINDING)

    def test_binding_read_stall_is_upstream_unavailable_not_conflict(self):
        store = BindingStore(POLICY, rest=type('Rest', (), {'_token': staticmethod(lambda: 'offline-token')})(),
                             clock=lambda: 1000)
        with patch.object(base, 'exchange', side_effect=TimeoutError('stalled raw secret')) as transport:
            with self.assertRaises(UpstreamUnavailable) as caught:
                store.read(DIGEST)
            self.assertNotIsInstance(caught.exception, Conflict)
            self.assertNotIn('raw secret', str(caught.exception))
            self.assertNotIn('stalled', str(caught.exception))
            self.assertEqual(transport.call_args.kwargs['timeout_seconds'], UPSTREAM_BUDGET_SECONDS)
            self.assertEqual(UPSTREAM_BUDGET_SECONDS, 8)
        with patch.object(base, 'exchange', return_value=(503, {'Retry-After': '2'}, b'{"secret":"raw"}')):
            with self.assertRaisesRegex(UpstreamUnavailable, '^live_binding_read_unavailable$'):
                store.read(DIGEST)
        with patch.object(base, 'exchange', return_value=(409, {}, b'{}')):
            with self.assertRaises(Conflict):
                store.read(DIGEST)
        with patch.object(base, 'exchange', return_value=(403, {}, b'{}')):
            with self.assertRaises(BrokerError) as caught:
                store.read(DIGEST)
            self.assertNotIsInstance(caught.exception, (UpstreamUnavailable, Conflict))
        with patch.object(base, 'exchange', return_value=(200, {}, b'{')):
            with self.assertRaises(UpstreamUnavailable):
                store.read(DIGEST)
        with patch.object(base, 'exchange', side_effect=OSError('lost raw secret')) as transport:
            with self.assertRaises(MutationUncertain) as caught:
                store.exchange('POST', 'documents:commit', {'writes': []})
            self.assertNotIsInstance(caught.exception, UpstreamUnavailable)
            self.assertNotIn('raw secret', str(caught.exception))
            self.assertEqual(transport.call_args.kwargs['timeout_seconds'], 8)

    def test_config_unique_known_identity_and_no_opaque_credentials(self):
        config = {'schema_version': 1, 'audience': ENDPOINT, 'policies': [asdict(POLICY)]}
        self.assertEqual(parse_config(config)[1], (POLICY,))
        config['policies'].append(asdict(POLICY))
        with self.assertRaises(base.BoundaryError): parse_config(config)


if __name__ == '__main__': unittest.main()
