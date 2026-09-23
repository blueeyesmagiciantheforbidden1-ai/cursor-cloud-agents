"""Local, summary-only CLI for context, task contracts and bounded research.

This is a trusted-operator adapter, not a remote control or approval API. It
never executes candidate code, calls a provider, merges a patch or deploys.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from .context import ArtifactStore, ContextError, ContextPolicy, DENIED_PARTS, compile_context
from .contracts import ContractError, TaskLedger, validate_contract
from .research import ResearchArchive, ResearchError


MAX_REQUEST_BYTES = 262_144
MAX_STATUS_ENTRIES = 50_000
HASH = re.compile(r"[a-f0-9]{64}\Z")
TASK_STATES = frozenset(("pending", "running", "reconcile_required", "failed", "built",
                         "verification_failed", "verified", "approved", "promoted",
                         "budget_exceeded", "needs_rebase"))
RECORD_KINDS = ("candidate", "epoch", "trial", "pair", "decision", "search",
                "intervention", "interaction", "abstraction")


class LabError(ValueError):
    """An error with a static public code, never request or artifact contents."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _no_links(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise LabError("linked_path_rejected")


def _absolute(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise LabError("absolute_path_required")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise LabError("absolute_path_required")
    if any(part.casefold() in DENIED_PARTS for part in path.parts):
        raise LabError("excluded_directory")
    _no_links(path)
    return path.resolve()


def state_root(value):
    """Require an explicit existing private directory, outside the source tree.

    POSIX group/other access is rejected. Windows ACL ownership/access must be
    provisioned by the operator; this function does not claim to audit an ACL.
    """
    root = _absolute(value)
    project = Path(__file__).resolve().parents[1]
    if not root.is_dir() or root == Path(root.anchor) or root.is_relative_to(project):
        raise LabError("private_state_directory_required")
    if os.name != "nt" and root.stat().st_mode & 0o077:
        raise LabError("private_state_permissions_required")
    return root


def _state_path(root, name):
    path = root / name
    _no_links(path)
    if path.exists() and not path.is_file():
        raise LabError("state_file_required")
    for suffix in ("-wal", "-shm", "-journal"):
        _no_links(path.with_name(path.name + suffix))
    return path


def _keys(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= value.keys()
            or value.keys() - set(required) - set(optional)):
        raise LabError("request_fields_invalid")


def _pairs_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LabError("duplicate_json_key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise LabError("nonfinite_json_rejected")


def read_request(value):
    path = _absolute(value)
    if path.suffix.casefold() != ".json":
        raise LabError("json_file_required")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
            raise LabError("request_byte_limit")
        raw = handle.read(MAX_REQUEST_BYTES + 1)
    _no_links(path)
    if len(raw) > MAX_REQUEST_BYTES:
        raise LabError("request_byte_limit")
    try:
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_object,
                             parse_constant=_nonfinite)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, LabError):
            raise
        raise LabError("invalid_json") from exc
    # Keep even syntactically valid deeply nested payloads out of downstream
    # validators. The request byte cap bounds the number of visited values.
    pending = [(request, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 48:
            raise LabError("request_depth_limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise LabError("nonfinite_json_rejected")
    if not isinstance(request, dict):
        raise LabError("request_object_required")
    return request


@contextmanager
def _read_database(path):
    # Never instantiate a module's writer merely to show status.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    remaining = 10_000
    def progress():
        nonlocal remaining
        remaining -= 1
        return int(remaining <= 0)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.set_progress_handler(progress, 1000)
        db.execute("BEGIN")
        yield db
    finally:
        db.close()


def _artifact_counts(directory):
    _no_links(directory)
    result = {"count": 0, "bytes": 0, "other_entries": 0}
    if not directory.exists():
        return result
    if not directory.is_dir():
        raise LabError("context_directory_required")
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_STATUS_ENTRIES:
                raise LabError("status_entry_limit")
            _no_links(Path(entry.path))
            if HASH.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                result["count"] += 1
                result["bytes"] += entry.stat(follow_symlinks=False).st_size
            else:
                result["other_entries"] += 1
    return result


def status(root):
    """Return aggregate local state only; never source, prompts or identities."""
    result = {"schema_version": 1, "scope": "local_lab", "provider_calls": False}
    context_path = root / "context"
    _no_links(context_path)
    if context_path.exists() and not context_path.is_dir():
        raise LabError("context_directory_required")
    result["context"] = {"present": context_path.exists(),
                         "blobs": _artifact_counts(context_path / "blobs"),
                         "cache_entries": _artifact_counts(context_path / "cache")}
    contract_path = _state_path(root, "contracts.sqlite3")
    contracts = {"present": contract_path.exists(), "tasks": 0, "states": {},
                 "attempts": 0, "evidence": 0, "approvals": 0, "promotions": 0,
                 "spent_microusd": 0, "reserved_microusd": 0}
    if contract_path.exists():
        with _read_database(contract_path) as db:
            row = db.execute("SELECT COUNT(*), COALESCE(SUM(spent),0), COALESCE(SUM(reserved),0) FROM tasks").fetchone()
            contracts.update(tasks=row[0], spent_microusd=row[1], reserved_microusd=row[2])
            for state, count in db.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state LIMIT 32"):
                if state not in TASK_STATES:
                    raise LabError("unknown_contract_state")
                contracts["states"][state] = count
            for table in ("attempts", "evidence", "approvals", "promotions"):
                contracts[table] = db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    result["contracts"] = contracts
    research_path = _state_path(root, "research.sqlite3")
    research = {"present": research_path.exists(), "records": {kind: 0 for kind in RECORD_KINDS},
                "trials": 0, "completed_trials": 0, "pending_trials": 0,
                "recorded_pairs": 0, "reserved_holdouts": 0}
    if research_path.exists():
        with _read_database(research_path) as db:
            for kind, count in db.execute("SELECT kind, COUNT(*) FROM research_records GROUP BY kind LIMIT 32"):
                if kind not in RECORD_KINDS:
                    raise LabError("unknown_research_record_kind")
                research["records"][kind] = count
            for label, table in (("trials", "research_trials"), ("completed_trials", "research_decisions"),
                                 ("recorded_pairs", "research_pairs"), ("reserved_holdouts", "research_holdouts")):
                research[label] = db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            research["pending_trials"] = research["trials"] - research["completed_trials"]
    result["research"] = research
    return result


def _contract_summary(record):
    # task_id is supplied by the operator; use the immutable digest as the
    # public reference so accidental descriptive/private IDs are not echoed.
    return {key: record[key] for key in ("state", "fence", "contract_digest", "spent_microusd",
                                         "reserved_microusd", "evidence_digest", "candidate_revision")}


def dispatch(root, area, action, request):
    if area == "context" and action == "compile":
        _keys(request, ("repo_root", "selected_files", "repo_revision", "toolchain"),
              ("allowed_files", "selected_symbols", "test_files", "config_files", "environment", "policy"))
        values = dict(request)
        values["repo_root"] = _absolute(values["repo_root"])
        policy = values.pop("policy", {})
        if not isinstance(policy, dict):
            raise LabError("request_fields_invalid")
        context_policy = ContextPolicy(**policy)
        packed = compile_context(**values, policy=context_policy, store=ArtifactStore(root / "context"))
        return {"artifact_hash": packed.artifact_hash, "cache_key": packed.cache_key,
                "cache_hit": packed.cache_hit, "files": len(packed.manifest["files"]),
                "input_bytes": packed.manifest["input_bytes"]}
    if area == "contract":
        path = _state_path(root, "contracts.sqlite3")
        if action == "register":
            _keys(request, ("contracts",))
            contracts = request["contracts"]
            if not isinstance(contracts, list) or not 1 <= len(contracts) <= 64:
                raise LabError("contract_batch_limit")
            # Validate before initializing storage so malformed input is inert.
            for contract in contracts:
                validate_contract(contract)
            ledger = TaskLedger(path)
            ids = ledger.register(contracts)
            return {"count": len(ids), "contracts": [_contract_summary(ledger.get(identity)) for identity in ids]}
        if action == "get":
            _keys(request, ("task_id",))
            if not isinstance(request["task_id"], str) or not 1 <= len(request["task_id"]) <= 128:
                raise LabError("task_id_invalid")
            if not path.exists():
                raise LabError("contracts_not_initialized")
            return _contract_summary(TaskLedger(path).get(request["task_id"]))
    if area == "research":
        path = _state_path(root, "research.sqlite3")
        schemas = {
            "add-candidate": (("kind", "artifact"), ("parents", "proposer", "niche", "metadata", "expected_content_hash")),
            "create-epoch": (("policy",), ()),
            "preregister": (("epoch_id", "candidate_id", "baseline_id", "holdout_ids", "independent_evaluator"),
                            ("candidate_overhead", "baseline_overhead", "search_id")),
            "record": (("trial_id", "holdout_id", "candidate_score", "baseline_score", "candidate_cost", "baseline_cost",
                        "candidate_latency_ms", "candidate_robustness", "contracts_pass", "guard_deltas", "evaluator_digest",
                        "protected_contract_digest", "candidate_content_hash", "baseline_content_hash"), ()),
            "finalize": (("trial_id",), ()),
        }
        if action not in schemas:
            raise LabError("unknown_command")
        _keys(request, *schemas[action])
        if not path.exists() and action not in ("add-candidate", "create-epoch"):
            raise LabError("research_not_initialized")
        archive = ResearchArchive(path)
        if action == "add-candidate":
            result = archive.add_candidate(**request)
            return {"id": result["id"], "content_hash": result["content_hash"], "kind": result["kind"],
                    "parent_count": len(result["parents"])}
        if action == "create-epoch":
            result = archive.create_epoch(**request)
            return {key: result[key] for key in ("id", "sample_size", "max_candidates", "alpha",
                                                "meaningful_delta", "budget_per_arm", "evaluator_digest", "protected_contract_digest")}
        if action == "preregister":
            result = archive.preregister_trial(**request)
            return {"id": result["id"], "epoch_id": result["epoch_id"], "candidate_id": result["candidate_id"],
                    "baseline_id": result["baseline_id"], "required_pairs": len(result["holdout_ids"])}
        if action == "record":
            return archive.record_pair(**request)
        if action == "finalize":
            result = archive.finalize_trial(**request)
            # Guard names can contain private labels. Return aggregate pass and
            # numeric decision facts without those names or individual samples.
            summary = {key: result[key] for key in ("id", "trial_id", "epoch_id", "candidate_id", "baseline_id",
                       "sample_size", "alpha_per_candidate", "mean_delta", "lower_bound", "meaningful_delta",
                       "contracts_pass", "equal_total_budget", "valid", "eligible", "reasons", "actual_cost", "metrics")}
            summary["no_observed_regression"] = all(value >= 0 for value in result["guard_minima"].values())
            summary["record_only"] = True
            return summary
    raise LabError("unknown_command")


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse normally echoes unrecognized arguments, which could contain
        # accidentally pasted credentials. Keep CLI failures static and JSON.
        raise LabError("arguments_invalid")


def parser():
    result = _Parser(description=__doc__)
    result.add_argument("--root", required=True, help="Existing private absolute state directory, outside this source tree")
    groups = result.add_subparsers(dest="area", required=True)
    groups.add_parser("status", help="Read aggregate local counts without initializing stores")
    for area, actions in (("context", ("compile",)), ("contract", ("register", "get")),
                           ("research", ("add-candidate", "create-epoch", "preregister", "record", "finalize"))):
        group = groups.add_parser(area)
        commands = group.add_subparsers(dest="action", required=True)
        for action in actions:
            command = commands.add_parser(action)
            command.add_argument("--input", required=True, help="Absolute path to a bounded UTF-8 JSON request file")
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        root = state_root(args.root)
        result = status(root) if args.area == "status" else dispatch(root, args.area, args.action, read_request(args.input))
        print(json.dumps({"ok": True, "result": result}, sort_keys=True, allow_nan=False))
        return 0
    except LabError as exc:
        code = exc.code
    except ContextError:
        code = "context_validation_failed"
    except ContractError:
        code = "contract_validation_failed"
    except ResearchError:
        code = "research_validation_failed"
    except (OSError, sqlite3.Error):
        code = "local_storage_unavailable"
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        code = "request_validation_failed"
    print(json.dumps({"ok": False, "error": code}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
