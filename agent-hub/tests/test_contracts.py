"""Offline ledger checks. Receipt fixtures are synthetic, never model evidence."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import unittest

from agent_hub.contracts import (Conflict, ContractError, TaskLedger, resolve_allowed_path,
                                  validate_changed_files, validate_contract)


BASE = "1" * 40
CANDIDATE = "2" * 40
DEFINITION = hashlib.sha256(b"SYNTHETIC acceptance definition").hexdigest()
PATCH = b"SYNTHETIC fixture patch; no real repository revision is asserted."


def contract(task_id="task-one", **updates):
    result = {
        "task_id": task_id, "repository": {"id": "sample-repo", "base_commit": BASE},
        "allowed_files": ["src/"], "dependencies": [], "capability": "code.edit",
        "model": {"provider": "openai", "model": "gpt-6", "effort": "high"},
        "reviewer_model": {"provider": "anthropic", "model": "claude-opus", "effort": "high"},
        "permissions": {"tools": ["file.read", "file.patch"], "network_origins": [], "credential_aliases": []},
        "budget": {"max_attempts": 3, "max_cost_microusd": 1000, "attempt_reserve_microusd": 400},
        "deadline": 2000, "acceptance_tests": [{"id": "behavior", "definition_sha256": DEFINITION}],
        "approval": {"human_required": True, "independent_review": True},
    }
    result.update(updates)
    return result


class ContractTests(unittest.TestCase):
    def test_contract_requires_exact_commit_and_explicit_model_effort(self):
        self.assertEqual(validate_contract(contract()), contract())
        changes = ({"repository": {"id": "repo", "base_commit": "main"}},
                   {"model": {"provider": "openai", "model": "auto", "effort": "high"}},
                   {"model": {"provider": "openai", "model": "gpt-6", "effort": "auto"}},
                   {"approval": {"human_required": False, "independent_review": True}},
                   {"acceptance_tests": []}, {"deadline": True})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_contract(contract(**change))

    def test_protected_unsafe_and_windows_ambiguous_paths_fail(self):
        bad_paths = ("../secrets", "C:/secrets", "src\\file.py", "/etc/passwd", "src/*", "src/../policy/x",
                     "src/tests/test_x.py", "POLICIES/rules.json", "approvals/allow.json", "tests/",
                     ".git/config", "AGENTS.md", "src/COM1.txt", "src/file.", "src/a%2fb", "src/file:stream")
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(ContractError):
                validate_contract(contract(allowed_files=[path]))
        with self.assertRaises(ContractError):
            validate_changed_files(contract(), ["elsewhere/file.py"])
        with self.assertRaises(ContractError):
            validate_changed_files(contract(), ["src/tests/acceptance.py"])

    def test_permissions_are_aliases_and_origins_not_secret_strings_or_commands(self):
        for permissions in ({"tools": ["cmd /c del"], "network_origins": [], "credential_aliases": []},
                            {"tools": [], "network_origins": ["https://user:password@host.example"], "credential_aliases": []},
                            {"tools": [], "network_origins": ["http://host.example"], "credential_aliases": []},
                            {"tools": [], "network_origins": [], "credential_aliases": ["a secret with spaces"]}):
            with self.assertRaises(ContractError):
                validate_contract(contract(permissions=permissions))

    def test_resolved_path_is_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(resolve_allowed_path(root, "src/app.py", contract()), root / "src" / "app.py")
            with self.assertRaises(ContractError):
                resolve_allowed_path(root, "../outside.py", contract())
            self.assertTrue((root / "src" / "app.py").resolve().is_relative_to(root))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "task-ledger.sqlite3"
        self.now = 1000
        self.ledger = TaskLedger(self.path, clock=lambda: self.now)
        self.ledger.register([contract()])

    def tearDown(self):
        self.assertTrue(self.path.resolve().is_relative_to(self.root))
        self.temp.cleanup()

    def acquire(self, task_id="task-one", **kwargs):
        return self.ledger.acquire(task_id, "worker-one", ["code.edit"], contract()["model"], **kwargs)

    def complete(self, lease, *, success=True, cost=100, actual_model="builder-snapshot", candidate=CANDIDATE):
        return self.ledger.complete(lease, succeeded=success, actual_cost_microusd=cost,
                                    actual_model_id=actual_model, summary="Synthetic test fixture",
                                    candidate_revision=candidate if success else None,
                                    patch_sha256=hashlib.sha256(PATCH).hexdigest() if success else None)

    def evidence_args(self, *, candidate=CANDIDATE, actual_reviewer="reviewer-snapshot", exit_code=0):
        return dict(candidate_revision=candidate, base_commit=BASE, patch=PATCH, changed_files=["src/app.py"],
                    test_results=[{"id": "behavior", "definition_sha256": DEFINITION, "revision": candidate,
                                   "base_commit": BASE, "exit_code": exit_code, "status": "passed" if exit_code == 0 else "failed",
                                   "started_at": 1001, "finished_at": 1002, "log": b"SYNTHETIC test log, not production test evidence"}],
                    review={"selection": contract()["reviewer_model"], "actual_model_id": actual_reviewer,
                            "revision": candidate, "base_commit": BASE, "verdict": "approve", "blocking_findings": [],
                            "report": b"SYNTHETIC review fixture, no provider was called"},
                    toolchain={"python": "3.12", "dependencies_sha256": "a" * 64}, risks=[])

    def verified(self, task_id="task-one", candidate=CANDIDATE):
        self.complete(self.acquire(task_id), candidate=candidate)
        return self.ledger.record_evidence(task_id, **self.evidence_args(candidate=candidate))["evidence_digest"]

    def approved(self, task_id="task-one", candidate=CANDIDATE):
        digest = self.verified(task_id, candidate)
        approval = self.ledger.approve(task_id, evidence_digest=digest, revision=candidate,
                                       current_base=BASE, approved_by="unit-test-operator")
        return digest, approval["approval_id"]

    def test_restart_retains_contract_running_lease_and_reserved_budget(self):
        lease = self.acquire()
        self.ledger = TaskLedger(self.path, clock=lambda: self.now)
        current = self.ledger.get("task-one")
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["reserved_microusd"], 400)
        self.assertNotIn(lease["lease_token"], str(current))
        self.complete(lease)
        self.assertEqual(self.ledger.get("task-one")["spent_microusd"], 100)

    def test_duplicate_completion_is_idempotent_but_different_result_is_rejected(self):
        lease = self.acquire()
        first = self.complete(lease)
        self.assertEqual(self.complete(lease), first)
        self.assertEqual(self.ledger.get("task-one")["spent_microusd"], 100)
        with self.assertRaises(Conflict):
            self.complete(lease, cost=99)
        self.assertEqual(self.ledger.get("task-one")["fence"], 1)

    def test_disconnect_holds_budget_until_reconciled_and_rejects_stale_completion(self):
        old = self.acquire(lease_seconds=10)
        self.now += 11
        self.assertEqual(self.ledger.expire(), 1)
        current = self.ledger.get("task-one")
        self.assertEqual((current["state"], current["reserved_microusd"]), ("reconcile_required", 400))
        with self.assertRaises(Conflict):
            self.acquire()
        with self.assertRaises(Conflict):
            self.complete(old)
        self.ledger.reconcile("task-one", old["fence"], outcome="completed_unrecoverable", actual_cost_microusd=120)
        self.ledger.reconcile("task-one", old["fence"], outcome="completed_unrecoverable", actual_cost_microusd=120)
        fresh = self.acquire()
        self.assertGreater(fresh["fence"], old["fence"])
        with self.assertRaises(Conflict):
            self.complete(old)
        self.complete(fresh)
        self.assertEqual(self.ledger.get("task-one")["spent_microusd"], 220)

    def test_parallel_acquisition_has_one_fence_owner(self):
        def attempt(_):
            try:
                return self.acquire()
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            leases = list(pool.map(attempt, range(6)))
        self.assertEqual(sum(lease is not None for lease in leases), 1)
        self.assertEqual(self.ledger.get("task-one")["reserved_microusd"], 400)

    def test_failed_attempts_consume_cost_and_budget_cannot_be_oversubscribed(self):
        self.complete(self.acquire(), success=False, cost=350)
        self.complete(self.acquire(), success=False, cost=350)
        with self.assertRaises(Conflict):
            self.acquire()
        self.assertEqual(self.ledger.get("task-one")["spent_microusd"], 700)

    def test_actual_overrun_is_recorded_and_blocks_further_spending(self):
        self.complete(self.acquire(), success=False, cost=450)
        current = self.ledger.get("task-one")
        self.assertEqual((current["state"], current["spent_microusd"]), ("budget_exceeded", 450))
        with self.assertRaises(Conflict):
            self.acquire()

    def test_not_started_reconciliation_still_consumes_attempt_limit(self):
        for _ in range(3):
            lease = self.acquire(lease_seconds=1)
            self.now += 2
            self.ledger.reconcile("task-one", lease["fence"], outcome="not_started", actual_cost_microusd=0)
        with self.assertRaises(Conflict):
            self.acquire()
        self.assertEqual(self.ledger.get("task-one")["spent_microusd"], 0)

    def test_deadline_caps_lease_and_heartbeat(self):
        self.now = 1990
        lease = self.acquire(lease_seconds=45)
        self.assertEqual(lease["expires_at"], 2000)
        self.now = 1999
        self.assertEqual(self.ledger.heartbeat(lease)["expires_at"], 2000)
        self.now = 2000
        with self.assertRaises(Conflict):
            self.ledger.heartbeat(lease)

    def test_capability_and_exact_model_effort_are_enforced(self):
        for capabilities, model in (([], contract()["model"]),
                                    (["code.edit"], {**contract()["model"], "effort": "low"}),
                                    (["code.edit"], {**contract()["model"], "model": "cheaper-model"})):
            with self.assertRaises(ContractError):
                self.ledger.acquire("task-one", "worker", capabilities, model)
        self.assertEqual(self.ledger.get("task-one")["fence"], 0)

    def test_contract_and_acceptance_definitions_are_immutable(self):
        self.assertEqual(self.ledger.register([contract()]), ["task-one"])
        changed = contract(acceptance_tests=[{"id": "behavior", "definition_sha256": "f" * 64}])
        with self.assertRaises(Conflict):
            self.ledger.register([changed])
        self.assertEqual(self.ledger.get("task-one")["contract"]["acceptance_tests"][0]["definition_sha256"], DEFINITION)

    def test_dag_cycles_and_missing_dependencies_are_rejected_atomically(self):
        with self.assertRaises(ContractError):
            self.ledger.register([contract("a", dependencies=["b"]), contract("b", dependencies=["a"])])
        with self.assertRaises(ContractError):
            self.ledger.get("a")
        with self.assertRaises(ContractError):
            self.ledger.register([contract("a", dependencies=["missing"])])

    def test_dependency_evidence_is_required_and_pinned_during_execution(self):
        self.ledger.register([contract("dependent", dependencies=["task-one"])])
        with self.assertRaises(Conflict):
            self.acquire("dependent")
        self.verified()
        dependent = self.acquire("dependent")
        self.acquire("task-one")
        self.complete(dependent)
        with self.assertRaises(Conflict):
            self.ledger.record_evidence("dependent", **self.evidence_args())

    def test_evidence_has_attached_content_and_exact_candidate_test_bindings(self):
        digest = self.verified()
        self.ledger = TaskLedger(self.path, clock=lambda: self.now)
        bundle = self.ledger.evidence(digest)
        self.assertEqual(self.ledger.artifact(bundle["patch"]["sha256"]), PATCH)
        self.assertIn(b"SYNTHETIC", self.ledger.artifact(bundle["tests"][0]["log"]["sha256"]))
        for field, value in (("patch", b"different patch"), ("candidate_revision", "3" * 40),
                             ("changed_files", ["tests/acceptance.py"])):
            values = self.evidence_args()
            values[field] = value
            with self.assertRaises(ContractError):
                self.ledger.record_evidence("task-one", **values)
        values = self.evidence_args()
        values["test_results"][0]["definition_sha256"] = "f" * 64
        with self.assertRaises(ContractError):
            self.ledger.record_evidence("task-one", **values)

    def test_unknown_or_same_underlying_model_does_not_pass_independent_review(self):
        self.complete(self.acquire(), actual_model="same-snapshot")
        for actual_reviewer in (None, "same-snapshot", "SAME-SNAPSHOT"):
            result = self.ledger.record_evidence("task-one", **self.evidence_args(actual_reviewer=actual_reviewer))
            self.assertFalse(result["verified"])
            self.assertFalse(result["independent_model_ids"])
            with self.assertRaises(Conflict):
                self.ledger.approve("task-one", evidence_digest=result["evidence_digest"], revision=CANDIDATE,
                                     current_base=BASE, approved_by="operator")
        # App/provider labels differ in this fixture; they alone are not evidence.
        self.assertNotEqual(contract()["model"]["provider"], contract()["reviewer_model"]["provider"])

    def test_failed_actual_test_cannot_receive_approval(self):
        self.complete(self.acquire())
        result = self.ledger.record_evidence("task-one", **self.evidence_args(exit_code=1))
        self.assertFalse(result["verified"])
        with self.assertRaises(Conflict):
            self.ledger.approve("task-one", evidence_digest=result["evidence_digest"], revision=CANDIDATE,
                                 current_base=BASE, approved_by="operator")

    def test_approval_is_bound_to_exact_revision_base_and_digest(self):
        digest = self.verified()
        for overrides in ({"revision": "3" * 40}, {"current_base": "3" * 40}, {"evidence_digest": "f" * 64}):
            values = dict(evidence_digest=digest, revision=CANDIDATE, current_base=BASE, approved_by="operator")
            values.update(overrides)
            with self.assertRaises(Conflict):
                self.ledger.approve("task-one", **values)

    def test_changed_base_invalidates_an_existing_approval(self):
        _, approval = self.approved()
        self.ledger.observe_base("sample-repo", "3" * 40)
        self.ledger = TaskLedger(self.path, clock=lambda: self.now)
        self.assertEqual(self.ledger.get("task-one")["state"], "needs_rebase")
        with self.assertRaises(Conflict):
            self.ledger.promote("task-one", approval_id=approval, expected_base=BASE)

    def test_changed_evidence_invalidates_prior_approval(self):
        _, approval = self.approved()
        values = self.evidence_args()
        values["risks"] = ["Newly identified synthetic risk"]
        self.ledger.record_evidence("task-one", **values)
        with self.assertRaises(Conflict):
            self.ledger.promote("task-one", approval_id=approval, expected_base=BASE)

    def test_promotion_is_serialized_and_idempotent_without_executing_git(self):
        self.ledger.register([contract("other")])
        _, first = self.approved()
        _, second = self.approved("other", "3" * 40)
        def promote(item):
            try:
                return self.ledger.promote(item[0], approval_id=item[1], expected_base=BASE)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(promote, [("task-one", first), ("other", second)]))
        winner = next(receipt for receipt in receipts if receipt is not None)
        self.assertEqual(sum(receipt is not None for receipt in receipts), 1)
        self.assertTrue(winner["record_only"])
        self.assertEqual(self.ledger.promote(winner["task_id"], approval_id=winner["approval_id"], expected_base=BASE), winner)
        self.assertFalse((self.root / ".git").exists())


if __name__ == "__main__":
    unittest.main()
