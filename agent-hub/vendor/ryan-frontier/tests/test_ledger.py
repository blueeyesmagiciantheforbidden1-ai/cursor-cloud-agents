"""Failure and concurrency tests for the durable ledger (no external services)."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from ryan_frontier.ledger import (
    BudgetExceeded,
    IdempotencyConflict,
    Ledger,
    LeaseConflict,
    MAX_AMOUNT,
    ReconciliationRequired,
    StaleLease,
    UnsettledInvocations,
)


def _reserve_then_exit(database: str) -> None:
    """Die without interpreter cleanup after committing a reservation."""
    ledger = Ledger(database, budget_limit=100)
    task = ledger.create_task({"work": "crash-test"}, "crash")
    lease = ledger.claim(task, "doomed-worker", ttl_seconds=10, now=100)
    ledger.reserve(task, lease["fence"], "crash-invocation", 70, now=101)
    os._exit(0)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.database, budget_limit=100)

    def task(self, key: str = "task") -> tuple[str, int]:
        task_id = self.ledger.create_task({"work": key}, key)
        lease = self.ledger.claim(task_id, "worker", ttl_seconds=100, now=100)
        return task_id, lease["fence"]

    @staticmethod
    def race(count, operation):
        barrier = threading.Barrier(count)

        def run(index):
            barrier.wait(timeout=15)
            try:
                return operation(index)
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(run, range(count)))

    def test_wal_and_persisted_budget(self):
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with self.assertRaises(ValueError):
            Ledger(self.database, budget_limit=101)
        self.assertEqual(Ledger(self.database, budget_limit=100).summary()["budget_limit"], 100)

    def test_task_idempotency_compares_canonical_payload(self):
        first = self.ledger.create_task({"a": 1, "b": [2, 3]}, "same")
        second = self.ledger.create_task({"b": [2, 3], "a": 1}, "same")
        self.assertEqual(first, second)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.create_task({"a": 2, "b": [2, 3]}, "same")
        self.assertEqual(len(self.ledger.events(first)), 1)

    def test_invalid_inputs_do_not_silently_coerce_money_or_json(self):
        task, fence = self.task()
        for amount in (-1, 1.5, True, MAX_AMOUNT + 1):
            with self.subTest(amount=amount), self.assertRaises((TypeError, ValueError)):
                self.ledger.reserve(task, fence, "invalid", amount, now=101)
        for payload in ({1: "ambiguous"}, {"x": float("nan")}, {"x": (1, 2)}):
            with self.subTest(payload=payload), self.assertRaises(TypeError):
                self.ledger.create_task(payload, "invalid")
        for timestamp in (float("inf"), float("nan"), True):
            with self.subTest(timestamp=timestamp), self.assertRaises((TypeError, ValueError)):
                self.ledger.reserve(task, fence, "invalid", 1, now=timestamp)
        self.assertEqual(self.ledger.summary()["invocations"], 0)

    def test_expiry_reclaim_fences_and_renewal(self):
        task = self.ledger.create_task({}, "leases")
        first = self.ledger.claim(task, "a", ttl_seconds=10, now=100)
        with self.assertRaises(LeaseConflict):
            self.ledger.claim(task, "a", now=109)
        renewed = self.ledger.renew(task, first["fence"], ttl_seconds=10, now=105)
        self.assertEqual(renewed["lease_expires_at"], 115)
        # A short renewal does not shorten an existing lease.
        self.assertEqual(self.ledger.renew(task, 1, ttl_seconds=1, now=106)["lease_expires_at"], 115)
        with self.assertRaises(StaleLease):
            self.ledger.renew(task, first["fence"], now=115)
        second = self.ledger.claim(task, "b", ttl_seconds=10, now=115)
        self.assertEqual(second["fence"], first["fence"] + 1)
        for action in (
            lambda: self.ledger.reserve(task, first["fence"], "stale", 1, now=116),
            lambda: self.ledger.complete(task, first["fence"], {"bad": True}, now=116),
            lambda: self.ledger.renew(task, first["fence"], now=116),
        ):
            with self.assertRaises(StaleLease):
                action()

    def test_reservation_retries_do_not_authorize_duplicate_dispatch(self):
        task, fence = self.task()
        first = self.ledger.reserve(task, fence, "one", 40, now=101)
        retry = self.ledger.reserve(task, fence, "one", 40, now=102)
        self.assertTrue(first["created"])
        self.assertTrue(first["dispatch_allowed"])
        self.assertFalse(retry["created"])
        self.assertFalse(retry["dispatch_allowed"])
        self.assertEqual(retry["lease_expires_at"], 200)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.reserve(task, fence, "one", 41, now=103)
        with self.assertRaises(StaleLease):
            self.ledger.reserve(task, fence, "one", 40, now=200)
        self.assertEqual(self.ledger.summary()["reserved"], 40)

    def test_invocation_identity_cannot_move_to_another_task_or_fence(self):
        task, fence = self.task()
        other, other_fence = self.task("other")
        self.ledger.reserve(task, fence, "global-id", 10, now=101)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.reserve(other, other_fence, "global-id", 10, now=102)
        successor = self.ledger.claim(task, "new-worker", now=201)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.reserve(task, successor["fence"], "global-id", 10, now=202)

    def test_uncertain_funds_survive_expiry_until_explicit_reconciliation(self):
        task, fence = self.task()
        self.ledger.reserve(task, fence, "unknown", 80, now=101)
        self.ledger.mark_uncertain("unknown")
        self.ledger.mark_uncertain("unknown")
        successor = self.ledger.claim(task, "replacement", now=201)
        summary = self.ledger.summary()
        self.assertEqual((summary["reserved"], summary["uncertain"], summary["available"]), (80, 80, 20))
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve(task, successor["fence"], "too-much", 21, now=202)
        with self.assertRaises(ReconciliationRequired):
            self.ledger.settle("unknown", 0)
        with self.assertRaises(ValueError):
            self.ledger.reconcile("unknown", 0, " ")
        with self.assertRaises(UnsettledInvocations):
            self.ledger.complete(task, successor["fence"], "done", now=202)
        resolved = self.ledger.reconcile("unknown", 30, "Provider receipt receipt-123 reports 30.")
        self.assertEqual(resolved["reconciliation_evidence"], "Provider receipt receipt-123 reports 30.")
        self.assertEqual(self.ledger.summary()["available"], 70)
        self.assertEqual(self.ledger.complete(task, successor["fence"], "done", now=203)["result"], "done")

    def test_settlement_is_idempotent_and_releases_only_its_reservation(self):
        task, fence = self.task()
        self.ledger.reserve(task, fence, "one", 60, now=101)
        self.ledger.reserve(task, fence, "two", 40, now=101)
        result = self.ledger.settle("one", 25)
        self.assertEqual(self.ledger.settle("one", 25), result)
        self.assertEqual(self.ledger.mark_uncertain("one"), result)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.settle("one", 24)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.reconcile("one", 0, "Cannot rewrite already-recorded spend.")
        summary = self.ledger.summary()
        self.assertEqual((summary["spent"], summary["reserved"], summary["available"]), (25, 40, 35))

    def test_overage_is_recorded_truthfully_and_blocks_new_reservations(self):
        task, fence = self.task()
        self.ledger.reserve(task, fence, "unexpected-charge", 60, now=101)
        self.ledger.reserve(task, fence, "still-running", 40, now=101)
        self.ledger.settle("unexpected-charge", 120)
        summary = self.ledger.summary()
        self.assertEqual(summary["spent"], 120)
        self.assertEqual(summary["reserved"], 40)
        self.assertEqual(summary["over_budget"], 60)
        self.assertEqual(summary["spent_over_budget"], 20)
        self.assertEqual(summary["available"], 0)
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve(task, fence, "blocked", 1, now=102)
        self.ledger.reconcile("still-running", 0, "Provider confirms request never arrived.")
        self.assertEqual(self.ledger.summary()["over_budget"], 20)

    def test_accounting_totals_do_not_overflow_sqlite_integer_sum(self):
        big = Ledger(Path(self.temporary.name) / "big.sqlite3", budget_limit=MAX_AMOUNT)
        task = big.create_task({}, "big")
        fence = big.claim(task, "worker", now=100)["fence"]
        big.reserve(task, fence, "a", 1, now=101)
        big.reserve(task, fence, "b", 1, now=101)
        big.settle("a", MAX_AMOUNT)
        big.settle("b", MAX_AMOUNT)
        self.assertEqual(big.summary()["spent"], MAX_AMOUNT * 2)
        self.assertEqual(big.summary()["over_budget"], MAX_AMOUNT)

    def test_completed_result_is_immutable_and_idempotent(self):
        task, fence = self.task()
        accepted = self.ledger.complete(task, fence, {"answer": 42}, now=101)
        self.assertEqual(self.ledger.complete(task, fence, {"answer": 42}, now=1000), accepted)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.complete(task, fence, {"answer": 43}, now=102)
        with self.assertRaises(StaleLease):
            self.ledger.complete(task, fence + 1, {"answer": 42}, now=102)
        with self.assertRaises(LeaseConflict):
            self.ledger.claim(task, "late-worker", now=1000)
        with self.assertRaises(StaleLease):
            self.ledger.reserve(task, fence, "late", 1, now=102)
        events = self.ledger.events(task)
        self.assertEqual(sum(event["kind"] == "result_accepted" for event in events), 1)

    def test_fault_in_result_acceptance_rolls_back_result_and_audit_atomically(self):
        task, fence = self.task()
        original_events = self.ledger.events(task)
        with patch.object(self.ledger, "_event", side_effect=RuntimeError("simulated disk failure")):
            with self.assertRaises(RuntimeError):
                self.ledger.complete(task, fence, {"must": "roll back"}, now=101)
        self.assertEqual(self.ledger.get_task(task)["status"], "leased")
        self.assertIsNone(self.ledger.get_task(task)["result"])
        self.assertEqual(self.ledger.events(task), original_events)
        self.ledger.complete(task, fence, {"accepted": True}, now=102)

    def test_restart_preserves_uncertainty_fences_results_and_spending(self):
        task, fence = self.task()
        self.ledger.reserve(task, fence, "restart", 70, now=101)
        self.ledger.mark_uncertain("restart")
        restarted = Ledger(self.database, budget_limit=100)
        self.assertEqual(restarted.summary()["uncertain"], 70)
        lease = restarted.claim(task, "new-process", now=201)
        self.assertEqual(lease["fence"], fence + 1)
        restarted.reconcile("restart", 50, "Confirmed charge after process restart.")
        restarted.complete(task, lease["fence"], {"durable": True}, now=202)
        again = Ledger(self.database, budget_limit=100)
        self.assertEqual(again.get_task(task)["result"], {"durable": True})
        self.assertEqual(again.summary()["spent"], 50)
        self.assertEqual(again.summary()["reserved"], 0)

    def test_abrupt_process_exit_does_not_refund_committed_reservation(self):
        process = multiprocessing.get_context("spawn").Process(
            target=_reserve_then_exit, args=(str(self.database),)
        )
        process.start()
        process.join(timeout=20)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail("crash-test process did not finish")
        self.assertEqual(process.exitcode, 0)
        restarted = Ledger(self.database, budget_limit=100)
        task = restarted.create_task({"work": "crash-test"}, "crash")
        self.assertEqual(restarted.summary()["reserved"], 70)
        successor = restarted.claim(task, "replacement", now=110)
        self.assertEqual(successor["fence"], 2)
        with self.assertRaises(StaleLease):
            restarted.complete(task, 1, "stale", now=111)
        with self.assertRaises(UnsettledInvocations):
            restarted.complete(task, 2, "unknown-charge", now=111)

    def test_concurrent_task_idempotency_has_one_identity(self):
        results = self.race(12, lambda _: self.ledger.create_task({"a": 1}, "concurrent"))
        self.assertTrue(all(isinstance(result, str) for result in results), results)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.ledger.summary()["tasks"], {"pending": 1})

    def test_concurrent_claims_have_exactly_one_winner(self):
        task = self.ledger.create_task({}, "race-claim")
        results = self.race(12, lambda index: self.ledger.claim(task, f"worker-{index}", now=100))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(sum(isinstance(result, LeaseConflict) for result in results), 11, results)
        self.assertEqual(self.ledger.get_task(task)["fence"], 1)

    def test_concurrent_reservations_cannot_oversubscribe_budget(self):
        tasks = [self.task(f"task-{index}") for index in range(16)]
        results = self.race(16, lambda index: self.ledger.reserve(
            tasks[index][0], tasks[index][1], f"invoke-{index}", 15, now=101
        ))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 6, results)
        self.assertEqual(sum(isinstance(result, BudgetExceeded) for result in results), 10, results)
        self.assertEqual(self.ledger.summary()["reserved"], 90)
        self.assertEqual(self.ledger.summary()["available"], 10)

    def test_concurrent_duplicate_reservations_authorize_one_dispatch(self):
        task, fence = self.task()
        results = self.race(12, lambda _: self.ledger.reserve(task, fence, "shared", 60, now=101))
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        self.assertEqual(sum(result["dispatch_allowed"] for result in results), 1)
        self.assertEqual(self.ledger.summary()["reserved"], 60)

    def test_concurrent_duplicate_settlements_count_actual_cost_once(self):
        task, fence = self.task()
        self.ledger.reserve(task, fence, "shared", 80, now=101)
        results = self.race(12, lambda _: self.ledger.settle("shared", 33))
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        self.assertEqual(self.ledger.summary()["spent"], 33)
        self.assertEqual(self.ledger.summary()["reserved"], 0)
        self.assertEqual(sum(event["kind"] == "charge_settled" for event in self.ledger.events(task)), 1)

    def test_concurrent_conflicting_results_have_one_accepted_value(self):
        task, fence = self.task()
        results = self.race(2, lambda index: self.ledger.complete(task, fence, {"winner": index}, now=101))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(sum(isinstance(result, IdempotencyConflict) for result in results), 1, results)
        accepted = next(result for result in results if isinstance(result, dict))
        self.assertEqual(self.ledger.get_task(task)["result"], accepted["result"])

    def test_completion_and_reservation_race_cannot_leave_completed_unsettled_task(self):
        task, fence = self.task()

        def operation(index):
            if index == 0:
                return self.ledger.complete(task, fence, "done", now=101)
            return self.ledger.reserve(task, fence, "simultaneous", 10, now=101)

        results = self.race(2, operation)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        state = self.ledger.get_task(task)
        if state["status"] == "completed":
            self.assertIsInstance(results[1], StaleLease)
            self.assertEqual(self.ledger.summary()["reserved"], 0)
        else:
            self.assertIsInstance(results[0], UnsettledInvocations)
            self.assertEqual(self.ledger.summary()["reserved"], 10)

    def test_expiry_race_always_rejects_old_result_and_preserves_late_cost(self):
        task, fence = self.task()
        self.ledger.reserve(task, fence, "late-cost", 50, now=101)

        def operation(index):
            if index == 0:
                return self.ledger.complete(task, fence, "stale", now=200)
            return self.ledger.claim(task, "replacement", now=200)

        results = self.race(2, operation)
        self.assertIsInstance(results[0], StaleLease)
        self.assertIsInstance(results[1], dict)
        self.assertEqual(results[1]["fence"], fence + 1)
        # A stale result cannot commit, but the old provider's real charge counts.
        self.ledger.settle("late-cost", 45)
        self.assertEqual(self.ledger.summary()["spent"], 45)
        self.ledger.complete(task, results[1]["fence"], "current", now=201)


if __name__ == "__main__":
    unittest.main()
