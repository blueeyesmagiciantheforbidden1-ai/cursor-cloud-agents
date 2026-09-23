"""Additional authorization/publication negatives; every cloud call is mocked."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SOURCE))

from agent_hub import credential_broker_service as base
from agent_hub.cloud_credential_broker import BrokerError, MutationUncertain
from dynamic_broker import BindingStore, load_client
import test_dynamic_broker as fixtures
from test_dynamic_broker import (MemoryStore, POLICY, BINDING, GRANT,
                                 DIGEST, EXEC, UID, ENDPOINT)


class ReviewedDynamicTests(unittest.TestCase):
    def service(self):
        return fixtures.DynamicTests().service()

    def test_wrong_service_account_rejected_before_store(self):
        service, store = self.service()
        service.authenticator = lambda token: {'subject': POLICY.caller_subject,
                                               'service_account': 'wrong-service-account'}
        store.read = lambda value: self.fail('unauthorized caller reached binding store')
        with self.assertRaises(base.BoundaryError):
            service.dispatch('bootstrap', 'offline-token', GRANT, {})

    def test_observed_execution_service_account_mismatch_rejected(self):
        service, _ = self.service()
        service.brokers[POLICY]._execution_status = lambda name: {
            'uid': UID, 'template': {'serviceAccount': 'wrong-service-account'}}
        with self.assertRaises(base.BoundaryError):
            service.dispatch('bootstrap', 'offline-token', GRANT, {})

    def test_deleted_execution_rejected(self):
        service, _ = self.service()
        service.brokers[POLICY]._execution_status = lambda name: {
            'uid': UID, 'deleteTime': 'observed',
            'template': {'serviceAccount': POLICY.caller_service_account}}
        with self.assertRaises(base.BoundaryError):
            service.dispatch('bootstrap', 'offline-token', GRANT, {})

    def test_http_boundary_exposes_no_publication_or_reconciliation(self):
        service, store = self.service()
        service.authenticator = lambda value: self.fail('unknown endpoint authenticated')
        store.publish = lambda value: self.fail('worker published binding')
        for action in ('publish', 'publish-execution', 'reconcile', 'retire', 'initialize-binding'):
            with self.subTest(action=action), self.assertRaises(base.BoundaryError):
                service.dispatch(action, 'offline-token', GRANT, {})

    def test_grant_store_policy_substitution_rejected(self):
        service, _ = self.service()
        other = POLICY.binding('3' * 64, BINDING.expires_at)
        service.grant_store_factory = lambda binding: SimpleNamespace(
            read=lambda: base.ActiveBinding(other, EXEC, UID))
        with self.assertRaises(base.BoundaryError):
            service.dispatch('bootstrap', 'offline-token', GRANT, {})

    def test_unpublished_exact_execution_is_not_ready(self):
        service, _ = self.service()
        service.grant_store_factory = lambda binding: SimpleNamespace(read=lambda: None)
        self.assertEqual(service.dispatch('bootstrap', 'offline-token', GRANT, {}), {'ready': False})
        with self.assertRaises(base.BoundaryError):
            service.dispatch('acquire', 'offline-token', GRANT, {'request_id': '1' * 32})

    def test_malformed_success_publication_response_remains_uncertain(self):
        store = BindingStore(POLICY, rest=SimpleNamespace(_token=lambda: 'offline-token'), clock=lambda: 1000)
        for raw in (b'', b'not-json', b'[]', b'{"writeResults":[null]}'):
            with self.subTest(raw=raw), patch.object(base, 'exchange', side_effect=[
                    (200, {}, raw), (404, {}, b'')]) as exchange:
                with self.assertRaises(MutationUncertain):
                    store.publish(BINDING)
                self.assertEqual([call.kwargs['method'] for call in exchange.call_args_list], ['POST', 'GET'])

    def test_lost_publication_and_unreadable_reconciliation_remain_uncertain(self):
        store = BindingStore(POLICY, rest=SimpleNamespace(_token=lambda: 'offline-token'), clock=lambda: 1000)
        with patch.object(base, 'exchange', side_effect=[OSError('offline lost reply'), (403, {}, b'')]) as exchange:
            with self.assertRaises(MutationUncertain):
                store.publish(BINDING)
            self.assertEqual(exchange.call_count, 2)

    def test_malformed_ack_but_exact_durable_document_reconciles_read_only(self):
        store = MemoryStore()
        original = store.exchange
        def exchange(method, path, value=None):
            result = original(method, path, value)
            return {'writeResults': [None]} if method == 'POST' else result
        store.exchange = exchange
        self.assertEqual(store.publish(BINDING), BINDING)
        self.assertEqual(store.writes, 1)

    def test_client_removes_capability_before_bootstrap_or_native_launch(self):
        config = {'schema_version': 2, 'endpoint': ENDPOINT,
                  'profile': dict(POLICY.profile.__dict__), 'bootstrap_timeout_seconds': 60}
        observed = {}
        class Client:
            def __init__(self, profile, **kwargs): observed.update(kwargs)
            def bootstrap(self, **kwargs):
                import os
                self.assert_absent = 'RUNCREW_EXECUTION_GRANT' not in os.environ
        with patch.object(base, 'read_protected', return_value=config), \
             patch.object(base, 'BrokerHTTPClient', Client), \
             patch.dict('os.environ', {'RUNCREW_EXECUTION_GRANT': GRANT,
                 'CLOUD_RUN_EXECUTION': EXEC.rsplit('/', 1)[-1]}, clear=True):
            client = load_client('/fixed/offline/config')
            self.assertTrue(client.assert_absent)
            self.assertEqual(observed['grant'], GRANT)
            self.assertEqual(observed['execution'], EXEC)


if __name__ == '__main__':
    unittest.main()
