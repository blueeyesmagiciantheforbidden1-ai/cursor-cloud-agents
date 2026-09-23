**Scope.** This is a spec review only. No code, credentials, or GCP project were available, so nothing about the running system is verified. Claims below are design intents; implementation, IAM, and billing behavior are unconfirmed.

**Intended system.** A private Cloud Run Python controller owns a Firestore transactional ledger. Bounded GCE workers, leased by that ledger, call allowlisted hosted models or CLI adapters, or a separate self-hosted inference service on Google compute (never a personal PC). A read-only IAP dashboard observes state. Spend is hard-capped in-app; uncertain charges stay reserved after failures. At most one logical completion may be accepted. External provider billing is not exactly-once. Modes: strongest approved hosted model; validated self-hosted first with explicit hosted escalation; self-hosted-only, which must not call hosted APIs and must abstain if inference is unavailable. One model is loaded; the inference service is bounded by CPU, RAM, context, and concurrency. Research emits immutable candidates; a protected verifier plus human promotion are the only accept path. A finite-state checker explores an explicit bounded model, not the production system. Meta-transfer freezes K methods and A conditions, uses independently seeded researchers and the full budget including failures, scores differences in \([-1,1]\), and requires every condition’s two-sided union-Hoeffding lower bound \(\sqrt{2\ln(2KA/\delta)/m}\) to beat preregistered thresholds.

**Highest-risk correctness gaps**

1. **Accept vs pay.** Ledger “at most one accept” does not stop duplicate provider charges, lost in-flight work, or cap exhaustion by retained uncertain spend with zero accepted output.
2. **Lease fencing.** Cloud Run concurrency plus GCE preemption can yield two live leases or a stale worker writing completion unless every mutation is fenced by lease epoch in the same Firestore transaction.
3. **Mode isolation.** Self-hosted-only fails if any adapter, retry, or logging path can reach hosted APIs; mixed mode fails if escalation is implicit.
4. **Cap is application policy.** GCP billing/quotas are not the ledger. A bug, replay, or unmodeled side channel can spend past the “fixed” cap.
5. **Promotion TCB.** “Immutable candidates” and “protected verifier” are unverified. If the proposer can mutate artifacts or the verifier can write promotion records, the research path is not an accept gate.
6. **Checker ≠ production.** Bounded FSM exploration omits IAP, IAM, retries, partial Firestore commits, and provider ambiguity. Treating it as a proof is incorrect.
7. **Statistics.** Hoeffding + union bound needs independent bounded scores, no post-hoc dropping of A, and failures counted. Shared traces, adaptive stopping, or leaked seeds invalidate transfer claims.

**Six decisive acceptance checks**

1. Concurrent controller instances and killed workers: ≤1 accepted completion per job; stale leases cannot accept.
2. No model/CLI call without a reserved ledger amount; certain + uncertain ≤ cap; failed calls remain reserved.
3. Self-hosted-only: zero hosted egress on timeout, OOM, and abstention; mixed mode escalates only on recorded validation failure.
4. Completions and spend writes include a lease epoch; mismatched epoch aborts.
5. Candidates are content-addressed; verifier identity cannot write candidates; only human promotion changes production config.
6. K, A, δ, m, thresholds frozen before runs; independent seeds; failures in m; published intervals match the stated bound; no dropped conditions.

**Recommendations (not verified):** fencing tokens, allowlist egress, separate proposer/verifier/promoter identities, treat uncertain spend as cap consumption, and never advertise the checker or Hoeffding bound as production proof. **Verified capabilities:** none.