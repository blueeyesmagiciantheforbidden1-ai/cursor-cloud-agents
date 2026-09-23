# Bounded research and improvement archive

`agent_hub.research.ResearchArchive` implements a local experiment ledger and decision rule for the user's two architecture documents. It records immutable candidates, matches evidence to frozen versions, charges fixed budgets, and identifies candidates eligible for a separate promotion process. It does not run candidate code, call providers, merge, deploy, modify its evaluator, or run a background optimization loop.

This is a facility for testing proposed improvements. No measured improvement of the real hub, Retina integration, automatic theorem discovery, model-weight adaptation, or general recursive intelligence claim follows from the synthetic tests.

## Candidate and evidence boundary

Candidates have a kind (`task_agent` or `improvement_procedure`), JSON artifact, SHA-256 content hash, existing parent IDs, proposer identity, niche, and metadata. The candidate ID hashes the entire immutable record; changing source, lineage, or metadata creates a new identity. Store actual source revisions, model/toolchain identifiers, artifact hashes, and relevant environment details in the artifact or metadata. An artifact can describe code or a workflow, but the archive never executes it.

Records are bounded canonical UTF-8 JSON. SQLite write transactions serialize registrations, globally unique holdout reservations, pair recordings, and decisions. Update/delete triggers prevent accidental in-place changes. Reads verify stored content hashes. These measures protect ordinary API operations; they are not a defense against an administrator who can rewrite the database and its schema.

Use one controller with a persistent local disk. Do not mount the database for direct writes by agents or use Cloud Run's ephemeral filesystem. This module is not wired to the current HTTP/MCP hub and has no Firestore backend yet. Expose it through an authenticated controller only after trusted evaluator identities, artifact storage, retention, and deployment integration are ready.

## Fixed evaluation epochs

`create_epoch(policy)` requires these fields:

| Field | Meaning |
| --- | --- |
| `name` | Unique human-readable epoch name. |
| `sample_size` | Fixed number of paired observations, 1–1024. |
| `max_candidates` | Maximum confirmatory candidates, 1–128; incomplete and failed trials count. |
| `alpha` | Total error allowance for this epoch, between 1e-9 and 0.25. |
| `meaningful_delta` | Required utility gain, fixed before evaluation, in [0,1]. |
| `budget_per_arm` | Fixed positive integer cost units for each candidate and baseline arm. |
| `evaluator_digest` | SHA-256 of the independently controlled evaluator and its configuration. |
| `protected_contract_digest` | SHA-256 of the protected acceptance obligations. |
| `guard_names` | Required no-regression measurements, 1–16 names. |

The full policy is content-addressed. A changed policy under the same name is rejected. A different evaluator may be registered in a new epoch for externally anchored evaluation, but protected contracts and guard names cannot be changed anywhere in this archive. Agent-generated challenges can be stored as new candidate artifacts and exploratory evidence; they cannot silently rewrite the active grading rule or protected tests.

The API checks digests, not whether a proposed evaluator is scientifically better. A responsible operator must independently review an evaluator change and its external anchor before opening a new epoch. Cross-epoch statistical guarantees require a separately preregistered allocation whose epoch alpha values sum to the desired total; the implementation currently enforces the limit within each epoch only.

## Preregister, measure, then decide

1. Freeze the candidate and baseline with `add_candidate`.
2. Call `preregister_trial(epoch_id, candidate_id, baseline_id, holdout_ids, independent_evaluator=...)` before looking at audit results. The sample must contain exactly the fixed N unique IDs. The named evaluator cannot equal either proposer.
3. Optionally declare `candidate_overhead` and `baseline_overhead` at registration. Include proposal/search, failed attempts, context preparation, and other attributable work in those actual costs; do not ignore them because they happened before task evaluation.
4. Supply each matched observation through `record_pair`. It requires candidate and baseline scores in [0,1], actual integer costs, candidate latency, candidate robustness in [0,1], a boolean contract result, every guard delta in [-1,1], and the exact evaluator, protected-contract, and candidate/baseline content digests.
5. Call `finalize_trial` once all N pairs are recorded. Earlier calls fail without an interim bound or promotion. Repeated calls return the same immutable decision.

The preregistered score schema defines each paired improvement as `candidate_score - baseline_score`, hence a bounded vector in [-1,1]. Scores are observed outcomes, not values guessed during preregistration. Costs use one declared unit consistently, such as microdollars or normalized compute units. Each arm's measured cost plus its declared overhead must equal the fixed per-arm budget. Overruns are rejected; unequal or underbudget completed trials cannot qualify. Do not fabricate costs to make a comparison pass.

Holdout identifiers cannot be reused across candidates, epochs, aborted registrations that committed, or exploratory interaction records in the same archive. Reservations survive failed/incomplete trials. Renaming the same task to make it look fresh is outside what identifier uniqueness can detect. The evaluator must control the actual task registry, sample selection, hidden outputs, and fresh independent evaluation. An identity string alone does not establish evaluator independence.

## Acceptance calculation and its limits

For fixed N independent paired deltas `d_i` in [-1,1], the implemented one-sided Hoeffding bound is:

```text
alpha_candidate = alpha_epoch / max_candidates
LCB = max(-1, mean(d_i) - sqrt(2 * log(1 / alpha_candidate) / N))
```

The range length is two. The factor in this formula must not be replaced by the bound for observations in [0,1]. The union bound covers at most `max_candidates` trials in an epoch, including adaptively proposed candidates when each frozen candidate receives genuinely fresh independent evaluation conditional on earlier history. Pairing permits dependence between the baseline and candidate scores for the same task; the bound requires independent pairs and an appropriate fixed sampling design. These assumptions are not verified by SQLite.

Eligibility requires all protected contracts pass, exact equal total budgets, every observed guard delta is nonnegative, and `LCB > meaningful_delta`. A positive sample mean alone is insufficient. The guard is an empirical veto against observed regressions, not a proof of zero regression on every possible input. There is no repeated-peeking interval, sequential stopping rule, adaptive holdout reuse, or automatic claim of universal correctness.

`eligible` records evidence for the controller's promotion gate. It never grants permission, changes a budget, merges a patch, or deploys anything. Approval must still bind to the exact verified resulting revision under the controller's separate policy.

## Archive diversity and descendants

`pareto_frontier(epoch_id, kind=...)` retains valid completed candidates not dominated on correctness, evaluation cost per matched case, latency, and robustness. Correctness and robustness are maximized; cost and latency are minimized. Evaluation cost per case excludes preregistered generation overhead, while the validity decision still includes that overhead in the equal total budget. Both figures are retained so a cheaper runtime cannot hide expensive research.

`quality_diversity(epoch_id)` keeps one candidate per `(kind, niche)`, ranking correctness, robustness, evaluation cost, and latency. A valid specialist can stay in the archive without passing the release bound. Niches are declared immutable labels supplied by the experimenter; this is not learned behavioral clustering. Scalar ranking within a niche and Pareto retention across candidates are explicit, inspectable policies.

`register_search(epoch_id, procedure_id, baseline_id, max_descendants)` freezes a search campaign. The procedure must be an improvement-procedure candidate and the baseline a task-agent candidate. Each campaign trial must name `search_id`, share the fixed baseline and epoch, and have the procedure in its ancestry. Every descendant receives the same two-arm allocation; total budget is `2 * budget_per_arm * max_descendants`.

`metaproductivity(search_id)` withholds final best-descendant figures until the fixed number of trials is complete. It reports actual total costs, best valid descendant correctness, best statistically eligible lower bound, and that certified gain per fixed budget unit. Failed trials remain in the campaign. This is an observed search outcome, not an unbiased estimate of expected future improvement or causal credit across arbitrary generations. Comparable campaigns need the same epoch, baseline, total budget, and task-sampling design.

## Intervention memory, interactions, replay, and abstractions

- `record_intervention` links a completed trial to a structured change, hypothesis, conditions, where it helped, and where it failed. The narrative remains a hypothesis; the measured decision is separately linked.
- `record_interaction` accepts frozen baseline/A/B/AB candidates, matched block IDs, four full score vectors, and equal measured costs. It archives `U(AB)-U(A)-U(B)+U(baseline)` for every block and their mean. This is an exploratory descriptive contrast, not a confirmatory release test. Its blocks are consumed and cannot later become audit holdouts. The caller must establish controlled intervention isolation and matched experimental conditions.
- `replay(action_ids)` returns only outcomes for recorded interventions. Unknown actions fail by default; `strict=False` returns coverage and explicitly lists unsupported actions without predicting their outcomes. It executes no policy, invents no counterfactual reward, and makes no extrapolation outside the recorded actions.
- `record_abstraction` indexes a reusable candidate with an explicit interface and a source-bound test artifact. It requires a valid completed trial and named passing checks matching the protected contract digest. It stores the test-artifact hash and states that the tests were supplied externally. The archive is not a test executor or theorem prover.

## Minimal synthetic use

The following is a toy demonstration with synthetic measurements, not evidence of real performance:

```python
from agent_hub.research import ResearchArchive, content_hash

archive = ResearchArchive("research.sqlite3")
policy = {
    "name": "synthetic-v1", "sample_size": 64, "max_candidates": 8,
    "alpha": .05, "meaningful_delta": .1, "budget_per_arm": 64,
    "evaluator_digest": content_hash({"evaluator": "synthetic-only"}),
    "protected_contract_digest": content_hash({"contract": "synthetic-only"}),
    "guard_names": ["security"],
}
epoch = archive.create_epoch(policy)
base = archive.add_candidate("task_agent", {"version": 1}, proposer="builder")
candidate = archive.add_candidate("task_agent", {"version": 2},
                                 parents=[base["id"]], proposer="builder")
trial = archive.preregister_trial(epoch["id"], candidate["id"], base["id"],
    [f"synthetic-{i}" for i in range(64)], independent_evaluator="test-fixture")
for holdout in trial["holdout_ids"]:
    archive.record_pair(trial["id"], holdout,
        candidate_score=.9, baseline_score=.1, candidate_cost=1, baseline_cost=1,
        candidate_latency_ms=10, candidate_robustness=.9,
        contracts_pass=True, guard_deltas={"security": 0},
        evaluator_digest=policy["evaluator_digest"],
        protected_contract_digest=policy["protected_contract_digest"],
        candidate_content_hash=candidate["content_hash"],
        baseline_content_hash=base["content_hash"])
print(archive.finalize_trial(trial["id"])["eligible"])
```

Run validation with `python -W error::ResourceWarning -m unittest discover -s tests -p test_research.py -v`. Tests cover hash mismatch, evaluator drift, immutable obligations, concurrent holdout reservation, incomplete trials, false promotion, contract and guard failures, budgets including overhead, diversity/dominance, descendant accounting, replay coverage, interaction arithmetic, and tested abstraction records.

The record ceiling is 20,000 and record size is 256 KiB. Reaching these bounds fails explicitly. Archive rotation must preserve the global holdout registry and the error-allocation history; a fresh database is not permission to reuse audits or reset statistical budgets.

## Research concepts not implemented as learning algorithms

The supplied documents describe deeper research directions. This module provides lineage and evidence facilities for investigating them, not implementations of differentiable meta-gradient propagation, stochastic credit assignment through arbitrary program edits, learned definition generators, resolution/extended-resolution proof discovery, DRAT checking, self-certified axioms, model-weight training, or a learned replay search policy. It also does not establish the documents' conditional acceleration claims experimentally. Those require specific domains, independently checked evaluators, controlled datasets, algorithms, and measured experiments before any success claim.
