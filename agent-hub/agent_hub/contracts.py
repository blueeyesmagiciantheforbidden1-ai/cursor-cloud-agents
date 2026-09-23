"""Local transactional task contracts and evidence, separate from legacy rooms.

The caller is the trusted controller/verifier. This module executes no commands,
authenticates no users, and does not prove that supplied execution receipts are
truthful. It enforces their scope, integrity, ordering, and revision bindings.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit


MAX_CONTRACT_BYTES = 32_000
MAX_BUNDLE_BYTES = 512_000
MAX_TASKS = 10_000
PROTECTED_PARTS = frozenset((".git", ".github", ".codex", ".agents", "policy", "policies",
                             "approval", "approvals", "tests", "acceptance", "protected-tests"))
PROTECTED_FILES = frozenset(("agents.md", "agent_hub/contracts.py", "agent_hub/core.py"))
EFFORTS = frozenset(("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"))
READY_DEPENDENCIES = frozenset(("verified", "approved", "promoted"))


class ContractError(ValueError):
    pass


class Conflict(ContractError):
    pass


def _require(condition, message, error=ContractError):
    if not condition:
        raise error(message)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ContractError("Value must be bounded, finite UTF-8 JSON") from exc


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _keys(value, required, optional=()):
    _require(isinstance(value, dict) and set(required) <= value.keys()
             and not value.keys() - set(required) - set(optional), "Missing or unknown contract/receipt fields")


def _text(value, name, limit=160):
    _require(isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= limit
             and not any(ord(char) < 32 for char in value), f"Invalid {name}")
    return value


def _identifier(value, name="identifier"):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is not None,
             f"Invalid {name}")
    return value


def _commit(value):
    _require(isinstance(value, str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is not None,
             "Revision must be an exact lowercase 40- or 64-character commit hash")
    return value


def _sha256(value):
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
             "Artifact/definition hash must be SHA-256")
    return value


def _integer(value, minimum, maximum, name):
    _require(type(value) is int and minimum <= value <= maximum, f"Invalid {name}")
    return value


def _model(value):
    _keys(value, ("provider", "model", "effort"))
    _identifier(value["provider"], "provider")
    _text(value["model"], "model", 160)
    _require(value["model"].lower() not in ("auto", "default", "unknown"), "Select an explicit model")
    _require(isinstance(value["effort"], str) and value["effort"] in EFFORTS, "Select an explicit supported effort setting")
    return dict(value)


def _actual_model(value):
    if value is not None:
        _text(value, "actual model ID", 160)
        _require(value.casefold() not in ("unknown", "unavailable", "auto", "default", "undetermined"),
                 "Use null when the actual model identity is unknown")


def _path(value, *, directory=False):
    _require(isinstance(value, str) and 0 < len(value) <= 500 and "\\" not in value
             and not any(char in value for char in ':*?<>|%\x00') and not value.startswith("/"),
             "Use a relative POSIX file path without wildcards or encoded/Windows separators")
    if directory and value.endswith("/"):
        value = value[:-1]
    parts = value.split("/")
    for part in parts:
        _require(part not in ("", ".", "..") and part == part.rstrip(" .")
                 and not any(ord(char) < 32 for char in part), "Unsafe path component")
        stem = part.split(".")[0].casefold()
        _require(stem not in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                             *(f"lpt{i}" for i in range(1, 10))}, "Windows device paths are forbidden")
    _require(not any(part.casefold() in PROTECTED_PARTS for part in parts)
             and value.casefold() not in PROTECTED_FILES, "Approval, policy, and acceptance-test paths are protected")
    return value


def validate_changed_files(contract, files):
    _require(isinstance(files, list) and 1 <= len(files) <= 256 and all(isinstance(path, str) for path in files)
             and len(set(files)) == len(files),
             "Supply a bounded, unique changed-file list")
    for path in files:
        _path(path)
        _require(any(path == allowed or (allowed.endswith("/") and path.startswith(allowed))
                     for allowed in contract["allowed_files"]), "Changed file is outside the contract allowlist")
    return list(files)


def resolve_allowed_path(workspace, relative, contract):
    """Check lexical scope and actual containment, including existing symlinks."""
    validate_changed_files(contract, [relative])
    root = Path(workspace).resolve(strict=True)
    _require(root.is_dir(), "Workspace must be a directory")
    result = (root / relative).resolve(strict=False)
    _require(result.is_relative_to(root), "Resolved path escapes the workspace")
    return result


def validate_contract(value):
    encoded = _json(value)
    _require(len(encoded) <= MAX_CONTRACT_BYTES, "Contract exceeds its byte limit")
    value = json.loads(encoded)
    _keys(value, ("task_id", "repository", "allowed_files", "dependencies", "capability", "model",
                  "reviewer_model", "permissions", "budget", "deadline", "acceptance_tests", "approval"))
    _identifier(value["task_id"], "task ID")
    _keys(value["repository"], ("id", "base_commit"))
    _text(value["repository"]["id"], "repository ID", 256)
    _commit(value["repository"]["base_commit"])
    paths = value["allowed_files"]
    _require(isinstance(paths, list) and 1 <= len(paths) <= 128, "Supply a bounded allowed-files list")
    for path in paths:
        _path(path, directory=True)
    _require(len(set(paths)) == len(paths), "Allowed-file entries must be unique")
    dependencies = value["dependencies"]
    _require(isinstance(dependencies, list) and len(dependencies) <= 64, "Invalid dependencies")
    for dependency in dependencies:
        _identifier(dependency, "dependency")
    _require(len(set(dependencies)) == len(dependencies) and value["task_id"] not in dependencies,
             "Dependencies must be unique and cannot include the task itself")
    _identifier(value["capability"], "capability")
    _model(value["model"])
    _model(value["reviewer_model"])
    permissions = value["permissions"]
    _keys(permissions, ("tools", "network_origins", "credential_aliases"))
    for key in ("tools", "credential_aliases"):
        _require(isinstance(permissions[key], list) and len(permissions[key]) <= 32, "Invalid permission list")
        for name in permissions[key]:
            _identifier(name, "permission alias")
        _require(len(set(permissions[key])) == len(permissions[key]), "Permission aliases must be unique")
    _require(isinstance(permissions["network_origins"], list) and len(permissions["network_origins"]) <= 32,
             "Invalid network permission list")
    for origin in permissions["network_origins"]:
        _text(origin, "network origin", 256)
        parsed = urlsplit(origin)
        _require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
                 and not parsed.password and parsed.path in ("", "/") and not parsed.query and not parsed.fragment,
                 "Network permissions must be HTTPS origins without credentials or paths")
        try:
            parsed.port
        except ValueError as exc:
            raise ContractError("Invalid network origin port") from exc
    budget = value["budget"]
    _keys(budget, ("max_attempts", "max_cost_microusd", "attempt_reserve_microusd"))
    _integer(budget["max_attempts"], 1, 100, "attempt budget")
    _integer(budget["max_cost_microusd"], 1, 10**12, "cost budget")
    _integer(budget["attempt_reserve_microusd"], 1, budget["max_cost_microusd"], "attempt reservation")
    _integer(value["deadline"], 1, 2**53, "absolute UTC deadline")
    tests = value["acceptance_tests"]
    _require(isinstance(tests, list) and 1 <= len(tests) <= 16, "Supply 1 to 16 immutable acceptance-test definitions")
    for test in tests:
        _keys(test, ("id", "definition_sha256"))
        _identifier(test["id"], "acceptance test")
        _sha256(test["definition_sha256"])
    _require(len({test["id"] for test in tests}) == len(tests), "Acceptance test IDs must be unique")
    _require(value["approval"] == {"human_required": True, "independent_review": True},
             "Human approval and independent review cannot be disabled")
    return value


def _acyclic(contracts):
    graph = {key: set(value["dependencies"]) for key, value in contracts.items()}
    _require(all(dependency in graph for dependencies in graph.values() for dependency in dependencies),
             "A dependency is missing from the ledger or submitted batch")
    children = {key: [] for key in graph}
    for key, dependencies in graph.items():
        for dependency in dependencies:
            children[dependency].append(key)
    ready = [key for key, dependencies in graph.items() if not dependencies]
    visited = 0
    while ready:
        key = ready.pop()
        visited += 1
        for child in children[key]:
            graph[child].remove(key)
            if not graph[child]:
                ready.append(child)
    _require(visited == len(graph), "Task dependency graph contains a cycle")


class TaskLedger:
    """Single-controller SQLite ledger; never put its file on a shared VPS mount."""
    def __init__(self, path, clock=time.time):
        _require(str(path) != ":memory:", "The task ledger requires a persistent database file")
        self.path = str(Path(path).resolve())
        self.clock = clock
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            _require(version in (0, 1), "Unsupported task ledger schema version")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS repositories (id TEXT PRIMARY KEY, current_base TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, contract_json TEXT NOT NULL,
                    contract_digest TEXT NOT NULL, state TEXT NOT NULL, fence INTEGER NOT NULL DEFAULT 0,
                    spent INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0,
                    evidence_digest TEXT, candidate TEXT);
                CREATE TABLE IF NOT EXISTS attempts (
                    task_id TEXT NOT NULL, fence INTEGER NOT NULL, worker TEXT NOT NULL, lease_hash TEXT NOT NULL,
                    lease_until REAL NOT NULL, state TEXT NOT NULL, reservation INTEGER NOT NULL,
                    dependencies_json TEXT NOT NULL, result_digest TEXT, result_json TEXT,
                    PRIMARY KEY(task_id, fence), FOREIGN KEY(task_id) REFERENCES tasks(id));
                CREATE TABLE IF NOT EXISTS artifacts (digest TEXT PRIMARY KEY, content BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence (
                    digest TEXT PRIMARY KEY, task_id TEXT NOT NULL, fence INTEGER NOT NULL,
                    bundle_json TEXT NOT NULL, verified INTEGER NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id));
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, evidence_digest TEXT NOT NULL,
                    revision TEXT NOT NULL, base_commit TEXT NOT NULL, approved_by TEXT NOT NULL,
                    created REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS promotions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    approval_id TEXT NOT NULL UNIQUE, repository_id TEXT NOT NULL,
                    base_commit TEXT NOT NULL, revision TEXT NOT NULL, evidence_digest TEXT NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connection(self, *, transaction=False):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            if transaction:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if transaction:
                connection.commit()
        except BaseException:
            if transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _task(self, connection, task_id):
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        _require(row is not None, "Task not found")
        return row, json.loads(row["contract_json"])

    def _base(self, connection, contract):
        row = connection.execute("SELECT current_base FROM repositories WHERE id=?", (contract["repository"]["id"],)).fetchone()
        _require(row is not None and row[0] == contract["repository"]["base_commit"],
                 "Repository base changed; create a new contract for the new base", Conflict)

    def _dependencies(self, connection, contract):
        result = {}
        for identifier in contract["dependencies"]:
            task, _ = self._task(connection, identifier)
            _require(task["state"] in READY_DEPENDENCIES and task["evidence_digest"], "Task dependencies are not verified", Conflict)
            result[identifier] = {"candidate": task["candidate"], "evidence_digest": task["evidence_digest"]}
        return result

    def register(self, contracts):
        _require(isinstance(contracts, list) and 1 <= len(contracts) <= 64, "Register 1 to 64 contracts together")
        normalized = [validate_contract(contract) for contract in contracts]
        _require(len({item["task_id"] for item in normalized}) == len(normalized), "Duplicate task IDs in batch")
        with self._connection(transaction=True) as connection:
            existing = {row["id"]: json.loads(row["contract_json"]) for row in connection.execute("SELECT id, contract_json FROM tasks")}
            merged = dict(existing)
            for contract in normalized:
                identifier = contract["task_id"]
                _require(identifier not in existing or existing[identifier] == contract, "Registered contracts are immutable", Conflict)
                merged[identifier] = contract
            _require(len(merged) <= MAX_TASKS, "Local ledger task limit reached; archive with an operator-controlled workflow")
            _acyclic(merged)
            for contract in normalized:
                repository = contract["repository"]
                connection.execute("INSERT OR IGNORE INTO repositories VALUES (?, ?)", (repository["id"], repository["base_commit"]))
                if contract["task_id"] not in existing:
                    self._base(connection, contract)
                    connection.execute("INSERT INTO tasks (id, repository_id, contract_json, contract_digest, state) VALUES (?, ?, ?, ?, 'pending')",
                                       (contract["task_id"], repository["id"], _json(contract).decode(), _digest(contract)))
        return [item["task_id"] for item in normalized]

    def get(self, task_id):
        with self._connection() as connection:
            row, contract = self._task(connection, task_id)
            return {"task_id": row["id"], "state": row["state"], "fence": row["fence"], "contract": contract,
                    "contract_digest": row["contract_digest"], "spent_microusd": row["spent"],
                    "reserved_microusd": row["reserved"], "evidence_digest": row["evidence_digest"], "candidate_revision": row["candidate"]}

    def expire(self):
        """Persist uncertain outcomes; do not release their spending reservations."""
        now = self.clock()
        with self._connection(transaction=True) as connection:
            rows = connection.execute("SELECT task_id, fence FROM attempts WHERE state='running' AND lease_until<=?", (now,)).fetchall()
            for row in rows:
                connection.execute("UPDATE attempts SET state='uncertain' WHERE task_id=? AND fence=?", tuple(row))
                connection.execute("UPDATE tasks SET state='reconcile_required' WHERE id=? AND fence=?", tuple(row))
            return len(rows)

    def acquire(self, task_id, worker_id, capabilities, model, *, lease_seconds=45):
        _identifier(worker_id, "worker ID")
        _model(model)
        _require(isinstance(capabilities, list) and len(capabilities) <= 64, "Invalid worker capabilities")
        for capability in capabilities:
            _identifier(capability, "worker capability")
        _integer(lease_seconds, 1, 300, "lease duration")
        self.expire()
        with self._connection(transaction=True) as connection:
            row, contract = self._task(connection, task_id)
            _require(row["state"] in ("pending", "failed", "built", "verification_failed", "verified", "approved"),
                     "Task cannot start another attempt in its current state", Conflict)
            self._base(connection, contract)
            _require(self.clock() < contract["deadline"], "Task deadline has passed", Conflict)
            _require(contract["capability"] in capabilities and model == contract["model"],
                     "Worker capability/model/effort does not match the contract; no downgrade is permitted")
            dependencies = self._dependencies(connection, contract)
            budget = contract["budget"]
            reservation = budget["attempt_reserve_microusd"]
            _require(row["fence"] < budget["max_attempts"] and row["spent"] + row["reserved"] + reservation <= budget["max_cost_microusd"],
                     "Attempt or spending budget exhausted", Conflict)
            fence, token = row["fence"] + 1, secrets.token_urlsafe(32)
            expires_at = min(self.clock() + lease_seconds, contract["deadline"])
            connection.execute("INSERT INTO attempts (task_id,fence,worker,lease_hash,lease_until,state,reservation,dependencies_json) VALUES (?,?,?,?,?,'running',?,?)",
                               (task_id, fence, worker_id, hashlib.sha256(token.encode()).hexdigest(), expires_at, reservation, _json(dependencies).decode()))
            connection.execute("UPDATE tasks SET state='running', fence=?, reserved=reserved+?, evidence_digest=NULL, candidate=NULL WHERE id=?",
                               (fence, reservation, task_id))
            connection.execute("UPDATE approvals SET state='invalidated' WHERE task_id=? AND state='active'", (task_id,))
            return {"task_id": task_id, "fence": fence, "lease_token": token, "worker_id": worker_id,
                    "expires_at": expires_at, "reserved_microusd": reservation, "dependencies": dependencies,
                    "contract_digest": row["contract_digest"], "permissions": contract["permissions"]}

    def _lease(self, connection, lease):
        _require(isinstance(lease, dict), "Invalid lease receipt")
        row = connection.execute("SELECT * FROM attempts WHERE task_id=? AND fence=?", (lease.get("task_id"), lease.get("fence"))).fetchone()
        token = lease.get("lease_token")
        _require(row is not None and isinstance(token, str) and row["worker"] == lease.get("worker_id")
                 and secrets.compare_digest(row["lease_hash"], hashlib.sha256(token.encode()).hexdigest()), "Invalid lease ownership", Conflict)
        return row

    def heartbeat(self, lease, *, lease_seconds=45):
        _integer(lease_seconds, 1, 300, "lease duration")
        self.expire()
        with self._connection(transaction=True) as connection:
            attempt = self._lease(connection, lease)
            row, contract = self._task(connection, attempt["task_id"])
            _require(attempt["state"] == "running" and row["fence"] == attempt["fence"], "Lease is stale or uncertain", Conflict)
            self._base(connection, contract)
            expires_at = min(self.clock() + lease_seconds, contract["deadline"])
            _require(expires_at > self.clock(), "Task deadline has passed", Conflict)
            connection.execute("UPDATE attempts SET lease_until=? WHERE task_id=? AND fence=?", (expires_at, attempt["task_id"], attempt["fence"]))
            return {"expires_at": expires_at}

    def complete(self, lease, *, succeeded, actual_cost_microusd, actual_model_id, summary,
                 candidate_revision=None, patch_sha256=None):
        _require(type(succeeded) is bool, "succeeded must be a boolean")
        _integer(actual_cost_microusd, 0, 10**12, "actual attempt cost")
        _text(summary, "result summary", 8_000)
        _actual_model(actual_model_id)
        if succeeded:
            _commit(candidate_revision)
            _sha256(patch_sha256)
        else:
            _require(candidate_revision is None and patch_sha256 is None, "Failed attempts cannot advertise a candidate")
        result = {"succeeded": succeeded, "actual_cost_microusd": actual_cost_microusd,
                  "actual_model_id": actual_model_id, "summary": summary,
                  "candidate_revision": candidate_revision, "patch_sha256": patch_sha256}
        result_digest = _digest(result)
        self.expire()
        with self._connection(transaction=True) as connection:
            attempt = self._lease(connection, lease)
            if attempt["result_digest"] is not None:
                _require(attempt["result_digest"] == result_digest, "Completion differs from the recorded outcome", Conflict)
                return json.loads(attempt["result_json"])
            row, contract = self._task(connection, attempt["task_id"])
            _require(attempt["state"] == "running" and row["fence"] == attempt["fence"], "Lease is stale or uncertain; reconcile before another attempt", Conflict)
            overrun = actual_cost_microusd > attempt["reservation"] or row["spent"] + actual_cost_microusd > contract["budget"]["max_cost_microusd"]
            state = "budget_exceeded" if overrun else "built" if succeeded else "failed"
            recorded = {**result, "task_id": row["id"], "fence": attempt["fence"], "state": state}
            connection.execute("UPDATE attempts SET state='settled', result_digest=?, result_json=? WHERE task_id=? AND fence=?",
                               (result_digest, _json(recorded).decode(), row["id"], attempt["fence"]))
            connection.execute("UPDATE tasks SET state=?, spent=spent+?, reserved=reserved-?, candidate=? WHERE id=?",
                               (state, actual_cost_microusd, attempt["reservation"], candidate_revision, row["id"]))
            return recorded

    def reconcile(self, task_id, fence, *, outcome, actual_cost_microusd):
        """Trusted operator reconciles provider status; uncertain work is not rerun."""
        _require(outcome in ("not_started", "failed", "completed_unrecoverable"), "Unknown reconciliation outcome")
        _integer(actual_cost_microusd, 0, 10**12, "reconciled cost")
        _require(outcome != "not_started" or actual_cost_microusd == 0, "A not-started attempt cannot have a charge")
        receipt = {"outcome": outcome, "actual_cost_microusd": actual_cost_microusd}
        digest = _digest(receipt)
        self.expire()
        with self._connection(transaction=True) as connection:
            attempt = connection.execute("SELECT * FROM attempts WHERE task_id=? AND fence=?", (task_id, fence)).fetchone()
            _require(attempt is not None, "Attempt not found")
            if attempt["state"] == "reconciled":
                _require(attempt["result_digest"] == digest, "Reconciliation differs from the recorded outcome", Conflict)
                return receipt
            _require(attempt["state"] == "uncertain", "Only uncertain attempts may be reconciled", Conflict)
            row, contract = self._task(connection, task_id)
            overrun = actual_cost_microusd > attempt["reservation"] or row["spent"] + actual_cost_microusd > contract["budget"]["max_cost_microusd"]
            connection.execute("UPDATE attempts SET state='reconciled', result_digest=?, result_json=? WHERE task_id=? AND fence=?",
                               (digest, _json(receipt).decode(), task_id, fence))
            connection.execute("UPDATE tasks SET state=?, spent=spent+?, reserved=reserved-? WHERE id=?",
                               ("budget_exceeded" if overrun else "failed", actual_cost_microusd, attempt["reservation"], task_id))
            return receipt

    def record_evidence(self, task_id, *, candidate_revision, base_commit, patch, changed_files,
                        test_results, review, toolchain, risks):
        """Attach verifier receipts and raw bounded artifacts, never execute them."""
        _commit(candidate_revision)
        _commit(base_commit)
        artifacts = {}
        total = 0
        def attach(content, maximum):
            nonlocal total
            _require(isinstance(content, bytes) and 0 < len(content) <= maximum, "Attach actual, nonempty bounded artifact bytes")
            total += len(content)
            _require(total <= MAX_BUNDLE_BYTES, "Evidence artifacts exceed bundle byte limit")
            digest = hashlib.sha256(content).hexdigest()
            artifacts[digest] = content
            return {"sha256": digest, "bytes": len(content)}
        patch_artifact = attach(patch, 128_000)
        _require(isinstance(test_results, list) and 1 <= len(test_results) <= 16, "Invalid test receipts")
        normalized_tests = []
        for test in test_results:
            _keys(test, ("id", "definition_sha256", "revision", "base_commit", "exit_code", "status", "started_at", "finished_at", "log"))
            _identifier(test["id"], "test ID")
            _sha256(test["definition_sha256"])
            _require(test["revision"] == candidate_revision and test["base_commit"] == base_commit, "Test evidence belongs to a different revision/base")
            _integer(test["exit_code"], -255, 2**32, "test exit code")
            _require(test["status"] in ("passed", "failed", "error"), "Unknown test result status")
            _integer(test["started_at"], 1, 2**53, "test start time")
            _integer(test["finished_at"], test["started_at"], 2**53, "test finish time")
            normalized_tests.append({**{key: value for key, value in test.items() if key != "log"}, "log": attach(test["log"], 32_000)})
        _keys(review, ("selection", "actual_model_id", "revision", "base_commit", "verdict", "blocking_findings", "report"))
        _model(review["selection"])
        _require(review["revision"] == candidate_revision and review["base_commit"] == base_commit, "Review belongs to a different revision/base")
        _require(review["verdict"] in ("approve", "changes_requested", "unknown"), "Invalid review verdict")
        _actual_model(review["actual_model_id"])
        _require(isinstance(review["blocking_findings"], list) and len(review["blocking_findings"]) <= 32, "Invalid review findings")
        for finding in review["blocking_findings"]:
            _text(finding, "review finding", 2000)
        normalized_review = {**{key: value for key, value in review.items() if key != "report"}, "report": attach(review["report"], 32_000)}
        _require(isinstance(toolchain, dict) and 1 <= len(toolchain) <= 32, "Record toolchain and dependency identifiers")
        for key, value in toolchain.items():
            _identifier(key, "toolchain key")
            _text(value, "toolchain/dependency identifier", 256)
        _require(isinstance(risks, list) and len(risks) <= 32, "Invalid unresolved risks")
        for risk in risks:
            _text(risk, "risk", 2000)
        with self._connection(transaction=True) as connection:
            row, contract = self._task(connection, task_id)
            _require(row["state"] in ("built", "verification_failed", "verified", "approved"), "Task has no candidate ready for verification", Conflict)
            self._base(connection, contract)
            _require(base_commit == contract["repository"]["base_commit"], "Evidence base differs from immutable contract")
            validate_changed_files(contract, changed_files)
            attempt = connection.execute("SELECT * FROM attempts WHERE task_id=? AND fence=?", (task_id, row["fence"])).fetchone()
            result = json.loads(attempt["result_json"])
            _require(result["succeeded"] and result["candidate_revision"] == candidate_revision
                     and result["patch_sha256"] == patch_artifact["sha256"], "Evidence does not match the worker's recorded candidate/patch")
            expected_tests = {test["id"]: test["definition_sha256"] for test in contract["acceptance_tests"]}
            _require(len({test["id"] for test in normalized_tests}) == len(normalized_tests)
                     and {test["id"]: test["definition_sha256"] for test in normalized_tests} == expected_tests,
                     "Evidence must contain exactly the immutable acceptance tests")
            _require(review["selection"] == contract["reviewer_model"], "Reviewer model/effort differs from contract")
            dependencies = self._dependencies(connection, contract)
            _require(dependencies == json.loads(attempt["dependencies_json"]), "Dependency evidence changed during execution", Conflict)
            actual_builder = result["actual_model_id"]
            actual_reviewer = review["actual_model_id"]
            independent = bool(actual_builder and actual_reviewer and actual_builder.casefold() != actual_reviewer.casefold())
            verified = independent and review["verdict"] == "approve" and not review["blocking_findings"] and all(
                test["status"] == "passed" and test["exit_code"] == 0 for test in normalized_tests)
            bundle = {"task_id": task_id, "fence": row["fence"], "contract_digest": row["contract_digest"],
                      "candidate_revision": candidate_revision, "base_commit": base_commit, "patch": patch_artifact,
                      "changed_files": sorted(changed_files), "tests": normalized_tests, "review": normalized_review,
                      "executor": {"selection": contract["model"], "actual_model_id": actual_builder},
                      "independent_model_ids": independent, "toolchain": toolchain, "risks": risks, "dependencies": dependencies}
            _require(len(_json(bundle)) <= MAX_CONTRACT_BYTES, "Evidence metadata exceeds byte limit")
            digest = _digest(bundle)
            for artifact_digest, content in artifacts.items():
                connection.execute("INSERT OR IGNORE INTO artifacts VALUES (?, ?)", (artifact_digest, content))
            connection.execute("INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?)", (digest, task_id, row["fence"], _json(bundle).decode(), int(verified)))
            if row["evidence_digest"] != digest:
                connection.execute("UPDATE approvals SET state='invalidated' WHERE task_id=? AND state='active'", (task_id,))
                connection.execute("UPDATE tasks SET state=?, evidence_digest=? WHERE id=?", ("verified" if verified else "verification_failed", digest, task_id))
            return {"evidence_digest": digest, "verified": bool(verified), "independent_model_ids": independent}

    def evidence(self, digest):
        _sha256(digest)
        with self._connection() as connection:
            row = connection.execute("SELECT bundle_json FROM evidence WHERE digest=?", (digest,)).fetchone()
            _require(row is not None, "Evidence not found")
            return json.loads(row[0])

    def artifact(self, digest):
        _sha256(digest)
        with self._connection() as connection:
            row = connection.execute("SELECT content FROM artifacts WHERE digest=?", (digest,)).fetchone()
            _require(row is not None, "Artifact not found")
            _require(hashlib.sha256(row[0]).hexdigest() == digest, "Artifact integrity check failed")
            return row[0]

    def approve(self, task_id, *, evidence_digest, revision, current_base, approved_by):
        _sha256(evidence_digest)
        _commit(revision)
        _commit(current_base)
        _text(approved_by, "approving human identity", 256)
        with self._connection(transaction=True) as connection:
            row, contract = self._task(connection, task_id)
            self._base(connection, contract)
            _require(row["state"] in ("verified", "approved") and row["evidence_digest"] == evidence_digest
                     and row["candidate"] == revision and contract["repository"]["base_commit"] == current_base,
                     "Approval must bind the current verified evidence, revision, and base", Conflict)
            bundle = json.loads(connection.execute("SELECT bundle_json FROM evidence WHERE digest=?", (evidence_digest,)).fetchone()[0])
            _require(bundle["dependencies"] == self._dependencies(connection, contract), "Dependency evidence changed before approval", Conflict)
            prior = connection.execute("SELECT id FROM approvals WHERE task_id=? AND evidence_digest=? AND approved_by=? AND state='active'",
                                       (task_id, evidence_digest, approved_by)).fetchone()
            if prior:
                return {"approval_id": prior[0], "revision": revision, "evidence_digest": evidence_digest}
            identifier = secrets.token_hex(16)
            connection.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                               (identifier, task_id, evidence_digest, revision, current_base, approved_by, self.clock()))
            connection.execute("UPDATE tasks SET state='approved' WHERE id=?", (task_id,))
            return {"approval_id": identifier, "revision": revision, "evidence_digest": evidence_digest}

    def observe_base(self, repository_id, current_base):
        """Record a trusted Git observation and invalidate approvals for old bases."""
        _text(repository_id, "repository ID", 256)
        _commit(current_base)
        with self._connection(transaction=True) as connection:
            row = connection.execute("SELECT current_base FROM repositories WHERE id=?", (repository_id,)).fetchone()
            _require(row is not None, "Repository not registered")
            if row[0] != current_base:
                connection.execute("UPDATE repositories SET current_base=? WHERE id=?", (current_base, repository_id))
                connection.execute("UPDATE approvals SET state='invalidated' WHERE state='active' AND task_id IN (SELECT id FROM tasks WHERE repository_id=?)", (repository_id,))
                connection.execute("UPDATE tasks SET state='needs_rebase' WHERE repository_id=? AND state IN ('verified','approved')", (repository_id,))

    def promote(self, task_id, *, approval_id, expected_base):
        """Serialize a ledger-only promotion CAS. This does not merge or deploy."""
        _commit(expected_base)
        _text(approval_id, "approval ID", 64)
        with self._connection(transaction=True) as connection:
            prior = connection.execute("SELECT * FROM promotions WHERE approval_id=?", (approval_id,)).fetchone()
            if prior:
                _require(prior["task_id"] == task_id and prior["base_commit"] == expected_base, "Promotion receipt mismatch", Conflict)
                return {**dict(prior), "record_only": True}
            row, contract = self._task(connection, task_id)
            self._base(connection, contract)
            approval = connection.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            _require(approval is not None and approval["state"] == "active" and approval["task_id"] == task_id
                     and row["state"] == "approved" and approval["revision"] == row["candidate"]
                     and approval["evidence_digest"] == row["evidence_digest"] and approval["base_commit"] == expected_base,
                     "Promotion requires an active approval for this exact revision/base/evidence", Conflict)
            bundle = json.loads(connection.execute("SELECT bundle_json FROM evidence WHERE digest=?", (row["evidence_digest"],)).fetchone()[0])
            _require(bundle["dependencies"] == self._dependencies(connection, contract), "Dependency evidence changed before promotion", Conflict)
            repository_id = contract["repository"]["id"]
            changed = connection.execute("UPDATE repositories SET current_base=? WHERE id=? AND current_base=?",
                                         (row["candidate"], repository_id, expected_base)).rowcount
            _require(changed == 1, "Repository promotion compare-and-swap failed", Conflict)
            connection.execute("INSERT INTO promotions (task_id,approval_id,repository_id,base_commit,revision,evidence_digest) VALUES (?, ?, ?, ?, ?, ?)",
                               (task_id, approval_id, repository_id, expected_base, row["candidate"], row["evidence_digest"]))
            connection.execute("UPDATE tasks SET state='promoted' WHERE id=?", (task_id,))
            connection.execute("UPDATE approvals SET state='invalidated' WHERE state='active' AND task_id IN (SELECT id FROM tasks WHERE repository_id=?)", (repository_id,))
            connection.execute("UPDATE approvals SET state='consumed' WHERE id=?", (approval_id,))
            connection.execute("UPDATE tasks SET state='needs_rebase' WHERE repository_id=? AND id<>? AND state IN ('verified','approved')", (repository_id, task_id))
            receipt = connection.execute("SELECT * FROM promotions WHERE approval_id=?", (approval_id,)).fetchone()
            return {**dict(receipt), "record_only": True}
