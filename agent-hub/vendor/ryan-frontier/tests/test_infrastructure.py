"""Invariant tests for content integrity, invalidation, conflicts, and routing."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from ryan_frontier.artifacts import ArtifactError, ArtifactStore, IntegrityError
from ryan_frontier.graph import (
    ConflictError, CycleError, GraphError, StaleSnapshotError, VersionedGraph,
    read_write_conflicts,
)
from ryan_frontier.routing import RoutingConfigError, RoutingPlanner, RoutingPolicy, RoutingRequest


class ArtifactStoreTests(unittest.TestCase):
    def test_concurrent_content_addressed_writes_and_verified_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            data = b"immutable artifact\n" * 10000
            with ThreadPoolExecutor(max_workers=8) as executor:
                digests = list(executor.map(store.put, [data] * 24))
            self.assertEqual(set(digests), {hashlib.sha256(data).hexdigest()})
            self.assertEqual(store.get(digests[0]), data)
            self.assertTrue(store.verify(digests[0]))
            self.assertEqual([path.name for path in Path(directory).iterdir()], [digests[0]])

    def test_corruption_is_rejected_on_read_and_repeat_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            digest = store.put(b"known")
            (Path(directory) / digest).write_bytes(b"tampered")
            with self.assertRaises(IntegrityError):
                store.get(digest)
            with self.assertRaises(IntegrityError):
                store.put(b"known")
            self.assertFalse(store.verify(digest))

    def test_traversal_and_symlinks_cannot_escape_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            outside = Path(directory) / "outside"
            outside.write_bytes(b"private")
            for digest in ("../outside", str(outside), "a" * 63, "A" * 64, "a" * 64 + "/suffix"):
                with self.assertRaises(ArtifactError):
                    store.get(digest)
            digest = hashlib.sha256(b"private").hexdigest()
            (store.root / digest).symlink_to(outside)
            with self.assertRaises(OSError):
                store.get(digest)
            self.assertFalse(store.verify(digest))


class VersionedGraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = VersionedGraph()
        self.graph.add_resource("contract:auth", kind="contract")
        self.graph.add_resource("file:client.py")
        self.graph.add_resource("artifact:client-tests", kind="artifact")
        self.graph.add_resource("file:unrelated.py")
        self.graph.add_dependency("file:client.py", "contract:auth")
        self.graph.add_dependency("artifact:client-tests", "file:client.py")

    def test_contract_change_invalidates_reverse_transitive_cache(self):
        for key in self.graph.resources:
            self.graph.cache_put(key, "cached:" + key)
        affected = self.graph.update_resource("contract:auth", content_hash="new-interface")
        self.assertEqual(affected, {"contract:auth", "file:client.py", "artifact:client-tests"})
        for key in affected:
            with self.assertRaises(KeyError):
                self.graph.cache_get(key)
        self.assertEqual(self.graph.cache_get("file:unrelated.py"), "cached:file:unrelated.py")

    def test_cycle_rejection_preserves_graph(self):
        before = self.graph.to_json()
        with self.assertRaises(CycleError):
            self.graph.add_dependency("contract:auth", "artifact:client-tests")
        self.assertEqual(self.graph.to_json(), before)
        with self.assertRaises(CycleError):
            self.graph.add_dependency("contract:auth", "contract:auth")

    def test_stale_contract_snapshot_rejects_whole_commit(self):
        transaction = self.graph.begin_transaction(reads=["artifact:client-tests"], writes=["file:client.py", "file:unrelated.py"])
        versions_before = {key: self.graph.resources[key].version for key in transaction.writes}
        self.graph.update_resource("contract:auth")
        with self.assertRaises(StaleSnapshotError):
            self.graph.commit(transaction, content_hashes={"file:client.py": "new"})
        self.assertEqual({key: self.graph.resources[key].version for key in transaction.writes}, versions_before)

    def test_dependency_edge_change_invalidates_existing_snapshot(self):
        snapshot = self.graph.snapshot(["file:unrelated.py"])
        self.graph.add_dependency("file:unrelated.py", "contract:auth")
        with self.assertRaises(StaleSnapshotError):
            self.graph.validate_snapshot(snapshot)

    def test_contract_read_write_and_write_write_conflicts(self):
        reader = self.graph.begin_transaction(reads=["artifact:client-tests"])
        with self.assertRaises(ConflictError):
            self.graph.begin_transaction(writes=["contract:auth"])
        unrelated = self.graph.begin_transaction(writes=["file:unrelated.py"])
        self.graph.commit(unrelated)
        self.graph.abort(reader)
        writer = self.graph.begin_transaction(writes=["contract:auth"])
        with self.assertRaises(ConflictError):
            self.graph.begin_transaction(writes=["contract:auth"])
        self.graph.abort(writer)
        self.assertEqual(read_write_conflicts(["contract:auth"], [], [], ["contract:auth"]), {"contract:auth"})

    def test_optimistic_overlapping_writer_is_stale_after_first_commit(self):
        first = self.graph.begin_transaction(writes=["contract:auth"], detect_conflicts=False)
        second = self.graph.begin_transaction(writes=["contract:auth"], detect_conflicts=False)
        self.graph.commit(first)
        with self.assertRaises(StaleSnapshotError):
            self.graph.commit(second)

    def test_round_trip_preserves_versions_and_snapshot_validity(self):
        self.graph.update_resource("contract:auth", content_hash="revision")
        snapshot = self.graph.snapshot(["artifact:client-tests"])
        loaded = VersionedGraph.from_json(self.graph.to_json())
        self.assertEqual(loaded.to_dict(), self.graph.to_dict())
        loaded.validate_snapshot(snapshot)
        with self.assertRaises(StaleSnapshotError):
            VersionedGraph().validate_snapshot(snapshot)
        with self.assertRaises(TypeError):
            snapshot.versions["contract:auth"] = 0

    def test_commit_rejects_undeclared_write(self):
        transaction = self.graph.begin_transaction(writes=["file:client.py"])
        before = self.graph.to_json()
        with self.assertRaises(GraphError):
            self.graph.commit(transaction, content_hashes={"contract:auth": "changed"})
        self.assertEqual(self.graph.to_json(), before)
        self.graph.abort(transaction)

    def test_divergent_restored_graphs_reject_same_number_different_revisions(self):
        serialized = self.graph.to_json()
        left = VersionedGraph.from_json(serialized)
        right = VersionedGraph.from_json(serialized)
        original_snapshot = self.graph.snapshot(["contract:auth"])
        left.validate_snapshot(original_snapshot)
        right.validate_snapshot(original_snapshot)

        left.update_resource("contract:auth", content_hash="left-content")
        right.update_resource("contract:auth", content_hash="right-content")
        self.assertEqual(left.resources["contract:auth"].version,
                         right.resources["contract:auth"].version)
        self.assertNotEqual(left.resources["contract:auth"].revision_id,
                            right.resources["contract:auth"].revision_id)
        left_snapshot = left.snapshot(["contract:auth"])
        with self.assertRaises(StaleSnapshotError):
            right.validate_snapshot(left_snapshot)
        with self.assertRaises(StaleSnapshotError):
            right.cache_put("contract:auth", "left result", snapshot=left_snapshot)

        # The identity survives persistence without invalidating unchanged state.
        restored_left = VersionedGraph.from_json(left.to_json())
        restored_left.validate_snapshot(left_snapshot)


def provider(key, kind="subscription", **overrides):
    result = {
        "id": key, "kind": kind, "model": "EXPLICIT_MODEL",
        "strongest_model": "EXPLICIT_MODEL", "effort": "EXPLICIT_MAX_EFFORT",
        "highest_effort": "EXPLICIT_MAX_EFFORT", "supported_efforts": ["EXPLICIT_MAX_EFFORT"],
        "capabilities": ["code"], "verified": True,
    }
    if kind == "subscription":
        result["allowance_pool"] = key
    if kind == "api":
        result["cash_pool"] = key
    result.update(overrides)
    return result


def policy_config():
    return {
        "schema_version": 1,
        "allowance_pools": [
            {"id": "alpha", "capacity": 100, "used": 20, "weight": 1},
            {"id": "beta", "capacity": 1000, "used": 100, "weight": 1},
            {"id": "chatgpt", "capacity": 1000, "used": 0, "weight": 1},
        ],
        "api_cash_pools": [{"id": "paid", "budget": 10, "spent": 0}],
        "providers": [provider("alpha"), provider("beta"), provider("chatgpt", reserve_for_connection=True), provider("paid", kind="api")],
    }


class RoutingTests(unittest.TestCase):
    def route(self, config=None, **request):
        policy = RoutingPolicy.from_dict(config if config is not None else policy_config())
        return RoutingPlanner(policy).plan(RoutingRequest(capabilities=frozenset({"code"}), **request))

    def test_normalized_utilization_and_weight_drive_allowance_fairness(self):
        self.assertEqual(self.route().provider_id, "beta")
        config = policy_config()
        config["allowance_pools"][0]["weight"] = 3
        self.assertEqual(self.route(config).provider_id, "alpha")

    def test_connection_reserve_excluded_unless_explicitly_included(self):
        self.assertEqual(self.route().provider_id, "beta")
        self.assertEqual(self.route(include_connection_reserve=True).provider_id, "chatgpt")

    def test_api_cash_is_separate_and_requires_explicit_opt_in_and_estimate(self):
        config = policy_config()
        for pool in config["allowance_pools"]:
            pool["used"] = pool["capacity"]
        self.assertTrue(self.route(config).abstained)
        self.assertTrue(self.route(config, allow_api_cash=True).abstained)
        plan = self.route(config, allow_api_cash=True, estimated_api_cost=2)
        self.assertEqual((plan.provider_id, plan.funding_pool), ("paid", "paid"))
        self.assertTrue(self.route(config, allow_api_cash=True, estimated_api_cost=11).abstained)
        self.assertEqual(config["api_cash_pools"][0]["spent"], 0)

    def test_local_only_abstains_until_capable_attested_local_provider_exists(self):
        config = policy_config()
        self.assertTrue(self.route(config, local_only=True, allow_api_cash=True, estimated_api_cost=1).abstained)
        config["providers"].append(provider("local", kind="local", capabilities=["text"]))
        self.assertTrue(self.route(config, local_only=True).abstained)
        config["providers"][-1]["capabilities"] = ["code"]
        self.assertEqual(self.route(config, local_only=True).provider_id, "local")

    def test_unverified_model_effort_and_capability_fail_closed(self):
        cases = [
            {"verified": False},
            {"model": "PROBABLY_STRONGER_MODEL"},
            {"effort": "lower"},
            {"supported_efforts": ["lower"]},
            {"capabilities": ["text"]},
        ]
        for change in cases:
            with self.subTest(change=change):
                config = policy_config()
                config["providers"] = [provider("alpha", **change)]
                self.assertTrue(self.route(config).abstained)

    def test_frozen_policy_hash_is_stable_and_source_mutation_cannot_change_plan(self):
        config = policy_config()
        original = deepcopy(config)
        policy = RoutingPolicy.from_dict(config)
        planner = RoutingPlanner(policy)
        request = RoutingRequest(frozenset({"code"}))
        before = planner.plan(request).to_dict()
        config["providers"][1]["verified"] = False
        config["allowance_pools"][1]["used"] = 1000
        self.assertEqual(planner.plan(request).to_dict(), before)
        self.assertEqual(policy.policy_hash, RoutingPolicy.from_dict(original).policy_hash)
        self.assertNotEqual(policy.policy_hash, RoutingPolicy.from_dict(config).policy_hash)
        self.assertEqual(policy.to_dict(), original)

    def test_invalid_funding_and_numeric_inputs_are_rejected(self):
        config = policy_config()
        config["providers"][0]["cash_pool"] = "paid"
        with self.assertRaises(RoutingConfigError):
            RoutingPolicy.from_dict(config)
        for invalid in (0, -1, float("inf"), True):
            with self.subTest(invalid=invalid), self.assertRaises(RoutingConfigError):
                RoutingRequest(frozenset({"code"}), allowance_units=invalid)
        with self.assertRaises(RoutingConfigError):
            RoutingRequest(frozenset())


if __name__ == "__main__":
    unittest.main()
