# Task contracts and durable evidence

`agent_hub.contracts` is a standalone local controller component. It is not connected to the existing room queue, Firestore, remote workers, GitHub, or production deployment. It executes no commands and calls no models. Its passing tests establish ledger behavior using explicitly synthetic receipts; they are not evidence of a real coding-agent recovery exercise.

The controller owns contract creation, provider dispatch, trusted verification, authentication, and human approval. Builders must never receive direct database access or access to the approval/reconciliation APIs. In particular, the `approved_by` argument records an identity supplied by the trusted caller; it does not authenticate a person.

## Contract schema

All listed fields are required; unknown fields are rejected. Contracts are copied, canonicalized, hashed, and immutable once registered.

| Field | Meaning |
| --- | --- |
| `task_id` | Stable, unique controller task identifier |
| `repository.id` | Exact repository identifier in the controller's trusted repository registry |
| `repository.base_commit` | Full lowercase 40- or 64-character commit hash |
| `allowed_files` | Exact relative POSIX paths, or directory prefixes ending in `/`; no globs |
| `dependencies` | Task IDs whose verified evidence must be ready and unchanged during this attempt |
| `capability` | Required adapter capability identifier, such as `code.edit` |
| `model` | Object with explicit `provider`, `model`, and `effort` |
| `reviewer_model` | The independent reviewer's explicit provider/model/effort selection |
| `permissions` | `tools`, `network_origins`, and `credential_aliases` arrays |
| `budget` | Integer `max_attempts`, `max_cost_microusd`, and `attempt_reserve_microusd` |
| `deadline` | Absolute UTC Unix timestamp in whole seconds; execution leases cannot extend it |
| `acceptance_tests` | Array of objects with `id` and SHA-256 `definition_sha256` |
| `approval` | Exactly `{"human_required": true, "independent_review": true}` |

Effort is one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`. This is a contract vocabulary, not a claim that every provider supports every setting. The adapter must advertise its actual support. Acquisition requires the selected provider, model, and effort to match exactly; the ledger does not select a cheaper fallback.

Permissions are inert declarations that the dispatch layer must enforce. Tool names and credential names are aliases, not shell commands or secret values. Network permissions are HTTPS origins without embedded credentials, query strings, or paths. The ledger never reads credential material.

Approval/policy/test directory components and `.git`, `.github`, `.codex`, `.agents`, `AGENTS.md`, and the hub contract/core authorization code are protected. Protection applies to actual changed-file names even when a parent directory was allowed. Absolute paths, `..`, Windows drive/stream/device forms, backslashes, and wildcard/encoded path forms are rejected. `resolve_allowed_path(workspace, relative, contract)` also resolves existing symlinks/junctions and rejects escape from the workspace. A real file-writing adapter must recheck containment when opening files; this module does not provide an OS sandbox or eliminate filesystem races.

## Public API and execution sequence

Create a separate database with `TaskLedger(path, clock=time.time)`. SQLite uses WAL, full synchronous commits, per-operation connections, and immediate write transactions. Use a persistent disk attached to one controller. Workers talk to the controller rather than mounting the SQLite file. Back it up using SQLite's consistent backup mechanism; copying only the main file while WAL writes are active is not a consistent backup.

1. `register([contract, ...])` registers up to 64 contracts atomically. The combined graph must have no missing dependencies or cycles. Re-registering the exact same contract is idempotent; changing its acceptance tests or permissions is rejected. The bounded local ledger supports 10,000 tasks.
2. `acquire(task_id, worker_id, capabilities, model, lease_seconds=45)` validates the recorded repository base, dependencies, deadline, capability, exact model/effort, attempt count, and spending budget. It returns `task_id`, monotonically increasing `fence`, secret `lease_token`, `worker_id`, expiration, reservation, dependency evidence, contract digest, and permissions. Only a token hash is persisted.
3. `heartbeat(lease, lease_seconds=45)` renews the same attempt without exceeding its absolute deadline. Stale workers cannot acquire ownership by heartbeating.
4. `complete(lease, succeeded=..., actual_cost_microusd=..., actual_model_id=..., summary=..., candidate_revision=..., patch_sha256=...)` records an outcome. Success requires the exact candidate commit and patch hash; failure must omit them. Repeating the same completion returns the prior result without charging twice. A different result, obsolete fence, incorrect owner, or expired lease is rejected.
5. `record_evidence(task_id, candidate_revision=..., base_commit=..., patch=bytes, changed_files=[...], test_results=[...], review={...}, toolchain={...}, risks=[...])` attaches already-produced verification evidence. It returns `evidence_digest`, `verified`, and `independent_model_ids`.
6. `approve(task_id, evidence_digest=..., revision=..., current_base=..., approved_by=...)` creates an approval bound to this exact verified evidence, candidate, and current base. The caller must obtain the human's actual approval; an agent cannot grant it to itself.
7. `promote(task_id, approval_id=..., expected_base=...)` serializes a compare-and-swap of the ledger's recorded repository head. Its receipt always includes `record_only: true`. **It does not merge Git, push, publish, or deploy.** External promotion needs a separately authorized implementation and reconciliation of uncertain external outcomes.

`get(task_id)` returns bounded state, immutable contract, spending, and evidence identifiers, without a lease token. `evidence(digest)` retrieves a bundle; `artifact(digest)` retrieves attached bytes and checks their content hash.

`observe_base(repository_id, current_base)` records a trusted observation of Git and invalidates prior active approvals when the base changes. Rebase work uses a new immutable contract for the new base and new test/review evidence. The controller must refresh its actual Git observation before approval and external promotion: a ledger-only comparison cannot discover unobserved Git changes.

## Reservation and uncertain outcomes

One attempt's reservation must cover the entire bounded execution, tests, and independent review. Run these under that reservation; settle with their aggregate measured cost only after the calls finish. The evidence attachment then records the already-produced artifacts. Do not settle just the builder call and launch an unreserved reviewer afterward. Micro-USD is an accounting unit, not an assertion that providers expose exact costs; if reliable settlement is unavailable, retain the reservation and reconcile instead of entering an invented zero.

Failed attempts consume their measured cost and an attempt slot. Even a reconciled `not_started` attempt consumes its fencing/attempt slot. Cost beyond a reservation is recorded rather than discarded and puts the task in `budget_exceeded`, blocking more spending.

`expire()` marks elapsed leases `reconcile_required` and retains their reservations. Acquisition does not automatically retry those requests. `reconcile(task_id, fence, outcome=..., actual_cost_microusd=...)` is a trusted controller/operator operation after inspecting provider status. Supported outcomes are `not_started`, `failed`, and `completed_unrecoverable`; the first requires zero known cost. Reconciliation is idempotent. Late results from the old attempt remain rejected. A recoverable successful provider result needs a future authenticated reconciliation path; this version deliberately does not silently accept a stale result.

This is not universal exactly-once external execution. It is durable ownership, conservative spending, stale-write rejection, and explicit reconciliation.

## Evidence format and trust boundary

Every `test_results` entry contains exactly:

```text
id, definition_sha256, revision, base_commit,
exit_code, status, started_at, finished_at, log
```

`log` is nonempty bytes from the actual trusted verifier, limited to 32 KB per result. Test definitions must match the immutable contract exactly. Passing evidence requires `status == "passed"` and `exit_code == 0` for every required test. Status values also support `failed` and `error` so unsuccessful evidence is preserved. This module does not run baseline comparisons; a verifier must retain baseline/regression analysis in its trusted reports and follow any additional project acceptance requirements.

The `review` object contains exactly:

```text
selection, actual_model_id, revision, base_commit,
verdict, blocking_findings, report
```

`selection` is the required provider/model/effort object. `actual_model_id` is the adapter's canonical resolved underlying model ID, or `null` when unavailable. `report` contains actual reviewer output bytes. `verdict` is `approve`, `changes_requested`, or `unknown`. Unknown model IDs, identical underlying model IDs across differently branded applications, blocking findings, or an unapproved verdict prevent verification. Distinct model IDs establish distinct identity only; they do not prove statistically independent reasoning or review quality.

`toolchain` is a nonempty mapping of identifiers to versions/content hashes, including dependency/environment identifiers needed to reproduce the checks. `risks` preserves bounded unresolved-risk descriptions. The builder's completion binds the patch hash and candidate; the bundle binds them again alongside contract, base, file list, tests, reviewer identity, toolchain, dependencies, and risks. Any changed evidence invalidates an active approval. Attached patch/log/review artifacts are stored by SHA-256, with a 512 KB total input limit and 32 KB metadata limit.

The trusted verifier must derive the actual changed files from Git, verify candidate/base objects in the correct repository, load protected test definitions itself, execute the tests, and obtain reliable model identity/usage metadata. The ledger validates and preserves those receipts; it cannot distinguish a truthful receipt from fabricated data supplied by a compromised trusted caller. Artifact contents are not automatically secret-redacted, so the verifier must avoid supplying credentials. Nothing in this module exports source or logs to a provider.

## Verification

Run `python -W error::ResourceWarning -m unittest discover -s tests -p test_contracts.py -v`.

The tests cover persistence across reopening, concurrent acquisition, worker disconnect, stale/duplicate completion, held uncertainty reservations, failed-attempt costs and overruns, exact model/effort matching, immutable acceptance definitions, DAG validation, dependency-evidence changes, evidence attachments, unknown/same-model reviewers, changed-base/evidence invalidation, and concurrent serialized ledger promotion. A real worker/controller restart and Git integration exercise remains necessary before using this as the production controller.
