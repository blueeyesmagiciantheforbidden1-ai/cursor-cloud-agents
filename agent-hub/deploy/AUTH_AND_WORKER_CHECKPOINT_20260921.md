# Provider enrollment and cloud commissioning — 2026-09-21

This checkpoint supersedes older statements that the second Codex or Claude enrollment is pending. Authentication enrollment and a production worker job are separate milestones.

## Confirmed enrollment

| Provider | Intended account | Evidence | Cloud project task |
|---|---|---|---|
| Codex, Ryan profile | ryan-owner@example.invalid | Official device login succeeded; account/read matched | Not verified |
| Codex, Blueeyes profile | cursor-owner@example.invalid | Separate official device login succeeded; account/read matched; backend account/pool IDs differ from Ryan | Not verified |
| Claude Code | ryan-owner@example.invalid | Official setup-token succeeded; independent browser owner receipt; read-only first-party initialize accepted Fable 5.1/max | Not verified |
| Cursor | cursor-owner@example.invalid | Existing key verified using official read-only account/model endpoints | Not verified |
| GitHub Copilot | cursor-owner@example.invalid | Official device login succeeded for blueeyesmagiciantheforbidden1-ai; GitHub primary email verified; SDK metadata authenticated | Not verified |
| Grok Build | cursor-owner@example.invalid | Official device login succeeded with independently observed account consent | Not verified |

Credentials stay outside this source tree in the private enrollment directories. They are never build inputs. Both private MyHero ChatGPT registrations previously completed real hub_status calls; ChatGPT remains the front door.

Provider allowance snapshots collected on September 20 are historical, not current launch authorization. Ryan's Codex snapshot had exhausted included usage; Blueeyes had included usage remaining. Claude and Copilot had included allowances available. Cursor had remaining Cursor Models allowance but exhausted Other Models allowance, with unlimited on-demand spending enabled. Grok allowance was unavailable. Refresh before choosing a route; do not infer balances or currency from plan names or a bare credit number.

## Cloud commissioning

The approved project remains project-0c6d31fa-509e-4116-a2c, region us-central1, existing organization 833782852711. Do not reopen domain/new-organization signup or use unrelated projects.

Claude worker build 2ab3c860-ff37-4102-942a-78f11c935c18 succeeded with image digest `sha256:d8a5cdd4cc623cc48f187c3a211dcc05ee67cf4a4a37b3ee0422c0c22b002d03`. It adds an explicit heartbeat-only mode: no task claim, provider credential, catalog read or inference; one acknowledged offline/auth-unknown report. There are 44 passing worker/telemetry tests and 17 passing packaging tests. The deployment helper is outside source at `ARTROOT/cloud_claude_heartbeat.py`.

The dedicated worker identity and its resource-scoped hub permission and two secret permissions are created. Google's initial identity propagation delay was reconciled through read-only identity/IAM checks before repeating the exact idempotent grant. No broad role was added.

Cloud Run execution `runcrew-worker-claude-lpdhv` completed successfully at 2026-09-21T15:35:17Z, succeededCount=1, failedCount=0. Google queued startup for about three minutes; the actual container started shortly before completion. This verifies the rootless image, mounted config, metadata-service identity and acknowledged private-hub telemetry at 1 CPU/512MiB. It does not verify a provider prompt or production project readiness. The job has zero automatic retries, no schedule, no provider credential, and a 60-second task runtime bound. Its last report is intentionally offline/auth-unknown because the one-shot execution has ended.

## Further verified cloud checks

All five logical provider workers have now acknowledged cloud heartbeat reports. Their one-shot jobs exit afterward, so `offline/auth unknown` is truthful, not evidence of provider task readiness. Codex execution `runcrew-worker-codex-b69dk` and Copilot execution `runcrew-worker-copilot-kbthr` also reached successful terminal status. Cursor and Grok hub acknowledgements are recorded by `hub_status`. No project-task result has been verified for any provider.

Cursor metadata execution `runcrew-cursor-metadata-20260921a-mfk2l` succeeded. The native Linux status verified the intended owner and returned 39 model catalog entries plus controls; no inference was performed. The Cursor on-demand spending decision remains unanswered, so inference stays paused. Composer's absent effort parameter is not proof of maximum/fixed effort.

Claude diagnostic executions `runcrew-claude-probe-20260921a-6z2f6` and `runcrew-claude-probe-20260921b-thlsn` both failed on `unexpected_post_prompt_frame`. The second diagnostic recorded two control-response frames and one unknown frame. A prompt was sent; actual model acceptance and account charge remain unknown. Do not automatically repeat either job or report success based on browser percentage rounding. No further prompt rerun was launched after the second failure.

The isolated credential foundation is verified: Firestore `runcrew-provider-auth` in us-central1, deletion protection enabled, dedicated `runcrew-credential-broker` identity and a database-scoped custom role with only `datastore.entities.get/update`. Provider secrets/enrollments and the broker's deployed HTTP service are not established by this foundation. The source HTTP broker/client and Codex metadata adapter have offline tests; production credential refresh and Linux account metadata commissioning remain necessary.

The installed MyHero connection returned real `hub_status` at 2026-09-21T16:57:33Z. All five entries were configured/offline/auth unknown, with no rooms. Do not create synthetic contributions to fill that status page.

## Parallel implementation

- Root: cloud checks, shared-memory/API integration and the approved usage allocation.
- mcp_tests_resume: Codex metadata, independent learning regressions and browser-workspace packaging.
- claude_subscription_worker: attachment requirements audit, release review and cloud rollout preparation.
- cloud_agents_auth_routes: release state machine and durable coordinator.

These are Codex implementation assignments for external-provider workers, not proof that the external agents themselves executed cloud tasks. Grok's attempted allocator-review model command crashed before inference; no review is claimed.

## Usage allocator

agent_hub/usage_balancer.py and docs/usage-balancer.md are implemented; 32 offline tests pass. The allocator binds one immutable fleet-plan hash, separates canonical account/pool identities, reserves in-flight usage, honors concurrency, waits for unresolved included allowance before spending credits, and never changes model/effort to reduce cost. ChatGPT conversation usage remains unavailable unless independently measured.

It is not yet wired into a production dispatcher. Live allowance collection, durable workspaces/artifacts, cloud job triggers, provider adapters, a pinned fleet-wide model plan and real provider contributions remain required before the five-agent hub is ready. No API fallback, purchases, resets or auto reloads were enabled.

The user explicitly selected **25% improvement / 75% project work**. The planner now caps improvement per verified quota pool/reset, counts committed and in-flight research allocation, blocks background work when foreground work is queued/active, preserves urgent-work headroom and forbids purchased-credit routes for improvement. Unknown accounting is not zero usage. These remain planner rules until the durable cloud dispatcher is commissioned; automatic improvement inference remains disabled.

## Self-improvement source and cloud packaging

The full latest attachment is inventoried in `docs/SELF_IMPROVEMENT_REQUIREMENTS_20260921.md`; referenced missing specifications are not fabricated. `docs/LEARNING_AND_RECOVERY.md` distinguishes implemented advisory shared memory and release controls from remaining production evaluation/deployment adapters. Shared memory passed the hub/worker/MCP compatibility suite and an independent 13-case adversarial regression suite. Release reducer tests cover failed health, lost acknowledgements, abort and rollback intentions. None establishes exponential gains or zero downtime.

Browser coding access is requested in addition to ChatGPT. Google Cloud Shell Editor opened under Ryan for this project; its source-bucket metadata check was denied. Exact object access still needs verification. Source snapshot/bootstrap packaging and narrowly scoped access are tracked independently of Cloud Run runtime services. An automatic browser approval review subsequently blocked a terminal action citing unrelated Gmail access; browser interaction stopped. No mailbox access is required or authorized by this work.

Cloud release preflight observed existing service limits: hub service max3/revision max1, gateway service/revision max1, all min0, 1CPU/512MiB, request-based CPU, concurrency8/timeout60. Updates must preserve these without increasing capacity. The first read-only preflight stopped on omitted protobuf `reconciling=false`; the helper now handles that documented default. No new learning revision or traffic switch has yet been claimed by this checkpoint.
