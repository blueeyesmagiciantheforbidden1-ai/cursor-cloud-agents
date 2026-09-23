"""Durable admission for the reviewed, finite, zero-model research runner.

This is an internal controller, not an authentication boundary. Its caller must
already be authenticated by Cloud Run IAM and the hub's manager authorization.
Use a dedicated Firestore database, never a worker's local SQLite evidence DB.
``store`` implements get_state / atomic mutate_states from agent_hub.store.
The callback may be retried; all runner/archive I/O happens after its confirmed
commit. An ambiguous commit never authorizes execution from this process.

The injected trusted runner must bound/terminate its child, verify output bytes
and referenced evidence, and return an immutable GCS generation receipt. This
module checks that receipt's shape and publication, not the scientific evidence
inside an archive. No provider, deployment, activation, or secret capability is
offered. A reservation consumes one of four UTC-day runs even after failure.

A reserved/uncertain slot has NO automatic expiry or takeover. A crash before
dispatch may therefore require operator reconciliation, deliberately preferring
an unused reservation over duplicate work. There is no public reset API here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Callable


SOURCE_ARCHIVE_SHA256 = "87bd79de9d2dd26040fd150df26dc24de3703bf6341843ef0d4e3fa49a81c5a1"
MAX_RUNS_PER_DAY = 4
CHILD_CPU_SECONDS_LIMIT = 25
REQUEST_TIMEOUT_SECONDS = 60
MAX_RESULT_BYTES = 48 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
CONTROL_KEY = "frontier_control_v1"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_GENERATION = re.compile(r"[1-9][0-9]{0,19}\Z")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]\Z")


class FrontierError(ValueError):
    """A safe, fixed error code; never contains runner output or credentials."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


ResearchError = FrontierError


class CommitUncertain(FrontierError):
    """Storage acknowledgment is uncertain; get/reconcile, never redispatch."""

    def __init__(self, code="storage_commit_uncertain_get_before_reconciliation"):
        super().__init__(code, 503)


def _json(value, *, max_bytes=MAX_RESULT_BYTES):
    def inspect(item, depth=0):
        if depth > 18:
            raise ResearchError("json_too_deep")
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                inspect(child, depth + 1)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                inspect(child, depth + 1)
            return
        raise ResearchError("invalid_json_value")

    inspect(value)
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (ValueError, OverflowError, RecursionError):
        raise ResearchError("invalid_json_value") from None
    if len(data) > max_bytes:
        raise ResearchError("json_too_large")
    return data


def _identifier(value, code="invalid_request_id"):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ResearchError(code)
    return value


def _request(document):
    if type(document) is not dict or set(document) - {
            "request_id", "workspace", "seed", "units", "budget"}:
        raise ResearchError("invalid_run_request")
    request_id = _identifier(document.get("request_id"))
    parameters = {
        "workspace": _identifier(document.get("workspace", "default"), "invalid_workspace"),
        "seed": document.get("seed", 20260921),
        "units": document.get("units", 4),
        "budget": document.get("budget", 6),
    }
    if type(parameters["seed"]) is not int or not 0 <= parameters["seed"] <= 2**31 - 1:
        raise ResearchError("invalid_seed")
    for name in ("units", "budget"):
        if type(parameters[name]) is not int or not 1 <= parameters[name] <= 8:
            raise ResearchError("invalid_" + name)
    return request_id, parameters


def _fingerprint(parameters):
    return hashlib.sha256(_json({"parameters": parameters,
                                "source_archive_sha256": SOURCE_ARCHIVE_SHA256})).hexdigest()


def _run_key(request_id):
    return "frontier_run_" + request_id


def _public(record):
    return copy.deepcopy({k: v for k, v in record.items() if k != "dispatch_nonce"})


class ResearchService:
    """One global active run, atomic idempotency, immutable result publication.

    ``runner(parameters, request_id)`` is a synchronous trusted callable. Its
    contract is documented in _validated_result. It must not itself retry a
    failed/unknown execution, and cannot receive provider credentials.
    ``artifact_bucket`` is a fixed operator setting, never a request argument.
    """

    def __init__(self, store, runner: Callable, *, artifact_bucket: str,
                 clock: Callable = time.time):
        if type(artifact_bucket) is not str or not _BUCKET.fullmatch(artifact_bucket):
            raise ResearchError("invalid_artifact_bucket")
        if not callable(runner) or not callable(clock):
            raise ResearchError("missing_trusted_runner_or_clock")
        self.store = store
        self.runner = runner
        self.artifact_bucket = artifact_bucket
        self.clock = clock

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 253402300799:
            raise ResearchError("invalid_clock")
        return float(now)

    def _mutate(self, keys, callback):
        try:
            return self.store.mutate_states(keys, callback)
        except ResearchError:
            raise
        except Exception:
            # Do not inspect or expose exception text: cloud errors may embed
            # request bodies, and a server could have committed before timeout.
            raise CommitUncertain("storage_commit_uncertain_get_before_reconciliation") from None

    def _checked_record(self, record, request_id):
        if (type(record) is not dict or record.get("schema_version") != 1 or
                record.get("request_id") != request_id or
                record.get("source_archive_sha256") != SOURCE_ARCHIVE_SHA256 or
                record.get("status") not in {"reserved", "succeeded", "blocked_uncertain"}):
            raise ResearchError("stored_run_invalid")
        if type(record.get("parameters")) is not dict or type(record.get("dispatch_nonce")) is not str:
            raise ResearchError("stored_run_invalid")
        _, parameters = _request({"request_id": request_id, **record["parameters"]})
        if (record.get("parameters") != parameters or
                record.get("parameters_sha256") != _fingerprint(parameters) or
                not _ID.fullmatch(record.get("dispatch_nonce", ""))):
            raise ResearchError("stored_run_invalid")
        if record["status"] == "succeeded":
            result = self._validated_result(record.get("result"), parameters, request_id)
            if record.get("result_sha256") != hashlib.sha256(_json(result)).hexdigest():
                raise ResearchError("stored_result_invalid")
        _json(record, max_bytes=MAX_RESULT_BYTES + 4096)
        return record

    def get(self, document):
        """Read a receipt without claiming, charging, retrying, or clearing it."""
        if type(document) is not dict or set(document) != {"request_id"}:
            raise ResearchError("invalid_get_request")
        request_id = _identifier(document["request_id"])
        record = self.store.get_state(_run_key(request_id))
        if not record:
            return {"schema_version": 1, "request_id": request_id, "status": "not_found"}
        return _public(self._checked_record(record, request_id))

    def run(self, document):
        request_id, parameters = _request(document)
        now = self._now()
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        day_key = "frontier_day_" + day
        run_key = _run_key(request_id)
        fingerprint = _fingerprint(parameters)
        nonce = uuid.uuid4().hex

        def reserve(states):
            existing = states[run_key]
            if existing:
                self._checked_record(existing, request_id)
                if existing["parameters_sha256"] != fingerprint:
                    raise ResearchError("request_id_parameters_conflict", 409)
                return {"dispatch": False, "record": _public(existing)}
            control, daily = states[CONTROL_KEY], states[day_key]
            if control:
                if control.get("schema_version") != 1 or "active" not in control:
                    raise ResearchError("stored_control_invalid")
                if control["active"] is not None:
                    raise ResearchError("another_run_active_reconciliation_may_be_required", 409)
            if daily and (daily.get("schema_version") != 1 or daily.get("day") != day or
                          type(daily.get("charged_runs")) is not int or
                          not 0 <= daily["charged_runs"] <= MAX_RUNS_PER_DAY):
                raise ResearchError("stored_daily_allowance_invalid")
            charged = daily.get("charged_runs", 0)
            if charged >= MAX_RUNS_PER_DAY:
                raise ResearchError("daily_offline_research_allowance_exhausted", 429)
            record = {
                "schema_version": 1, "request_id": request_id, "parameters": parameters,
                "parameters_sha256": fingerprint, "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
                "status": "reserved", "dispatch_nonce": nonce, "reserved_at": now,
                "utc_day": day, "reservation": {
                    "offline_runs": 1, "child_cpu_seconds_limit": CHILD_CPU_SECONDS_LIMIT,
                    "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
                    "model_calls": 0, "api_spend_microdollars": 0,
                },
            }
            states[run_key] = record
            states[CONTROL_KEY] = {"schema_version": 1, "active": {
                "request_id": request_id, "dispatch_nonce": nonce,
                "parameters_sha256": fingerprint, "reserved_at": now,
            }}
            states[day_key] = {"schema_version": 1, "day": day, "charged_runs": charged + 1}
            return {"dispatch": True, "record": _public(record)}

        reservation = self._mutate((run_key, CONTROL_KEY, day_key), reserve)
        if not reservation["dispatch"]:
            return reservation["record"]
        # This branch is reached only on confirmed reservation commit. It is
        # never called inside a transaction callback or after an unknown ack.
        try:
            raw = self.runner(copy.deepcopy(parameters), request_id)
            result = self._validated_result(raw, parameters, request_id)
        except Exception:
            self._block(run_key, request_id, nonce, fingerprint)
            raise ResearchError("run_or_archive_uncertain_reconciliation_required", 503) from None
        finished_at = self._now()

        def publish(states):
            record = self._owned(states, run_key, request_id, nonce, fingerprint)
            record.update(status="succeeded", completed_at=finished_at, result=result,
                          result_sha256=hashlib.sha256(_json(result)).hexdigest())
            states[CONTROL_KEY]["active"] = None
            return _public(record)

        # A lost success ack may have released the slot and saved the result.
        # It is safe to read; never invoke the runner again to reconstruct it.
        return self._mutate((run_key, CONTROL_KEY), publish)

    def _owned(self, states, run_key, request_id, nonce, fingerprint):
        record = self._checked_record(states[run_key], request_id)
        active = states[CONTROL_KEY].get("active")
        if (record["status"] != "reserved" or record["dispatch_nonce"] != nonce or
                record["parameters_sha256"] != fingerprint or type(active) is not dict or
                active.get("request_id") != request_id or active.get("dispatch_nonce") != nonce or
                active.get("parameters_sha256") != fingerprint):
            raise ResearchError("publication_fence_lost")
        return record

    def _block(self, run_key, request_id, nonce, fingerprint):
        now = self._now()

        def block(states):
            record = self._owned(states, run_key, request_id, nonce, fingerprint)
            record.update(status="blocked_uncertain", blocked_at=now,
                          failure_code="run_or_archive_uncertain")
            # Retain active and the original daily charge, including timeouts.
            return None

        self._mutate((run_key, CONTROL_KEY), block)

    def _validated_result(self, result, parameters, request_id):
        if type(result) is not dict or set(result) != {
                "schema_version", "parameters", "source_archive_sha256", "summary", "artifact"}:
            raise ResearchError("invalid_runner_result")
        data = _json(result)
        if (type(result["schema_version"]) is not int or result["schema_version"] != 1 or
                result["parameters"] != parameters or
                result["source_archive_sha256"] != SOURCE_ARCHIVE_SHA256):
            raise ResearchError("runner_provenance_mismatch")
        summary = result["summary"]
        if (type(summary) is not dict or summary.get("promotion_status") not in {"inconclusive", "review_required"} or
                "active_release" not in summary or summary["active_release"] is not None or
                type(summary.get("model_calls")) is not int or summary["model_calls"] != 0 or
                type(summary.get("api_spend_microdollars")) is not int or
                summary["api_spend_microdollars"] != 0):
            raise ResearchError("runner_exceeded_offline_authority")
        if any(type(summary.get(key)) is not int or summary[key] != parameters[key]
               for key in ("seed", "units", "budget")):
            raise ResearchError("runner_summary_parameters_mismatch")
        if summary.get("confirmation") != {
                "inconclusive": "inconclusive",
                "review_required": "evidence_passed_manual_review_required",
        }[summary["promotion_status"]]:
            raise ResearchError("runner_confirmation_status_mismatch")
        artifact = result["artifact"]
        if type(artifact) is not dict or set(artifact) != {"bucket", "object", "generation", "sha256", "bytes"}:
            raise ResearchError("invalid_archive_receipt")
        digest = artifact["sha256"]
        if (type(digest) is not str or not _SHA.fullmatch(digest) or
                artifact["bucket"] != self.artifact_bucket or
                artifact["object"] != f"frontier/runs/{request_id}/{digest}.tar.gz" or
                type(artifact["generation"]) is not str or not _GENERATION.fullmatch(artifact["generation"]) or
                type(artifact["bytes"]) is not int or not 1 <= artifact["bytes"] <= MAX_ARTIFACT_BYTES):
            raise ResearchError("invalid_archive_receipt")
        return json.loads(data)
