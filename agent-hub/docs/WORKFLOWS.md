# Finite-state workflow repair experiments

`agent_hub.workflows` implements the restricted first research domain from the supplied recursive-research blueprint. It provides executable finite models, bounded exhaustive checking, counterexample traces, checked state-representation migration, and deterministic repair search. It performs no network requests, model calls, shell commands, source-code generation, promotion, or deployment.

This is a research component, not a proof of the deployed distributed hub. It does not establish learned search operators, transfer to unfamiliar agents, recursive improvement, or frontier-model equivalence. Those claims require the separate controlled experiments described in the blueprint.

## Explicit model language

A model contains exactly `id`, `states`, `initial`, and `transitions`:

```json
{
  "id": "one-job",
  "states": [
    {"id": "pending", "labels": {"phase": "pending", "lease_expired": false, "request": true, "response": false}},
    {"id": "running", "labels": {"phase": "running", "lease_expired": false, "request": false, "response": false}},
    {"id": "done", "labels": {"phase": "completed", "lease_expired": false, "request": false, "response": true}}
  ],
  "initial": ["pending"],
  "transitions": [
    {"id": "start", "source": "pending", "action": "start", "target": "running", "accepts_completion": false},
    {"id": "commit", "source": "running", "action": "commit", "target": "done", "accepts_completion": true},
    {"id": "retry", "source": "done", "action": "retry", "target": "pending", "accepts_completion": false}
  ]
}
```

The example deliberately contains an incorrect retry after completion. State labels are explicit facts in this finite abstraction. `accepts_completion` is the explicit logical-commit effect of traversing an edge; it is not inferred from an action's name. A trace begins with zero accepted effects, so prior completions outside the modeled execution are not automatically included. To analyze recovery histories, include their relevant acceptance transitions in the model.

Every state has the same label names and scalar types. `phase` is one of `pending`, `running`, `completed`, `failed`, `stalled`, or `cancelled`; `lease_expired` is boolean. Additional labels are bounded booleans, integers, or strings. There are no embedded guards, Python expressions, callbacks, or executable strings. Represent conditions and effects by explicitly enumerating the finite states and permitted edges.

Input limits are 64 states, 256 transitions, 16 labels per state, and 128 KB of JSON. Identifiers and scalar values are bounded. The execution horizon is 0–64 transitions. A state without outgoing transitions implicitly stutters forever without accepting another completion. Deadlock therefore cannot hide an unanswered response requirement.

## Reference checker and results

Call:

```python
from agent_hub.workflows import DEFAULT_PROPERTIES, ResearchBudget, reference_check

requirements = [
    *DEFAULT_PROPERTIES,
    {"id": "respond", "kind": "bounded_response",
     "trigger": {"request": True}, "response": {"response": True}, "within": 2},
]
evidence = reference_check(model, requirements, horizon=4,
                           budget=ResearchBudget(max_check_states=1000, max_transitions=2000),
                           dependencies={})
```

The three default requirements are:

| Kind | Trace semantics |
| --- | --- |
| `completed_never_pending` | Once a trace has visited a completed state, no later state may be pending, even after intermediate states |
| `expired_cannot_commit` | An edge from a state with `lease_expired == true` cannot have `accepts_completion == true` |
| `at_most_one_completion` | At most one accepted-completion effect may occur in a logical-job trace |

`bounded_response` uses conjunctions of exact, typed label equality. A trigger at step *t* must see its response by *t + within*, inclusively; a response at the trigger step satisfies it. A continuing trigger does not reset an older obligation. One response satisfies all currently pending obligations for that requirement. At most eight requirements are supported.

The checker is an independent reference interpreter relative to the candidate edit operators. It does not invoke an operator-supplied evaluator or execute a candidate's code. It explores every enabled edge with breadth-first search. Histories merge only when graph state, depth, completed-history flag, accepted-completion count, and every outstanding response age agree. Merging solely by graph state would be unsound for these history-dependent requirements.

Possible statuses:

| Status | Meaning |
| --- | --- |
| `bounded_verified` | All modeled executions through the exact horizon satisfy the requirements, with no unresolved response obligation at the boundary |
| `counterexample` | A concrete violating trace was found; checking may stop at this witness |
| `horizon_inconclusive` | No violation was found, but a response obligation remains unresolved beyond the chosen horizon |
| `budget_exhausted` | Full requested verification did not complete; this is never treated as success |

Counterexamples record the requirement, states, labels, transition actions/effects, and step numbers. Breadth-first exploration returns a shortest discovered witness, with deterministic ordering. Positive evidence records model/property hashes, dependencies, checker version, horizon, and checking work. A finite horizon does not establish unbounded liveness, timing under real network delays, or faithful abstraction of an implementation.

## Bounded repair synthesis

The caller supplies an explicit approved list of possible edits. Supported operations are `remove_transition`, `redirect_transition` to an existing state, and `disable_acceptance`. Candidates cannot edit state labels, initial states, requirements, checker code, or resource limits.

```python
from agent_hub.workflows import synthesize

result = synthesize(
    model,
    [{"kind": "remove_transition", "transition_id": "retry"}],
    requirements,
    horizon=4,
    max_edits=1,
    budget=ResearchBudget(max_candidates=10, max_check_states=1000, max_transitions=2000),
    dependencies={},
)
```

For the example, the baseline supplies a counterexample, and removing `retry` supplies a candidate with fresh bounded verification evidence. The result includes both attempts and their charged work. `found` means a repair met the supplied finite-model requirements; it does not authorize a software change.

Search orders the approved edits deterministically and enumerates combinations by increasing edit count, including the unchanged baseline first. Limits are 32 approved edits and at most three edits per candidate. Conflicting edits on one transition are rejected atomically. Syntactically valid but inapplicable edits count as failed candidate attempts. The source model remains unchanged.

Requirements must capture desired functionality. Safety alone can reward removing all progress. Include response requirements when progress matters. Even the supplied requirements cannot guarantee unexpressed behavior: changing a declared acceptance effect is a model edit, not evidence that a real implementation preserves its intended output semantics. A trusted implementation mapping and real acceptance tests remain necessary.

## Resource accounting and exact cache reuse

`ResearchBudget` tracks candidate attempts, checked state/monitor configurations, attempted transition expansions, and cache hits. State and transition work from failed candidates remains charged. Candidate attempts still count when exact prior verification is reused. Exhausting any active limit stops search; no unchecked candidate is returned as a repair.

The counters are deterministic work units, not dollar, CPU-time, or memory measurements. JSON normalization, hashing, and bookkeeping are bounded by the input/cache limits but are not converted into priced compute. A full economic experiment must additionally measure elapsed time, memory, compilation/execution expense, provider charges if introduced, and failed research costs. This implementation makes zero model calls.

`CheckCache(max_entries=128)` reuses only an exact normalized full model, requirements, horizon, dependency SHA-256 manifest, checker version, and deadlock semantics. Changing even an unreachable state or an unrelated declared dependency invalidates the entry. Budget-exhausted checks are never cached as evidence. Returned values are defensive copies; reused evidence preserves its original checking-work record while reporting zero new exploration work.

The dependency manifest is explicit provenance supplied by the trusted experiment controller. No cone-of-influence reduction is implemented or claimed. All checks fall back to the full finite model; dependency annotations alone are not a proof that reduced checking would preserve a property. The cache is bounded and in-memory, not a shared persistent trust store. Change the checker version when its semantics change.

## Checked representation migration

`migrate_states(model, mapping, budget=None)` renames every state through an explicit bijection and returns the migrated model plus equivalence evidence. `verify_migration(original, migrated, mapping, budget=None)` independently compares the complete initial-state set, typed state labels, and all transition endpoints, actions, IDs, and acceptance effects. It includes unreachable states. A boolean label and integer label are different, even when Python's ordinary equality would equate `true` and `1`.

This establishes a label/action/effect-preserving graph isomorphism for this restricted representation. It is a sufficient semantic-preservation check, not a general solver for program equivalence. Migration never changes the property meanings. When supplied, the shared research budget charges every compared state and transition; exhaustion prevents a migration from being accepted.

## Verification and remaining experiment

Run `python -W error::ResourceWarning -m unittest discover -s tests -p test_workflows.py -v`.

The executable tests cover all three safety counterexamples, history-sensitive state merging, inclusive response bounds, nondeterministic paths, deadlocks, insufficient horizons, exhausted budgets, exact cache validity, bijection/effect/label checks, and a small repair found with baseline counterexample and fresh verification evidence.

The next research step is to compare frozen edit/search procedures on fresh workflow families under equal total budgets, then transfer a revised method without its answer cache or private experimental history. This module supplies a controlled domain and checker for that experiment; it does not claim that the experiment has already succeeded or that the resulting method is recursively self-improving.
