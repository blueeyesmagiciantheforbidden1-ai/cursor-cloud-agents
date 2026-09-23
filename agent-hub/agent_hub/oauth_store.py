"""Small transactional OAuth stores. Values must never contain raw credentials.

Firestore uses the existing named database; document expiry is always enforced
by OAuthService, independently of asynchronous storage cleanup.
"""
from __future__ import annotations

from copy import deepcopy
import re
import threading

_COLLECTIONS = frozenset(("subjects", "consents", "codes", "access", "refresh", "families"))


def _key(collection, key):
    if collection not in _COLLECTIONS or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("Invalid OAuth storage key")
    return collection, key


class _MemoryTransaction:
    def __init__(self, data):
        self.data = data

    def get(self, collection, key):
        return deepcopy(self.data.get(_key(collection, key)))

    def put(self, collection, key, value):
        self.data[_key(collection, key)] = deepcopy(value)


class MemoryOAuthStore:
    """Serializable in-process implementation for local tests only."""
    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()

    def run(self, operation):
        with self._lock:
            working = deepcopy(self._data)
            result = operation(_MemoryTransaction(working))
            self._data = working
            return result

    def snapshot(self):
        """Test-only diagnostic: values contain hashes, never bearer credentials."""
        with self._lock:
            return deepcopy(self._data)


class _FirestoreTransaction:
    def __init__(self, client, transaction, prefix):
        self.client = client
        self.transaction = transaction
        self.prefix = prefix
        self.cache = {}
        self.writes = {}

    def _reference(self, collection, key):
        _key(collection, key)
        return self.client.collection(self.prefix + "_" + collection).document(key)

    def get(self, collection, key):
        locator = _key(collection, key)
        if locator in self.writes:
            return deepcopy(self.writes[locator])
        if locator not in self.cache:
            snapshot = self._reference(*locator).get(transaction=self.transaction)
            self.cache[locator] = snapshot.to_dict() if snapshot.exists else None
        return deepcopy(self.cache[locator])

    def put(self, collection, key, value):
        self.writes[_key(collection, key)] = deepcopy(value)

    def flush(self):
        # Buffer writes until all reads finish: Firestore prohibits reads after
        # writes. The transaction retries the entire operation on contention.
        for locator, value in self.writes.items():
            self.transaction.set(self._reference(*locator), value)


class FirestoreOAuthStore:
    """Shared persistence; no instance-local locks or TTL assumptions."""
    def __init__(self, project, *, database="runcrew-hub", prefix="runcrew_oauth", client=None):
        if not project or database not in ("runcrew-hub", "runcrew-auth"):
            raise ValueError("OAuth persistence requires a named RunCrew database")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", prefix):
            raise ValueError("Invalid OAuth collection prefix")
        from google.cloud import firestore
        self.firestore = firestore
        self.client = client or firestore.Client(project=project, database=database)
        self.prefix = prefix

    def run(self, operation):
        @self.firestore.transactional
        def apply(transaction):
            view = _FirestoreTransaction(self.client, transaction, self.prefix)
            result = operation(view)
            view.flush()
            return result
        return apply(self.client.transaction())
