# Shared learning and recovery

The user's selected allocation is **25% improvement / 75% project work**, confirmed September 21, 2026. Improvement uses included allowance only. Project work retains its existing included-first, approved-existing-credit policy. Urgent queued/running work blocks new background dispatch. Unknown or stale usage, unknown reset windows, unknown improvement accounting and uncertain prior charges prevent a new improvement run. The allocation is per canonical quota pool and reset window, not a merged balance across providers.

The source implementation includes a planner gate and tests. It is not yet an operational provider billing cap: an authenticated cloud dispatcher must atomically reserve each pool and reconcile failed, in-flight and uncertain consumption. Actual model usage can exceed an estimate. Until that integration and provider evidence are ready, no automatic improvement model loop is enabled. No purchased credit, GPU, always-running model service or new subscription is needed for these components.

## Failure-driven improvement

The intended loop is failure → reproducible case → regression test → bounded repair → independent review → staged release → health observation → retained lesson. Preserve failed hypotheses and real costs. A fix is useful only after checks establish its effect; peer agreement is not a test result. Unexpected provider outcomes require reconciliation before a repeat. This does not guarantee zero errors, exponential improvement or retraining of provider model weights.

## Implemented shared context

`learning.py`, `core.py`, `store.py`, `adapters.py`, `server.py` and the MCP manager tools implement attributed cross-task project memory. This is an initial advisory layer, not the fully verified research archive in the requirements inventory.

- `hub_learn(operation="propose", room_id, source_message, text, kind)` stores an exact bounded quote from a completed contribution. The hub records its actual agent, source index and message digest. Failed outputs can supply failure notes only.
- A different agent reviews a lesson by returning **only** a JSON object with `lesson_id`, `verdict` (`support` or `reject`) and `reason`. `hub_learn(operation="review", room_id, source_message, lesson_id, verdict, quote)` checks the whole recorded response. Embedded examples, duplicate JSON keys and relabeled verdicts are rejected.
- `hub_lessons(workspace, cursor?, limit?)` exposes bounded source-linked summaries. It does not claim reviewer brands prove different underlying models or independent verification.
- One objection excludes a lesson from future claims. Separate reserved capacity prevents a full positive-review log from suppressing an objection. Retirement and archive retain provenance, free active capacity and prevent replay from resurrecting archived content.
- A room claim reads the room and memory in one transaction. The task keeps that advisory snapshot; retirement affects later claims and does not silently rewrite a running prompt. Relevant lessons share the existing prompt budget, with at most 2 KB of learned material. Completion records exactly the offered lesson IDs and digest.
- Firestore holds production memory. SQLite is used for offline component tests. Memory has no credentials, deployment authority or permissions to alter model/billing policy. Known credential markers are rejected as a backstop, not a comprehensive secret detector.

Lessons currently have workspace, attribution and textual relevance scopes. Automatic code/requirement/toolchain compatibility, verified test-backed acceptance, conflict detection between distinct lesson IDs and integration with research/transfer ledgers remain separate work. A reviewer must not treat an old project fact as automatically true for a new revision.

## Code release controls

`improvement_release.py` implements exact-artifact gates for unit, integration, connection-continuity, security and budget evidence. It tracks canary, promotion, health, abort, reconciliation and rollback intentions. Replayed or uncertain claims do not redispatch the effect. A trusted controller must verify receipts and enforce deployment-generation preconditions; the reducer itself neither runs tests nor performs deployment.

Protected evaluator, credential, permission, budget and controller code is outside automatic candidate edits. These protections cannot be changed through lessons. Changes to that boundary require the existing authorized development/control workflow. Keep a known-good image and compatible schema available for rollback.

The cloud release helper stages separate hub/gateway revisions with zero production traffic and retains their previous images, configuration and traffic mapping. Real cloud tests and rollout/rollback observations are required before claiming continuity. The hub and gateway cannot be switched atomically, so changes must preserve both directions of protocol compatibility.

## Cloud workspace

ChatGPT remains the private front door for the two authorized accounts. Google Cloud Shell Editor is the optional interactive browser workspace. It is not the production worker host; closing it must not stop Cloud Run or Firestore. Source snapshots belong in private Cloud Storage and runtime images in Artifact Registry. No local machine must remain powered on. Workspace setup and the final cloud commissioning state are tracked separately from passing offline tests.

See `SELF_IMPROVEMENT_REQUIREMENTS_20260921.md` for all supplied requirements, unavailable referenced specifications and the remaining experimental claims. No claimed improvement percentage or recursive research result has been invented.
