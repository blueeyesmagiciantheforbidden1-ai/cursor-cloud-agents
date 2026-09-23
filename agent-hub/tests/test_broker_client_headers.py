"""Cloud Run platform authentication must preserve signed app authorization."""
import unittest
from unittest.mock import patch
from tests import test_credential_broker_service as fixture
from agent_hub import credential_broker_service as service


class ClientHeaderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ServiceTests()
        self.fixture.setUp()

    def client(self, bootstrap=False):
        f = self.fixture
        return service.BrokerHTTPClient(f.profile, endpoint=fixture.ENDPOINT,
            execution=f.execution, execution_uid=None if bootstrap else f.execution_uid,
            grant=fixture.GRANT)

    def headers(self, transport):
        headers = transport.call_args.kwargs['headers']
        self.assertEqual(headers['Authorization'], 'Bearer synthetic.jwt.signature')
        self.assertEqual(headers['X-Serverless-Authorization'], headers['Authorization'])
        self.assertEqual(headers['X-RunCrew-Execution-Grant'], fixture.GRANT)
        self.assertEqual(transport.call_count, 1)

    def test_mutation_preserves_full_application_signature(self):
        client = self.client()
        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), \
                patch.object(service, 'exchange', return_value=(200, {}, b'{}')) as transport:
            client._post('release', {})
        self.headers(transport)

    def test_bootstrap_uses_same_dual_header_contract(self):
        client = self.client(bootstrap=True)
        f = self.fixture
        body = service.encode({'ready': True, 'execution': f.execution, 'execution_uid': f.execution_uid})
        with patch.object(client, '_id_token', return_value='synthetic.jwt.signature'), \
                patch.object(service, 'exchange', return_value=(200, {}, body)) as transport:
            client.bootstrap(timeout_seconds=1)
        self.headers(transport)
        self.assertEqual(client.execution_uid, f.execution_uid)
