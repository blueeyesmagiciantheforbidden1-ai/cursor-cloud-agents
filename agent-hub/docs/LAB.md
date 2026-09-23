# Private local lab CLI

`python -m agent_hub.lab` makes the existing context compiler, task ledger and research archive usable from an operator terminal. It uses Python's standard library and performs no model calls, candidate-code execution, dispatch, approvals, merges or deployments. These local records are not yet connected to the hub dashboard, HTTP API or MCP. Provider identities and authenticated approval wiring remain separate work.

Run it from the project directory with an explicit **existing private absolute state directory outside the source tree**:

```powershell
python -m agent_hub.lab --root C:/RunCrewPrivate/lab status
python -m agent_hub.lab --root C:/RunCrewPrivate/lab context compile --input C:/RunCrewPrivate/requests/context.json
python -m agent_hub.lab --root C:/RunCrewPrivate/lab contract register --input C:/RunCrewPrivate/requests/contracts.json
python -m agent_hub.lab --root C:/RunCrewPrivate/lab contract get --input C:/RunCrewPrivate/requests/task.json
python -m agent_hub.lab --root C:/RunCrewPrivate/lab research add-candidate --input C:/RunCrewPrivate/requests/candidate.json
python -m agent_hub.lab --root C:/RunCrewPrivate/lab research create-epoch --input C:/RunCrewPrivate/requests/epoch.json
python -m agent_hub.lab --root C:/RunCrewPrivate/lab research preregister --input C:/RunCrewPrivate/requests/trial.json
python -m agent_hub.lab --root C:/RunCrewPrivate/lab research record --input C:/RunCrewPrivate/requests/pair.json
python -m agent_hub.lab --root C:/RunCrewPrivate/lab research finalize --input C:/RunCrewPrivate/requests/finalize.json
```

The paths above are examples to provision, not directories created by installation. The CLI never defaults to the current directory or the credential workspace. Provision the state and request directories with access limited to the trusted operator/controller. On Windows, configure and inspect their ACLs separately: the CLI does **not** verify Windows ACL ownership or access. On POSIX it rejects a state directory with group/other permission bits. Use a persistent local controller disk, not a shared VPS mount or Cloud Run's ephemeral filesystem.

The directory contains `context/` (source/navigation/manifest blobs and cache entries), `contracts.sqlite3`, and `research.sqlite3`, plus SQLite sidecars when needed. Context blobs and candidate artifacts can contain private source; the summary-only terminal output does not make storage public-safe. Protect backups too. Explicitly selected files are not a secret-content scanning service.

All request files must be absolute, regular UTF-8 `.json` files of at most 262,144 bytes. Unknown top-level fields, duplicate JSON keys, nonfinite numbers and nesting beyond 48 levels are rejected. Symlinks/junctions in relevant paths and excluded credential directories such as `API_KEYS`, `.git`, `.ssh` and `.gcloud` are rejected. The underlying modules retain their narrower schema, file, budget and record limits. This is a trusted local operator interface; another process with write access to its files can tamper with state or race path checks.

## Output and status

Successful commands print one JSON object and exit 0:

```json
{"ok": true, "result": {"record_only": true}}
```

The `result` fields depend on the command. Failures print `{"ok": false, "error": "static_error_code"}` and exit 2. Input values, artifact text and exception tracebacks are not echoed; normal `--help` is human-readable. Module validation failures use `context_validation_failed`, `contract_validation_failed` or `research_validation_failed`. Inspect the request against the linked module documentation, rather than putting sensitive request bodies into public logs.

`status` opens existing databases in read-only mode without initializing absent stores. It returns:

- Context: blob/cache-file counts and aggregate bytes. Cache counts include expired entries; this is storage inventory, not a hit rate or integrity scan.
- Contracts: actual task-state counts, attempt/evidence/approval/promotion counts, and recorded spending/reservations in micro-USD. It does not query provider balances or bill payment systems.
- Research: counts by record kind, registered/completed/pending trials, recorded pairs and reserved holdouts. Pending includes abandoned incomplete trials; no successful outcome is inferred from their existence.

Status queries have finite work limits and directory inventories stop after 50,000 entries per directory. A corrupt, incompatible or inaccessible store returns an error rather than reporting invented zero counts. Each database snapshot is internally consistent; the three modules are not read in one cross-store transaction. Read-only SQLite connections may use existing WAL/SHM coordination files. Status does not recompute every stored artifact hash, assert workers are healthy, or establish provider/model participation.

## Context requests

This complete synthetic example compiles one explicitly selected local source file:

```json
{
  "repo_root": "C:/RunCrewPrivate/synthetic-repository",
  "selected_files": ["example.py"],
  "repo_revision": "synthetic-demo-revision",
  "toolchain": {"python": "synthetic-demo-toolchain"}
}
```

Required fields are `repo_root`, `selected_files`, `repo_revision` and `toolchain`. Optional fields are `allowed_files`, `selected_symbols`, `test_files`, `config_files`, `environment` and `policy` (a `ContextPolicy` field mapping). The repository root must be absolute. See [CONTEXT.md](CONTEXT.md) for exact source selection, import expansion, AST/navigation and cache behavior.

The output contains only `artifact_hash`, `cache_key`, `cache_hit`, `files` and `input_bytes`. The complete context is retained in the private blob store. Revision/toolchain labels are supplied by the caller, not verified by running Git or the toolchain.

## Contract requests

`contract register` accepts `{"contracts": [<one to 64 complete contracts>]}`. Use the immutable schema in [CONTRACTS.md](CONTRACTS.md): exact repository revision and file scope, dependencies, explicit builder/reviewer model and effort, permissions, spending/attempt/deadline bounds, acceptance-test definition hashes, and human approval/independent review requirements. This command registers facts and grants no executable authority.

`contract get` accepts:

```json
{"task_id": "synthetic-example-task"}
```

Registration returns a count and summaries; lookup returns one summary. Summaries contain `contract_digest`, `state`, `fence`, `spent_microusd`, `reserved_microusd`, `evidence_digest` and `candidate_revision`. Full contracts and arbitrary task labels are omitted from terminal output. Keep your task-ID-to-digest association privately. The underlying `TaskLedger` API remains available to a trusted controller that needs the complete contract.

## Research requests

The CLI passes explicit request fields to the existing archive methods and returns bounded summaries. See [RESEARCH.md](RESEARCH.md) for the statistical rule, constraints, lineage and broader Python API.

| Command | Required request fields | Optional fields |
| --- | --- | --- |
| `add-candidate` | `kind`, `artifact` | `parents`, `proposer`, `niche`, `metadata`, `expected_content_hash` |
| `create-epoch` | `policy` | none |
| `preregister` | `epoch_id`, `candidate_id`, `baseline_id`, `holdout_ids`, `independent_evaluator` | `candidate_overhead`, `baseline_overhead`, `search_id` |
| `record` | `trial_id`, `holdout_id`, `candidate_score`, `baseline_score`, `candidate_cost`, `baseline_cost`, `candidate_latency_ms`, `candidate_robustness`, `contracts_pass`, `guard_deltas`, `evaluator_digest`, `protected_contract_digest`, `candidate_content_hash`, `baseline_content_hash` | none |
| `finalize` | `trial_id` | none |

`kind` is `task_agent` or `improvement_procedure`. A candidate's `artifact` is bounded JSON and is never executed. Candidate output contains its immutable `id`, `content_hash`, kind and parent count; proposer, niche, metadata and artifact content are omitted. Save returned hashes for later requests.

`create-epoch` requires an exact policy mapping with `name`, `sample_size`, `max_candidates`, `alpha`, `meaningful_delta`, `budget_per_arm`, `evaluator_digest`, `protected_contract_digest` and `guard_names`. Preregister exactly that fixed number of fresh paired holdout IDs before supplying any measurements. `preregister` returns the trial ID and pair count, without revealing holdout/evaluator labels. One `record` invocation supplies one measured pair; it returns the number of accepted pairs, never an interim statistical bound. Each invocation commits separately, so a later rejected pair does not roll back prior accepted measurements.

`finalize` refuses incomplete trials. A complete result includes the multiplicity-adjusted one-sided Hoeffding lower bound, fixed meaningful improvement threshold, measured costs, contract/guard checks, metrics, `eligible` and reason codes. `record_only` is always true. An eligible result is statistical evidence under stated assumptions, not permission to merge/deploy or a theorem that the system will improve indefinitely. Guard labels and per-case measurements remain private. Two perfect synthetic observations, for example, are insufficient to pass a conservative bound at ordinary alpha; the end-to-end test checks that the CLI does not turn them into a promotion.

Quality-diversity, Pareto, descendant productivity, intervention memory, 2x2 interaction and tested-abstraction functions are available in `ResearchArchive`; this deliberately small CLI does not wrap every helper. Search registration is currently a Python API operation, so only supply `search_id` if that search already exists.

## Evaluation boundaries and next integration

Measurements and their provenance come from a trusted evaluator. The archive checks that supplied hashes, schema, costs and fixed rules agree; it does not independently meter a model or prove the evaluator independent. Before real trials, account for generation, retries, failed attempts, speculative branches, context compilation/retrieval and evaluation in each arm's total. Use the declared overhead fields for costs outside per-case execution. Matching a headline cap while omitting that work is not equal-budget evidence. Do not pad costs with invented spending to satisfy the equality check.

Unique holdout IDs alone do not establish fresh independent data: dataset-family overlap, repeated scaffolding, retrieval caches, shared feedback and renamed samples can still leak test information. Keep confirmatory data isolated, freeze preprocessing/evaluator policy, track dataset-family/provenance and cache state outside the candidate's access, and explicitly test contamination risks. The current archive prevents identifier reuse within one archive; it does not detect those semantic overlaps or enforce a lifetime alpha schedule across arbitrary new epochs/archives.

For future behavioral diversity metrics, compare what variants actually retrieve or do. For example, retrieval-set Jaccard distance (`1 - |A intersect B| / |A union B|`, with identical empty sets at distance 0) can describe observed retrieval diversity. Configuration edit distance alone does not show different behavior. Retrieval-set distance is a recommendation for later evaluation design, not an implemented metric or evidence of higher quality.

Connecting these modules to the hub needs an authenticated trusted controller, verified provider/actual-model receipts, independent evaluation and explicit approval-to-revision bindings. This CLI intentionally supplies none of those missing trust claims. No public endpoint or private ChatGPT account receives access through its installation.

## Offline verification

```text
python -W error::ResourceWarning -m unittest discover -s tests -p test_lab.py -v
```

Tests launch the actual CLI in subprocesses with temporary synthetic source/JSON/state. They check read-only empty status, real context caching, immutable contract registration and summaries, full research registration/measurement/finalization, incomplete-trial refusal, false-promotion prevention, finite input limits, non-echoing errors and actual stored counters. A symlink rejection test skips on Windows accounts that cannot create symlinks. These fixtures are not provider outputs or evidence of real-world improvement.
