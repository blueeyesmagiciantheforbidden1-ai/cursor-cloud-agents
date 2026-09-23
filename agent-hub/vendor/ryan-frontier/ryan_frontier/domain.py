"""A finite, executable lease/result-acceptance repair benchmark.

The plant is a deliberately small *model*, not a distributed implementation.
Candidates contain only conjunctions from a closed, typed JSON grammar.  The
verifier interprets that grammar; its reference decision and transition monitor
are separate from both the interpreter and the Python source generator.

A passing result means every reachable transition within ``task.horizon`` is
safe in this finite model.  It is not an unbounded or distributed-correctness
guarantee.  Expiry is an explicit event: there is no real clock or concurrency.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from hashlib import sha256
from itertools import combinations, product
import json
import random
from typing import Any, Iterable, Mapping


GUARD_NAMES = ("terminal", "fence", "lease", "owner", "once")
GUARD_BITS = {name: 1 << index for index, name in enumerate(GUARD_NAMES)}
ALL_GUARDS_MASK = (1 << len(GUARD_NAMES)) - 1
SCHEMA = "lease-policy/v1"
PHASES = ("pending", "leased", "done")
_COMPLETE_GUARDS = frozenset({"fence", "lease", "owner", "once"})
_RETRY_GUARDS = frozenset({"terminal"})


@dataclass(frozen=True, slots=True)
class State:
    phase: str = "pending"
    owner: int = -1
    fence: int = 0
    lease_active: bool = False
    accepted_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    worker: int = -1
    fence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    split: str
    horizon: int
    workers: int
    max_fence: int
    scenarios: tuple[str, ...]
    initial_mask: int = 0

    def __post_init__(self) -> None:
        if type(self.horizon) is not int or not 0 <= self.horizon <= 12:
            raise ValueError("horizon must be an integer from 0 through 12")
        if type(self.workers) is not int or not 1 <= self.workers <= 4:
            raise ValueError("workers must be an integer from 1 through 4")
        if type(self.max_fence) is not int or not 1 <= self.max_fence <= 4:
            raise ValueError("max_fence must be an integer from 1 through 4")
        if not isinstance(self.scenarios, tuple) or len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("scenarios must be a tuple without duplicates")
        if set(self.scenarios) - set(GUARD_NAMES):
            raise ValueError("unknown scenario")
        _mask_to_names(self.initial_mask)

    @property
    def required_guards(self) -> tuple[str, ...]:
        """Scenario metadata for audits; search should learn from failures."""
        return tuple(name for name in GUARD_NAMES if name in self.scenarios)

    @property
    def initial_candidate(self) -> dict[str, Any]:
        return candidate_spec(self.initial_mask)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["scenarios"] = list(self.scenarios)
        result["initial_candidate"] = self.initial_candidate
        result["scope"] = "finite model; at most horizon events from the initial state"
        return result


@dataclass(frozen=True, slots=True)
class TraceStep:
    before: State
    event: Event
    accepted: bool
    after: State
    violation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Verification:
    passed: bool
    checked_states: int
    checked_transitions: int
    counterexample: tuple[TraceStep, ...]
    failure_kind: str | None
    horizon: int
    maximum_depth: int
    frontier_states: int
    checked_policy_decisions: int = 0
    covered_event_kinds: tuple[str, ...] = ()

    @property
    def exhaustive(self) -> bool:
        """True only if every reachable transition through the bound was checked."""
        return self.passed

    @property
    def nonvacuous(self) -> bool:
        """Whether any candidate-controlled decision was actually checked."""
        return self.checked_policy_decisions > 0

    @property
    def acceptance_eligible(self) -> bool:
        """Passing bounded evidence with at least one checked policy decision.

        This does not imply coverage of scenarios deeper than the event bound.
        ``passed`` alone deliberately retains vacuous bounded-truth semantics.
        """
        return self.passed and self.nonvacuous

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checked_states": self.checked_states,
            "checked_transitions": self.checked_transitions,
            "checked_policy_decisions": self.checked_policy_decisions,
            "covered_event_kinds": list(self.covered_event_kinds),
            "counterexample": [step.to_dict() for step in self.counterexample],
            "failure_kind": self.failure_kind,
            "horizon": self.horizon,
            "maximum_depth": self.maximum_depth,
            "frontier_states": self.frontier_states,
            "exhaustive_within_bound": self.exhaustive,
            "nonvacuous": self.nonvacuous,
            "acceptance_eligible": self.acceptance_eligible,
            "scope": "bounded finite-model check; no unbounded or distributed guarantee",
        }


def _mask_to_names(mask: int) -> tuple[str, ...]:
    if type(mask) is not int or not 0 <= mask <= ALL_GUARDS_MASK:
        raise ValueError("candidate mask must be an integer from 0 through 31")
    return tuple(name for name in GUARD_NAMES if mask & GUARD_BITS[name])


def candidate_spec(guards: int | Iterable[str] = 0) -> dict[str, Any]:
    """Construct the canonical AST for a subset of the five available guards."""
    if type(guards) is int:
        names = _mask_to_names(guards)
    else:
        try:
            names = tuple(guards)
        except TypeError as exc:
            raise ValueError("guards must be an integer mask or an iterable of names") from exc
    if any(type(name) is not str for name in names) or set(names) - set(GUARD_NAMES):
        raise ValueError("unknown guard name")
    if len(set(names)) != len(names):
        raise ValueError("duplicate guard")

    def expression(allowed: frozenset[str]) -> dict[str, Any]:
        return {
            "kind": "and",
            "type": "bool",
            "terms": [
                {"kind": "predicate", "type": "bool", "name": name}
                for name in GUARD_NAMES if name in names and name in allowed
            ],
        }

    return {
        "schema": SCHEMA,
        "kind": "policy",
        "complete": expression(_COMPLETE_GUARDS),
        "retry": expression(_RETRY_GUARDS),
    }


def validate_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact keys, node types, scopes and bounds; return a fresh AST.

    There are exactly 32 distinct policies in this language. Arbitrary source,
    function names, attributes, literals and expression nesting are not accepted.
    """
    if type(candidate) is not dict or set(candidate) != {"schema", "kind", "complete", "retry"}:
        raise ValueError("policy must be a plain object with exactly the schema's keys")
    if candidate["schema"] != SCHEMA or candidate["kind"] != "policy":
        raise ValueError("unsupported policy schema")
    names: list[str] = []
    for action, allowed in (("complete", _COMPLETE_GUARDS), ("retry", _RETRY_GUARDS)):
        node = candidate[action]
        if type(node) is not dict or set(node) != {"kind", "type", "terms"}:
            raise ValueError("expected a typed conjunction")
        if node["kind"] != "and" or node["type"] != "bool" or type(node["terms"]) is not list:
            raise ValueError("expected a Boolean conjunction with a list of predicates")
        if len(node["terms"]) > len(allowed):
            raise ValueError("too many predicates")
        for term in node["terms"]:
            if type(term) is not dict or set(term) != {"kind", "type", "name"}:
                raise ValueError("expected a typed predicate")
            if term["kind"] != "predicate" or term["type"] != "bool":
                raise ValueError("expected a Boolean predicate")
            name = term["name"]
            if type(name) is not str or name not in allowed or name in names:
                raise ValueError("unknown, duplicated or incorrectly scoped predicate")
            names.append(name)
    return candidate_spec(names)


def guard_names(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    clean = validate_candidate(candidate)
    names = {term["name"] for action in ("complete", "retry") for term in clean[action]["terms"]}
    return tuple(name for name in GUARD_NAMES if name in names)


def candidate_mask(candidate: Mapping[str, Any]) -> int:
    return sum(GUARD_BITS[name] for name in guard_names(candidate))


def candidate_key(candidate: Mapping[str, Any]) -> str:
    return json.dumps(validate_candidate(candidate), sort_keys=True, separators=(",", ":"))


def canonicalize(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical representation: typed conjunctions in the fixed guard order."""
    return validate_candidate(candidate)


def _legacy_guards(document: Mapping[str, Any]) -> tuple[str, ...]:
    if type(document) is not dict or set(document) != {"schema", "guards"}:
        raise ValueError("legacy policy must have exactly schema and guards")
    if document["schema"] != "lease-policy/v0" or type(document["guards"]) is not list:
        raise ValueError("unsupported legacy policy")
    guards = document["guards"]
    if len(guards) > len(GUARD_NAMES) or any(type(name) is not str for name in guards):
        raise ValueError("invalid legacy guards")
    if len(set(guards)) != len(guards) or set(guards) - set(GUARD_NAMES):
        raise ValueError("unknown or repeated legacy guard")
    return tuple(guards)


def migrate_candidate(document: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate flat v0 guards to a typed v1 AST; canonicalize existing v1 ASTs."""
    if type(document) is dict and document.get("schema") == "lease-policy/v0":
        return candidate_spec(_legacy_guards(document))
    return canonicalize(document)


def certify_representation(
    task: Task,
    source: Mapping[str, Any],
    target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check migration and compiled semantics on every bounded state/event input.

    Unlike reachability verification, this enumerates even unreachable Cartesian
    state combinations. It checks only representation equivalence, not safety.
    The independent legacy adapter does not use the migration's output to decide
    the expected behavior. A changed target is accepted only if equivalent.
    The JSON-serializable validation record binds source, target and emitted code
    with hashes, reports exact expected coverage, and distinguishes a completed
    certificate from a failed early check. Hashes provide identities, not signed
    attestations. This checks neither workflow safety nor general improvement.
    """
    legacy = type(source) is dict and source.get("schema") == "lease-policy/v0"
    original = _legacy_guards(source) if legacy else validate_candidate(source)
    migrated = migrate_candidate(source) if target is None else validate_candidate(target)
    source_record = {"schema": "lease-policy/v0", "guards": list(original)} if legacy else original
    source_json = json.dumps(source_record, sort_keys=True, separators=(",", ":"))
    generated_source = compile_candidate(migrated)
    function = compiled_policy(migrated)
    event_space = [Event("expire"), Event("retry")]
    for worker in range(task.workers):
        event_space.extend([Event("claim", worker), Event("reclaim", worker)])
        event_space.extend(Event("complete", worker, fence) for fence in range(task.max_fence + 1))
    state_count = len(PHASES) * (task.workers + 1) * (task.max_fence + 1) * 2 * 3
    expected_inputs = state_count * len(event_space)
    metadata = {
        "certificate_schema": "lease-representation-certificate/v1",
        "source_schema": source_record["schema"],
        "target_schema": SCHEMA,
        "source_sha256": sha256(source_json.encode("utf-8")).hexdigest(),
        "target_sha256": sha256(candidate_key(migrated).encode("utf-8")).hexdigest(),
        "python_source_sha256": sha256(generated_source.encode("utf-8")).hexdigest(),
        "hash_encodings": {
            "source": "UTF-8 sorted-key compact JSON; v1 predicate order canonicalized",
            "target": "UTF-8 sorted-key compact JSON of canonical target AST",
            "python_source": "exact UTF-8 compile_candidate output",
        },
        "expected_inputs": expected_inputs,
        "scope": "representation equivalence in finite Cartesian model",
        "safety_checked": False,
        "coverage": {
            "state_count": state_count,
            "event_count": len(event_space),
            "phases": list(PHASES),
            "state_owners": list(range(-1, task.workers)),
            "state_and_completion_fences": list(range(task.max_fence + 1)),
            "lease_active_values": [False, True],
            "accepted_counts": [0, 1, 2],
            "event_workers": list(range(task.workers)),
            "event_kinds": ["claim", "expire", "reclaim", "complete", "retry"],
            "includes_unreachable_states": True,
            "event_horizon_applies": False,
        },
    }

    def record(passed: bool, checked: int, witness: dict[str, Any] | None) -> dict[str, Any]:
        exhaustive = checked == expected_inputs
        return {
            **metadata, "passed": passed, "checked_inputs": checked,
            "nonvacuous": checked > 0, "exhaustive_inputs": exhaustive,
            "acceptance_eligible": passed and exhaustive and checked > 0,
            "counterexample": witness,
        }

    checked = 0
    for phase, owner, fence, active, count in product(
        PHASES, range(-1, task.workers), range(task.max_fence + 1), (False, True), range(3)
    ):
        state = State(phase, owner, fence, active, count)
        for event in event_space:
            if legacy:
                if event.kind == "retry":
                    expected = "terminal" not in original or state.phase != "done"
                elif event.kind == "complete":
                    expected = (("fence" not in original or event.fence == state.fence)
                                and ("owner" not in original or event.worker == state.owner)
                                and ("lease" not in original or state.lease_active)
                                and ("once" not in original or state.accepted_count == 0))
                else:
                    expected = True
            else:
                expected = _interpret_validated(original, state, event)
            interpreted = _interpret_validated(migrated, state, event)
            compiled = function(state.to_dict(), event.to_dict())
            checked += 1
            if expected != interpreted or interpreted != compiled:
                return record(False, checked, {
                    "state": state.to_dict(), "event": event.to_dict(),
                    "source": expected, "target": interpreted, "compiled": compiled,
                })
    return record(True, checked, None)


def enumerate_candidates(
    order: str = "baseline",
    learned_guards: Iterable[str] = (),
    base_candidate: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a finite, deterministic search order, optionally preserving guards.

    Baseline enumerates numeric masks. Targeted first applies the reusable guard
    bundle, then grows it by single-predicate edits; the remaining grammar comes
    last. It receives no task metadata or hidden reference specification.
    """
    learned = candidate_mask(candidate_spec(learned_guards))
    base = 0 if base_candidate is None else candidate_mask(base_candidate)
    masks = [mask for mask in range(ALL_GUARDS_MASK + 1) if mask & base == base]
    if order == "baseline":
        pass
    elif order in {"targeted", "learned"}:
        preferred = base | learned
        masks.sort(key=lambda mask: (0 if mask & preferred == preferred else 1,
                                     (mask ^ preferred).bit_count(), mask))
    else:
        raise ValueError("order must be baseline, targeted, or learned")
    return [candidate_spec(mask) for mask in masks]


def repair_operators(candidate: Mapping[str, Any], learned_guards: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Current policy, learned-bundle insertion, then every one-guard insertion."""
    current = candidate_mask(candidate)
    learned = candidate_mask(candidate_spec(learned_guards))
    masks = [current, current | learned] + [current | GUARD_BITS[name] for name in GUARD_NAMES]
    return [candidate_spec(mask) for mask in dict.fromkeys(masks)]


def _predicate(name: str, state: State, event: Event) -> bool:
    if name == "terminal":
        return state.phase != "done"
    if name == "fence":
        return event.fence == state.fence
    if name == "lease":
        return state.lease_active
    if name == "owner":
        return event.worker == state.owner
    if name == "once":
        return state.accepted_count == 0
    raise ValueError("unvalidated predicate")


def _interpret_validated(candidate: Mapping[str, Any], state: State, event: Event) -> bool:
    if event.kind not in {"complete", "retry"}:
        return True
    return all(_predicate(term["name"], state, event) for term in candidate[event.kind]["terms"])


def interpret_candidate(candidate: Mapping[str, Any], state: State, event: Event) -> bool:
    """Interpret only a validated, closed policy; return the policy guard decision."""
    return _interpret_validated(validate_candidate(candidate), state, event)


def compile_candidate(candidate: Mapping[str, Any]) -> str:
    """Compile validated AST into deterministic, readable, actual Python source.

    Every emitted expression is a fixed trusted template. No input string is
    inserted into source, including identifiers or literals from submitted JSON.
    The emitted function accepts ordinary dictionaries and returns a bool.
    """
    clean = validate_candidate(candidate)
    expressions = {
        "terminal": 'state["phase"] != "done"',
        "fence": 'event["fence"] == state["fence"]',
        "lease": 'state["lease_active"]',
        "owner": 'event["worker"] == state["owner"]',
        "once": 'state["accepted_count"] == 0',
    }
    lines = [
        '# Generated from the validated finite lease-policy/v1 grammar.',
        '# Policy guard only: plant eligibility is enforced separately.',
        'def policy(state, event):',
    ]
    for action in ("complete", "retry"):
        terms = [expressions[term["name"]] for term in clean[action]["terms"]]
        expression = " and ".join(f"({term})" for term in terms) or "True"
        lines.extend([f'    if event["kind"] == "{action}":', f"        return {expression}"])
    lines.extend(["    return True", ""])
    return "\n".join(lines)


def compiled_policy(candidate: Mapping[str, Any]):
    """Load only compiler-produced trusted code, never arbitrary Python input."""
    source = compile_candidate(candidate)
    namespace: dict[str, Any] = {}
    exec(compile(source, "<validated-lease-policy>", "exec"), {"__builtins__": {}}, namespace)
    return namespace["policy"]


def evaluate_compiled(candidate: Mapping[str, Any], state: State, event: Event) -> bool:
    return compiled_policy(candidate)(state.to_dict(), event.to_dict())


def events(task: Task, state: State) -> tuple[Event, ...]:
    """Finite environment choices, independent of the candidate's policy.

    A completion retains the lease *record* to expose duplicate acceptance. The
    record's active flag alone is therefore not a right to commit a second time.
    Scenario switches determine which adversarial deliveries the environment
    includes; they do not alter the reference requirements.
    """
    result: list[Event] = []
    if state.phase == "pending" and state.fence < task.max_fence:
        result.extend(Event("claim", worker) for worker in range(task.workers))
    if state.phase == "leased":
        if state.lease_active:
            result.append(Event("expire"))
        else:
            if state.fence < task.max_fence:
                result.extend(Event("reclaim", worker) for worker in range(task.workers))
            result.append(Event("retry"))
    if state.phase == "done" and "terminal" in task.scenarios:
        result.append(Event("retry"))
    complete_enabled = (
        state.phase == "leased" and (state.lease_active or "lease" in task.scenarios)
    ) or (state.phase == "done" and "once" in task.scenarios)
    if complete_enabled:
        # First deliver a valid identity, then mismatches. BFS finds short traces.
        workers = [state.owner]
        if "owner" in task.scenarios:
            workers.extend(worker for worker in range(task.workers) if worker != state.owner)
        fences = [state.fence]
        if "fence" in task.scenarios:
            fences.extend(fence for fence in range(1, state.fence))
        result.extend(Event("complete", worker, fence) for worker in workers for fence in fences)
    return tuple(result)


def plant_step(state: State, event: Event, policy_allows: bool, max_fence: int) -> tuple[State, bool]:
    """Trusted plant mechanics. Return next state and whether event took effect.

    Plant eligibility describes mechanics only. Candidate guards make acceptance
    decisions, so a bug really can mutate the state and trigger the monitor.
    """
    if event.kind == "claim" and state.phase == "pending" and state.fence < max_fence:
        return State("leased", event.worker, state.fence + 1, True, state.accepted_count), True
    if event.kind == "expire" and state.phase == "leased" and state.lease_active:
        return State(state.phase, state.owner, state.fence, False, state.accepted_count), True
    if event.kind == "reclaim" and state.phase == "leased" and not state.lease_active and state.fence < max_fence:
        return State("leased", event.worker, state.fence + 1, True, state.accepted_count), True
    if event.kind == "complete" and state.phase in {"leased", "done"} and policy_allows:
        return State("done", state.owner, state.fence, state.lease_active, min(2, state.accepted_count + 1)), True
    if event.kind == "retry" and (state.phase == "done" or (state.phase == "leased" and not state.lease_active)) and policy_allows:
        return State("pending", -1, state.fence, False, state.accepted_count), True
    return state, False


def reference_decision(state: State, event: Event, max_fence: int) -> bool:
    """Independent specification; does not call interpreter/compiler/plant."""
    if event.kind == "claim":
        return state.phase == "pending" and state.fence < max_fence
    if event.kind == "expire":
        return state.phase == "leased" and state.lease_active
    if event.kind == "reclaim":
        return state.phase == "leased" and not state.lease_active and state.fence < max_fence
    if event.kind == "retry":
        return state.phase == "leased" and not state.lease_active
    if event.kind == "complete":
        return (state.phase == "leased" and state.accepted_count == 0
                and state.lease_active and event.worker == state.owner
                and event.fence == state.fence)
    return False


def invariant_violation(before: State, event: Event, accepted: bool, after: State, max_fence: int) -> str | None:
    """Monitor history encoded in immutable state, independently of policy AST."""
    if before.phase == "done" and after.phase != "done":
        return "terminal"
    if event.kind == "complete" and accepted:
        if before.accepted_count != 0 or after.accepted_count > 1:
            return "once"
        if event.fence != before.fence:
            return "fence"
        if event.worker != before.owner:
            return "owner"
        if not before.lease_active:
            return "lease"
    if accepted != reference_decision(before, event, max_fence):
        return "wrong_decision"
    if after.fence < before.fence or after.fence > max_fence:
        return "plant_fence"
    if after.accepted_count < before.accepted_count:
        return "plant_history"
    return None


def verify(task: Task, candidate: Mapping[str, Any]) -> Verification:
    """BFS all reachable model transitions through an explicit event bound.

    Failed candidates stop at the first counterexample. Passing candidates
    exhaust all reachable states through the bound. State deduplication is sound
    here because all monitor history is in State and the event model is Markov;
    the earliest occurrence leaves the largest remaining event budget.
    """
    clean = validate_candidate(candidate)
    initial = State()
    queue = deque([(initial, ())])
    seen: dict[State, int] = {initial: 0}
    transitions = 0
    policy_decisions = 0
    event_kinds: set[str] = set()
    max_depth = 0
    frontier = 0
    while queue:
        state, prefix = queue.popleft()
        depth = len(prefix)
        if depth == task.horizon:
            frontier += 1
            continue
        for event in events(task, state):
            allowed = _interpret_validated(clean, state, event)
            after, accepted = plant_step(state, event, allowed, task.max_fence)
            violation = invariant_violation(state, event, accepted, after, task.max_fence)
            transitions += 1
            policy_decisions += event.kind in {"complete", "retry"}
            event_kinds.add(event.kind)
            max_depth = max(max_depth, depth + 1)
            step = TraceStep(state, event, accepted, after, violation)
            if violation is not None:
                return Verification(False, len(seen), transitions, prefix + (step,), violation,
                                    task.horizon, max_depth, frontier, policy_decisions,
                                    tuple(sorted(event_kinds)))
            if after not in seen:
                seen[after] = depth + 1
                queue.append((after, prefix + (step,)))
    return Verification(True, len(seen), transitions, (), None, task.horizon, max_depth, frontier,
                        policy_decisions, tuple(sorted(event_kinds)))


def replay_counterexample(task: Task, candidate: Mapping[str, Any], trace: Iterable[TraceStep]) -> bool:
    """Check a stored witness against fresh candidate execution and the monitor."""
    clean = validate_candidate(candidate)
    state = State()
    steps = tuple(trace)
    if not steps or len(steps) > task.horizon:
        return False
    for index, step in enumerate(steps):
        if step.before != state or step.event not in events(task, state):
            return False
        after, accepted = plant_step(state, step.event, _interpret_validated(clean, state, step.event), task.max_fence)
        failure = invariant_violation(state, step.event, accepted, after, task.max_fence)
        if (after, accepted, failure) != (step.after, step.accepted, step.violation):
            return False
        if failure is not None and index != len(steps) - 1:
            return False
        state = after
    return steps[-1].violation is not None


def make_tasks(seed: int, count: int, split: str = "development") -> list[Task]:
    """Generate reproducible public tasks without consuming global RNG state.

    Development, confirmation and heldout use the same scenario mixture with
    independent seed streams. Shift/stress activates all five scenarios and uses
    three workers, three lease generations and an eight-event horizon.  Splits
    do not hide the domain model; separation is experimental, not cryptographic.
    """
    if type(seed) is not int or type(count) is not int or count < 0:
        raise ValueError("seed and nonnegative count must be integers")
    if split not in {"development", "confirmation", "heldout", "shift", "stress"}:
        raise ValueError("unknown task split")
    rng = random.Random(f"lease-domain-v1:{seed}:{split}")
    # Include isolated and mixed faults, so successful repairs can transfer.
    families = [tuple(pair) for pair in combinations(GUARD_NAMES, 2)]
    families += [(name,) for name in GUARD_NAMES]
    families += [GUARD_NAMES]
    tasks: list[Task] = []
    for index in range(count):
        shifted = split in {"shift", "stress"}
        scenarios = GUARD_NAMES if shifted else rng.choice(families)
        # Existing correct guards vary independently of task scenarios. Search
        # may preserve these pre-existing checks while repairing missing checks.
        initial_mask = sum(GUARD_BITS[name] for name in GUARD_NAMES if rng.random() < 0.15)
        tasks.append(Task(
            task_id=f"{split}-{seed}-{index:04d}", split=split,
            horizon=8 if shifted else 5 + rng.randrange(2),
            workers=3 if shifted else 2, max_fence=3 if shifted else 2,
            scenarios=tuple(scenarios), initial_mask=initial_mask,
        ))
    return tasks
