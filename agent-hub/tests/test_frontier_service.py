"""Offline lifecycle tests: persistence/races/uncertain commits, no providers."""
import copy
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from agent_hub.frontier_service import (
    CommitUncertain, CONTROL_KEY, FrontierError, ResearchService, SOURCE_ARCHIVE_SHA256,
)
from agent_hub.store import FirestoreStore, SQLiteStore


BUCKET = "test-frontier-artifacts"
NOW = 1790035200


def result(parameters, request_id):
    sha = "a" * 64
    return {
        "schema_version": 1, "parameters": copy.deepcopy(parameters),
        "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
        "summary": {"seed": parameters["seed"], "units": parameters["units"],
                    "budget": parameters["budget"], "confirmation": "inconclusive",
                    "promotion_status": "inconclusive", "active_release": None,
                    "model_calls": 0, "api_spend_microdollars": 0},
        "artifact": {"bucket": BUCKET,
                     "object": f"frontier/runs/{request_id}/{sha}.tar.gz",
                     "generation": "1789936000123456", "sha256": sha, "bytes": 512},
    }


class FaultStore:
    """Raise at a particular commit boundary, possibly after durable commit."""
    def __init__(self, delegate, fail_on, *, after):
        self.delegate, self.fail_on, self.after = delegate, fail_on, after
        self.calls = 0

    def get_state(self, key):
        return self.delegate.get_state(key)

    def mutate_states(self, keys, callback):
        self.calls += 1
        if self.calls == self.fail_on and not self.after:
            raise TimeoutError("private backend internals")
        value = self.delegate.mutate_states(keys, callback)
        if self.calls == self.fail_on and self.after:
            raise TimeoutError("private backend internals")
        return value


class RetryingFirestore:
    """Use the real FirestoreStore adapter with aborted transaction trials."""
    def __init__(self):
        self.rows = {}
        self.trials = 0
        self.lock = threading.RLock()
        self.store = object.__new__(FirestoreStore)
        self.store.client = SimpleNamespace(transaction=lambda: None)
        self.store.durable_state = self
        self.store.firestore = SimpleNamespace(transactional=self.transactional)

    def document(self, key):
        backend = self
        class Reference:
            def get(self, transaction=None):
                data = copy.deepcopy(backend.rows.get(key))
                return SimpleNamespace(exists=data is not None, to_dict=lambda: copy.deepcopy(data))
        reference = Reference()
        reference.key = key
        return reference

    def transactional(self, callback):
        def run(_):
            with self.lock:
                for attempt in range(3):
                    pending = {}
                    transaction = SimpleNamespace(set=lambda ref, data: pending.__setitem__(ref.key, copy.deepcopy(data)))
                    value = callback(transaction)
                    self.trials += 1
                    if attempt == 2:
                        self.rows.update(pending)
                        return value
        return run


class FrontierServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "authority.sqlite"
        self.store = SQLiteStore(self.path)
        self.calls = []
        self.now = NOW

    def runner(self, parameters, request_id):
        self.calls.append(request_id)
        return result(parameters, request_id)

    def service(self, store=None, runner=None):
        return ResearchService(store or self.store, runner or self.runner,
                               artifact_bucket=BUCKET, clock=lambda: self.now)

    def test_success_survives_restart_and_duplicate_defaults(self):
        original = self.service().run({"request_id": "one"})
        restart = self.service(SQLiteStore(self.path))
        self.assertEqual(original, restart.get({"request_id": "one"}))
        self.assertEqual(original, restart.run({"request_id": "one", "workspace": "default",
                                                "seed": 20260921, "units": 4, "budget": 6}))
        self.assertEqual(self.calls, ["one"])
        self.assertEqual(original["status"], "succeeded")
        self.assertNotIn("dispatch_nonce", original)
        self.assertIsNone(self.store.get_state(CONTROL_KEY)["active"])
        with self.assertRaisesRegex(FrontierError, "parameters_conflict"):
            restart.run({"request_id": "one", "budget": 7})
        self.assertEqual(self.calls, ["one"])

    def test_duplicate_racing_requests_never_duplicate_runner(self):
        entered, release = threading.Event(), threading.Event()
        def slow(parameters, request_id):
            self.calls.append(request_id)
            entered.set()
            self.assertTrue(release.wait(3))
            return result(parameters, request_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service(runner=slow).run, {"request_id": "race"})
            self.assertTrue(entered.wait(3))
            duplicate = self.service(SQLiteStore(self.path)).run({"request_id": "race"})
            self.assertEqual(duplicate["status"], "reserved")
            with self.assertRaisesRegex(FrontierError, "another_run_active"):
                self.service().run({"request_id": "other", "workspace": "other_workspace"})
            release.set()
            self.assertEqual(first.result(timeout=3)["status"], "succeeded")
        self.assertEqual(self.calls, ["race"])

    def test_firestore_transaction_retries_have_no_runner_or_double_charge(self):
        backend = RetryingFirestore()
        value = self.service(backend.store).run({"request_id": "trial"})
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(self.calls, ["trial"])
        self.assertEqual(backend.trials, 6)
        daily = [row for key, row in backend.rows.items() if key.startswith("frontier_day_")]
        self.assertEqual([d["charged_runs"] for d in daily], [1])

    def test_lost_reservation_ack_never_dispatches_or_takes_over_next_day(self):
        service = self.service(FaultStore(self.store, 1, after=True))
        with self.assertRaises(CommitUncertain):
            service.run({"request_id": "lost"})
        self.assertEqual(self.calls, [])
        self.now += 86400 * 10
        self.assertEqual(self.service().run({"request_id": "lost"})["status"], "reserved")
        with self.assertRaisesRegex(FrontierError, "another_run_active"):
            self.service().run({"request_id": "successor"})
        self.assertEqual(self.calls, [])

    def test_definite_absence_can_be_read_after_failed_reservation(self):
        with self.assertRaises(CommitUncertain):
            self.service(FaultStore(self.store, 1, after=False)).run({"request_id": "absent"})
        self.assertEqual(self.service().get({"request_id": "absent"})["status"], "not_found")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service().run({"request_id": "absent"})["status"], "succeeded")
        self.assertEqual(self.calls, ["absent"])

    def test_lost_publication_ack_returns_durable_result_without_rerun(self):
        with self.assertRaises(CommitUncertain):
            self.service(FaultStore(self.store, 2, after=True)).run({"request_id": "saved"})
        self.assertEqual(self.service().get({"request_id": "saved"})["status"], "succeeded")
        self.assertEqual(self.service().run({"request_id": "saved"})["status"], "succeeded")
        self.assertEqual(self.calls, ["saved"])
        self.assertIsNone(self.store.get_state(CONTROL_KEY)["active"])

    def test_failed_publication_does_not_release_or_reconstruct_by_rerunning(self):
        with self.assertRaises(CommitUncertain):
            self.service(FaultStore(self.store, 2, after=False)).run({"request_id": "unsaved"})
        self.assertEqual(self.service().run({"request_id": "unsaved"})["status"], "reserved")
        self.assertEqual(self.calls, ["unsaved"])
        self.assertEqual(self.store.get_state(CONTROL_KEY)["active"]["request_id"], "unsaved")

    def test_runner_error_is_sanitized_and_keeps_charge_and_slot(self):
        def failed(parameters, request_id):
            self.calls.append(request_id)
            raise RuntimeError("private-token-provider-output")
        with self.assertRaises(FrontierError) as caught:
            self.service(runner=failed).run({"request_id": "failed"})
        self.assertNotIn("private", str(caught.exception))
        receipt = self.service().get({"request_id": "failed"})
        self.assertEqual(receipt["status"], "blocked_uncertain")
        self.assertNotIn("private", str(receipt))
        self.now += 86400
        self.assertEqual(self.service().run({"request_id": "failed"}), receipt)
        with self.assertRaisesRegex(FrontierError, "another_run_active"):
            self.service().run({"request_id": "next"})
        self.assertEqual(self.calls, ["failed"])

    def test_unknown_block_ack_still_never_retries(self):
        def failed(*_):
            self.calls.append("failed")
            raise TimeoutError()
        with self.assertRaises(CommitUncertain):
            self.service(FaultStore(self.store, 2, after=True), failed).run({"request_id": "failed"})
        self.assertEqual(self.service().run({"request_id": "failed"})["status"], "blocked_uncertain")
        self.assertEqual(self.calls, ["failed"])

    def test_four_runs_per_utc_day_is_durable_and_no_duplicate_charge(self):
        for index in range(4):
            self.service(SQLiteStore(self.path)).run({"request_id": f"day-{index}"})
        self.service().run({"request_id": "day-0"})
        with self.assertRaises(FrontierError) as caught:
            self.service().run({"request_id": "fifth"})
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(self.calls), 4)
        self.now += 86400
        self.service().run({"request_id": "fifth"})
        self.assertEqual(len(self.calls), 5)

    def test_bad_inputs_do_not_claim_or_charge(self):
        bad = [None, [], {}, {"request_id": "../bad"}, {"request_id": "x", "units": True},
               {"request_id": "x", "units": 9}, {"request_id": "x", "budget": 0},
               {"request_id": "x", "seed": -1}, {"request_id": "x", "seed": 2**31},
               {"request_id": "x", "workspace": "a/b"}, {"request_id": "x", "provider": "paid"}]
        for document in bad:
            with self.subTest(document=document), self.assertRaises(FrontierError):
                self.service().run(document)
        self.assertFalse(self.store.get_state(CONTROL_KEY))
        self.assertEqual(self.calls, [])

    def test_bad_runner_receipts_fail_closed_and_keep_slot(self):
        changes = [
            lambda v: v.update(source_archive_sha256="b" * 64),
            lambda v: v["parameters"].update(seed=1),
            lambda v: v["summary"].update(seed=1),
            lambda v: v["summary"].update(confirmation="evidence_passed_manual_review_required"),
            lambda v: v["summary"].update(model_calls=1),
            lambda v: v["summary"].update(model_calls=False),
            lambda v: v["summary"].update(api_spend_microdollars=1),
            lambda v: v["summary"].update(active_release="release-id"),
            lambda v: v["summary"].update(promotion_status="active"),
            lambda v: v["summary"].update(oversized="x" * 50000),
            lambda v: v["summary"].update(nan=float("nan")),
            lambda v: v["artifact"].update(bucket="unapproved-bucket"),
            lambda v: v["artifact"].update(object="frontier/runs/another/" + "a" * 64 + ".tar.gz"),
            lambda v: v["artifact"].update(generation="latest"),
            lambda v: v["artifact"].update(bytes=33 * 1024 * 1024),
        ]
        for index, alter in enumerate(changes):
            isolated = SQLiteStore(Path(self.temporary.name) / f"bad-{index}.sqlite")
            def malformed(parameters, request_id):
                value = result(parameters, request_id)
                alter(value)
                return value
            with self.subTest(index=index), self.assertRaisesRegex(FrontierError, "reconciliation_required"):
                self.service(isolated, malformed).run({"request_id": "bad"})
            self.assertEqual(self.service(isolated).get({"request_id": "bad"})["status"], "blocked_uncertain")
            self.assertIsNotNone(isolated.get_state(CONTROL_KEY)["active"])

    def test_results_are_copied_and_tampering_is_detected(self):
        saved = self.service().run({"request_id": "copy"})
        saved["result"]["summary"]["seed"] = 999
        self.assertEqual(self.service().get({"request_id": "copy"})["result"]["summary"]["seed"], 20260921)
        def tamper(states):
            states["frontier_run_copy"]["result"]["summary"]["seed"] = 999
        self.store.mutate_states(("frontier_run_copy",), tamper)
        with self.assertRaisesRegex(FrontierError, "runner_summary_parameters_mismatch"):
            self.service().get({"request_id": "copy"})

    def test_fence_loss_never_publishes_or_releases_someone_else(self):
        def runner(parameters, request_id):
            def change(states):
                states[CONTROL_KEY]["active"]["dispatch_nonce"] = "other"
            self.store.mutate_states((CONTROL_KEY,), change)
            return result(parameters, request_id)
        with self.assertRaisesRegex(FrontierError, "publication_fence_lost"):
            self.service(runner=runner).run({"request_id": "fenced"})
        self.assertEqual(self.store.get_state(CONTROL_KEY)["active"]["dispatch_nonce"], "other")
        self.assertEqual(self.service().get({"request_id": "fenced"})["status"], "reserved")


if __name__ == "__main__":
    unittest.main()
