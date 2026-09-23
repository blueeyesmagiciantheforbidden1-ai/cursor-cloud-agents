"""Durable release gate; callable only by the trusted controller process.

This module does not provide an OS sandbox or authenticate a human. A caller
with database write access has controller authority. Never mount this database
or invoke this API in a candidate worker. Evidence thresholds are supplied by
the trusted evaluator, not the proposal. Hashes bind identity, not correctness.
"""
from __future__ import annotations
import json
import math
import re
import sqlite3
import time
import uuid


class GateError(ValueError):
    pass


class PromotionRegistry:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS proposals (
          id TEXT PRIMARY KEY, candidate TEXT NOT NULL, evaluator TEXT NOT NULL,
          evidence TEXT NOT NULL, lower_bound REAL NOT NULL, margin REAL NOT NULL,
          status TEXT NOT NULL, approver TEXT, created REAL NOT NULL,
          checks_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS releases (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, candidate TEXT NOT NULL,
          proposal TEXT, actor TEXT NOT NULL, action TEXT NOT NULL, created REAL NOT NULL);
        """)
        # Existing research bundles remain readable when explicit compound
        # evidence gates are added to their registry.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(proposals)")}
        if "checks_json" not in columns:
            self.db.execute("ALTER TABLE proposals ADD COLUMN checks_json TEXT NOT NULL DEFAULT '{}'")

    def stage(self, candidate: str, evaluator: str, evidence: str,
              lower_bound: float, margin: float = 0.0, *, checks: dict[str, bool] | None = None) -> str:
        """Record exact evidence and all trusted evaluator eligibility checks.

        The scalar lower bound is retained without alteration even when another
        required check fails. Every supplied check must pass before review can
        authorize this exact candidate/evaluator/evidence tuple.
        """
        for digest in (candidate, evaluator, evidence):
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise GateError("Expected SHA-256 identities")
        if not all(not isinstance(x, bool) and isinstance(x, (int, float))
                   and -1 <= x <= 1 and math.isfinite(x) for x in (lower_bound, margin)):
            raise GateError("Thresholds must be finite quality differences in [-1,1]")
        if checks is None:
            checks = {}
        if (not isinstance(checks, dict) or any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, bool)
            for key, value in checks.items()
        )):
            raise GateError("Evidence checks must map nonempty names to booleans")
        pid = uuid.uuid4().hex
        self.db.execute("INSERT INTO proposals "
                        "(id,candidate,evaluator,evidence,lower_bound,margin,status,approver,created,checks_json) "
                        "VALUES (?,?,?,?,?,?,?,NULL,?,?)",
                        (pid, candidate, evaluator, evidence, lower_bound, margin,
                         "review_required" if lower_bound > margin and all(checks.values())
                         else "inconclusive", time.time(), json.dumps(checks, sort_keys=True)))
        return pid

    def proposal(self, pid):
        row = self.db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise GateError("Unknown proposal")
        result = dict(row)
        result["checks"] = json.loads(result.pop("checks_json"))
        return result

    def approve(self, pid, expected_candidate, approver):
        if not isinstance(approver, str) or not approver.strip():
            raise GateError("Approval identity is required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.proposal(pid)
            if row["candidate"] != expected_candidate or row["status"] != "review_required":
                raise GateError("Approval must bind an eligible, exact candidate")
            self.db.execute("UPDATE proposals SET status='approved',approver=? WHERE id=?", (approver, pid))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def activate(self, pid, *, candidate, evaluator, evidence):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.proposal(pid)
            if row["status"] != "approved":
                raise GateError("Exact-candidate approval and sufficient evidence required")
            if (row["candidate"], row["evaluator"], row["evidence"]) != (candidate, evaluator, evidence):
                raise GateError("Candidate, evaluator, or evidence changed since review")
            self.db.execute("INSERT INTO releases(candidate,proposal,actor,action,created) VALUES (?,?,?,?,?)",
                            (candidate, pid, row["approver"], "activate", time.time()))
            self.db.execute("UPDATE proposals SET status='active' WHERE id=?", (pid,))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.current()

    def rollback(self, sequence: int, actor: str):
        if not isinstance(actor, str) or not actor.strip():
            raise GateError("Rollback identity is required")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence < (1 << 63):
            raise GateError("Rollback target must be an existing positive release sequence")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT candidate FROM releases WHERE sequence=?", (sequence,)).fetchone()
            if row is None:
                raise GateError("Rollback target must be an existing release")
            self.db.execute("INSERT INTO releases(candidate,actor,action,created) VALUES (?,?,?,?)",
                            (row["candidate"], actor, "rollback", time.time()))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.current()

    def current(self):
        row = self.db.execute("SELECT * FROM releases ORDER BY sequence DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def close(self):
        self.db.close()
