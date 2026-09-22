"""Versioned dependency graph, optimistic snapshots, and resource conflicts.

Dependencies point from a consumer to an input. A contract is an ordinary
versioned resource, so consumers must declare contract edges just as file edges.
All in-memory mutations are synchronized. Serialization preserves versions and
graph identity; callers supply their own durable transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Iterable, Mapping
import uuid


class GraphError(ValueError):
    pass


class CycleError(GraphError):
    pass


class StaleSnapshotError(GraphError):
    pass


class ConflictError(GraphError):
    pass


@dataclass(frozen=True)
class Resource:
    key: str
    kind: str = "file"
    version: int = 1
    content_hash: str | None = None
    revision_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class Snapshot:
    graph_id: str
    versions: Mapping[str, int]
    revisions: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))
        object.__setattr__(self, "revisions", MappingProxyType(dict(self.revisions)))
        if self.versions.keys() != self.revisions.keys():
            raise GraphError("Snapshot must contain a revision identity for every version")

    def to_dict(self) -> dict[str, Any]:
        return {"graph_id": self.graph_id, "versions": dict(self.versions),
                "revisions": dict(self.revisions)}


@dataclass(frozen=True)
class Transaction:
    token: str
    reads: frozenset[str]
    writes: frozenset[str]
    snapshot: Snapshot


def read_write_conflicts(
    reads_a: Iterable[str], writes_a: Iterable[str],
    reads_b: Iterable[str], writes_b: Iterable[str],
) -> frozenset[str]:
    """Return write/write and both directions of read/write overlap."""
    ra, wa, rb, wb = map(set, (reads_a, writes_a, reads_b, writes_b))
    return frozenset((wa & (rb | wb)) | (wb & ra))


class VersionedGraph:
    def __init__(self, *, graph_id: str | None = None) -> None:
        self.graph_id = graph_id or uuid.uuid4().hex
        self._resources: dict[str, Resource] = {}
        self._dependencies: dict[str, set[str]] = {}
        self._cache: dict[str, tuple[Any, Snapshot]] = {}
        self._active: dict[str, Transaction] = {}
        self._lock = RLock()

    @property
    def resources(self) -> Mapping[str, Resource]:
        with self._lock:
            return MappingProxyType(dict(self._resources))

    def add_resource(
        self, key: str, *, kind: str = "file", content_hash: str | None = None,
    ) -> Resource:
        with self._lock:
            if not isinstance(key, str) or not key or key in self._resources:
                raise GraphError("Resource keys must be nonempty and unique")
            if kind not in {"file", "contract", "artifact", "logical"}:
                raise GraphError(f"Unknown resource kind: {kind}")
            resource = Resource(key, kind, 1, content_hash)
            self._resources[key] = resource
            self._dependencies[key] = set()
            return resource

    def _require(self, keys: Iterable[str]) -> set[str]:
        result = set(keys)
        missing = result - self._resources.keys()
        if missing:
            raise GraphError(f"Unknown resources: {sorted(missing)}")
        return result

    def _closure(self, keys: Iterable[str]) -> set[str]:
        result = self._require(keys)
        pending = list(result)
        while pending:
            key = pending.pop()
            for dependency in self._dependencies[key] - result:
                result.add(dependency)
                pending.append(dependency)
        return result

    def add_dependency(self, consumer: str, dependency: str) -> None:
        with self._lock:
            self._require((consumer, dependency))
            if dependency in self._dependencies[consumer]:
                return
            if consumer in self._closure((dependency,)):
                raise CycleError(f"Dependency would form a cycle: {consumer} -> {dependency}")
            self._dependencies[consumer].add(dependency)
            self._bump(consumer, self._resources[consumer].content_hash)

    def dependencies(self, key: str, *, transitive: bool = False) -> frozenset[str]:
        with self._lock:
            self._require((key,))
            return frozenset(self._closure((key,)) - {key} if transitive
                             else self._dependencies[key])

    def dependents(self, key: str, *, transitive: bool = True) -> frozenset[str]:
        with self._lock:
            self._require((key,))
            found: set[str] = set()
            pending = {key}
            while pending:
                direct = {k for k, deps in self._dependencies.items() if deps & pending}
                new = direct - found
                found.update(new)
                if not transitive:
                    break
                pending = new
            return frozenset(found)

    def snapshot(self, inputs: Iterable[str], *, transitive: bool = True) -> Snapshot:
        with self._lock:
            keys = self._closure(inputs) if transitive else self._require(inputs)
            return Snapshot(
                self.graph_id,
                {key: self._resources[key].version for key in sorted(keys)},
                {key: self._resources[key].revision_id for key in sorted(keys)},
            )

    def validate_snapshot(self, snapshot: Snapshot) -> None:
        with self._lock:
            if snapshot.graph_id != self.graph_id:
                raise StaleSnapshotError("Snapshot belongs to another graph")
            stale = [key for key, version in snapshot.versions.items()
                     if key not in self._resources or self._resources[key].version != version
                     or self._resources[key].revision_id != snapshot.revisions[key]]
            if stale:
                raise StaleSnapshotError(f"Input versions or revision identities changed: {sorted(stale)}")

    def invalidate(self, key: str) -> frozenset[str]:
        with self._lock:
            affected = {key} | set(self.dependents(key))
            # Explicit snapshots may contain additional inputs beyond graph edges.
            affected.update(owner for owner, (_, snap) in self._cache.items()
                            if key in snap.versions)
            for owner in affected:
                self._cache.pop(owner, None)
            return frozenset(affected)

    def _bump(self, key: str, content_hash: str | None) -> frozenset[str]:
        previous = self._resources[key]
        self._resources[key] = Resource(key, previous.kind, previous.version + 1, content_hash)
        return self.invalidate(key)

    def update_resource(self, key: str, *, content_hash: str | None = None) -> frozenset[str]:
        with self._lock:
            self._require((key,))
            return self._bump(key, content_hash)

    def cache_put(self, key: str, value: Any, *, snapshot: Snapshot | None = None) -> None:
        with self._lock:
            required = self._closure((key,))
            snapshot = snapshot or self.snapshot((key,))
            self.validate_snapshot(snapshot)
            if not required <= snapshot.versions.keys():
                raise GraphError("Cache snapshot must include the resource and all its inputs")
            self._cache[key] = (value, snapshot)

    def cache_get(self, key: str) -> Any:
        with self._lock:
            if key not in self._cache:
                raise KeyError(key)
            value, snapshot = self._cache[key]
            try:
                self.validate_snapshot(snapshot)
            except StaleSnapshotError:
                self._cache.pop(key, None)
                raise KeyError(key) from None
            return value

    def begin_transaction(
        self, *, reads: Iterable[str] = (), writes: Iterable[str] = (),
        detect_conflicts: bool = True,
    ) -> Transaction:
        with self._lock:
            read_keys = self._closure(reads)
            write_keys = self._require(writes)
            # A write also depends on the current versions of its declared inputs.
            input_keys = read_keys | self._closure(write_keys)
            transaction = Transaction(uuid.uuid4().hex, frozenset(input_keys),
                                      frozenset(write_keys), self.snapshot(input_keys))
            if detect_conflicts:
                for active in self._active.values():
                    conflict = read_write_conflicts(transaction.reads, transaction.writes,
                                                    active.reads, active.writes)
                    if conflict:
                        raise ConflictError(f"In-flight resource conflicts: {sorted(conflict)}")
            self._active[transaction.token] = transaction
            return transaction

    def abort(self, transaction: Transaction) -> None:
        with self._lock:
            self._active.pop(transaction.token, None)

    def commit(
        self, transaction: Transaction, *, content_hashes: Mapping[str, str | None] | None = None,
    ) -> frozenset[str]:
        with self._lock:
            if self._active.get(transaction.token) != transaction:
                raise GraphError("Transaction is unknown or already completed")
            changes = dict(content_hashes or {})
            if not changes.keys() <= transaction.writes:
                raise GraphError("Commit contains undeclared writes")
            try:
                # Validate every input and write version before changing anything.
                self.validate_snapshot(transaction.snapshot)
                invalidated: set[str] = set()
                for key in sorted(transaction.writes):
                    invalidated.update(self._bump(key, changes.get(key, self._resources[key].content_hash)))
                return frozenset(invalidated)
            finally:
                self._active.pop(transaction.token, None)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": 1,
                "graph_id": self.graph_id,
                "resources": [
                    {"key": r.key, "kind": r.kind, "version": r.version,
                     "content_hash": r.content_hash, "revision_id": r.revision_id,
                     "dependencies": sorted(self._dependencies[r.key])}
                    for r in sorted(self._resources.values(), key=lambda item: item.key)
                ],
            }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VersionedGraph:
        if data.get("schema_version") != 1 or not isinstance(data.get("graph_id"), str) or not data["graph_id"]:
            raise GraphError("Invalid graph schema or identity")
        graph = cls(graph_id=data["graph_id"])
        records = data.get("resources", [])
        for record in records:
            version = record["version"]
            if type(version) is not int or version < 1:
                raise GraphError("Resource version must be a positive integer")
            if not isinstance(record.get("revision_id"), str) or not record["revision_id"]:
                raise GraphError("Resource revision identity is required")
            graph.add_resource(record["key"], kind=record["kind"], content_hash=record.get("content_hash"))
        for record in records:
            for dependency in record.get("dependencies", []):
                graph.add_dependency(record["key"], dependency)
        # Loading edges must not manufacture revisions in the stored history.
        for record in records:
            key = record["key"]
            graph._resources[key] = Resource(key, record["kind"], record["version"],
                                             record.get("content_hash"), record["revision_id"])
        return graph

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> VersionedGraph:
        return cls.from_dict(json.loads(text))


ResourceGraph = VersionedGraph
