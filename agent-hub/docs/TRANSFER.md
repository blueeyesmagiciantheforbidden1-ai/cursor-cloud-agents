# Frozen improvement-procedure transfer experiments

`agent_hub.transfer` implements a bounded evidence archive for the transfer experiment in the two user-supplied September 20 architecture notes. It tests whether a revised **improvement procedure** produces better descendants from fresh starting controllers under declared budgets. It records methods, independent units, observations and eligibility decisions. It does not execute an improvement procedure, prove supplied observations truthful, run a local model, modify model weights, or establish autonomous recursive improvement.

The deployment target remains a dedicated cloud controller and cloud workers. This module creates no personal-PC observation feed. It is not wired into a public endpoint, dashboard, worker loop or ChatGPT app. Its SQLite file belongs on a private persistent controller disk, not Cloud Run's ephemeral filesystem or a filesystem shared directly with workers. A later authenticated controller must own dispatch, full spending receipts, result provenance and approval; research workers must not be able to edit the database or evaluator.

## What is frozen

`TransferArchive(path).freeze_method(snapshot)` accepts exactly five SHA-256 references:

```python
snapshot = {
    "code_digest": code_digest,
    "prompts_digest": prompts_digest,
    "dependencies_digest": dependencies_digest,
    "parameters_digest": learned_parameters_digest,
    "configuration_digest": configuration_digest,
}
method = archive.freeze_method(snapshot)
```

The returned `id` hashes the entire immutable method record; `snapshot_digest` hashes the snapshot mapping. Changing any reference creates another method. An empty set of learned parameters still needs an explicit artifact digest. The archive holds references, not the referenced code or weights. A trusted executor must verify those artifacts before use.

`preregister(spec)` freezes the following exact schema before any results:

| Level | Fields |
| --- | --- |
| Experiment | `name`, `incumbent_id`, `candidate_ids`, `delta`, `sample_size`, `conditions` |
| Each condition | `id`, `task_family_digest`, `research_horizon`, `research_budget_per_arm`, `execution_budget_per_arm`, `resource_unit`, `eta`, `evaluator_digest`, `common_library_digest`, `routing_policy_digest`, `units` |
| Each independent unit | `id`, `start_controller_digest`, `task_batch_digest`, `randomness_digest`, `holdout_ids` |

`candidate_ids` declares K distinct frozen alternatives to the frozen incumbent. Every one of the A conditions has exactly m units (`sample_size`). A condition fixes its task family, research horizon, research/execution resource budgets, common starting capability library, routing and evaluator. The evaluator digest must bind its quality function, aggregation within a task batch, protected requirements and stopping rule. All conditions use one explicitly declared research-accounting unit; adding unlike money, time or compute measurements is rejected.

Each unit represents **one independent starting-controller/task-batch/randomness draw**. Candidate and incumbent arms receive copies of that unit. Hundreds of tasks evaluated on the same resulting descendant contribute to its one quality score, not hundreds of independent lineages. The incumbent arm is stored once per condition/unit and shared across its K paired comparisons. Correlation between comparisons caused by this shared baseline is permitted by the union bound; independence is required across the m draws used for each mean.

All declared units are reserved atomically before results arrive. The archive permanently reserves unit IDs, hashes of the starting-controller/task-batch/randomness tuple, and holdout IDs. Renaming a unit while retaining that tuple is rejected. Reusing an experiment name is rejected. Identical units/holdouts are also prohibited across conditions and across subsequent trials in this archive; paired arms intentionally share their already reserved unit. An unsuccessful registration rolls back its reservations.

These checks do not prove semantic independence. Overlapping dataset families, reused scaffolding, shared private answers, retrieval caches or history can contaminate seemingly different identifiers/digests. Provision genuinely fresh protected draws and keep their contents inaccessible to candidate authors. Reservations are archive-local, so using another database is not a legitimate way to make reused data fresh. Candidate revisions and adaptively selected follow-up experiments need new confirmation data and a separately justified error-spending policy.

## Measurement and full budgets

Record one arm with:

```python
receipt = archive.record_result(
    trial_id, method_id, condition_id, unit_id,
    quality=measured_quality,                  # finite value in [0, 1]
    descendant_digest=verified_descendant_digest,
    research_costs=measured_cost_components,
    research_steps=measured_research_steps,
    execution_cost=measured_final_evaluation_cost,
    evaluator_digest=frozen_evaluator_digest,
    routing_policy_digest=frozen_router_digest,
    common_library_digest=frozen_starting_library_digest,
    research_disabled=True,
    private_state_transferred=False,
    contracts_pass=protected_checks_passed,
)
```

`research_costs` requires every key in `RESEARCH_COSTS`: `search_execution`, `failed_search`, `model_calls`, `compilation`, `verification`, `retries`, `speculation`, `context`, `migration`, `maintenance`, `other`. Each is a measured nonnegative integer in the condition's declared accounting unit. They are **disjoint accounting categories**, not overlapping subtotals: for example, a paid model retry is charged once under the preregistered partition, rather than once as a model call and again as a retry. Zero is appropriate only when no such cost occurred. The independent controller must reconcile the partition against complete source receipts.

The sum includes unsuccessful work and overhead, not only the winning descendant. It cannot exceed the condition's per-arm research budget; reported steps cannot exceed its horizon. The final execution cost cannot exceed the separate fixed execution cap. Both candidate and incumbent must actually match the declared total research budget for that comparison to be eligible. Underspending is recorded but fails this exact-budget protocol; do not invent spending or pad costs to pass it. If a different equal-cap design is desired, specify and validate a different protocol before collecting confirmation data.

The total declared research budget is `m * (K + 1) * sum(per_condition_budget)`: the incumbent is charged once per unit/condition, and every candidate is charged. The decision also reports actual total research cost and final execution cost separately. Model-provider balances are not queried. An overrun is rejected as evidence; the real controller's spending ledger must still retain the actual charge and reconciliation state. Rejected evidence is not permission to rerun a paid operation.

The final descendant-quality evaluation must disable further research. All arms start with the same frozen L0 capability library. Private task answers, result caches and experimental history must not accompany the revised method. Declared learned parameters may transfer only as part of its frozen method snapshot. The two boolean fields record evaluator attestations of these conditions; this archive cannot independently inspect the execution environment or detect concealed state in an artifact.

`record_result` and `progress(trial_id)` return counts only. Duplicate arm/unit results are rejected. The public API provides no interim means, bounds or eligibility. This is an interface discipline, not a secrecy guarantee against an operator with direct SQLite access or knowledge of submitted scores. Methods, evaluator and stopping rules must remain frozen regardless of any privately observed partial results.

## Confirmatory decision

For each candidate j and condition a, the measured paired difference is `D_i,j,a = q_i,j,a - q_i,0,a`, bounded in `[-1, 1]`. After **every** declared `(K + 1) * A * m` arm is recorded, `finalize(trial_id)` computes:

```text
epsilon = sqrt(2 * ln(2 * K * A / delta) / m)
```

Under the preregistered bounded and independent-unit sampling assumptions, the two-sided Hoeffding bound plus a union bound gives simultaneous coverage of at least `1 - delta` across the K×A mean differences. It needs neither independence between candidates nor independence between condition-wise estimates. It does require the declared sampling interpretation for each expected mean; the code cannot establish that interpretation from submitted labels.

A method is eligible only if **every required condition** satisfies `mean_D - epsilon >= eta_a`, both arms satisfy the exact full research budget, and all protected contract checks pass. Each `eta_a` is frozen in `[-1, 1]`; a negative value explicitly permits that measured tradeoff for the condition. Positive margins can demand a meaningful gain. Protected contracts cannot be waived by a negative margin.

Displayed intervals are clipped to the natural `[-1, 1]` range. The eligibility comparison uses the specified **unclipped** `mean_D - epsilon`. A favorable condition cannot compensate for another required condition failing. An unfinished or abandoned arm keeps the whole experiment incomplete, including candidates that have already finished all their own cells. Finalization is immutable and idempotent, and always returns `record_only: true`. Eligibility does not merge, deploy, grant permissions or supply human approval.

`required_units(K, A, delta, radius)` computes the conservative sample-size requirement. With K=4, A=3, delta=0.05 and radius=0.05, it returns **4,940 independent units per condition**. The implementation test checks the arithmetic. This is a design calculation, not evidence that such a study was run. Repeating small experiments until one passes has no lifetime error guarantee here.

## Exploratory factorial pilot

`record_exploratory_2x2(...)` accepts frozen incumbent/revised method IDs, initial/accumulated library digests, matched units, and four equally sized score vectors:

| Score key | Method | Library |
| --- | --- | --- |
| `m0_l0` | incumbent | initial |
| `m0_l1` | incumbent | accumulated |
| `m1_l0` | revised | initial |
| `m1_l1` | revised | accumulated |

It records the mean descriptive interaction `m1_l1 - m1_l0 - m0_l1 + m0_l0`, with `confirmatory: false` and `eligible: false`. This helps distinguish accumulated tools from a transferable research method, but does not by itself establish causal attribution, equal budgets or a statistical guarantee. Its units and holdouts become unavailable for confirmation. Pilot results may inform method development; freeze the resulting method and collect fresh independent confirmation data afterward.

## Economic reuse test

`economic_break_even(...)` requires costs in disjoint `discovery`, `unsuccessful_research`, `migration`, `maintenance`, and `verification` categories, baseline and candidate costs per future task, a reuse count, an explicit common money unit, and `quality_comparable=True`.

```python
result = economic_break_even(
    costs={"discovery": 100, "unsuccessful_research": 40, "migration": 10,
           "maintenance": 20, "verification": 30},
    baseline_per_task=3, candidate_per_task=1, reuses=120,
    unit="USD", quality_comparable=True,
)
# Synthetic arithmetic only: full cost 200, break-even 100 tasks, net savings 40.
```

It computes `net_savings = reuses * (baseline_per_task - candidate_per_task) - full_discovery_cost`. For positive savings it returns the fractional break-even reuse level and its ceiling in whole tasks. Without positive savings, there is no positive-savings break-even claim. A numeric overflow in the break-even level is reported as unavailable rather than infinity. For operational money ledgers, integer micro-USD avoids ordinary floating-point price-rounding ambiguity.

When comparable quality is not established or a common money unit is absent, the function returns `defined: false` and no scalar economic result. It does not convert quality, latency, memory or heterogeneous resource vectors to dollars. Report those dimensions separately unless a justified conversion is declared elsewhere. Include all failed studies and amortized maintenance in the discovery inputs; the helper does not retrieve those costs automatically or guarantee future savings.

## Bounds and verification

Supported limits are K≤16, A≤8, m≤8,192, at most 100,000 arm cells, 8 MiB per JSON record, 128 holdout IDs per unit and 200,000 stored records per archive. Serialization and SHA-256 checks detect record changes through the API; append-only SQLite triggers reject updates/deletes. A database administrator can remove triggers or replace storage, so this is not a tamperproof audit system. Runtime, filesystem permissions, data provenance, distributed leases and authentic provider/evaluator identities remain controller responsibilities.

Offline verification:

```text
python -W error::ResourceWarning -m unittest discover -s tests -p test_transfer.py -v
```

The tests use synthetic method hashes, units, costs and quality values. They check the exact bound/sample size, frozen identities, immutable records, no partial decisions, every-condition eligibility, duplicate/reused units and holdouts, renamed-unit fingerprints, drift and budget rejection, contract prerequisites, exploratory-data exclusion, restart stability and economic accounting. They show that the archive applies its protocol; they are not measurements of model capability, a trained improvement operator or a deployed autonomous system.
