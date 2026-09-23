"""A durable, single-host task and paid-invocation ledger.

Amounts are nonnegative integer microdollars. SQLite WAL transactions serialize
lease grants, reservations, and result acceptance across threads/processes. A
reservation is permission to dispatch *once*, only when ``dispatch_allowed`` is
true. It is not an exactly-once guarantee for an external provider: a crash can
leave a reservation whose external outcome must be reconciled.

Leases use the host wall clock. Hosts must share a trustworthy clock, and this
module cannot revoke work already dispatched to an external service. Late cost
reports are accepted, but stale task results are rejected. Provider-side hard
spending caps are needed if actual charges can exceed reserved estimates.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
import sqlite3
import time
from typing import Any, Iterator
import uuid


MAX_AMOUNT = (1 << 63) - 1


class LedgerError(RuntimeError):
    """Base class for rejected ledger operations."""


class TaskNotFound(LedgerError):
    """The task identifier is unknown."""


class InvocationNotFound(LedgerError):
    """The invocation identifier is unknown."""


class IdempotencyConflict(LedgerError):
    """An existing identity was reused with different immutable contents."""


class LeaseConflict(LedgerError):
    """A task is already leased or has completed."""


class StaleLease(LedgerError):
    """The supplied fence is no longer the live owner of the task."""


class BudgetExceeded(LedgerError):
    """A new reservation would exceed the available budget."""


class UnsettledInvocations(LedgerError):
    """A result cannot be accepted while task charges are unresolved."""


class ReconciliationRequired(LedgerError):
    """An uncertain charge requires explicit evidence-backed reconciliation."""


def _amount(value: int, name: str = "amount") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer number of microdollars")
    if not 0 <= value <= MAX_AMOUNT:
        raise ValueError(f"{name} must be between 0 and {MAX_AMOUNT}")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _json(value: Any) -> str:
    # Reject silent JSON coercions, such as integer keys and tuples, so the
    # bytes used to compare idempotent requests unambiguously represent inputs.
    def validate(item: Any) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                validate(child)
            return
        raise TypeError("value must contain only finite, JSON-compatible data")

    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now(value: float | None) -> float:
    result = time.time() if value is None else value
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise TypeError("now must be a finite timestamp")
    if not math.isfinite(result):
        raise ValueError("now must be a finite timestamp")
    return float(result)


def _expiry(now: float, ttl_seconds: float) -> float:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
        raise TypeError("ttl_seconds must be a positive finite number")
    if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive finite number")
    result = now + ttl_seconds
    if not math.isfinite(result) or result <= now:
        raise ValueError("ttl_seconds does not produce a usable lease deadline")
    return result


class Ledger:
    """A file-backed ledger safe to use from multiple threads and processes.

    Each operation opens its own SQLite connection. ``budget_limit`` is stored
    when the database is created; reopening with a different limit is rejected.
    A local filesystem supporting SQLite locking is required; network-mounted
    databases and ``:memory:`` databases are deliberately unsupported.
    """

    def __init__(self, path: str | os.PathLike[str], budget_limit: int = 1_000_000):
        supplied_path = os.fspath(path)
        self._budget_limit = _amount(budget_limit, "budget_limit")
        if supplied_path == ":memory:" or not supplied_path:
            raise ValueError("a nonempty, file-backed database path is required")
        self.path = os.path.abspath(supplied_path)
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError("SQLite WAL mode is required")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_config (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    budget_limit INTEGER NOT NULL CHECK (budget_limit >= 0)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'leased', 'completed')),
                    fence INTEGER NOT NULL DEFAULT 0 CHECK (fence >= 0),
                    worker_id TEXT,
                    lease_expires_at REAL,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE TABLE IF NOT EXISTS invocations (
                    invocation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    fence INTEGER NOT NULL CHECK (fence > 0),
                    reserved_amount INTEGER NOT NULL CHECK (reserved_amount >= 0),
                    actual_amount INTEGER CHECK (actual_amount >= 0),
                    status TEXT NOT NULL CHECK (status IN ('reserved', 'uncertain', 'settled')),
                    created_at REAL NOT NULL,
                    settled_at REAL,
                    reconciliation_evidence TEXT,
                    CHECK ((status = 'settled' AND actual_amount IS NOT NULL)
                        OR (status != 'settled' AND actual_amount IS NULL))
                );
                CREATE INDEX IF NOT EXISTS invocations_task ON invocations(task_id);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    invocation_id TEXT REFERENCES invocations(invocation_id),
                    kind TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO ledger_config VALUES (1, 1, ?)",
                (self.budget_limit,),
            )
            config = connection.execute("SELECT * FROM ledger_config").fetchone()
            if config["schema_version"] != 1:
                raise ValueError("unsupported ledger schema version")
            if config["budget_limit"] != self.budget_limit:
                raise ValueError("budget_limit differs from the persisted ledger limit")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @property
    def budget_limit(self) -> int:
        """The immutable limit validated against this database at construction."""
        return self._budget_limit

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _task(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return row

    @staticmethod
    def _invocation(connection: sqlite3.Connection, invocation_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM invocations WHERE invocation_id = ?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise InvocationNotFound(invocation_id)
        return row

    @staticmethod
    def _live(task: sqlite3.Row, fence: int, now: float) -> None:
        if isinstance(fence, bool) or not isinstance(fence, int):
            raise TypeError("fence must be an integer")
        if (
            task["status"] != "leased"
            or task["fence"] != fence
            or task["lease_expires_at"] <= now
        ):
            raise StaleLease(f"task {task['task_id']} does not have live fence {fence}")

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        task_id: str,
        kind: str,
        now: float,
        details: dict[str, Any],
        invocation_id: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events (task_id, invocation_id, kind, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, invocation_id, kind, _json(details), now),
        )

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        encoded = result.pop("result_json")
        result["result"] = json.loads(encoded) if encoded is not None else None
        return result

    @staticmethod
    def _totals(connection: sqlite3.Connection) -> dict[str, int]:
        # Python integer accumulation avoids SQLite SUM's signed-64-bit overflow
        # when truthful actual charges cumulatively exceed the configured limit.
        spent = reserved = uncertain = invocations = 0
        for row in connection.execute(
            "SELECT status, reserved_amount, actual_amount FROM invocations"
        ):
            invocations += 1
            if row["status"] == "settled":
                spent += row["actual_amount"]
            else:
                reserved += row["reserved_amount"]
                if row["status"] == "uncertain":
                    uncertain += row["reserved_amount"]
        return {"spent": spent, "reserved": reserved, "uncertain": uncertain,
                "invocations": invocations}

    def create_task(self, payload: dict[str, Any], idempotency_key: str) -> str:
        """Create a pending task, or return the identical existing request's ID."""
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        encoded = _json(payload)
        _identifier(idempotency_key, "idempotency_key")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT task_id, payload_json FROM tasks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != encoded:
                    raise IdempotencyConflict("task key already has a different payload")
                return existing["task_id"]
            task_id = uuid.uuid4().hex
            now = _now(None)
            connection.execute(
                "INSERT INTO tasks (task_id, idempotency_key, payload_json, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (task_id, idempotency_key, encoded, now),
            )
            self._event(connection, task_id, "task_created", now, {})
            return task_id

    def claim(
        self, task_id: str, worker_id: str, ttl_seconds: float = 60, now: float | None = None
    ) -> dict[str, Any]:
        """Claim pending/expired work and increment its durable fencing token.

        A live lease cannot be claimed again, even by the same worker. Use
        :meth:`renew` for heartbeats. Expiry does not release any reservations.
        """
        _identifier(worker_id, "worker_id")
        with self._transaction() as connection:
            timestamp = _now(now)
            expiry = _expiry(timestamp, ttl_seconds)
            task = self._task(connection, task_id)
            if task["status"] == "completed":
                raise LeaseConflict("task already completed")
            if task["status"] == "leased" and task["lease_expires_at"] > timestamp:
                raise LeaseConflict("task already has a live lease")
            if task["fence"] == MAX_AMOUNT:
                raise LeaseConflict("fencing token space exhausted")
            fence = task["fence"] + 1
            connection.execute(
                "UPDATE tasks SET status = 'leased', fence = ?, worker_id = ?, "
                "lease_expires_at = ? WHERE task_id = ?",
                (fence, worker_id, expiry, task_id),
            )
            self._event(connection, task_id, "lease_claimed", timestamp,
                        {"fence": fence, "worker_id": worker_id, "expires_at": expiry})
            return self._task_dict(self._task(connection, task_id))

    def renew(
        self, task_id: str, fence: int, ttl_seconds: float = 60, now: float | None = None
    ) -> dict[str, Any]:
        """Extend a live lease without changing its fence or shortening its expiry."""
        with self._transaction() as connection:
            timestamp = _now(now)
            task = self._task(connection, task_id)
            self._live(task, fence, timestamp)
            expiry = max(task["lease_expires_at"], _expiry(timestamp, ttl_seconds))
            connection.execute("UPDATE tasks SET lease_expires_at = ? WHERE task_id = ?",
                               (expiry, task_id))
            self._event(connection, task_id, "lease_renewed", timestamp,
                        {"fence": fence, "expires_at": expiry})
            return self._task_dict(self._task(connection, task_id))

    def reserve(
        self, task_id: str, fence: int, invocation_id: str, amount: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve funds before a single paid dispatch.

        Dispatch is allowed only for the call returning ``dispatch_allowed=True``.
        The same ID with identical task/fence/amount is an idempotent read with
        dispatch disallowed. A stale lease always fails, including on retries.
        The caller must still avoid dispatch after its returned lease deadline;
        an external provider must enforce fences for stronger revocation.
        """
        _identifier(invocation_id, "invocation_id")
        _amount(amount)
        with self._transaction() as connection:
            timestamp = _now(now)
            task = self._task(connection, task_id)
            self._live(task, fence, timestamp)
            existing = connection.execute(
                "SELECT * FROM invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
            created = existing is None
            if existing is not None:
                if (existing["task_id"], existing["fence"], existing["reserved_amount"]) != (
                    task_id, fence, amount
                ):
                    raise IdempotencyConflict("invocation ID has different reservation contents")
            else:
                totals = self._totals(connection)
                if totals["spent"] + totals["reserved"] + amount > self.budget_limit:
                    raise BudgetExceeded("insufficient uncommitted budget")
                connection.execute(
                    "INSERT INTO invocations "
                    "(invocation_id, task_id, fence, reserved_amount, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'reserved', ?)",
                    (invocation_id, task_id, fence, amount, timestamp),
                )
                self._event(connection, task_id, "funds_reserved", timestamp,
                            {"fence": fence, "amount": amount}, invocation_id)
            result = dict(self._invocation(connection, invocation_id))
            result.update(created=created, dispatch_allowed=created,
                          lease_expires_at=task["lease_expires_at"])
            return result

    def mark_uncertain(self, invocation_id: str) -> dict[str, Any]:
        """Mark an unknown provider outcome; keep the entire reservation held.

        Repeated calls are harmless. A settled invocation remains settled.
        """
        with self._transaction() as connection:
            invocation = self._invocation(connection, invocation_id)
            if invocation["status"] == "reserved":
                connection.execute("UPDATE invocations SET status = 'uncertain' "
                                   "WHERE invocation_id = ?", (invocation_id,))
                self._event(connection, invocation["task_id"], "charge_uncertain",
                            _now(None), {}, invocation_id)
            return dict(self._invocation(connection, invocation_id))

    def settle(self, invocation_id: str, actual_amount: int) -> dict[str, Any]:
        """Record known actual cost and release its reservation atomically.

        Late cost reports do not require a live lease. An actual charge larger
        than its reservation or the budget is still recorded, never hidden or
        rejected; later reservations are blocked as necessary. Identical repeat
        settlements are idempotent; changed amounts are conflicts. Uncertain
        outcomes must instead use :meth:`reconcile`.
        """
        return self._settle(invocation_id, actual_amount, evidence=None)

    def reconcile(
        self, invocation_id: str, actual_amount: int, evidence: str
    ) -> dict[str, Any]:
        """Explicitly resolve an outstanding charge using externally checked evidence.

        For a proven undispatched/unbilled invocation, use actual_amount=0.
        Evidence is retained for audit; this prototype cannot verify its truth.
        Already-settled costs are immutable, including after reconciliation.
        """
        _identifier(evidence, "evidence")
        return self._settle(invocation_id, actual_amount, evidence=evidence)

    def _settle(
        self, invocation_id: str, actual_amount: int, evidence: str | None
    ) -> dict[str, Any]:
        _amount(actual_amount, "actual_amount")
        with self._transaction() as connection:
            invocation = self._invocation(connection, invocation_id)
            if invocation["status"] == "settled":
                if invocation["actual_amount"] != actual_amount:
                    raise IdempotencyConflict("invocation already settled at a different cost")
                return dict(invocation)
            if invocation["status"] == "uncertain" and evidence is None:
                raise ReconciliationRequired("uncertain charges require reconcile with evidence")
            timestamp = _now(None)
            connection.execute(
                "UPDATE invocations SET status = 'settled', actual_amount = ?, "
                "settled_at = ?, reconciliation_evidence = ? WHERE invocation_id = ?",
                (actual_amount, timestamp, evidence, invocation_id),
            )
            self._event(connection, invocation["task_id"],
                        "charge_reconciled" if evidence is not None else "charge_settled",
                        timestamp, {"actual_amount": actual_amount, "evidence": evidence},
                        invocation_id)
            return dict(self._invocation(connection, invocation_id))

    def complete(
        self, task_id: str, fence: int, result: Any, now: float | None = None
    ) -> dict[str, Any]:
        """Atomically accept a result from the live fence with no unresolved charges.

        This checks all task invocations, including earlier expired leases.
        An identical retry by the accepted fence is idempotent after completion;
        another fence or changed result is rejected. No external side effects
        are performed by accepting a result.
        """
        encoded = _json(result)
        if isinstance(fence, bool) or not isinstance(fence, int):
            raise TypeError("fence must be an integer")
        with self._transaction() as connection:
            timestamp = _now(now)
            task = self._task(connection, task_id)
            if task["status"] == "completed":
                if task["fence"] != fence:
                    raise StaleLease("result was accepted under a different fence")
                if task["result_json"] != encoded:
                    raise IdempotencyConflict("task already completed with a different result")
                return self._task_dict(task)
            self._live(task, fence, timestamp)
            outstanding = connection.execute(
                "SELECT 1 FROM invocations WHERE task_id = ? AND status != 'settled' LIMIT 1",
                (task_id,),
            ).fetchone()
            if outstanding is not None:
                raise UnsettledInvocations("all task invocation costs must be resolved first")
            connection.execute(
                "UPDATE tasks SET status = 'completed', result_json = ?, completed_at = ?, "
                "lease_expires_at = NULL WHERE task_id = ?", (encoded, timestamp, task_id)
            )
            self._event(connection, task_id, "result_accepted", timestamp, {"fence": fence})
            return self._task_dict(self._task(connection, task_id))

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Read task metadata, decoded payload, and accepted result."""
        with self._transaction(write=False) as connection:
            return self._task_dict(self._task(connection, task_id))

    def get_invocation(self, invocation_id: str) -> dict[str, Any]:
        """Read the durable reservation/outcome record for an invocation."""
        with self._transaction(write=False) as connection:
            return dict(self._invocation(connection, invocation_id))

    def events(self, task_id: str) -> list[dict[str, Any]]:
        """Read ordered audit events; these are transactional, not tamper-proof."""
        with self._transaction(write=False) as connection:
            self._task(connection, task_id)
            result = []
            for row in connection.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY event_id", (task_id,)
            ):
                event = dict(row)
                event["details"] = json.loads(event.pop("details_json"))
                result.append(event)
            return result

    def summary(self) -> dict[str, Any]:
        """Return a consistent accounting snapshot.

        ``committed = spent + reserved``. ``uncertain`` is a subset of reserved.
        ``available`` is clamped at zero; ``over_budget`` exposes any actual-cost
        overrun plus outstanding commitments. ``spent_over_budget`` counts the
        already-observed overrun alone. Totals use Python's unbounded integers.
        """
        with self._transaction(write=False) as connection:
            result: dict[str, Any] = self._totals(connection)
            committed = result["spent"] + result["reserved"]
            result.update(
                budget_limit=self.budget_limit,
                committed=committed,
                available=max(0, self.budget_limit - committed),
                over_budget=max(0, committed - self.budget_limit),
                spent_over_budget=max(0, result["spent"] - self.budget_limit),
                tasks={row["status"]: row["count"] for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                )},
            )
            return result
