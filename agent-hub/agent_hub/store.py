"""Durable room storage. SQLite for local use; Firestore for Cloud Run."""
import copy
import json
import sqlite3
from pathlib import Path


class MissingRoom(Exception):
    pass


class SQLiteStore:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        def work(connection):
            connection.execute('CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS durable_state (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.commit()
        self._run(work)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute('PRAGMA busy_timeout=15000')
        return connection

    def _run(self, work):
        # sqlite3 context managers commit/rollback but do not close; Windows then locks the file.
        connection = self._connect()
        try:
            return work(connection)
        finally:
            connection.close()

    def close(self):
        return None

    def get_state(self, key):
        row = self._run(lambda connection: connection.execute(
            'SELECT data FROM durable_state WHERE id=?', (key,)).fetchone())
        return json.loads(row[0]) if row else {}

    def mutate_states(self, keys, change):
        def work(connection):
            connection.execute('BEGIN IMMEDIATE')
            try:
                states = {}
                for key in sorted(set(keys)):
                    row = connection.execute('SELECT data FROM durable_state WHERE id=?', (key,)).fetchone()
                    states[key] = json.loads(row[0]) if row else {}
                before = copy.deepcopy(states)
                result = change(states)
                for key, state in states.items():
                    if state != before[key]:
                        connection.execute('INSERT INTO durable_state VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                                           (key, json.dumps(state, allow_nan=False)))
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
        return self._run(work)

    def mutate_state(self, key, change):
        """Callbacks may be retried by the cloud backend; never perform I/O in them."""
        def work(connection):
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('SELECT data FROM durable_state WHERE id=?', (key,)).fetchone()
                state = json.loads(row[0]) if row else {}
                before = copy.deepcopy(state)
                result = change(state)
                if state != before:
                    connection.execute('INSERT INTO durable_state VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                                       (key, json.dumps(state, allow_nan=False)))
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
        return self._run(work)

    def mutate_room_with_state(self, room_id, key, change):
        """Select advisory memory and claim a room in the same transaction."""
        def work(connection):
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('SELECT data FROM rooms WHERE id=?', (room_id,)).fetchone()
                if row is None:
                    raise MissingRoom(room_id)
                saved = connection.execute('SELECT data FROM durable_state WHERE id=?', (key,)).fetchone()
                state = json.loads(saved[0]) if saved else {}
                room = json.loads(row[0])
                before = copy.deepcopy(room)
                result = change(room, state)
                if room != before:
                    connection.execute('UPDATE rooms SET data=? WHERE id=?', (json.dumps(room), room_id))
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
        return self._run(work)

    def put_worker(self, worker_id, data):
        def work(connection):
            connection.execute('INSERT INTO workers VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                               (worker_id, json.dumps(data)))
            connection.commit()
        self._run(work)

    def list_workers(self, limit=100):
        rows = self._run(lambda connection: connection.execute(
            "SELECT data FROM workers ORDER BY json_extract(data, '$.received_at') DESC LIMIT ?", (limit,)).fetchall())
        return [json.loads(row[0]) for row in rows]

    def put_observation(self, data):
        """Replace the single configured machine's bounded observation record."""
        def work(connection):
            connection.execute("INSERT INTO observations VALUES ('latest', ?) "
                               "ON CONFLICT(id) DO UPDATE SET data=excluded.data", (json.dumps(data, allow_nan=False),))
            connection.commit()
        self._run(work)

    def get_observation(self):
        row = self._run(lambda connection: connection.execute(
            "SELECT data FROM observations WHERE id='latest'").fetchone())
        return json.loads(row[0]) if row else None

    def create(self, room):
        def work(connection):
            connection.execute('INSERT INTO rooms VALUES (?, ?)', (room['id'], json.dumps(room)))
            connection.commit()
        self._run(work)

    def get(self, room_id):
        def work(connection):
            return connection.execute('SELECT data FROM rooms WHERE id=?', (room_id,)).fetchone()
        row = self._run(work)
        if row is None:
            raise MissingRoom(room_id)
        return json.loads(row[0])

    def list(self, agent=None, limit=50):
        def work(connection):
            if agent:
                return connection.execute(
                    "SELECT data FROM rooms WHERE json_extract(data, '$.status')='queued' "
                    "AND json_extract(data, '$.next_agent')=? "
                    "ORDER BY json_extract(data, '$.created_at') ASC, rowid ASC LIMIT ?",
                    (agent, limit)).fetchall()
            return connection.execute(
                "SELECT data FROM rooms ORDER BY json_extract(data, '$.created_at') DESC, "
                'rowid DESC LIMIT ?', (limit,)).fetchall()
        return [json.loads(row[0]) for row in self._run(work)]

    def mutate(self, room_id, change):
        def work(connection):
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('SELECT data FROM rooms WHERE id=?', (room_id,)).fetchone()
                if row is None:
                    raise MissingRoom(room_id)
                room = json.loads(row[0])
                before = copy.deepcopy(room)
                result = change(room)
                if room != before:
                    connection.execute('UPDATE rooms SET data=? WHERE id=?', (json.dumps(room), room_id))
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
        return self._run(work)


class FirestoreStore:
    def __init__(self, project, collection='agent_hub_rooms', database='(default)'):
        if not project:
            raise ValueError('GOOGLE_CLOUD_PROJECT is required for Firestore')
        from google.cloud import firestore
        self.firestore = firestore
        self.client = firestore.Client(project=project, database=database)
        self.collection = self.client.collection(collection)
        self.workers = self.client.collection(collection + '_workers')
        self.observations = self.client.collection(collection + '_observations')
        self.durable_state = self.client.collection(collection + '_state')

    def get_state(self, key):
        document = self.durable_state.document(key).get()
        return document.to_dict() if document.exists else {}

    def mutate_states(self, keys, change):
        references = {key: self.durable_state.document(key) for key in sorted(set(keys))}

        @self.firestore.transactional
        def apply(transaction):
            states = {}
            for key, reference in references.items():
                snapshot = reference.get(transaction=transaction)
                states[key] = snapshot.to_dict() if snapshot.exists else {}
            before = copy.deepcopy(states)
            result = change(states)
            for key, state in states.items():
                if state != before[key]:
                    transaction.set(references[key], state)
            return result

        return apply(self.client.transaction())

    def mutate_state(self, key, change):
        reference = self.durable_state.document(key)

        @self.firestore.transactional
        def apply(transaction):
            snapshot = reference.get(transaction=transaction)
            state = snapshot.to_dict() if snapshot.exists else {}
            before = copy.deepcopy(state)
            result = change(state)
            if state != before:
                transaction.set(reference, state)
            return result

        return apply(self.client.transaction())

    def mutate_room_with_state(self, room_id, key, change):
        reference = self.collection.document(room_id)
        state_reference = self.durable_state.document(key)

        @self.firestore.transactional
        def apply(transaction):
            snapshot = reference.get(transaction=transaction)
            state_snapshot = state_reference.get(transaction=transaction)
            if not snapshot.exists:
                raise MissingRoom(room_id)
            room = snapshot.to_dict()
            before = copy.deepcopy(room)
            result = change(room, state_snapshot.to_dict() if state_snapshot.exists else {})
            if room != before:
                transaction.set(reference, room)
            return result

        return apply(self.client.transaction())

    def put_worker(self, worker_id, data):
        self.workers.document(worker_id).set(data)

    def list_workers(self, limit=100):
        query = self.workers.order_by('received_at', direction=self.firestore.Query.DESCENDING)
        return [document.to_dict() for document in query.limit(limit).stream()]

    def put_observation(self, data):
        self.observations.document('latest').set(data)

    def get_observation(self):
        document = self.observations.document('latest').get()
        return document.to_dict() if document.exists else None

    def create(self, room):
        self.collection.document(room['id']).create(room)

    def get(self, room_id):
        document = self.collection.document(room_id).get()
        if not document.exists:
            raise MissingRoom(room_id)
        return document.to_dict()

    def list(self, agent=None, limit=50):
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = self.collection
        if agent:
            query = query.where(filter=FieldFilter('next_agent', '==', agent))
            query = query.order_by('created_at', direction=self.firestore.Query.ASCENDING)
        else:
            query = query.order_by('created_at', direction=self.firestore.Query.DESCENDING)
        return [document.to_dict() for document in query.limit(limit).stream()]

    def mutate(self, room_id, change):
        reference = self.collection.document(room_id)

        @self.firestore.transactional
        def apply(transaction):
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                raise MissingRoom(room_id)
            room = snapshot.to_dict()
            before = copy.deepcopy(room)
            result = change(room)
            if room != before:
                transaction.set(reference, room)
            return result

        return apply(self.client.transaction())
