"""Behavioral checks for the finite model, DSL boundary, and compiler."""

import copy
from dataclasses import replace
from hashlib import sha256
from itertools import product
import json
import random
import unittest
from unittest.mock import patch

from ryan_frontier.domain import (
    ALL_GUARDS_MASK, GUARD_BITS, GUARD_NAMES, Event, State, Task,
    candidate_key, candidate_mask, candidate_spec, canonicalize,
    certify_representation, compile_candidate, compiled_policy,
    enumerate_candidates, events, guard_names, interpret_candidate,
    invariant_violation, make_tasks, migrate_candidate, plant_step,
    reference_decision, repair_operators, replay_counterexample,
    validate_candidate, verify,
)


class FiniteDomainTests(unittest.TestCase):
    def task(self, scenarios=GUARD_NAMES, horizon=6):
        return Task("unit", "development", horizon, 2, 2, tuple(scenarios))

    def test_each_missing_guard_has_a_replayable_minimal_witness(self):
        lengths = {"terminal": 3, "fence": 4, "lease": 3, "owner": 2, "once": 3}
        for guard in GUARD_NAMES:
            with self.subTest(guard=guard):
                task = self.task((guard,))
                candidate = candidate_spec(ALL_GUARDS_MASK ^ GUARD_BITS[guard])
                result = verify(task, candidate)
                self.assertFalse(result.passed)
                self.assertEqual(result.failure_kind, guard)
                self.assertEqual(len(result.counterexample), lengths[guard])
                self.assertTrue(replay_counterexample(task, candidate, result.counterexample))
                self.assertFalse(replay_counterexample(task, candidate_spec(31), result.counterexample))

    def test_full_policy_exhausts_fresh_and_shifted_tasks(self):
        for split in ("development", "confirmation", "heldout", "shift"):
            for task in make_tasks(912, 4, split):
                result = verify(task, candidate_spec(31))
                self.assertTrue(result.passed, result.to_dict())
                self.assertTrue(result.exhaustive)
                self.assertGreater(result.checked_transitions, 0)
                self.assertEqual(result.counterexample, ())
                self.assertIsNone(result.failure_kind)
                self.assertLessEqual(result.maximum_depth, task.horizon)

    def test_only_full_policy_passes_all_scenarios(self):
        passing = [mask for mask in range(32) if verify(self.task(), candidate_spec(mask)).passed]
        self.assertEqual(passing, [31])

    def test_scenario_boundaries_distinguish_policy_requirements(self):
        for guard in GUARD_NAMES:
            for mask in range(32):
                self.assertEqual(verify(self.task((guard,)), candidate_spec(mask)).passed,
                                 bool(mask & GUARD_BITS[guard]))

    def test_horizon_is_an_event_bound_not_a_claim_of_unbounded_safety(self):
        candidate = candidate_spec(31 ^ GUARD_BITS["fence"])
        below = verify(self.task(("fence",), horizon=3), candidate)
        exposed = verify(self.task(("fence",), horizon=4), candidate)
        self.assertTrue(below.passed)
        self.assertFalse(exposed.passed)
        self.assertEqual(len(exposed.counterexample), 4)
        self.assertIn("bounded", below.to_dict()["scope"])
        zero = verify(self.task(horizon=0), candidate_spec(0))
        self.assertTrue(zero.passed)
        self.assertEqual((zero.checked_states, zero.checked_transitions), (1, 0))

    def test_no_policy_decisions_cannot_supply_acceptance_evidence(self):
        for horizon in (0, 1):
            result = verify(self.task(horizon=horizon), candidate_spec(0))
            self.assertTrue(result.passed)
            self.assertTrue(result.exhaustive)
            self.assertEqual(result.checked_policy_decisions, 0)
            self.assertFalse(result.nonvacuous)
            self.assertFalse(result.acceptance_eligible)
            exported = result.to_dict()
            self.assertFalse(exported["acceptance_eligible"])
            self.assertEqual(exported["checked_policy_decisions"], 0)
        verified = verify(self.task(), candidate_spec(31))
        self.assertTrue(verified.acceptance_eligible)
        self.assertGreater(verified.checked_policy_decisions, 0)
        self.assertEqual(set(verified.covered_event_kinds), {"claim", "expire", "reclaim", "complete", "retry"})
        failed = verify(self.task(), candidate_spec(0))
        self.assertTrue(failed.nonvacuous)
        self.assertFalse(failed.acceptance_eligible)

    def test_verifier_counts_and_result_are_deterministic(self):
        task = self.task()
        first = verify(task, candidate_spec(0))
        self.assertEqual(first, verify(task, candidate_spec(0)))
        self.assertGreaterEqual(first.checked_transitions, len(first.counterexample))
        self.assertFalse(first.exhaustive)
        json.dumps(first.to_dict())

    def test_reference_monitor_rejects_reject_all_vacuity(self):
        # Injection simulates an implementation bug outside the closed grammar.
        # Safety alone would pass; reference decision equality detects starvation.
        with patch("ryan_frontier.domain._interpret_validated", return_value=False):
            result = verify(self.task(), candidate_spec(31))
        self.assertFalse(result.passed)
        self.assertEqual(result.failure_kind, "wrong_decision")
        self.assertEqual(result.counterexample[-1].event.kind, "complete")

    def test_plant_mechanics_and_independent_reference(self):
        initial = State()
        leased, accepted = plant_step(initial, Event("claim", 1), True, 2)
        self.assertTrue(accepted)
        self.assertEqual(leased, State("leased", 1, 1, True, 0))
        expired, _ = plant_step(leased, Event("expire"), True, 2)
        reclaimed, _ = plant_step(expired, Event("reclaim", 0), True, 2)
        self.assertEqual(reclaimed.fence, 2)
        stale = Event("complete", 0, 1)
        self.assertFalse(reference_decision(reclaimed, stale, 2))
        corrupted, accepted = plant_step(reclaimed, stale, True, 2)
        self.assertTrue(accepted)
        self.assertEqual(invariant_violation(reclaimed, stale, accepted, corrupted, 2), "fence")

    def test_completed_result_keeps_history_and_rejects_later_retry(self):
        leased = State("leased", 0, 1, True, 0)
        done, accepted = plant_step(leased, Event("complete", 0, 1), True, 2)
        self.assertTrue(accepted)
        self.assertEqual(done.accepted_count, 1)
        self.assertTrue(done.lease_active)
        self.assertFalse(reference_decision(done, Event("retry"), 2))
        self.assertFalse(reference_decision(done, Event("complete", 0, 1), 2))
        resurrected, accepted = plant_step(done, Event("retry"), True, 2)
        self.assertEqual(resurrected.accepted_count, 1)
        self.assertEqual(invariant_violation(done, Event("retry"), accepted, resurrected, 2), "terminal")

    def test_state_and_task_are_immutable(self):
        with self.assertRaises(AttributeError):
            State().phase = "done"
        with self.assertRaises(AttributeError):
            self.task().horizon = 99
        task = self.task()
        spec = task.initial_candidate
        spec["complete"]["terms"].append({"bad": "value"})
        self.assertEqual(task.initial_candidate, candidate_spec(0))

    def test_task_generation_is_reproducible_separated_and_rng_local(self):
        random.seed(891)
        before = random.getstate()
        a = make_tasks(4, 10)
        self.assertEqual(a, make_tasks(4, 10))
        self.assertEqual(random.getstate(), before)
        self.assertNotEqual(a, make_tasks(4, 10, "confirmation"))
        shifted = make_tasks(4, 1, "shift")[0]
        self.assertEqual(shifted.scenarios, GUARD_NAMES)
        self.assertGreater(shifted.horizon, max(task.horizon for task in a))
        self.assertGreater(shifted.workers, max(task.workers for task in a))
        self.assertEqual(make_tasks(0, 0), [])
        for kwargs in ({"horizon": -1}, {"horizon": True}, {"workers": 0}, {"scenarios": ["once"]}):
            with self.assertRaises(ValueError):
                replace(self.task(), **kwargs)
        with self.assertRaises(ValueError):
            make_tasks(0, -1)


class DSLAndCompilerTests(unittest.TestCase):
    def test_all_32_policies_round_trip_without_aliasing(self):
        keys = set()
        for mask in range(32):
            candidate = candidate_spec(mask)
            self.assertEqual(candidate_mask(candidate), mask)
            self.assertEqual(candidate, candidate_spec(guard_names(candidate)))
            self.assertEqual(candidate, validate_candidate(json.loads(json.dumps(candidate))))
            clean = validate_candidate(candidate)
            clean["complete"]["terms"].clear()
            self.assertEqual(candidate_mask(candidate), mask)
            keys.add(candidate_key(candidate))
        self.assertEqual(len(keys), 32)

    def test_rejects_arbitrary_code_extra_keys_and_wrong_types(self):
        bad_inputs = ["__import__('os').system('echo nope')", None, [],
                      {"schema": "lease-policy/v1", "source": "print('nope')"}]
        bad = candidate_spec(31)
        bad["complete"]["terms"][0]["name"] = "__import__('os')"
        bad_inputs.append(bad)
        bad = candidate_spec(31)
        bad["complete"]["terms"][0]["type"] = "str"
        bad_inputs.append(bad)
        bad = candidate_spec(31)
        bad["retry"]["terms"] = [copy.deepcopy(bad["complete"]["terms"][0])]
        bad_inputs.append(bad)
        bad = candidate_spec(31)
        bad["complete"]["terms"].append(copy.deepcopy(bad["complete"]["terms"][0]))
        bad_inputs.append(bad)
        bad = candidate_spec(0)
        bad["complete"]["kind"] = "not"
        bad_inputs.append(bad)
        for bad in bad_inputs:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    compile_candidate(bad)
        with self.assertRaises(ValueError):
            candidate_spec(32)
        for invalid in (True, False, 1.0, None):
            with self.assertRaises(ValueError):
                candidate_spec(invalid)

    def test_generated_python_matches_interpreter_and_direct_truth_table(self):
        # Cover every policy, all phase/history combinations, matching/mismatched
        # owners and fences, active/expired leases, and all five event kinds.
        for mask in range(32):
            candidate = candidate_spec(mask)
            compiled = compiled_policy(candidate)
            for phase, active, count, same_owner, same_fence in product(
                ("pending", "leased", "done"), (False, True), (0, 1, 2), (False, True), (False, True)
            ):
                state = State(phase, 0, 2, active, count)
                for kind in ("claim", "expire", "reclaim", "complete", "retry"):
                    event = Event(kind, 0 if same_owner else 1, 2 if same_fence else 1)
                    expected = True
                    if kind == "retry" and mask & GUARD_BITS["terminal"]:
                        expected = phase != "done"
                    elif kind == "complete":
                        expected = ((not mask & GUARD_BITS["fence"] or same_fence)
                                    and (not mask & GUARD_BITS["owner"] or same_owner)
                                    and (not mask & GUARD_BITS["lease"] or active)
                                    and (not mask & GUARD_BITS["once"] or count == 0))
                    interpreted = interpret_candidate(candidate, state, event)
                    emitted = compiled(state.to_dict(), event.to_dict())
                    self.assertIs(interpreted, expected)
                    self.assertIs(emitted, expected)

    def test_canonicalization_and_compilation_ignore_predicate_order(self):
        original = candidate_spec(31)
        reordered = copy.deepcopy(original)
        reordered["complete"]["terms"].reverse()
        self.assertEqual(canonicalize(reordered), original)
        self.assertEqual(compile_candidate(original), compile_candidate(reordered))
        self.assertEqual(candidate_key(original), candidate_key(reordered))

    def test_migration_certificate_checks_semantics_and_catches_changed_guard(self):
        task = Task("migration", "development", 6, 2, 2, GUARD_NAMES)
        legacy = {"schema": "lease-policy/v0", "guards": list(reversed(GUARD_NAMES))}
        migrated = migrate_candidate(legacy)
        self.assertEqual(migrated, candidate_spec(31))
        certificate = certify_representation(task, legacy, migrated)
        self.assertTrue(certificate["passed"])
        # 3 phases * 3 owners * 3 fences * 2 active flags * 3 counts,
        # times (2 generic + 4 worker actions + 6 completion identities).
        self.assertEqual(certificate["checked_inputs"], 1944)
        self.assertEqual(certificate["expected_inputs"], 1944)
        self.assertTrue(certificate["acceptance_eligible"])
        self.assertTrue(certificate["exhaustive_inputs"])
        self.assertFalse(certificate["safety_checked"])
        changed = certify_representation(task, legacy, candidate_spec(30))
        self.assertFalse(changed["passed"])
        self.assertFalse(changed["acceptance_eligible"])
        self.assertLess(changed["checked_inputs"], changed["expected_inputs"])
        self.assertIsNotNone(changed["counterexample"])

    def test_representation_record_binds_artifacts_and_reports_exact_coverage(self):
        # Representation checks do not depend on reachability's event horizon;
        # even a zero-horizon Task must perform the full nonempty Cartesian check.
        task = Task("certificate", "development", 0, 1, 1, ())
        source = {"schema": "lease-policy/v0", "guards": ["owner", "lease"]}
        target = migrate_candidate(source)
        record = certify_representation(task, source, target)
        exported = json.loads(json.dumps(record))
        self.assertEqual(record, exported)
        self.assertTrue(record["acceptance_eligible"])
        self.assertEqual(record["expected_inputs"], 432)
        self.assertEqual(record["checked_inputs"], 432)
        self.assertEqual(record["coverage"]["state_count"], 72)
        self.assertEqual(record["coverage"]["event_count"], 6)
        self.assertFalse(record["coverage"]["event_horizon_applies"])
        source_json = json.dumps(source, sort_keys=True, separators=(",", ":"))
        self.assertEqual(record["source_sha256"], sha256(source_json.encode()).hexdigest())
        self.assertEqual(record["target_sha256"], sha256(candidate_key(target).encode()).hexdigest())
        self.assertEqual(record["python_source_sha256"], sha256(compile_candidate(target).encode()).hexdigest())
        # Equivalent unsafe policies are certifiable representations, explicitly
        # not safety certificates; preserve this distinction in exported JSON.
        empty = {"schema": "lease-policy/v0", "guards": []}
        equivalent = certify_representation(task, empty)
        self.assertTrue(equivalent["passed"])
        self.assertFalse(equivalent["safety_checked"])
        self.assertFalse(verify(replace(task, horizon=5, workers=2, scenarios=GUARD_NAMES), candidate_spec(0)).passed)

    def test_search_order_is_finite_unique_and_memory_changes_priority(self):
        baseline = enumerate_candidates()
        self.assertEqual([candidate_mask(c) for c in baseline], list(range(32)))
        targeted = enumerate_candidates("targeted", ["fence", "once"])
        self.assertEqual(set(map(candidate_key, targeted)), set(map(candidate_key, baseline)))
        self.assertEqual(candidate_mask(targeted[0]), GUARD_BITS["fence"] | GUARD_BITS["once"])
        preserving = enumerate_candidates(base_candidate=candidate_spec(["owner"]))
        self.assertEqual(len(preserving), 16)
        self.assertTrue(all("owner" in guard_names(c) for c in preserving))
        repairs = repair_operators(candidate_spec(["owner"]), ["fence", "lease"])
        self.assertEqual(guard_names(repairs[1]), ("fence", "lease", "owner"))


if __name__ == "__main__":
    unittest.main()
