"""Synthetic controller receipts; never provider calls or a deployment test."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading
import unittest

from agent_hub.improvement_release import (
    GATES, ReleaseConflict, ReleaseError, create_release, digest, transition,
)


def spec():
    return {"repository": "hub", "base_revision": "1" * 40, "base_artifact_sha256": "1" * 64,
            "base_generation": "generation-1", "candidate_revision": "2" * 40,
            "candidate_artifact_sha256": "2" * 64, "changed_files": ["agent_hub/web/status.js"],
            "policy": {"evaluator_sha256": "3" * 64, "permissions_sha256": "4" * 64,
                       "budget_sha256": "5" * 64, "gate_definitions": {key: digest(key) for key in GATES},
                       "health_definition_sha256": "6" * 64, "max_cost_microusd": 1000,
                       "canary_percent": 5, "min_canary_seconds": 10, "min_promotion_seconds": 5,
                       "receipt_max_age_seconds": 100}}


class Harness:
    def __init__(self):
        self.record = create_release(spec(), now=100)
        self.now = 101
        self.sequence = 0

    def event(self, kind, payload, identifier=None):
        self.sequence += 1
        return {"id": identifier or f"event-{self.sequence}", "kind": kind, "payload": copy.deepcopy(payload)}

    def send(self, kind, payload, identifier=None):
        event = self.event(kind, payload, identifier)
        result = transition(self.record, event, expected_version=self.record["version"], now=self.now)
        self.record = result.record
        return result

    def receipt(self, gate=None, status="passed"):
        policy = self.record["spec"]["policy"]
        receipt = {"release_id": self.record["id"], "status": status,
                   "definition_sha256": policy["gate_definitions"][gate] if gate else policy["health_definition_sha256"],
                   "evaluator_sha256": policy["evaluator_sha256"], "report_sha256": digest([gate, status, self.now]),
                   "verifier": "synthetic-independent-verifier", "observed_at": self.now}
        if gate:
            receipt["gate"] = gate
            if gate == "budget":
                receipt.update(spent_microusd=100, reserved_microusd=500, reservation_sha256="7" * 64)
        else:
            receipt["observation"] = copy.deepcopy(self.record["observed"])
            receipt["window_started_at"] = self.record["phase_started_at"]
        return receipt

    def ready(self):
        for gate in GATES:
            self.send("evidence", self.receipt(gate))
        return self.record

    def stage(self):
        self.send("stage", {"observation": self.record["observed"]})

    def claim(self, identifier=None):
        action = self.record["actions"][-1]
        return self.send("claim", {"action_id": action["id"], "executor": "trusted-deployer",
                                   "observation": self.record["observed"]}, identifier)

    def acknowledgment(self, outcome="applied"):
        action = self.record["actions"][-1]
        observation = ({"revision": action["target_revision"], "artifact_sha256": action["target_artifact_sha256"],
                        "generation": f"generation-{action['kind']}-2", "traffic_percent": action["traffic_percent"]} if outcome == "applied"
                       else copy.deepcopy(action["expected"]) if outcome == "not_applied" else None)
        return {"action_id": action["id"], "executor": "trusted-deployer", "outcome": outcome,
                "receipt_sha256": digest([action["id"], outcome]), "observation": observation, "observed_at": self.now}

    def applied(self, outcome="applied"):
        self.claim()
        self.send("acknowledge", self.acknowledgment(outcome))

    def canary(self):
        self.ready(); self.stage(); self.applied()

    def healthy_canary(self):
        self.canary()
        self.now += 10
        self.send("health", self.receipt())

    def promoted(self):
        self.healthy_canary()
        self.send("promote", {"observation": self.record["observed"]})
        self.applied()
        self.now += 5
        self.send("health", self.receipt())


class ReleaseTests(unittest.TestCase):
    def test_full_lifecycle_requires_all_gates_canary_window_and_post_promotion_health(self):
        h = Harness()
        for gate in GATES[:-1]:
            h.send("evidence", h.receipt(gate))
        with self.assertRaises(ReleaseConflict):
            h.stage()
        h.send("evidence", h.receipt("budget")); h.stage()
        self.assertEqual(h.record["state"], "canary_pending")
        self.assertEqual(h.record["actions"][-1]["traffic_percent"], 5)
        h.applied()
        with self.assertRaisesRegex(ReleaseError, "health_window_incomplete"):
            h.send("health", h.receipt())
        with self.assertRaises(ReleaseConflict):
            h.send("promote", {"observation": h.record["observed"]})
        h.now += 10
        h.send("health", h.receipt())
        h.send("promote", {"observation": h.record["observed"]})
        self.assertEqual(h.record["actions"][-1]["traffic_percent"], 100)
        h.applied()
        self.assertEqual(h.record["state"], "promotion_verifying")
        h.now += 5
        h.send("health", h.receipt())
        self.assertEqual(h.record["state"], "promoted")
        self.assertEqual(h.record["spec"]["base_revision"], "1" * 40)

    def test_failed_canary_and_failed_promoted_health_both_restore_fixed_known_good(self):
        for initial in ("canary", "promoted"):
            h = Harness(); getattr(h, initial)(); h.now += 1
            h.send("health", h.receipt(status="failed"))
            self.assertEqual(h.record["state"], "rollback_pending")
            action = h.record["actions"][-1]
            self.assertEqual((action["target_revision"], action["target_artifact_sha256"]), ("1" * 40, "1" * 64))
            self.assertNotEqual(h.record["observed"]["revision"], "1" * 40)
            h.applied()
            self.assertEqual(h.record["state"], "rolled_back")
            self.assertEqual(h.record["observed"]["revision"], "1" * 40)

    def test_failure_between_queued_promotion_and_dispatch_cancels_promotion(self):
        h = Harness(); h.healthy_canary()
        h.send("promote", {"observation": h.record["observed"]})
        old_action = h.record["actions"][-1]["id"]
        h.now += 1
        h.send("health", h.receipt(status="failed"))
        self.assertEqual(h.record["actions"][-2]["state"], "cancelled")
        with self.assertRaises(ReleaseConflict):
            h.send("claim", {"action_id": old_action, "executor": "trusted-deployer", "observation": h.record["observed"]})
        self.assertEqual(h.claim().dispatch["kind"], "rollback")

    def test_unknown_health_blocks_claim_and_requires_fresh_observed_recovery(self):
        h = Harness(); h.healthy_canary()
        h.send("promote", {"observation": h.record["observed"]}); h.now += 1
        h.send("health", h.receipt(status="unknown"))
        self.assertEqual(h.record["state"], "health_blocked")
        with self.assertRaises(ReleaseConflict):
            h.claim()
        h.now += 1
        h.send("health", h.receipt())
        action = h.claim().dispatch
        self.assertEqual(action["kind"], "promote")
        self.assertEqual(action["health_sha256"], digest(h.record["last_health"]))

    def test_failed_and_unknown_gate_are_never_inferred_passed(self):
        for status, expected in (("failed", "rejected"), ("unknown", "blocked")):
            h = Harness()
            for gate in GATES:
                if gate != "security":
                    h.send("evidence", h.receipt(gate))
            h.send("evidence", h.receipt("security", status))
            self.assertEqual(h.record["state"], expected)
            with self.assertRaises(ReleaseConflict):
                h.stage()
        h = Harness(); receipt = h.receipt("unit"); receipt["status"] = "tests probably passed"
        with self.assertRaises(ReleaseError):
            h.send("evidence", receipt)
        receipt = h.receipt("unit"); receipt["summary"] = "all tests passed"
        with self.assertRaises(ReleaseError):
            h.send("evidence", receipt)

    def test_policy_source_and_definition_evidence_binding_prevents_reuse(self):
        for key in ("release_id", "definition_sha256", "evaluator_sha256"):
            h = Harness(); receipt = h.receipt("unit"); receipt[key] = "f" * 64
            with self.subTest(key=key), self.assertRaises(ReleaseError):
                h.send("evidence", receipt)
        for field in ("candidate_revision", "candidate_artifact_sha256", "base_revision", "base_artifact_sha256"):
            changed = spec(); changed[field] = "a" * len(changed[field])
            self.assertNotEqual(create_release(changed, now=100)["id"], create_release(spec(), now=100)["id"])
        h = Harness(); tampered = copy.deepcopy(h.record); tampered["spec"]["policy"]["max_cost_microusd"] *= 10
        with self.assertRaisesRegex(ReleaseError, "corrupt_release_record"):
            transition(tampered, h.event("evidence", h.receipt("unit")), expected_version=0, now=101)

    def test_protected_paths_include_policy_evaluators_credentials_and_controller(self):
        for path in ("tests/test_gate.py", "policy/rules.json", "AGENTS.md", "src/permissions.json",
                     "src/cloud_budget.py", "src/.env.prod", "credentials/account.json", "evaluator/check.py",
                     "agent_hub/research.py", "agent_hub/improvement_release.py", "../app.py",
                     "src/app.py:stream", "src\\app.py", "src/COM1.txt", "deploy/Dockerfile"):
            value = spec(); value["changed_files"] = [path]
            with self.subTest(path=path), self.assertRaises(ReleaseError):
                create_release(value, now=100)

    def test_budget_receipt_needs_real_reservation_reference_and_bounded_total(self):
        for change in ({"spent_microusd": 600, "reserved_microusd": 500},
                       {"reserved_microusd": 0}, {"reserved_microusd": True}, {"reservation_sha256": "unknown"}):
            h = Harness(); receipt = h.receipt("budget"); receipt.update(change)
            with self.subTest(change=change), self.assertRaises(ReleaseError):
                h.send("evidence", receipt)

    def test_stale_future_and_changed_deployment_generation_block_dispatch(self):
        h = Harness(); h.ready(); h.now += 101
        with self.assertRaisesRegex(ReleaseError, "stale_or_future"):
            h.stage()
        h = Harness(); receipt = h.receipt("unit"); receipt["observed_at"] += 1
        with self.assertRaises(ReleaseError):
            h.send("evidence", receipt)
        h = Harness(); h.ready(); h.stage()
        wrong = {**h.record["observed"], "generation": "other-controller-generation"}
        with self.assertRaisesRegex(ReleaseConflict, "generation_changed"):
            h.send("claim", {"action_id": h.record["actions"][-1]["id"], "executor": "trusted-deployer", "observation": wrong})
        h.now += 101
        with self.assertRaisesRegex(ReleaseError, "stale_or_future"):
            h.claim()

    def test_claim_retry_and_restart_never_return_dispatch_again(self):
        h = Harness(); h.ready(); h.stage()
        original = copy.deepcopy(h.record)
        event = h.event("claim", {"action_id": h.record["actions"][-1]["id"], "executor": "trusted-deployer",
                                  "observation": h.record["observed"]}, "dispatch-once")
        first = transition(original, event, expected_version=original["version"], now=h.now)
        restored = json.loads(json.dumps(first.record))
        retry = transition(restored, event, expected_version=original["version"], now=h.now)
        self.assertIsNotNone(first.dispatch); self.assertIsNone(retry.dispatch); self.assertTrue(retry.replayed)
        self.assertEqual(first.record, retry.record)
        h.record = restored
        with self.assertRaises(ReleaseConflict):
            h.claim()
        self.assertEqual(original["actions"][-1]["state"], "queued")

    def test_lost_dispatch_ack_requires_terminal_reconciliation_without_replay(self):
        h = Harness(); h.ready(); h.stage(); h.claim()
        h.send("uncertain", {"action_id": h.record["actions"][-1]["id"]})
        self.assertEqual(h.record["state"], "reconcile_required")
        with self.assertRaises(ReleaseConflict):
            h.claim()
        ack = h.acknowledgment(); ack["execution_terminal"] = False
        with self.assertRaises(ReleaseError):
            h.send("reconcile", ack)
        ack["execution_terminal"] = True
        result = h.send("reconcile", ack)
        self.assertIsNone(result.dispatch)
        self.assertEqual(h.record["state"], "canary")

    def test_unknown_and_failed_rollback_do_not_report_recovered(self):
        for outcome in ("unknown", "not_applied"):
            h = Harness(); h.canary(); h.now += 1
            h.send("health", h.receipt(status="failed")); h.applied(outcome)
            self.assertEqual(h.record["state"], "reconcile_required" if outcome == "unknown" else "rollback_failed")
            self.assertEqual(h.record["observed"]["revision"], "2" * 40)
            with self.assertRaises(ReleaseConflict):
                h.claim()

    def test_ack_wrong_executor_wrong_artifact_or_unchanged_generation_is_rejected(self):
        for mutate in (lambda value: value.update(executor="builder"),
                       lambda value: value["observation"].update(artifact_sha256="f" * 64),
                       lambda value: value["observation"].update(traffic_percent=100),
                       lambda value: value["observation"].update(generation="generation-1")):
            h = Harness(); h.ready(); h.stage(); h.claim()
            ack = h.acknowledgment(); mutate(ack)
            with self.assertRaises(ReleaseError):
                h.send("acknowledge", ack)
            self.assertEqual(h.record["actions"][-1]["state"], "claimed")

    def test_single_late_health_point_does_not_prove_a_full_window(self):
        h = Harness(); h.canary(); h.now += 20
        receipt = h.receipt(); receipt["window_started_at"] = h.now
        with self.assertRaisesRegex(ReleaseError, "health_window_incomplete"):
            h.send("health", receipt)
        receipt["window_started_at"] = h.record["phase_started_at"] - 1
        with self.assertRaisesRegex(ReleaseError, "health_window_wrong_phase"):
            h.send("health", receipt)

    def test_optimistic_storage_commit_allows_one_dispatch_owner(self):
        h = Harness(); h.ready(); h.stage()
        base = copy.deepcopy(h.record)
        barrier, lock = threading.Barrier(2), threading.Lock()
        stored = [copy.deepcopy(base)]

        def contender(owner):
            event = {"id": owner, "kind": "claim", "payload": {"action_id": base["actions"][-1]["id"],
                     "executor": owner, "observation": base["observed"]}}
            result = transition(base, event, expected_version=base["version"], now=101)
            barrier.wait()
            # Simulates the required storage CAS. A loser must discard its result.
            with lock:
                if stored[0]["version"] != base["version"]:
                    return None
                stored[0] = result.record
                return result.dispatch

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(contender, ("controller-a", "controller-b")))
        self.assertEqual(sum(result is not None for result in results), 1)
        with self.assertRaisesRegex(ReleaseConflict, "release_version_changed"):
            transition(stored[0], h.event("uncertain", {"action_id": base["actions"][-1]["id"]}),
                       expected_version=base["version"], now=101)

    def test_idempotency_key_cannot_be_used_for_different_content(self):
        h = Harness(); h.send("evidence", h.receipt("unit"), "fixed-id")
        with self.assertRaisesRegex(ReleaseConflict, "idempotency_key_reused"):
            h.send("evidence", h.receipt("security"), "fixed-id")

    def test_failed_promotion_requests_canary_removal_not_a_promotion_retry(self):
        h = Harness(); h.healthy_canary()
        h.send("promote", {"observation": h.record["observed"]}); h.applied("not_applied")
        self.assertEqual(h.record["state"], "rollback_pending")
        self.assertEqual(h.claim().dispatch["kind"], "rollback")

    def test_expired_evidence_can_abort_healthy_canary_without_fake_failure(self):
        for queued_promotion in (False, True):
            h = Harness(); h.healthy_canary()
            if queued_promotion:
                h.send("promote", {"observation": h.record["observed"]})
            h.now += 101
            with self.assertRaisesRegex(ReleaseError, "stale_or_future"):
                if queued_promotion:
                    h.claim()
                else:
                    h.send("promote", {"observation": h.record["observed"]})
            h.send("abort", {"observation": h.record["observed"], "reason": "evidence_expired", "report_sha256": "a" * 64})
            self.assertEqual(h.record["last_health"]["status"], "passed")
            self.assertEqual(h.claim().dispatch["kind"], "rollback")
            h.send("acknowledge", h.acknowledgment())
            self.assertEqual(h.record["state"], "rolled_back")

    def test_abort_cancels_queued_canary_but_cannot_race_inflight_mutation(self):
        for in_flight in (False, True):
            h = Harness(); h.ready(); h.stage()
            if in_flight:
                h.claim()
                with self.assertRaisesRegex(ReleaseConflict, "reconciliation"):
                    h.send("abort", {"observation": h.record["observed"], "reason": "operator_requested", "report_sha256": "a" * 64})
            else:
                h.send("abort", {"observation": h.record["observed"], "reason": "operator_requested", "report_sha256": "a" * 64})
                self.assertEqual(h.record["state"], "rejected")
                with self.assertRaises(ReleaseConflict):
                    h.claim()

    def test_impossible_canary_freshness_policy_is_rejected_before_staging(self):
        value = spec(); value["policy"]["receipt_max_age_seconds"] = 9
        with self.assertRaisesRegex(ReleaseError, "impossible_canary"):
            create_release(value, now=100)


if __name__ == "__main__":
    unittest.main()
