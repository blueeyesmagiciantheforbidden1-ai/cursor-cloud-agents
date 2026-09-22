"""Bounded, append-only research records. No candidate code is executed here.

Trusted evaluation workers supply measurements. This module validates evidence
shape, lineage, budgets and a preregistered decision rule, not their truthfulness.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import fmean


MAX_RECORD_BYTES = 262_144
MAX_SAMPLES = 1024
MAX_CANDIDATES = 128
MAX_ARCHIVE_RECORDS = 20_000
KINDS = ("task_agent", "improvement_procedure")
HEX = re.compile(r"[a-f0-9]{64}\Z")


class ResearchError(ValueError):
    pass


def canonical(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ResearchError("Record must be finite, UTF-8 JSON") from exc
    if len(encoded) > MAX_RECORD_BYTES:
        raise ResearchError("Research record exceeds its byte limit")
    return encoded


def content_hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _text(value, field, limit=1000):
    try:
        valid = isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= limit
    except UnicodeError:
        valid = False
    if not valid:
        raise ResearchError(f"{field} must be nonempty text within {limit} bytes")
    return value


def _integer(value, field, low=0, high=10**12):
    if type(value) is not int or not low <= value <= high:
        raise ResearchError(f"{field} must be an integer from {low} to {high}")
    return value


def _number(value, field, low=0, high=1):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not low <= value <= high):
        raise ResearchError(f"{field} must be finite and in [{low}, {high}]")
    return float(value)


def _digest(value, field):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ResearchError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _ids(values, field, maximum, *, minimum=1):
    if not isinstance(values, (list, tuple)) or not minimum <= len(values) <= maximum:
        raise ResearchError(f"{field} has an invalid count")
    normalized = [_text(value, field, 128) for value in values]
    if len(set(normalized)) != len(normalized):
        raise ResearchError(f"{field} must be unique")
    return normalized


def hoeffding_lower_bound(deltas, alpha):
    """One-sided bound for independent, preregistered paired deltas in [-1,1]."""
    if not deltas:
        raise ResearchError("A fixed nonempty sample is required")
    values = [_number(value, "paired delta", -1, 1) for value in deltas]
    _number(alpha, "alpha", 1e-12, 0.25)
    # Range length is TWO, hence sqrt(2 log(1/alpha) / N).
    return max(-1.0, fmean(values) - math.sqrt(2 * math.log(1 / alpha) / len(values)))


class ResearchArchive:
    """Single-host SQLite archive, with serializable append transactions.

    Use on a persistent controller disk. Do not place this file on Cloud Run's
    ephemeral filesystem or expose this object to untrusted candidate processes.
    """
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS research_records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(kind, id));
                CREATE TABLE IF NOT EXISTS research_trials (
                    id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, search_id TEXT,
                    UNIQUE(epoch_id, candidate_id));
                CREATE TABLE IF NOT EXISTS research_holdouts (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, purpose TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_pairs (
                    trial_id TEXT NOT NULL, holdout_id TEXT NOT NULL,
                    record_id TEXT NOT NULL, PRIMARY KEY(trial_id, holdout_id));
                CREATE TABLE IF NOT EXISTS research_decisions (
                    trial_id TEXT PRIMARY KEY, record_id TEXT NOT NULL);
            """)
            for table in ("research_records", "research_trials", "research_holdouts",
                          "research_pairs", "research_decisions"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_immutable "
                               f"BEFORE {action} ON {table} BEGIN "
                               "SELECT RAISE(ABORT, 'research archive is append-only'); END")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise ResearchError("Duplicate, reused, or immutable research record") from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _put(self, db, kind, data):
        payload = canonical(data)
        identity = hashlib.sha256(payload).hexdigest()
        if db.execute("SELECT 1 FROM research_records WHERE kind=? AND id=?", (kind, identity)).fetchone():
            return identity
        if db.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] >= MAX_ARCHIVE_RECORDS:
            raise ResearchError("Archive record limit reached; provision a reviewed new archive")
        db.execute("INSERT INTO research_records VALUES(?,?,?)", (kind, identity, payload.decode()))
        return identity

    def _get(self, db, kind, identity):
        _digest(identity, "record ID")
        row = db.execute("SELECT data FROM research_records WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if row is None:
            raise ResearchError(f"Unknown {kind} record")
        if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != identity:
            raise ResearchError("Archive content hash mismatch")
        return json.loads(row[0])

    def get(self, kind, identity):
        """Fetch one immutable record, with its content hash verified."""
        with self._transaction() as db:
            return {"id": identity, **self._get(db, kind, identity)}

    def add_candidate(self, kind, artifact, *, parents=(), proposer="operator", niche="general",
                      metadata=None, expected_content_hash=None):
        if kind not in KINDS:
            raise ResearchError("Candidate kind must be task_agent or improvement_procedure")
        parents = _ids(parents, "parents", 8, minimum=0)
        digest = content_hash(artifact)
        if expected_content_hash is not None and expected_content_hash != digest:
            raise ResearchError("Candidate content hash mismatch")
        if metadata is not None and not isinstance(metadata, dict):
            raise ResearchError("metadata must be an object")
        data = {"kind": kind, "artifact": artifact, "content_hash": digest,
                "parents": sorted(parents), "proposer": _text(proposer, "proposer", 128),
                "niche": _text(niche, "niche", 128), "metadata": metadata or {}}
        with self._transaction() as db:
            for parent in parents:
                self._get(db, "candidate", parent)
            identity = self._put(db, "candidate", data)
        return {"id": identity, **json.loads(canonical(data))}

    def create_epoch(self, policy):
        """Freeze the evaluator, protected contracts, sampling and budget policy."""
        keys = {"name", "sample_size", "max_candidates", "alpha", "meaningful_delta",
                "budget_per_arm", "evaluator_digest", "protected_contract_digest", "guard_names"}
        if not isinstance(policy, dict) or set(policy) != keys:
            raise ResearchError("Epoch policy fields do not match the fixed schema")
        policy = json.loads(canonical(policy))
        _text(policy["name"], "epoch name", 128)
        _integer(policy["sample_size"], "sample_size", 1, MAX_SAMPLES)
        _integer(policy["max_candidates"], "max_candidates", 1, MAX_CANDIDATES)
        _number(policy["alpha"], "alpha", 1e-9, 0.25)
        _number(policy["meaningful_delta"], "meaningful_delta", 0, 1)
        _integer(policy["budget_per_arm"], "budget_per_arm", 1)
        _digest(policy["evaluator_digest"], "evaluator_digest")
        _digest(policy["protected_contract_digest"], "protected_contract_digest")
        policy["guard_names"] = sorted(_ids(policy["guard_names"], "guard_names", 16))
        policy["score_schema"] = {"candidate_and_baseline": [0, 1], "paired_delta": [-1, 1],
                                  "utility": "candidate_score - baseline_score"}
        with self._transaction() as db:
            # Protected acceptance obligations cannot be relaxed by starting another epoch.
            for row in db.execute("SELECT id FROM research_records WHERE kind='epoch'"):
                previous = self._get(db, "epoch", row[0])
                if (previous["protected_contract_digest"] != policy["protected_contract_digest"]
                        or previous["guard_names"] != policy["guard_names"]):
                    raise ResearchError("Protected contracts and no-regression guards cannot change")
                if previous["name"] == policy["name"] and previous != policy:
                    raise ResearchError("Evaluator policy drift within an existing epoch")
            identity = self._put(db, "epoch", policy)
        return {"id": identity, **policy}

    def _descends(self, db, candidate, ancestor):
        pending, seen = [candidate], set()
        while pending:
            node = pending.pop()
            if node == ancestor:
                return True
            if node not in seen:
                seen.add(node)
                if len(seen) > 4096:
                    raise ResearchError("Lineage traversal limit reached")
                pending.extend(self._get(db, "candidate", node)["parents"])
        return False

    def register_search(self, epoch_id, procedure_id, baseline_id, max_descendants):
        """Preregister a fixed-budget descendant search; failed trials remain charged."""
        with self._transaction() as db:
            policy = self._get(db, "epoch", epoch_id)
            procedure = self._get(db, "candidate", procedure_id)
            baseline = self._get(db, "candidate", baseline_id)
            if procedure["kind"] != "improvement_procedure" or baseline["kind"] != "task_agent":
                raise ResearchError("A search needs an improvement procedure and a task-agent baseline")
            _integer(max_descendants, "max_descendants", 1, policy["max_candidates"])
            data = {"epoch_id": epoch_id, "procedure_id": procedure_id, "baseline_id": baseline_id,
                    "max_descendants": max_descendants,
                    "total_budget": 2 * policy["budget_per_arm"] * max_descendants}
            identity = self._put(db, "search", data)
        return {"id": identity, **data}

    def preregister_trial(self, epoch_id, candidate_id, baseline_id, holdout_ids, *,
                          independent_evaluator, candidate_overhead=0, baseline_overhead=0,
                          search_id=None):
        with self._transaction() as db:
            policy = self._get(db, "epoch", epoch_id)
            candidate = self._get(db, "candidate", candidate_id)
            baseline = self._get(db, "candidate", baseline_id)
            if candidate_id == baseline_id or candidate["kind"] != baseline["kind"]:
                raise ResearchError("Compare distinct candidates of the same kind")
            evaluator = _text(independent_evaluator, "independent_evaluator", 128)
            if evaluator in (candidate["proposer"], baseline["proposer"]):
                raise ResearchError("The evaluator must be independent of the proposers")
            holdouts = _ids(holdout_ids, "holdout_ids", policy["sample_size"])
            if len(holdouts) != policy["sample_size"]:
                raise ResearchError("Preregister exactly the fixed sample size")
            if db.execute("SELECT COUNT(*) FROM research_trials WHERE epoch_id=?", (epoch_id,)).fetchone()[0] >= policy["max_candidates"]:
                raise ResearchError("Epoch candidate limit exhausted")
            for label, value in (("candidate_overhead", candidate_overhead), ("baseline_overhead", baseline_overhead)):
                _integer(value, label, 0, policy["budget_per_arm"])
            if search_id:
                search = self._get(db, "search", search_id)
                if search["epoch_id"] != epoch_id or search["baseline_id"] != baseline_id:
                    raise ResearchError("Search epoch and baseline must match")
                if not self._descends(db, candidate_id, search["procedure_id"]):
                    raise ResearchError("Search candidate is not a descendant of its procedure")
                if db.execute("SELECT COUNT(*) FROM research_trials WHERE search_id=?", (search_id,)).fetchone()[0] >= search["max_descendants"]:
                    raise ResearchError("Search descendant budget exhausted")
            plan = {"epoch_id": epoch_id, "candidate_id": candidate_id, "baseline_id": baseline_id,
                    "candidate_content_hash": candidate["content_hash"], "baseline_content_hash": baseline["content_hash"],
                    "holdout_ids": holdouts, "independent_evaluator": evaluator,
                    "candidate_overhead": candidate_overhead, "baseline_overhead": baseline_overhead,
                    "search_id": search_id}
            identity = self._put(db, "trial", plan)
            db.execute("INSERT INTO research_trials VALUES(?,?,?,?)", (identity, epoch_id, candidate_id, search_id))
            self._reserve_holdouts(db, holdouts, identity, "confirmatory")
        return {"id": identity, **plan}

    def _reserve_holdouts(self, db, holdouts, owner, purpose):
        for holdout in holdouts:
            if db.execute("SELECT 1 FROM research_holdouts WHERE id=?", (holdout,)).fetchone():
                raise ResearchError("Holdout identifier was already used or reserved")
            db.execute("INSERT INTO research_holdouts VALUES(?,?,?)", (holdout, owner, purpose))

    def _pairs(self, db, trial_id):
        return [self._get(db, "pair", row[0]) for row in db.execute(
            "SELECT record_id FROM research_pairs WHERE trial_id=? ORDER BY holdout_id", (trial_id,))]

    def record_pair(self, trial_id, holdout_id, *, candidate_score, baseline_score,
                    candidate_cost, baseline_cost, candidate_latency_ms, candidate_robustness,
                    contracts_pass, guard_deltas, evaluator_digest, protected_contract_digest,
                    candidate_content_hash, baseline_content_hash):
        """Append one measured pair. Costs are actual integer units including failures."""
        with self._transaction() as db:
            plan = self._get(db, "trial", trial_id)
            policy = self._get(db, "epoch", plan["epoch_id"])
            if holdout_id not in plan["holdout_ids"]:
                raise ResearchError("Holdout is not in the preregistered sample")
            for key, value in (("evaluator_digest", evaluator_digest),
                               ("protected_contract_digest", protected_contract_digest)):
                if value != policy[key]:
                    raise ResearchError("Evaluator or protected contract drift")
            if (candidate_content_hash != plan["candidate_content_hash"]
                    or baseline_content_hash != plan["baseline_content_hash"]):
                raise ResearchError("Evaluated candidate content hash mismatch")
            if type(contracts_pass) is not bool:
                raise ResearchError("contracts_pass must be a measured boolean")
            if not isinstance(guard_deltas, dict) or set(guard_deltas) != set(policy["guard_names"]):
                raise ResearchError("Every fixed no-regression guard must be measured")
            data = {"trial_id": trial_id, "holdout_id": holdout_id,
                    "candidate_score": _number(candidate_score, "candidate_score"),
                    "baseline_score": _number(baseline_score, "baseline_score"),
                    "candidate_cost": _integer(candidate_cost, "candidate_cost"),
                    "baseline_cost": _integer(baseline_cost, "baseline_cost"),
                    "candidate_latency_ms": _number(candidate_latency_ms, "candidate_latency_ms", 0, 10**9),
                    "candidate_robustness": _number(candidate_robustness, "candidate_robustness"),
                    "contracts_pass": contracts_pass,
                    "guard_deltas": {key: _number(value, "guard delta", -1, 1) for key, value in guard_deltas.items()}}
            previous = self._pairs(db, trial_id)
            for side in ("candidate", "baseline"):
                total = plan[side + "_overhead"] + sum(row[side + "_cost"] for row in previous) + data[side + "_cost"]
                if total > policy["budget_per_arm"]:
                    raise ResearchError("Actual total budget exceeded; measurement rejected")
            identity = self._put(db, "pair", data)
            db.execute("INSERT INTO research_pairs VALUES(?,?,?)", (trial_id, holdout_id, identity))
        return {"id": identity, "trial_id": trial_id, "recorded_pairs": len(previous) + 1,
                "required_pairs": policy["sample_size"]}

    def finalize_trial(self, trial_id):
        """Record eligibility only after all fixed samples; never deploys or changes code."""
        with self._transaction() as db:
            old = db.execute("SELECT record_id FROM research_decisions WHERE trial_id=?", (trial_id,)).fetchone()
            if old:
                return {"id": old[0], **self._get(db, "decision", old[0])}
            plan = self._get(db, "trial", trial_id)
            policy = self._get(db, "epoch", plan["epoch_id"])
            rows = self._pairs(db, trial_id)
            if len(rows) != policy["sample_size"]:
                raise ResearchError("Fixed sample incomplete; no interim promotion or bound is available")
            costs = {side: plan[side + "_overhead"] + sum(row[side + "_cost"] for row in rows)
                     for side in ("candidate", "baseline")}
            deltas = [row["candidate_score"] - row["baseline_score"] for row in rows]
            alpha = policy["alpha"] / policy["max_candidates"]
            lower = hoeffding_lower_bound(deltas, alpha)
            contracts = all(row["contracts_pass"] for row in rows)
            equal_budget = all(cost == policy["budget_per_arm"] for cost in costs.values())
            guard_minima = {key: min(row["guard_deltas"][key] for row in rows) for key in policy["guard_names"]}
            guards = all(value >= 0 for value in guard_minima.values())
            reasons = []
            if not contracts:
                reasons.append("protected_contract_failed")
            if not equal_budget:
                reasons.append("equal_total_budget_not_met")
            if not guards:
                reasons.append("observed_regression")
            if not lower > policy["meaningful_delta"]:
                reasons.append("lower_bound_not_above_meaningful_delta")
            data = {"trial_id": trial_id, "epoch_id": plan["epoch_id"], "candidate_id": plan["candidate_id"],
                    "baseline_id": plan["baseline_id"], "sample_size": len(rows), "alpha_per_candidate": alpha,
                    "mean_delta": fmean(deltas), "lower_bound": lower,
                    "meaningful_delta": policy["meaningful_delta"], "guard_minima": guard_minima,
                    "contracts_pass": contracts, "equal_total_budget": equal_budget,
                    "valid": contracts and equal_budget, "eligible": not reasons, "reasons": reasons,
                    "actual_cost": costs,
                    "metrics": {"correctness": fmean(row["candidate_score"] for row in rows),
                                "cost": fmean(row["candidate_cost"] for row in rows),
                                "latency_ms": fmean(row["candidate_latency_ms"] for row in rows),
                                "robustness": fmean(row["candidate_robustness"] for row in rows)}}
            identity = self._put(db, "decision", data)
            db.execute("INSERT INTO research_decisions VALUES(?,?)", (trial_id, identity))
        return {"id": identity, **data}

    def _summaries(self, db, epoch_id):
        self._get(db, "epoch", epoch_id)
        summaries = []
        for row in db.execute("SELECT d.record_id FROM research_decisions d JOIN research_trials t "
                              "ON d.trial_id=t.id WHERE t.epoch_id=?", (epoch_id,)):
            decision = self._get(db, "decision", row[0])
            if decision["valid"]:
                candidate = self._get(db, "candidate", decision["candidate_id"])
                summaries.append({"candidate_id": decision["candidate_id"], "trial_id": decision["trial_id"],
                                  "kind": candidate["kind"], "niche": candidate["niche"],
                                  "metrics": decision["metrics"], "eligible": decision["eligible"]})
        return summaries

    def pareto_frontier(self, epoch_id, *, kind="task_agent"):
        if kind not in KINDS:
            raise ResearchError("Unknown candidate kind")
        with self._transaction() as db:
            rows = [row for row in self._summaries(db, epoch_id) if row["kind"] == kind]
        def vector(row):
            m = row["metrics"]
            return (m["correctness"], -m["cost"], -m["latency_ms"], m["robustness"])
        return [row for row in rows if not any(
            all(a >= b for a, b in zip(vector(other), vector(row)))
            and any(a > b for a, b in zip(vector(other), vector(row))) for other in rows)]

    def quality_diversity(self, epoch_id):
        with self._transaction() as db:
            rows = self._summaries(db, epoch_id)
        niches = {}
        def quality(row):
            m = row["metrics"]
            return (m["correctness"], m["robustness"], -m["cost"], -m["latency_ms"], row["candidate_id"])
        for row in rows:
            key = (row["kind"], row["niche"])
            if key not in niches or quality(row) > quality(niches[key]):
                niches[key] = row
        return [niches[key] for key in sorted(niches)]

    def metaproductivity(self, search_id):
        with self._transaction() as db:
            search = self._get(db, "search", search_id)
            trials = [row[0] for row in db.execute("SELECT id FROM research_trials WHERE search_id=?", (search_id,))]
            decisions = []
            for trial in trials:
                row = db.execute("SELECT record_id FROM research_decisions WHERE trial_id=?", (trial,)).fetchone()
                if row:
                    decisions.append(self._get(db, "decision", row[0]))
            result = {"search_id": search_id, "complete": len(decisions) == search["max_descendants"],
                      "completed_descendants": len(decisions), "required_descendants": search["max_descendants"],
                      "fixed_budget": search["total_budget"]}
            if not result["complete"]:
                return result
            actual = sum(sum(item["actual_cost"].values()) for item in decisions)
            certified = [item for item in decisions if item["eligible"]]
            result.update(actual_cost=actual, valid=actual == search["total_budget"],
                          best_descendant_utility=max((item["metrics"]["correctness"] for item in decisions if item["valid"]), default=None),
                          best_certified_gain=max((item["lower_bound"] for item in certified), default=0.0))
            result["certified_gain_per_budget_unit"] = result["best_certified_gain"] / search["total_budget"] if result["valid"] else None
            result["interpretation"] = "observed fixed-budget search outcome, not an estimate of expected future potential"
            return result

    def record_intervention(self, trial_id, *, change, hypothesis, conditions, helped, failed):
        """Save a structured semantic intervention and the measured trial it refers to."""
        with self._transaction() as db:
            plan = self._get(db, "trial", trial_id)
            row = db.execute("SELECT record_id FROM research_decisions WHERE trial_id=?", (trial_id,)).fetchone()
            if not row:
                raise ResearchError("An intervention outcome needs a completed trial")
            data = {"trial_id": trial_id, "decision_id": row[0], "parent_id": plan["baseline_id"],
                    "candidate_id": plan["candidate_id"],
                    "change": _text(change, "change", 4000), "hypothesis": _text(hypothesis, "hypothesis", 4000),
                    "conditions": _text(conditions, "conditions", 4000),
                    "helped": _text(helped, "helped", 4000), "failed": _text(failed, "failed", 4000)}
            identity = self._put(db, "intervention", data)
        return {"id": identity, **data}

    def replay(self, action_ids, *, strict=True):
        """Replay recorded interventions only; no interpolation or new action execution."""
        actions = _ids(action_ids, "action_ids", 256)
        with self._transaction() as db:
            found, missing = [], []
            for identity in actions:
                _digest(identity, "action ID")
                if not db.execute("SELECT 1 FROM research_records WHERE kind='intervention' AND id=?", (identity,)).fetchone():
                    missing.append(identity)
                    continue
                intervention = self._get(db, "intervention", identity)
                decision = self._get(db, "decision", intervention["decision_id"])
                found.append({"action_id": identity, "parent_id": intervention["parent_id"],
                              "candidate_id": intervention["candidate_id"], "observed_delta": decision["mean_delta"],
                              "eligible": decision["eligible"]})
            if missing and strict:
                raise ResearchError("Replay requested an unrecorded action; coverage is incomplete")
            return {"observations": found, "unknown_actions": missing,
                    "coverage": len(found) / len(actions), "complete": not missing,
                    "limitations": "Recorded outcomes only; no prediction for unobserved actions or changed conditions"}

    def record_interaction(self, epoch_id, candidate_ids, block_ids, scores, costs, *, evaluator_digest):
        """Archive an exploratory matched 2x2 contrast; never evidence for promotion.

        The caller supplies all four cells on the same blocks and equal measured
        total budgets. These block IDs become permanently ineligible for audits.
        """
        cells = {"baseline", "a", "b", "ab"}
        if not isinstance(candidate_ids, dict) or set(candidate_ids) != cells:
            raise ResearchError("2x2 interaction needs baseline, a, b and ab candidates")
        if not isinstance(scores, dict) or set(scores) != cells or not isinstance(costs, dict) or set(costs) != cells:
            raise ResearchError("Every interaction cell needs scores and total cost")
        blocks = _ids(block_ids, "block_ids", MAX_SAMPLES)
        vectors = {}
        for cell in cells:
            if not isinstance(scores[cell], (list, tuple)) or len(scores[cell]) != len(blocks):
                raise ResearchError("Interaction cells must have matched sample counts")
            vectors[cell] = [_number(value, "interaction score") for value in scores[cell]]
            _integer(costs[cell], "interaction total cost", 1)
        if len(set(costs.values())) != 1:
            raise ResearchError("Interaction cells require equal total budgets")
        if len(set(candidate_ids.values())) != 4:
            raise ResearchError("Interaction cells must use four distinct frozen candidates")
        with self._transaction() as db:
            policy = self._get(db, "epoch", epoch_id)
            if evaluator_digest != policy["evaluator_digest"]:
                raise ResearchError("Interaction evaluator drift")
            if any(cost > policy["budget_per_arm"] for cost in costs.values()):
                raise ResearchError("Interaction budget exceeded")
            variants = [self._get(db, "candidate", candidate_ids[cell]) for cell in sorted(cells)]
            if len({candidate["kind"] for candidate in variants}) != 1:
                raise ResearchError("Interaction candidate kinds must match")
            for cell in ("a", "b", "ab"):
                if not self._descends(db, candidate_ids[cell], candidate_ids["baseline"]):
                    raise ResearchError("Interaction variants must descend from the matched baseline")
            per_block = [vectors["ab"][i] - vectors["a"][i] - vectors["b"][i] + vectors["baseline"][i]
                         for i in range(len(blocks))]
            data = {"epoch_id": epoch_id, "candidate_ids": candidate_ids, "block_ids": blocks,
                    "scores": vectors, "costs": costs, "per_block_interaction": per_block,
                    "mean_interaction": fmean(per_block), "confirmatory": False,
                    "interpretation": "Matched descriptive contrast; causal attribution requires a controlled design"}
            identity = self._put(db, "interaction", data)
            self._reserve_holdouts(db, blocks, identity, "exploratory")
        return {"id": identity, **data}

    def record_abstraction(self, candidate_id, trial_id, *, interface, test_artifact):
        """Index a tested reusable artifact with source-bound external test evidence."""
        with self._transaction() as db:
            candidate = self._get(db, "candidate", candidate_id)
            plan = self._get(db, "trial", trial_id)
            policy = self._get(db, "epoch", plan["epoch_id"])
            row = db.execute("SELECT record_id FROM research_decisions WHERE trial_id=?", (trial_id,)).fetchone()
            if plan["candidate_id"] != candidate_id or not row or not self._get(db, "decision", row[0])["valid"]:
                raise ResearchError("An abstraction requires a valid completed trial for this candidate")
            if (not isinstance(test_artifact, dict) or test_artifact.get("passed") is not True
                    or test_artifact.get("source_hash") != candidate["content_hash"]
                    or test_artifact.get("contract_digest") != policy["protected_contract_digest"]):
                raise ResearchError("Test artifact must pass and match source and protected contracts")
            checks = test_artifact.get("checks")
            if (not isinstance(checks, list) or not 1 <= len(checks) <= 128
                    or any(not isinstance(item, dict) or item.get("passed") is not True
                           or not isinstance(item.get("name"), str) or not item["name"] for item in checks)):
                raise ResearchError("Test artifact requires named passing checks")
            data = {"candidate_id": candidate_id, "trial_id": trial_id,
                    "interface": _text(interface, "interface", 8000), "test_artifact": test_artifact,
                    "test_artifact_hash": content_hash(test_artifact),
                    "verification": "External test evidence recorded; not executed or proven by this archive"}
            identity = self._put(db, "abstraction", data)
        return {"id": identity, **data}
