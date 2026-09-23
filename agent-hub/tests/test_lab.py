"""Real CLI subprocess tests; all repositories, contracts and scores are synthetic."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from agent_hub.lab import MAX_REQUEST_BYTES


PROJECT = Path(__file__).resolve().parents[1]
PRIVATE_SENTINEL = "SYNTHETIC_PRIVATE_CONTENT_NOT_A_REAL_SECRET"


class LabCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runcrew-synthetic-lab-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "private-state"
        self.root.mkdir(mode=0o700)
        self.sequence = 0

    def command(self, *arguments, request=None, raw=None, expected=0, root=None):
        command = [sys.executable, "-m", "agent_hub.lab", "--root", str(root or self.root), *arguments]
        if request is not None or raw is not None:
            self.sequence += 1
            input_path = self.base / f"synthetic-request-{self.sequence}.json"
            input_path.write_text(raw if raw is not None else json.dumps(request), encoding="utf-8")
            command += ["--input", str(input_path)]
        process = subprocess.run(command, cwd=PROJECT, text=True, encoding="utf-8", capture_output=True,
                                 timeout=20, check=False)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertNotIn(PRIVATE_SENTINEL, process.stdout)
        envelope = json.loads(process.stdout)
        self.assertEqual(envelope["ok"], expected == 0)
        return envelope.get("result", envelope)

    def candidate(self, version):
        return self.command("research", "add-candidate", request={
            "kind": "task_agent", "artifact": {"synthetic": True, "version": version, "private": PRIVATE_SENTINEL},
            "proposer": PRIVATE_SENTINEL, "niche": PRIVATE_SENTINEL,
        })

    def contract(self):
        return {"task_id": PRIVATE_SENTINEL,
                "repository": {"id": "synthetic-project", "base_commit": "a" * 40},
                "allowed_files": ["src/example.py"], "dependencies": [], "capability": "code.edit",
                "model": {"provider": "synthetic", "model": "synthetic-builder", "effort": "high"},
                "reviewer_model": {"provider": "synthetic", "model": "synthetic-reviewer", "effort": "high"},
                "permissions": {"tools": [], "network_origins": [], "credential_aliases": []},
                "budget": {"max_attempts": 2, "max_cost_microusd": 100, "attempt_reserve_microusd": 50},
                "deadline": int(time.time()) + 3600,
                "acceptance_tests": [{"id": "synthetic-test", "definition_sha256": "b" * 64}],
                "approval": {"human_required": True, "independent_review": True}}

    def test_empty_status_is_read_only_and_explicit_root_required(self):
        result = self.command("status")
        self.assertFalse(result["contracts"]["present"])
        self.assertFalse(result["research"]["present"])
        self.assertFalse(result["context"]["present"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.command("status", root="relative-state", expected=2)
        self.command("status", root=self.base / "missing", expected=2)
        self.command("status", root=PROJECT, expected=2)
        self.assertFalse((self.base / "missing").exists())

    def test_context_compiles_real_source_and_reuses_hash_without_echo(self):
        repository = self.base / "synthetic-repository"
        repository.mkdir()
        content = f"# {PRIVATE_SENTINEL}\ndef add(a, b):\n    return a + b\n"
        (repository / "example.py").write_text(content, encoding="utf-8", newline="")
        request = {"repo_root": str(repository), "selected_files": ["example.py"],
                   "repo_revision": "synthetic-revision", "toolchain": {"python": "synthetic-toolchain"}}
        first = self.command("context", "compile", request=request)
        self.assertFalse(first["cache_hit"])
        self.assertEqual(first["files"], 1)
        self.assertEqual(first["input_bytes"], len(content.encode()))
        self.assertTrue((self.root / "context" / "blobs" / hashlib.sha256(content.encode()).hexdigest()).is_file())
        second = self.command("context", "compile", request=request)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["artifact_hash"], second["artifact_hash"])
        status = self.command("status")
        self.assertEqual(status["context"]["blobs"]["count"], 3)
        self.assertEqual(status["context"]["cache_entries"]["count"], 1)

    def test_contract_register_get_counts_and_immutable_rejection(self):
        contract = self.contract()
        result = self.command("contract", "register", request={"contracts": [contract]})
        self.assertEqual(result["count"], 1)
        digest = result["contracts"][0]["contract_digest"]
        state = self.command("contract", "get", request={"task_id": contract["task_id"]})
        self.assertEqual(state["contract_digest"], digest)
        self.assertEqual(state["state"], "pending")
        self.assertNotIn("contract", state)
        self.command("contract", "register", request={"contracts": [contract]})
        contract["allowed_files"] = ["src/different.py"]
        self.command("contract", "register", request={"contracts": [contract]}, expected=2)
        counts = self.command("status")["contracts"]
        self.assertEqual(counts["tasks"], 1)
        self.assertEqual(counts["states"], {"pending": 1})
        self.assertEqual(counts["spent_microusd"], 0)

    def test_research_end_to_end_preregister_record_finalize_no_false_promotion(self):
        baseline = self.candidate(0)
        candidate = self.candidate(1)
        epoch = self.command("research", "create-epoch", request={"policy": {
            "name": PRIVATE_SENTINEL, "sample_size": 2, "max_candidates": 1, "alpha": 0.05,
            "meaningful_delta": 0.1, "budget_per_arm": 2, "evaluator_digest": "e" * 64,
            "protected_contract_digest": "f" * 64, "guard_names": [PRIVATE_SENTINEL]}})
        holds = [PRIVATE_SENTINEL + "1", PRIVATE_SENTINEL + "2"]
        trial = self.command("research", "preregister", request={
            "epoch_id": epoch["id"], "candidate_id": candidate["id"], "baseline_id": baseline["id"],
            "holdout_ids": holds, "independent_evaluator": "synthetic-independent-evaluator"})
        before = self.command("status")["research"]
        self.assertEqual(before["pending_trials"], 1)
        self.assertEqual(before["reserved_holdouts"], 2)
        self.command("research", "finalize", request={"trial_id": trial["id"]}, expected=2)
        self.assertEqual(self.command("status")["research"]["completed_trials"], 0)
        for index, holdout in enumerate(holds, 1):
            result = self.command("research", "record", request={
                "trial_id": trial["id"], "holdout_id": holdout,
                "candidate_score": 1, "baseline_score": 0, "candidate_cost": 1, "baseline_cost": 1,
                "candidate_latency_ms": 10, "candidate_robustness": 1, "contracts_pass": True,
                "guard_deltas": {PRIVATE_SENTINEL: 0}, "evaluator_digest": "e" * 64,
                "protected_contract_digest": "f" * 64, "candidate_content_hash": candidate["content_hash"],
                "baseline_content_hash": baseline["content_hash"]})
            self.assertEqual(result["recorded_pairs"], index)
            self.assertNotIn("lower_bound", result)
        decision = self.command("research", "finalize", request={"trial_id": trial["id"]})
        self.assertTrue(decision["record_only"])
        self.assertTrue(decision["valid"])
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reasons"], ["lower_bound_not_above_meaningful_delta"])
        self.assertNotIn("guard_minima", decision)
        counts = self.command("status")["research"]
        self.assertEqual(counts["records"]["candidate"], 2)
        self.assertEqual(counts["records"]["epoch"], 1)
        self.assertEqual(counts["completed_trials"], 1)
        self.assertEqual(counts["pending_trials"], 0)
        self.assertEqual(counts["recorded_pairs"], 2)

    def test_bad_input_is_bounded_and_error_does_not_echo_values(self):
        for raw in ('{"artifact":1,"artifact":2,"kind":"task_agent"}',
                    '{"kind":"task_agent","artifact":NaN}',
                    '{"kind":"task_agent","artifact":1e999}',
                    '{"kind":"task_agent","artifact":' + '[' * 60 + '0' + ']' * 60 + '}',
                    PRIVATE_SENTINEL + "x" * MAX_REQUEST_BYTES):
            self.command("research", "add-candidate", raw=raw, expected=2)
        self.command("research", "add-candidate", request={"kind": "task_agent", "artifact": PRIVATE_SENTINEL,
                                                            "unexpected": PRIVATE_SENTINEL}, expected=2)
        self.command("status", "--accidental-credential", PRIVATE_SENTINEL, expected=2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_stores_not_created_by_lookup(self):
        self.command("contract", "get", request={"task_id": PRIVATE_SENTINEL}, expected=2)
        self.command("research", "finalize", request={"trial_id": "a" * 64}, expected=2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_status_reports_real_counters_and_rejects_unknown_state_without_echo(self):
        self.command("contract", "register", request={"contracts": [self.contract()]})
        with closing(sqlite3.connect(self.root / "contracts.sqlite3")) as db:
            db.execute("UPDATE tasks SET spent=7, reserved=11")
            db.commit()
        counts = self.command("status")["contracts"]
        self.assertEqual((counts["spent_microusd"], counts["reserved_microusd"]), (7, 11))
        with closing(sqlite3.connect(self.root / "contracts.sqlite3")) as db:
            db.execute("UPDATE tasks SET state=?", (PRIVATE_SENTINEL,))
            db.commit()
        self.command("status", expected=2)

    def test_symlink_state_is_rejected_if_platform_can_create_links(self):
        target = self.base / "another-private-state"
        target.mkdir(mode=0o700)
        linked = self.base / "linked-state"
        try:
            linked.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Platform account cannot create directory symlinks")
        self.command("status", root=linked, expected=2)


if __name__ == "__main__":
    unittest.main()
