"""Durable SQLite and simulated Firestore transactions; no live service calls."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from agent_hub.improvement_release import GATES, ReleaseConflict, digest
from agent_hub.release_coordinator import (
    AccessDenied, Authorization, CommitUncertain, CoordinatorError, DeploymentObservation,
    EvidenceApproval, ReleaseCoordinator,
)
from agent_hub.store import FirestoreStore, SQLiteStore
from tests.test_improvement_release import spec


class SyntheticTrust:
    """An explicit test-only trust fixture, not a production authenticator."""
    def __init__(self):
        self.now = 100
        self.context = object()
        self.subject = "deployer"
        self.issuer = "independent-verifier"
        self.target = {"revision": "1" * 40, "artifact_sha256": "1" * 64,
                       "generation": "generation-1", "traffic_percent": 100}
        self.healthy = True
        self.calls = {"authorize": 0, "verify": 0, "observe": 0}
        self.bad_binding = False
        self.deny_evidence = False
        self.verification_hook = None

    def authorize(self, context, request):
        self.calls["authorize"] += 1
        if context is not self.context:
            raise ValueError("not authenticated")
        return Authorization(self.subject, request["operation"], digest(request["scope"]),
                             request["request_sha256"], "a" * 64, self.now + 30)

    def verify(self, request):
        self.calls["verify"] += 1
        if self.deny_evidence:
            raise ValueError("synthetic evidence rejection")
        if self.verification_hook:
            self.verification_hook()
        return EvidenceApproval(self.issuer, "f" * 64 if self.bad_binding else digest(request), "b" * 64, self.now)

    def observe(self, request):
        self.calls["observe"] += 1
        return DeploymentObservation(request["scope"]["service"], copy.deepcopy(self.target), self.now,
                                     "c" * 64, self.healthy)

    def coordinator(self, store, *, service="services/hub", repository="hub"):
        return ReleaseCoordinator(store, service=service, repository=repository, authorize=self.authorize,
            verify_evidence=self.verify, observe=self.observe, trusted_verifier_subjects=["independent-verifier"],
            clock=lambda: self.now)


class Workflow:
    def __init__(self, coordinator, trust):
        self.coordinator, self.trust = coordinator, trust
        self.record = coordinator.create(spec(), auth_context=trust.context).record
        self.sequence = 0

    def event(self, kind, payload, identifier=None):
        self.sequence += 1
        return {"id": identifier or f"event-{self.sequence}", "kind": kind, "payload": copy.deepcopy(payload)}

    def apply_event(self, event, *, version=None):
        result = self.coordinator.apply(self.record["id"], event,
            expected_version=self.record["version"] if version is None else version, auth_context=self.trust.context)
        self.record = result.record
        return result

    def send(self, kind, payload, identifier=None):
        return self.apply_event(self.event(kind, payload, identifier))

    def receipt(self, gate=None, status="passed"):
        policy = self.record["spec"]["policy"]
        value = {"release_id": self.record["id"], "status": status,
            "definition_sha256": policy["gate_definitions"][gate] if gate else policy["health_definition_sha256"],
            "evaluator_sha256": policy["evaluator_sha256"], "report_sha256": digest([gate, status, self.trust.now]),
            "verifier": "independent-verifier", "observed_at": self.trust.now}
        if gate:
            value["gate"] = gate
            if gate == "budget":
                value.update(spent_microusd=100, reserved_microusd=500, reservation_sha256="7" * 64)
        else:
            value["window_started_at"] = self.record["phase_started_at"]
        return value

    def ready(self):
        for gate in GATES:
            self.send("evidence", self.receipt(gate))

    def stage(self):
        self.ready(); self.send("stage", {})

    def claim_event(self, identifier=None):
        return self.event("claim", {"action_id": self.record["actions"][-1]["id"], "executor": self.trust.subject}, identifier)

    def acknowledge(self, outcome="applied", *, reconcile=False):
        action = self.record["actions"][-1]
        if outcome == "applied":
            self.trust.target = {"revision": action["target_revision"], "artifact_sha256": action["target_artifact_sha256"],
                "generation": "applied-" + action["kind"], "traffic_percent": action["traffic_percent"]}
        payload = {"action_id": action["id"], "executor": self.trust.subject, "outcome": outcome, "receipt_sha256": "d" * 64}
        if reconcile:
            payload["execution_terminal"] = True
        return self.send("reconcile" if reconcile else "acknowledge", payload)

    def applied(self):
        self.apply_event(self.claim_event()); self.acknowledge()

    def canary(self):
        self.stage(); self.applied()

    def promoted(self):
        self.canary(); self.trust.now += 10
        self.send("health", self.receipt()); self.send("promote", {}); self.applied()
        self.trust.now += 5
        self.send("health", self.receipt())

    def retire(self, identifier="retire-once"):
        return self.coordinator.retire(self.record["id"], event_id=identifier,
            expected_version=self.record["version"], auth_context=self.trust.context)


class LostCommitAck:
    def __init__(self, store):
        self.store = store
        self.lose_next = False

    def get_state(self, key):
        return self.store.get_state(key)

    def mutate_states(self, keys, callback):
        result = self.store.mutate_states(keys, callback)
        if self.lose_next:
            self.lose_next = False
            raise OSError("simulated commit acknowledgment loss")
        return result


class SQLiteCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="release-coordinator-test-")
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = SQLiteStore(self.path)
        self.trust = SyntheticTrust()
        self.coordinator = self.trust.coordinator(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_restart_keeps_claim_and_repeated_request_never_redispatches(self):
        flow = Workflow(self.coordinator, self.trust); flow.stage()
        event = flow.claim_event("stable-claim")
        previous = flow.record["version"]
        first = flow.apply_event(event)
        self.assertEqual(first.dispatch["coordination"]["service_fence"], 1)
        reopened = self.trust.coordinator(SQLiteStore(self.path))
        read = reopened.read(flow.record["id"], auth_context=self.trust.context)
        self.assertIsNone(read.dispatch)
        retried = reopened.apply(flow.record["id"], event, expected_version=previous, auth_context=self.trust.context)
        self.assertTrue(retried.replayed); self.assertIsNone(retried.dispatch)
        self.assertEqual(read.record, retried.record)
        self.assertTrue(reopened.create(spec(), auth_context=self.trust.context).replayed)
        self.assertEqual(reopened.read(flow.record["id"], auth_context=self.trust.context).record["actions"][-1]["state"], "claimed")

    def test_competing_candidate_creation_has_only_one_slot_owner(self):
        barrier = threading.Barrier(2)
        self.trust.verification_hook = lambda: barrier.wait(timeout=5)
        first, second = spec(), spec()
        second.update(candidate_revision="3" * 40, candidate_artifact_sha256="3" * 64)

        def create(value):
            try:
                return self.coordinator.create(value, auth_context=self.trust.context)
            except ReleaseConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, (first, second)))
        self.assertEqual(sum(result is not None for result in results), 1)
        winner = next(result for result in results if result is not None)
        self.assertEqual(self.store.get_state(self.coordinator.service_key)["active_release"], winner.record["id"])
        loser = digest(second if winner.record["id"] == digest(first) else first)
        self.assertEqual(self.store.get_state(self.coordinator.record_key(loser)), {})

    def test_service_and_repository_locks_each_prevent_namespace_bypass(self):
        Workflow(self.coordinator, self.trust)
        same_service = self.trust.coordinator(self.store, repository="different-repo")
        other = spec(); other["repository"] = "different-repo"
        with self.assertRaisesRegex(ReleaseConflict, "another_release"):
            same_service.create(other, auth_context=self.trust.context)
        same_repo = self.trust.coordinator(self.store, service="services/other")
        with self.assertRaisesRegex(ReleaseConflict, "another_release"):
            same_repo.create(spec(), auth_context=self.trust.context)

    def test_racing_claims_yield_one_confirmed_dispatch(self):
        flow = Workflow(self.coordinator, self.trust); flow.stage()
        version = flow.record["version"]
        barrier = threading.Barrier(2)
        self.trust.verification_hook = lambda: barrier.wait(timeout=5)
        events = [flow.claim_event("claim-a"), flow.claim_event("claim-b")]

        def claim(event):
            try:
                return self.coordinator.apply(flow.record["id"], event, expected_version=version, auth_context=self.trust.context).dispatch
            except ReleaseConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, events))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_lost_storage_ack_returns_no_dispatch_and_requires_reconciliation(self):
        lossy = LostCommitAck(self.store)
        coordinator = self.trust.coordinator(lossy)
        flow = Workflow(coordinator, self.trust); flow.stage()
        event, version = flow.claim_event("lost-claim"), flow.record["version"]
        lossy.lose_next = True
        with self.assertRaisesRegex(CommitUncertain, "no_dispatch"):
            flow.apply_event(event)
        replay = coordinator.apply(flow.record["id"], event, expected_version=version, auth_context=self.trust.context)
        self.assertIsNone(replay.dispatch); self.assertTrue(replay.replayed)
        flow.record = replay.record
        with self.assertRaises(ReleaseConflict):
            flow.apply_event(flow.claim_event())
        flow.send("uncertain", {"action_id": flow.record["actions"][-1]["id"]})
        with self.assertRaises(ReleaseConflict):
            flow.retire()
        flow.acknowledge(reconcile=True)
        self.assertEqual(flow.record["state"], "canary")

    def test_retired_old_health_cannot_roll_back_successor_release(self):
        flow = Workflow(self.coordinator, self.trust); flow.promoted(); old_record = copy.deepcopy(flow.record)
        retired = flow.retire()
        self.assertTrue(retired.retired); self.assertTrue(flow.retire().replayed)
        candidate = spec()
        candidate.update(base_revision=self.trust.target["revision"], base_artifact_sha256=self.trust.target["artifact_sha256"],
            base_generation=self.trust.target["generation"], candidate_revision="3" * 40, candidate_artifact_sha256="3" * 64)
        successor = self.coordinator.create(candidate, auth_context=self.trust.context)
        with self.assertRaisesRegex(ReleaseConflict, "release_retired"):
            flow.send("health", flow.receipt(status="failed"))
        self.assertEqual(self.store.get_state(self.coordinator.service_key)["active_release"], successor.record["id"])
        self.assertEqual(self.coordinator.read(old_record["id"], auth_context=self.trust.context).record, old_record)
        self.assertTrue(self.coordinator.create(spec(), auth_context=self.trust.context).retired)

    def test_rollback_keeps_slots_until_fresh_healthy_terminal_verification(self):
        flow = Workflow(self.coordinator, self.trust); flow.canary(); self.trust.now += 1
        flow.send("health", flow.receipt(status="failed"))
        with self.assertRaises(ReleaseConflict):
            flow.retire()
        self.assertEqual(self.store.get_state(self.coordinator.service_key)["active_release"], flow.record["id"])
        flow.applied(); self.trust.healthy = None
        with self.assertRaisesRegex(CoordinatorError, "healthy_terminal"):
            flow.retire()
        self.trust.healthy = True; flow.retire()
        self.assertIsNone(self.store.get_state(self.coordinator.service_key)["active_release"])
        self.assertEqual(self.store.get_state(self.coordinator.repository_key)["head_revision"], "1" * 40)

    def test_false_known_good_seed_and_caller_observation_are_rejected(self):
        self.trust.healthy = None
        with self.assertRaisesRegex(CoordinatorError, "known_good"):
            Workflow(self.coordinator, self.trust)
        self.trust.healthy = True
        flow = Workflow(self.coordinator, self.trust); flow.ready()
        with self.assertRaisesRegex(CoordinatorError, "controller_derived"):
            flow.send("stage", {"observation": flow.record["observed"]})
        self.trust.target["generation"] = "external-change"
        with self.assertRaisesRegex(ReleaseConflict, "external_deployment_changed"):
            flow.send("stage", {})

    def test_no_default_auth_or_self_approved_evidence(self):
        with self.assertRaises(CoordinatorError):
            ReleaseCoordinator(self.store, service="hub", repository="hub", authorize=None,
                verify_evidence=None, observe=None, trusted_verifier_subjects=[])
        with self.assertRaises(AccessDenied):
            self.coordinator.create(spec(), auth_context={"role": "manager", "authenticated": True})
        self.trust.issuer = self.trust.subject
        with self.assertRaises(AccessDenied):
            self.coordinator.create(spec(), auth_context=self.trust.context)
        self.assertEqual(self.store.get_state(self.coordinator.service_key), {})

    def test_verifier_binding_and_authenticated_executor_are_enforced(self):
        flow = Workflow(self.coordinator, self.trust)
        self.trust.bad_binding = True
        with self.assertRaises(AccessDenied):
            flow.send("evidence", flow.receipt("unit"))
        self.trust.bad_binding = False; flow.stage()
        with self.assertRaisesRegex(AccessDenied, "executor_mismatch"):
            flow.send("claim", {"action_id": flow.record["actions"][-1]["id"], "executor": "some-other-executor"})
        self.trust.deny_evidence = True
        with self.assertRaises(AccessDenied):
            flow.apply_event(flow.claim_event())
        self.assertEqual(self.coordinator.read(flow.record["id"], auth_context=self.trust.context).record["actions"][-1]["state"], "queued")

    def test_current_observation_expires_before_delayed_transaction_commit(self):
        flow = Workflow(self.coordinator, self.trust); flow.stage()
        self.trust.verification_hook = lambda: setattr(self.trust, "now", self.trust.now + 11)
        with self.assertRaisesRegex(CoordinatorError, "receipt_expired"):
            flow.apply_event(flow.claim_event())
        self.assertEqual(self.coordinator.read(flow.record["id"], auth_context=self.trust.context).record["actions"][-1]["state"], "queued")

    def test_new_coordinator_module_cannot_be_modified_by_its_own_release(self):
        candidate = spec(); candidate["changed_files"] = ["agent_hub/release_coordinator.py"]
        with self.assertRaisesRegex(CoordinatorError, "is_protected"):
            self.coordinator.create(candidate, auth_context=self.trust.context)


class FakeFirestore:
    """Exercise real FirestoreStore transaction callbacks with discarded attempts."""
    def __init__(self):
        self.documents = {}
        self.retry_next = False
        self.lose_next = False
        self.callback_count = 0
        self.store = FirestoreStore.__new__(FirestoreStore)
        engine = self

        class Reference:
            def __init__(self, key):
                self.key = key

            def get(self, transaction=None):
                values = engine.documents if transaction is None else transaction.snapshot
                value = copy.deepcopy(values.get(self.key))
                return SimpleNamespace(exists=value is not None, to_dict=lambda: copy.deepcopy(value))

        class Transaction:
            def __init__(self):
                self.snapshot = copy.deepcopy(engine.documents)
                self.writes = {}

            def set(self, reference, value):
                self.writes[reference.key] = copy.deepcopy(value)

        def transactional(function):
            def run(ignored):
                count = 2 if engine.retry_next else 1
                engine.retry_next = False
                for attempt in range(count):
                    tx = Transaction()
                    engine.callback_count += 1
                    result = function(tx)
                    if attempt == count - 1:
                        engine.documents.update(tx.writes)
                if engine.lose_next:
                    engine.lose_next = False
                    raise OSError("simulated Firestore commit acknowledgment lost")
                return result
            return run

        self.store.durable_state = SimpleNamespace(document=Reference)
        self.store.client = SimpleNamespace(transaction=lambda: None)
        self.store.firestore = SimpleNamespace(transactional=transactional)


class FirestoreCoordinatorTests(unittest.TestCase):
    def test_retried_transaction_does_not_repeat_verifier_observer_or_effect(self):
        engine, trust = FakeFirestore(), SyntheticTrust()
        flow = Workflow(trust.coordinator(engine.store), trust); flow.stage()
        before = dict(trust.calls); callbacks = engine.callback_count
        engine.retry_next = True
        event = flow.claim_event("retry-safe-claim")
        result = flow.apply_event(event)
        self.assertEqual(engine.callback_count - callbacks, 2)
        self.assertEqual({key: trust.calls[key] - before[key] for key in trust.calls},
                         {"authorize": 1, "verify": 1, "observe": 1})
        self.assertIsNotNone(result.dispatch)
        self.assertIsNone(flow.apply_event(event).dispatch)
        self.assertEqual(flow.record["actions"][-1]["state"], "claimed")

    def test_firestore_commit_lost_ack_cannot_emit_or_replay_dispatch(self):
        engine, trust = FakeFirestore(), SyntheticTrust()
        flow = Workflow(trust.coordinator(engine.store), trust); flow.stage()
        event = flow.claim_event("lost-firestore-claim")
        engine.lose_next = True
        with self.assertRaises(CommitUncertain):
            flow.apply_event(event)
        result = flow.apply_event(event)
        self.assertTrue(result.replayed); self.assertIsNone(result.dispatch)
        self.assertEqual(result.record["actions"][-1]["state"], "claimed")


if __name__ == "__main__":
    unittest.main()
