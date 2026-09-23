"""Named Firestore database selection, with no SDK credentials or network."""
from contextlib import contextmanager
import io
import json
import os
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, call, patch

from agent_hub import server
from agent_hub.core import AGENTS
from agent_hub.store import FirestoreStore


@contextmanager
def fake_firestore():
    google = ModuleType('google')
    cloud = ModuleType('google.cloud')
    firestore = ModuleType('google.cloud.firestore')
    google.cloud = cloud
    cloud.firestore = firestore
    client = Mock(name='selected_database_client')
    collections = {name: Mock(name=name) for name in
                   ('isolated_rooms', 'isolated_rooms_workers', 'isolated_rooms_observations')}
    client.collection.side_effect = collections.__getitem__
    firestore.Client = Mock(return_value=client)
    firestore.transactional = lambda function: function
    with patch.dict(sys.modules, {'google': google, 'google.cloud': cloud,
                                  'google.cloud.firestore': firestore}):
        yield firestore, client, collections


class CloudDatabaseTests(unittest.TestCase):
    def test_named_database_owns_room_worker_observation_and_transaction_references(self):
        with fake_firestore() as (firestore, client, collections):
            store = FirestoreStore('synthetic-isolated-project', 'isolated_rooms', database='isolated-ledger')
            firestore.Client.assert_called_once_with(project='synthetic-isolated-project', database='isolated-ledger')
            self.assertEqual(client.collection.call_args_list,
                             [call('isolated_rooms'), call('isolated_rooms_workers'), call('isolated_rooms_observations')])
            room = {'id': 'synthetic-room', 'status': 'queued'}
            worker = {'received_at': 1, 'status': 'idle'}
            observation = {'schema_version': 1}
            store.create(room)
            store.put_worker('codex', worker)
            store.put_observation(observation)
            collections['isolated_rooms'].document.assert_called_once_with('synthetic-room')
            collections['isolated_rooms'].document.return_value.create.assert_called_once_with(room)
            collections['isolated_rooms_workers'].document.assert_called_once_with('codex')
            collections['isolated_rooms_workers'].document.return_value.set.assert_called_once_with(worker)
            collections['isolated_rooms_observations'].document.assert_called_once_with('latest')
            collections['isolated_rooms_observations'].document.return_value.set.assert_called_once_with(observation)

            reference = collections['isolated_rooms'].document.return_value
            reference.get.return_value.exists = True
            reference.get.return_value.to_dict.return_value = dict(room)
            transaction = client.transaction.return_value
            store.mutate('synthetic-room', lambda value: value.update(status='completed'))
            reference.get.assert_called_once_with(transaction=transaction)
            transaction.set.assert_called_once_with(reference, {'id': 'synthetic-room', 'status': 'completed'})
            firestore.Client.assert_called_once()

    def test_omitted_database_explicitly_selects_default(self):
        with fake_firestore() as (firestore, client, collections):
            FirestoreStore('synthetic-isolated-project', 'isolated_rooms')
            firestore.Client.assert_called_once_with(project='synthetic-isolated-project', database='(default)')

    def test_missing_project_never_initializes_sdk_client(self):
        with fake_firestore() as (firestore, client, collections):
            with self.assertRaises(ValueError):
                FirestoreStore(None, database='isolated-ledger')
            firestore.Client.assert_not_called()

    def test_cloud_server_passes_named_database_environment_and_defaults(self):
        tokens = {principal: str(index) * 32 for index, principal in enumerate(('manager', *AGENTS), 1)}
        environment = {'HUB_TOKENS_JSON': json.dumps(tokens), 'HUB_BACKEND': 'firestore',
                       'GOOGLE_CLOUD_PROJECT': 'synthetic-isolated-project', 'K_SERVICE': 'synthetic-hub',
                       'HUB_FIRESTORE_COLLECTION': 'isolated_rooms', 'PORT': '8080'}
        for selection in ('isolated-ledger', None):
            with self.subTest(database=selection):
                values = dict(environment)
                if selection is not None:
                    values['HUB_FIRESTORE_DATABASE'] = selection
                with patch.dict(os.environ, values, clear=True), patch.object(server, 'FirestoreStore') as store, \
                     patch.object(server, 'SQLiteStore') as sqlite, patch.object(server, 'ThreadingHTTPServer') as http, \
                     patch('sys.stdout', io.StringIO()):
                    http.return_value.server_port = 8080
                    server.main()
                    store.assert_called_once_with('synthetic-isolated-project', 'isolated_rooms',
                                                  database=selection or '(default)')
                    sqlite.assert_not_called()
                    http.return_value.serve_forever.assert_called_once_with()
                    http.return_value.server_close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
