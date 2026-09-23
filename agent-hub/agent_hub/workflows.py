"""Bounded finite-state workflow research; no model/network/command execution.

The reference checker interprets an explicit graph independently of candidate
edit operators. Results concern this finite model and stated horizon only.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import itertools
import json
import re


CHECKER_VERSION = "workflow-reference-v1"
MAX_MODEL_BYTES = 128_000
PHASES = frozenset(("pending", "running", "completed", "failed", "stalled", "cancelled"))
DEFAULT_PROPERTIES = (
    {"id": "completed-stays-completed", "kind": "completed_never_pending"},
    {"id": "expired-cannot-commit", "kind": "expired_cannot_commit"},
    {"id": "one-logical-completion", "kind": "at_most_one_completion"},
)


class WorkflowError(ValueError):
    pass


class BudgetExhausted(WorkflowError):
    pass


def _require(condition, message):
    if not condition:
        raise WorkflowError(message)


def _encode(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise WorkflowError("Use finite UTF-8 JSON data") from exc


def _copy(value):
    return json.loads(_encode(value))


def _hash(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _keys(value, expected):
    _require(isinstance(value, dict) and set(value) == set(expected), "Missing or unknown DSL fields")


def _id(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}", value) is not None,
             "Identifiers must be bounded names, not code")
    return value


def _integer(value, lower, upper, name):
    _require(type(value) is int and lower <= value <= upper, f"Invalid {name}")
    return value


def _scalar(value):
    _require(type(value) is bool or type(value) is int and -1000 <= value <= 1000
             or isinstance(value, str) and len(value.encode("utf-8")) <= 128,
             "Labels must be bounded booleans, integers, or strings")


def validate_model(model):
    """Normalize a finite, nondeterministic labeled transition graph."""
    raw = _encode(model)
    _require(len(raw) <= MAX_MODEL_BYTES, "Workflow model exceeds byte limit")
    model = json.loads(raw)
    _keys(model, ("id", "states", "initial", "transitions"))
    _id(model["id"])
    _require(isinstance(model["states"], list) and 1 <= len(model["states"]) <= 64, "Use 1 to 64 explicit states")
    states = {}
    label_types = None
    for state in model["states"]:
        _keys(state, ("id", "labels"))
        _id(state["id"])
        _require(state["id"] not in states, "State IDs must be unique")
        labels = state["labels"]
        _require(isinstance(labels, dict) and 2 <= len(labels) <= 16, "Use 2 to 16 state labels")
        _require(isinstance(labels.get("phase"), str) and labels["phase"] in PHASES
                 and type(labels.get("lease_expired")) is bool, "States need phase and boolean lease_expired labels")
        for key, value in labels.items():
            _id(key)
            _scalar(value)
        types = {key: type(value) for key, value in labels.items()}
        if label_types is None:
            label_types = types
        _require(types == label_types, "Every state must declare the same label names and scalar types")
        states[state["id"]] = state
    initial = model["initial"]
    _require(isinstance(initial, list) and 1 <= len(initial) <= len(states)
             and all(isinstance(key, str) and key in states for key in initial)
             and len(set(initial)) == len(initial), "Initial states must be a nonempty unique subset")
    transitions = model["transitions"]
    _require(isinstance(transitions, list) and len(transitions) <= 256, "Use at most 256 explicit transitions")
    transition_ids = set()
    for transition in transitions:
        _keys(transition, ("id", "source", "action", "target", "accepts_completion"))
        _id(transition["id"])
        _id(transition["action"])
        _require(isinstance(transition["source"], str) and transition["source"] in states
                 and isinstance(transition["target"], str) and transition["target"] in states,
                 "Transition endpoints must exist")
        _require(type(transition["accepts_completion"]) is bool, "Completion acceptance is an explicit boolean effect")
        _require(transition["id"] not in transition_ids, "Transition IDs must be unique")
        transition_ids.add(transition["id"])
    return {"id": model["id"], "states": sorted(states.values(), key=lambda state: state["id"]),
            "initial": sorted(initial), "transitions": sorted(transitions, key=lambda edge: edge["id"])}


def validate_properties(properties, model):
    properties = _copy(properties)
    _require(isinstance(properties, list) and 1 <= len(properties) <= 8, "Use 1 to 8 explicit requirements")
    label_types = {key: type(value) for key, value in model["states"][0]["labels"].items()}
    identifiers = set()
    for prop in properties:
        _require(isinstance(prop, dict), "Invalid requirement")
        if prop.get("kind") == "bounded_response":
            _keys(prop, ("id", "kind", "trigger", "response", "within"))
            _integer(prop["within"], 0, 64, "response bound")
            for key in ("trigger", "response"):
                predicate = prop[key]
                _require(isinstance(predicate, dict) and 1 <= len(predicate) <= 4, "Predicates are bounded label-equality conjunctions")
                for name, value in predicate.items():
                    _require(name in label_types and type(value) is label_types[name], "Predicate label is missing or has the wrong type")
                    _scalar(value)
        else:
            _keys(prop, ("id", "kind"))
            _require(isinstance(prop["kind"], str) and prop["kind"] in {item["kind"] for item in DEFAULT_PROPERTIES}, "Unknown requirement kind")
        _id(prop["id"])
        _require(prop["id"] not in identifiers, "Requirement IDs must be unique")
        identifiers.add(prop["id"])
    return sorted(properties, key=lambda prop: prop["id"])


def _dependencies(dependencies):
    dependencies = {} if dependencies is None else _copy(dependencies)
    _require(isinstance(dependencies, dict) and len(dependencies) <= 32, "Invalid dependency manifest")
    for name, digest in dependencies.items():
        _id(name)
        _require(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) is not None,
                 "Dependency versions must be content SHA-256 hashes")
    return dependencies


@dataclass
class ResearchBudget:
    max_candidates: int = 100
    max_check_states: int = 10_000
    max_transitions: int = 25_000
    candidates: int = 0
    check_states: int = 0
    transitions: int = 0
    cache_hits: int = 0

    def __post_init__(self):
        for name, maximum in (("max_candidates", 10_000), ("max_check_states", 1_000_000), ("max_transitions", 2_000_000)):
            _integer(getattr(self, name), 0, maximum, name)
        for name in ("candidates", "check_states", "transitions", "cache_hits"):
            _require(getattr(self, name) == 0, "Start a research budget with zero counters")

    def consume(self, resource):
        _require(resource in ("candidates", "check_states", "transitions"), "Unknown budget resource")
        if getattr(self, resource) >= getattr(self, "max_" + resource):
            raise BudgetExhausted(resource + " budget exhausted")
        setattr(self, resource, getattr(self, resource) + 1)

    def snapshot(self):
        return {"candidates": self.candidates, "check_states": self.check_states, "transitions": self.transitions,
                "cache_hits": self.cache_hits, "model_calls": 0,
                "limits": {"candidates": self.max_candidates, "check_states": self.max_check_states,
                           "transitions": self.max_transitions}}


class CheckCache:
    """Bounded exact-input cache; no similarity matching or reduced-model reuse."""
    def __init__(self, max_entries=128):
        self.max_entries = _integer(max_entries, 1, 1024, "cache size")
        self.entries = OrderedDict()

    def get(self, key):
        if key not in self.entries:
            return None
        self.entries.move_to_end(key)
        return _copy(self.entries[key])

    def put(self, key, result):
        self.entries[key] = _copy(result)
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)


def _predicate(labels, predicate):
    return all(type(labels[name]) is type(value) and labels[name] == value for name, value in predicate.items())


def reference_check(model, properties=DEFAULT_PROPERTIES, *, horizon=8, budget=None, dependencies=None, cache=None):
    """Exhaustively explore bounded executions; stop early on a real counterexample.

    Histories are merged only when state, depth, and *all* property-monitor state
    match. Deadlocks stutter forever, making unanswered deadlocks observable.
    """
    model = validate_model(model)
    properties = validate_properties(properties, model)
    horizon = _integer(horizon, 0, 64, "execution horizon")
    dependencies = _dependencies(dependencies)
    budget = budget if budget is not None else ResearchBudget()
    _require(isinstance(budget, ResearchBudget), "Use an explicit ResearchBudget")
    key_data = {"model": model, "properties": properties, "horizon": horizon,
                "dependencies": dependencies, "checker": CHECKER_VERSION, "deadlocks": "stutter"}
    cache_key = _hash(key_data)
    if cache is not None:
        _require(isinstance(cache, CheckCache), "Use a bounded exact CheckCache")
        cached = cache.get(cache_key)
        if cached is not None:
            budget.cache_hits += 1
            cached.update(cache_hit=True, checked_states=0, checked_transitions=0)
            return cached
    start_states, start_edges = budget.check_states, budget.transitions
    states = {state["id"]: state["labels"] for state in model["states"]}
    outgoing = {identifier: [] for identifier in states}
    for edge in model["transitions"]:
        outgoing[edge["source"]].append(edge)
    response_properties = [prop for prop in properties if prop["kind"] == "bounded_response"]
    kinds = {prop["kind"]: prop for prop in properties if prop["kind"] != "bounded_response"}
    parents = {}
    queue = deque()
    unresolved = None
    result = {"status": None, "model_hash": _hash(model), "properties_hash": _hash(properties),
              "horizon": horizon, "dependency_hashes": dependencies, "checker_version": CHECKER_VERSION,
              "cache_key": cache_key, "cache_hit": False, "reduction": "none", "counterexample": None,
              "checked_states": 0, "checked_transitions": 0, "exhaustive_within_horizon": False}

    def trace(key, edge=None, target=None):
        steps = []
        current = key
        while current is not None:
            previous, incoming = parents[current]
            state_id = current[1][0]
            steps.append({"step": current[0], "state": state_id, "labels": dict(states[state_id]),
                          "via": _copy(incoming) if incoming is not None else None})
            current = previous
        steps.reverse()
        if edge is not None:
            steps.append({"step": key[0] + 1, "state": target, "labels": dict(states[target]), "via": _copy(edge)})
        return steps

    def fail(prop, key, edge=None, target=None):
        result.update(status="counterexample", counterexample={"property_id": prop["id"], "kind": prop["kind"],
                                                                "trace": trace(key, edge, target)})

    def response_ages(labels, prior=None):
        ages = []
        for index, prop in enumerate(response_properties):
            age = None if prior is None or prior[index] is None else prior[index] + 1
            if _predicate(labels, prop["response"]):
                age = None
            elif _predicate(labels, prop["trigger"]) and age is None:
                age = 0
            ages.append(age)
        return tuple(ages)

    def violated_response(ages):
        for prop, age in zip(response_properties, ages):
            if age is not None and age >= prop["within"]:
                return prop
        return None

    try:
        for identifier in model["initial"]:
            budget.consume("check_states")
            labels = states[identifier]
            ages = response_ages(labels)
            node = (identifier, labels["phase"] == "completed", 0, ages)
            key = (0, node)
            parents[key] = (None, None)
            queue.append(key)
            violated = violated_response(ages)
            if violated:
                fail(violated, key)
                break
        while queue and result["status"] is None:
            key = queue.popleft()
            depth, (identifier, seen_completed, completions, ages) = key
            if depth == horizon:
                if any(age is not None for age in ages) and unresolved is None:
                    unresolved = key
                continue
            edges = outgoing[identifier]
            if not edges:
                edges = [{"id": "deadlock-stutter", "source": identifier, "action": "stutter",
                          "target": identifier, "accepts_completion": False}]
            for edge in edges:
                budget.consume("transitions")
                budget.consume("check_states")
                target = edge["target"]
                labels = states[target]
                next_completions = min(2, completions + int(edge["accepts_completion"]))
                next_ages = response_ages(labels, ages)
                violation = None
                if "completed_never_pending" in kinds and seen_completed and labels["phase"] == "pending":
                    violation = kinds["completed_never_pending"]
                elif "expired_cannot_commit" in kinds and states[identifier]["lease_expired"] and edge["accepts_completion"]:
                    violation = kinds["expired_cannot_commit"]
                elif "at_most_one_completion" in kinds and next_completions > 1:
                    violation = kinds["at_most_one_completion"]
                else:
                    violation = violated_response(next_ages)
                if violation:
                    fail(violation, key, edge, target)
                    break
                node = (target, seen_completed or labels["phase"] == "completed", next_completions, next_ages)
                next_key = (depth + 1, node)
                if next_key not in parents:
                    parents[next_key] = (key, edge)
                    queue.append(next_key)
        if result["status"] is None:
            result["exhaustive_within_horizon"] = True
            if unresolved is not None:
                result.update(status="horizon_inconclusive", pending_trace=trace(unresolved))
            else:
                result["status"] = "bounded_verified"
    except BudgetExhausted as exc:
        result.update(status="budget_exhausted", reason=str(exc))
    result["checked_states"] = budget.check_states - start_states
    result["checked_transitions"] = budget.transitions - start_edges
    result["verification_work"] = {"check_states": result["checked_states"], "transitions": result["checked_transitions"]}
    if cache is not None and result["status"] != "budget_exhausted":
        cache.put(cache_key, result)
    return result


def verify_migration(original, migrated, mapping, *, budget=None):
    """Check a full label/action/effect-preserving graph isomorphism.

    This is a sufficient finite equivalence check, not a general program solver.
    Unreachable states and transitions are checked too.
    """
    original, migrated = validate_model(original), validate_model(migrated)
    _require(isinstance(mapping, dict), "State migration must be an explicit mapping")
    before = {state["id"]: state["labels"] for state in original["states"]}
    after = {state["id"]: state["labels"] for state in migrated["states"]}
    _require(set(mapping) == set(before) and all(isinstance(value, str) for value in mapping.values()),
             "Migration must map every original state")
    _require(len(set(mapping.values())) == len(mapping) and set(mapping.values()) == set(after), "Migration must be bijective onto every target state")
    if budget is not None:
        _require(isinstance(budget, ResearchBudget), "Use an explicit ResearchBudget")
        for _ in before:
            budget.consume("check_states")
        for _ in original["transitions"]:
            budget.consume("transitions")
    reasons = []
    if {mapping[key] for key in original["initial"]} != set(migrated["initial"]):
        reasons.append("initial states changed")
    if any(_encode(before[key]) != _encode(after[mapping[key]]) for key in before):
        reasons.append("state labels changed")
    expected = [{**edge, "source": mapping[edge["source"]], "target": mapping[edge["target"]]}
                for edge in original["transitions"]]
    if expected != migrated["transitions"]:
        reasons.append("transitions, actions, or acceptance effects changed")
    return {"equivalent": not reasons, "reasons": reasons, "method": "complete_labeled_graph_isomorphism",
            "source_hash": _hash(original), "target_hash": _hash(migrated), "mapping_hash": _hash(mapping),
            "states_compared": len(before), "transitions_compared": len(expected)}


def migrate_states(model, mapping, *, budget=None):
    model = validate_model(model)
    _require(isinstance(mapping, dict) and set(mapping) == {state["id"] for state in model["states"]},
             "Migration must map every state")
    for target in mapping.values():
        _id(target)
    migrated = {**model, "states": [{"id": mapping[state["id"]], "labels": state["labels"]} for state in model["states"]],
                "initial": [mapping[key] for key in model["initial"]],
                "transitions": [{**edge, "source": mapping[edge["source"]], "target": mapping[edge["target"]]}
                                for edge in model["transitions"]]}
    migrated = validate_model(migrated)
    evidence = verify_migration(model, migrated, mapping, budget=budget)
    _require(evidence["equivalent"], "State migration failed its independent equivalence check")
    return {"model": migrated, "evidence": evidence}


def validate_edit(edit):
    edit = _copy(edit)
    _require(isinstance(edit, dict), "Edit must be an explicit operation")
    kind = edit.get("kind")
    if kind in ("remove_transition", "disable_acceptance"):
        _keys(edit, ("kind", "transition_id"))
    elif kind == "redirect_transition":
        _keys(edit, ("kind", "transition_id", "target"))
        _id(edit["target"])
    else:
        raise WorkflowError("Unsupported edit: labels, initial states, checker, and requirements cannot be edited")
    _id(edit["transition_id"])
    return edit


def apply_edits(model, edits):
    """Apply only explicit transition edits; conflicting edits fail atomically."""
    model = validate_model(model)
    _require(isinstance(edits, list) and len(edits) <= 3, "Use at most three edits per candidate")
    edits = [validate_edit(edit) for edit in edits]
    identifiers = [edit["transition_id"] for edit in edits]
    _require(len(set(identifiers)) == len(identifiers), "Conflicting edits target the same transition")
    edges = {edge["id"]: edge for edge in model["transitions"]}
    state_ids = {state["id"] for state in model["states"]}
    for edit in edits:
        identifier = edit["transition_id"]
        _require(identifier in edges, "Edited transition does not exist")
        if edit["kind"] == "remove_transition":
            del edges[identifier]
        elif edit["kind"] == "disable_acceptance":
            edges[identifier]["accepts_completion"] = False
        else:
            _require(edit["target"] in state_ids, "Redirect target does not exist")
            edges[identifier]["target"] = edit["target"]
    return validate_model({**model, "transitions": list(edges.values())})


def synthesize(model, allowed_edits, properties=DEFAULT_PROPERTIES, *, horizon=8, max_edits=1,
               budget=None, dependencies=None, cache=None):
    """Deterministic bounded enumeration, including baseline and failed candidates."""
    model = validate_model(model)
    properties = validate_properties(properties, model)
    horizon = _integer(horizon, 0, 64, "execution horizon")
    max_edits = _integer(max_edits, 0, 3, "candidate edit limit")
    dependencies = _dependencies(dependencies)
    _require(isinstance(allowed_edits, list) and len(allowed_edits) <= 32, "Approve at most 32 finite edits")
    allowed_edits = [validate_edit(edit) for edit in allowed_edits]
    allowed_edits = sorted({_hash(edit): edit for edit in allowed_edits}.values(), key=lambda edit: _encode(edit))
    budget = budget if budget is not None else ResearchBudget()
    _require(isinstance(budget, ResearchBudget), "Use an explicit ResearchBudget")
    attempts = []
    result = {"status": "no_repair", "candidate": None, "edits": None, "evidence": None,
              "attempts": attempts, "source_hash": _hash(model), "properties_hash": _hash(properties),
              "allowed_edits_hash": _hash(allowed_edits), "dependency_hashes": dependencies,
              "checker_version": CHECKER_VERSION}
    combinations = itertools.chain.from_iterable(itertools.combinations(allowed_edits, count)
                                                 for count in range(max_edits + 1))
    for combination in combinations:
        try:
            budget.consume("candidates")
        except BudgetExhausted as exc:
            result.update(status="budget_exhausted", reason=str(exc))
            break
        edits = list(combination)
        try:
            candidate = apply_edits(model, edits)
        except WorkflowError as exc:
            attempts.append({"edits": edits, "status": "invalid_candidate", "reason": str(exc),
                             "checked_states": 0, "checked_transitions": 0})
            continue
        evidence = reference_check(candidate, properties, horizon=horizon, budget=budget,
                                   dependencies=dependencies, cache=cache)
        attempts.append({"edits": edits, "status": evidence["status"], "model_hash": evidence["model_hash"],
                         "cache_hit": evidence["cache_hit"], "checked_states": evidence["checked_states"],
                         "checked_transitions": evidence["checked_transitions"],
                         "counterexample": evidence["counterexample"]})
        if evidence["status"] == "bounded_verified":
            result.update(status="found", candidate=candidate, edits=edits, evidence=evidence)
            break
        if evidence["status"] == "budget_exhausted":
            result.update(status="budget_exhausted", reason=evidence["reason"])
            break
    result["budget"] = budget.snapshot()
    return _copy(result)
