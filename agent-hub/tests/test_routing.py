"""Synthetic policy receipts test admission logic, not any model's capability."""
from dataclasses import replace
import unittest

from agent_hub.routing import (
    AdapterCandidate, Capability, CapabilityAttestation, CapabilityRegistry,
    CloudResources, Condition, Effect, Input, ModelConfiguration, ModelSelection,
    QuantizedEvaluation, Router, RoutingError, RoutingPolicy, TrainingDataPermission,
    check_adapter_promotion, check_training_permission,
)

NOW = 100_000
ARTIFACT = "1" * 64
EVIDENCE = "2" * 64
MEASUREMENT = "3" * 64
DATASET = "4" * 64
READ = Effect("read", "selected_repository")
WRITE = Effect("write", "selected_repository")


def operation(**changes):
    return replace(Capability(
        "inspect", ARTIFACT, (Input("path", "string"),),
        (Condition("fact.repository_selected", "eq", True),), (READ,),
        (Condition("fact.result_verified", "eq", True),),
    ), **changes)


def configuration(location="google_cloud", **changes):
    cloud = location == "google_cloud"
    return replace(ModelConfiguration(
        ModelSelection("test_cloud" if cloud else "test_hosted", "fixture-v1", "high"),
        "fixture-artifact-v1", location, True, 100, ("low", "high"),
        10 if cloud else 100, "gcp/test/inference" if cloud else None,
    ), **changes)


def attestation(model, capability, **changes):
    return replace(CapabilityAttestation(
        model.selection, model.actual_model_id, capability.digest, EVIDENCE,
        "fixture_verifier", True, NOW - 10, 2048, 2,
        2_000_000_000 if model.location == "google_cloud" else 0,
    ), **changes)


def resource(model=None, **changes):
    model = model or configuration()
    return replace(CloudResources(
        model.deployment_id, "google_cloud", (model.actual_model_id,),
        3_000_000_000, 0, 0, NOW - 5, MEASUREMENT,
    ), **changes)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.capability = operation()
        self.cloud = configuration()
        self.frontier = configuration("frontier")
        self.policy = RoutingPolicy(inference_authorized=True)

    def router(self, *, models=None, receipts=None, policy=None, capabilities=None):
        models = (self.cloud, self.frontier) if models is None else models
        receipts = tuple(attestation(item, self.capability) for item in models) if receipts is None else receipts
        return Router(CapabilityRegistry(capabilities or (self.capability,)), models, receipts,
                      policy=self.policy if policy is None else policy)

    def route(self, router=None, **changes):
        arguments = dict(mode="self_hosted_only", inputs={"path": "module.py"},
                         facts={"repository_selected": True}, allowed_effects=(READ,),
                         context_tokens=1024, budget_microusd=1000, now=NOW,
                         resources=resource(self.cloud))
        arguments.update(changes)
        return (router or self.router()).route("inspect", **arguments)

    def test_default_policy_abstains_and_never_implies_execution(self):
        router = Router(CapabilityRegistry((self.capability,)), (self.cloud,),
                        (attestation(self.cloud, self.capability),))
        decision = self.route(router)
        self.assertEqual((decision.action, decision.reason), ("abstain", "inference_not_authorized"))
        self.assertIsNone(decision.selection)

    def test_cloud_admission_returns_exact_selection_and_evidence(self):
        decision = self.route()
        self.assertEqual(decision.action, "route")
        self.assertEqual(decision.selection, self.cloud.selection)
        self.assertEqual(decision.evidence_sha256, EVIDENCE)
        self.assertEqual(decision.resource_measurement_sha256, MEASUREMENT)
        self.assertEqual(decision.capability_digest, self.capability.digest)
        self.assertEqual(decision.estimated_cost_microusd, 10)
        self.assertEqual(decision.digest, self.route().digest)

    def test_frontier_selects_highest_effort_of_strongest_approved_model(self):
        lower_effort = replace(self.frontier, selection=replace(self.frontier.selection, effort="low"))
        weaker = replace(self.frontier, selection=replace(self.frontier.selection, model="weaker-v1"),
                         quality_rank=10, estimated_cost_microusd=1)
        decision = self.route(self.router(models=(weaker, lower_effort, self.frontier)), mode="frontier", resources=None)
        self.assertEqual(decision.selection, self.frontier.selection)
        self.assertIsNone(decision.resource_measurement_sha256)

    def test_missing_highest_effort_does_not_fall_back_to_low_effort(self):
        for location in ("frontier", "google_cloud"):
            with self.subTest(location=location):
                model = configuration(location)
                low = replace(model, selection=replace(model.selection, effort="low"))
                mode = "frontier" if location == "frontier" else "self_hosted_only"
                decision = self.route(self.router(models=(low,)), mode=mode)
                self.assertEqual(decision.action, "abstain")
                self.assertIn("highest_supported_effort_not_approved", decision.reason)

    def test_highest_configuration_failure_never_silently_uses_weaker_model(self):
        for location in ("frontier", "google_cloud"):
            with self.subTest(location=location):
                best = configuration(location)
                weaker = replace(best, selection=replace(best.selection, model="weaker-v1"), quality_rank=5)
                router = self.router(models=(best, weaker), receipts=(attestation(weaker, self.capability),))
                decision = self.route(router, mode="frontier" if location == "frontier" else "self_hosted_only")
                self.assertEqual(decision.action, "abstain")
                self.assertIn("capability_gap", decision.reason)

    def test_explicit_model_pin_is_never_overridden(self):
        for selection, reason in (
            (replace(self.cloud.selection, model="unregistered-v2"), "exact_configuration_not_registered"),
            (self.frontier.selection, "exact_configuration_mismatch"),
        ):
            with self.subTest(selection=selection):
                decision = self.route(requested_model=selection)
                self.assertEqual(decision.action, "abstain")
                self.assertIn(reason, decision.reason)

    def test_hybrid_prefers_verified_cloud_without_escalation(self):
        decision = self.route(mode="hybrid", escalation_reason="resources_unavailable")
        self.assertEqual(decision.selection, self.cloud.selection)
        self.assertIsNone(decision.escalation_reason)

    def test_hybrid_escalation_requires_matching_explicit_reason(self):
        missing = self.route(mode="hybrid", resources=None)
        self.assertEqual(missing.reason, "explicit_hybrid_escalation_required")
        wrong = self.route(mode="hybrid", resources=None, escalation_reason="context_limit")
        self.assertEqual(wrong.reason, "escalation_reason_does_not_match_observed_failure")
        allowed = self.route(mode="hybrid", resources=None, escalation_reason="resources_unavailable")
        self.assertEqual(allowed.selection, self.frontier.selection)
        self.assertEqual(allowed.escalation_reason, "resources_unavailable")

    def test_hybrid_capability_gap_can_escalate_but_never_invents_frontier_evidence(self):
        router = self.router(receipts=(attestation(self.frontier, self.capability),))
        allowed = self.route(router, mode="hybrid", escalation_reason="capability_gap")
        self.assertEqual(allowed.selection, self.frontier.selection)
        denied = self.route(self.router(receipts=()), mode="hybrid", escalation_reason="capability_gap")
        self.assertEqual((denied.action, denied.reason), ("abstain", "capability_gap"))

    def test_self_hosted_aliases_never_use_frontier(self):
        for mode in ("self_hosted_only", "local-only", "local_only"):
            with self.subTest(mode=mode):
                decision = self.route(mode=mode, resources=None)
                self.assertEqual(decision.action, "abstain")
                self.assertEqual(decision.mode, "self_hosted_only")
                self.assertIsNone(decision.selection)

    def test_budget_insufficient_does_not_trigger_hybrid_spend(self):
        for mode in ("frontier", "self_hosted_only"):
            with self.subTest(mode=mode):
                decision = self.route(mode=mode, budget_microusd=0)
                self.assertEqual(decision.action, "abstain")
                self.assertIn("budget_insufficient", decision.reason)
        hybrid = self.route(mode="hybrid", budget_microusd=0, escalation_reason="resources_unavailable")
        self.assertEqual(hybrid.reason, "escalation_reason_does_not_match_observed_failure")

    def test_self_hosted_requires_exactly_one_already_loaded_cloud_model(self):
        for loaded in ((), ("wrong-artifact",), (self.cloud.actual_model_id, "another"),
                       (self.cloud.actual_model_id, self.cloud.actual_model_id)):
            with self.subTest(loaded=loaded):
                decision = self.route(resources=resource(loaded_model_ids=loaded))
                self.assertIn("resources_unavailable", decision.reason)
        for location in ("local", "pc", "localhost", "Windows"):
            with self.subTest(location=location), self.assertRaises(RoutingError):
                replace(self.cloud, location=location)
            with self.subTest(resource_location=location), self.assertRaises(RoutingError):
                resource(location=location)

    def test_ram_context_concurrency_and_freshness_must_all_fit(self):
        changes = (
            {"measured_ram_budget_bytes": 0},
            {"measured_ram_budget_bytes": 2_255_999_999},
            {"active_requests": 2},
            {"context_tokens_in_use": 3500},
            {"observed_at": NOW - 61},
            {"observed_at": NOW + 1},
            {"deployment_id": "gcp/wrong/inference"},
        )
        for values in changes:
            with self.subTest(values=values):
                decision = self.route(resources=resource(**values))
                self.assertEqual(decision.action, "abstain")
                self.assertIn("resources_unavailable", decision.reason)
        # At the measured boundary, the final admitted slot and context still fit.
        boundary = self.route(resources=resource(measured_ram_budget_bytes=2_256_000_000,
                                                 active_requests=1, context_tokens_in_use=3072))
        self.assertEqual(boundary.action, "route")

    def test_unevaluated_context_and_ram_are_not_inferred_from_model_size(self):
        decision = self.route(context_tokens=2049)
        self.assertIn("context_limit", decision.reason)
        receipts = (attestation(self.cloud, self.capability, peak_ram_bytes=0),)
        no_ram = self.route(self.router(models=(self.cloud,), receipts=receipts))
        self.assertIn("resources_unavailable", no_ram.reason)

    def test_attestation_binds_exact_capability_model_effort_and_time(self):
        changes = (
            {"capability_digest": "f" * 64},
            {"actual_model_id": "wrong-artifact"},
            {"selection": replace(self.cloud.selection, effort="low")},
            {"selection": replace(self.cloud.selection, model="other-v1")},
            {"passed": False},
            {"observed_at": NOW - 86401},
            {"observed_at": NOW + 1},
        )
        for values in changes:
            with self.subTest(values=values):
                router = self.router(receipts=(attestation(self.cloud, self.capability, **values),))
                decision = self.route(router)
                self.assertIn("capability_gap", decision.reason)

    def test_new_failed_receipt_revokes_old_pass_and_timestamp_conflicts_fail_closed(self):
        original = attestation(self.cloud, self.capability)
        for changed in (
            replace(original, passed=False, observed_at=NOW - 1),
            replace(original, passed=False, evidence_sha256="0" * 64),
            replace(original, context_tokens=4096, evidence_sha256="f" * 64),
        ):
            with self.subTest(changed=changed):
                decision = self.route(self.router(receipts=(original, changed)))
                self.assertIn("capability_gap", decision.reason)
        self.assertEqual(self.route(self.router(receipts=(original, original))).action, "route")

    def test_changed_capability_source_requires_new_attestation(self):
        updated = replace(self.capability, artifact_sha256="a" * 64)
        router = self.router(capabilities=(updated,), receipts=(attestation(self.cloud, self.capability),))
        self.assertIn("capability_gap", self.route(router).reason)

    def test_input_permissions_and_preconditions_apply_before_model_selection(self):
        for changes in ({"inputs": {}}, {"inputs": {"path": "x", "extra": "x"}},
                        {"facts": {"repository_selected": False}}, {"allowed_effects": ()}):
            with self.subTest(changes=changes):
                decision = self.route(**changes)
                self.assertEqual(decision.reason, "capability_preconditions_or_permissions_failed")

    def test_malformed_numeric_policy_and_measurement_values_fail(self):
        for changes in ({"active_requests": True}, {"measured_ram_budget_bytes": -1},
                        {"observed_at": float("nan")}, {"measurement_sha256": "not-a-hash"}):
            with self.subTest(changes=changes), self.assertRaises(RoutingError):
                resource(**changes)
        with self.assertRaises(RoutingError):
            self.router(policy="enabled")
        with self.assertRaises(RoutingError):
            self.route(escalation_reason="resources_unavailable")
        with self.assertRaises(RoutingError):
            self.route(mode="hybrid", escalation_reason="just_because")


class CapabilityTests(unittest.TestCase):
    def test_typed_inputs_and_explicit_postconditions(self):
        capability = operation(inputs=(Input("count", "integer"), Input("optional", "boolean", False)))
        self.assertTrue(capability.accepts({"count": 2}))
        self.assertFalse(capability.accepts({"count": True}))
        self.assertFalse(capability.accepts({"count": 1, "optional": 0}))
        self.assertFalse(capability.verify_postconditions({"count": 1}, {}))
        self.assertTrue(capability.verify_postconditions({"count": 1}, {"result_verified": True}))
        with self.assertRaises(RoutingError):
            operation(postconditions=())

    def test_fallback_preserves_contract_and_returns_auditable_chain(self):
        primary = operation(preconditions=(Condition("fact.primary_ready", "eq", True),), fallbacks=("backup",))
        backup = operation(id="backup")
        registry = CapabilityRegistry((primary, backup))
        chosen, chain = registry.resolve("inspect", {"path": "x"}, {"repository_selected": True}, (READ,))
        self.assertEqual(chosen, backup)
        self.assertEqual(chain, ("inspect", "backup"))

    def test_fallback_cannot_weaken_output_contract_or_expand_permissions(self):
        primary = operation(fallbacks=("backup",))
        for backup in (
            operation(id="backup", effects=(READ, WRITE)),
            operation(id="backup", postconditions=(Condition("fact.any_output", "present"),)),
            operation(id="backup", inputs=()),
        ):
            with self.subTest(backup=backup), self.assertRaises(RoutingError):
                CapabilityRegistry((primary, backup))

    def test_fallback_cycles_missing_targets_and_excess_depth_are_rejected(self):
        scenarios = (
            (operation(fallbacks=("inspect",)),),
            (operation(fallbacks=("missing",)),),
            (operation(fallbacks=("second",)), operation(id="second", fallbacks=("inspect",))),
            (operation(fallbacks=("second",)), operation(id="second", fallbacks=("third",)), operation(id="third")),
        )
        for capabilities in scenarios:
            with self.subTest(capabilities=capabilities), self.assertRaises(RoutingError):
                CapabilityRegistry(capabilities, max_fallback_hops=1)

    def test_shared_fallback_dag_is_valid_and_declaration_order_does_not_change_resolution(self):
        root = operation(fallbacks=("left", "right"), preconditions=(Condition("fact.unavailable", "eq", True),))
        left = replace(root, id="left", fallbacks=("leaf",))
        right = replace(root, id="right", fallbacks=("leaf",))
        leaf = operation(id="leaf")
        for ordered in ((root, left, right, leaf), (leaf, right, left, root)):
            registry = CapabilityRegistry(ordered, max_fallback_hops=2)
            _, chain = registry.resolve("inspect", {"path": "x"}, {"repository_selected": True}, (READ,))
            self.assertEqual(chain, ("inspect", "left", "leaf"))

    def test_digest_binds_source_contract_and_effects(self):
        capability = operation()
        for changes in ({"artifact_sha256": "a" * 64}, {"effects": (READ, WRITE)},
                        {"inputs": (Input("path", "string", False),)},
                        {"postconditions": (Condition("fact.result_verified", "eq", False),)}):
            with self.subTest(changes=changes):
                self.assertNotEqual(capability.digest, replace(capability, **changes).digest)


class TrainingGatesTests(unittest.TestCase):
    def setUp(self):
        self.permission = TrainingDataPermission(DATASET, "training", EVIDENCE, "test_owner", NOW + 3600)
        self.candidate = AdapterCandidate("5" * 64, "6" * 64, DATASET, operation().digest)
        self.evaluation = QuantizedEvaluation(self.candidate, "7" * 64, "fixture_verifier", True, NOW - 10)
        self.policy = RoutingPolicy(training_authorized=True, adapter_promotion_authorized=True)

    def training(self, **changes):
        options = dict(dataset_sha256=DATASET, permission=self.permission, policy=self.policy, now=NOW)
        options.update(changes)
        return check_training_permission(**options)

    def promotion(self, **changes):
        options = dict(candidate=self.candidate, permission=self.permission, evaluation=self.evaluation,
                       policy=self.policy, now=NOW)
        options.update(changes)
        return check_adapter_promotion(**options)

    def test_default_policy_never_authorizes_training_or_promotion(self):
        self.assertFalse(self.training(policy=RoutingPolicy()))
        self.assertFalse(self.promotion(policy=RoutingPolicy()))
        self.assertFalse(self.promotion(policy=replace(self.policy, adapter_promotion_authorized=False)))

    def test_training_needs_separate_exact_unexpired_dataset_permission(self):
        self.assertTrue(self.training())
        for permission in (None, replace(self.permission, purpose="inference"),
                           replace(self.permission, dataset_sha256="8" * 64),
                           replace(self.permission, expires_at=NOW)):
            with self.subTest(permission=permission):
                self.assertFalse(self.training(permission=permission))
                self.assertFalse(self.promotion(permission=permission))

    def test_promotion_binds_quantized_artifact_capability_and_fresh_passed_evaluation(self):
        self.assertTrue(self.promotion())
        for field in ("adapter_sha256", "quantized_artifact_sha256", "dataset_sha256", "capability_digest"):
            with self.subTest(field=field):
                mismatched = replace(self.candidate, **{field: "a" * 64})
                self.assertFalse(self.promotion(evaluation=replace(self.evaluation, candidate=mismatched)))
        for evaluation in (None, replace(self.evaluation, passed=False),
                           replace(self.evaluation, observed_at=NOW - 86401),
                           replace(self.evaluation, observed_at=NOW + 1)):
            with self.subTest(evaluation=evaluation):
                self.assertFalse(self.promotion(evaluation=evaluation))


if __name__ == "__main__":
    unittest.main()
