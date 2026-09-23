# Deployment checkpoint — 2026-09-20

**Latest continuation:** See [September 21 enrollment and worker checkpoint](AUTH_AND_WORKER_CHECKPOINT_20260921.md). Both Codex accounts and Claude enrollment have succeeded. Cloud worker commissioning is in progress; older pending-auth statements below are superseded.

## Current owner instructions

- Operate the hub and monitor Google Cloud only. This personal Windows PC is not a production worker or monitoring target.
- Use existing Google Cloud setup and standard Cloud Run `run.app` URLs. Earlier new-organization/domain requirements were superseded. The Porkbun cart was emptied without a purchase.
- Keep costs to the necessary minimum. No GPU, always-on inference VM, training, or automatic research spending.
- Cloud Qwen was the first requested connection. The owner subsequently clarified that Alibaba/Qwen is optional and the hub may use any suitable provider. Keep orchestration independent of the model vendor; preserve the Google hosting choice. See `docs/ORCHESTRATION.md`. No self-hosted model server is required for the initial integration.
- Private ChatGPT integration is required for exactly two accounts. Both accounts now have a registered MyHero plugin, a connected OAuth account, and all six discovered manager tools.
- Provider ownership is pinned by the user's later instruction: Codex and Claude use the Ryan Gmail account; Grok, Cursor and Copilot use the blueeyes Gmail account. Exact labels are stored privately in `runcrew-private/subscription-owners.json`. Intended owners are not proof of enrolled cloud credentials.
- Use included subscription usage first, then existing purchased credits, preserving verified provider spending caps. This later instruction permits existing credits; it does not authorize new purchases, automatic reloads, unrelated API fallback, or costly always-on infrastructure.

## Current verification — 2026-09-20 23:27 UTC

The MyHero plugin installed in this task made a real `hub_status` call at 23:27:17 UTC. It returned the healthy Google Cloud/Firestore hub and zero of five production workers. Both account registrations remain complete; this direct tool call is additional end-to-end evidence.

Cursor's supplied credential verified against the intended owner using official read-only metadata endpoints. No provider inference occurred. Chrome is disconnected from the browser gateway, so new browser enrollment challenges are held until it reconnects. The previous Codex and Grok device processes exited without fresh credentials.

Claude packaging now supports project work and a validated operator-configured staged fleet. Fourteen packaging checks passed; 86 focused model/runtime/worker tests completed successfully with one existing Windows capability skip after retrying outside the fixture-blocking sandbox.

Claude image build `f0a6eb8b-8618-49b6-b502-f5f2dc76b171` has status **SUCCESS**. Verified image: `us-central1-docker.pkg.dev/project-0c6d31fa-509e-4116-a2c/runcrew-hub/claude-worker@sha256:9d23d134ef2d083069907ef07bdaacd4a9ebaefbcfa294cd08dbe3a60dafcff3`. No worker job or provider inference was started. Build state is tracked separately in `cloud-worker-builds.json`.

## Actual Google resources

Authorized CLI account: `gcp-operator@example.invalid`. Organization: `833782852711` (LanegecKo). Billing: `REDACTED-BILLING-ACCOUNT`.

Google created dedicated project `runcrew-hub-20260920`, but linking billing failed with the billing-project quota. A retry after the user's reported account upgrade returned the same error. No compute was started in that project.

Using the already billed project `project-0c6d31fa-509e-4116-a2c` (number `496481413971`) with separate new RunCrew resources:

- Service accounts `runcrew-runtime`, `runcrew-builder`, `runcrew-monitor`, and `runcrew-gateway`.
- Named Firestore database `runcrew-hub`, delete protection enabled; queue index verified READY.
- Runtime database role restricted with an IAM condition to that database.
- Private Artifact Registry repository `runcrew-hub` in `us-central1`.
- Private source bucket `runcrew-496481413971-source`.
- Secret Manager secrets `runcrew-hub-tokens` and `runcrew-status-token` with separate readers.
- Separate named Firestore database `runcrew-auth`, delete protection enabled; exact database IAM conditions grant `datastore.user` to the gateway and monitor service accounts.
- Manager-only secret `runcrew-gateway-manager`, containing only the existing manager credential, with direct secret accessor granted only to the gateway service account. The gateway has resource-scoped invocation permission on `runcrew-hub`.

Existing `pharma-catalog` Cloud Run service was inspected and not modified. The authorized Google owner was verified in Chrome during deployment. After the later session interruption Chrome is no longer present in the browser gateway; reconnection is pending. CLI access remains available. Do not infer another browser account has the same authority.

The initial resumable deployment script is the parent directory's `cloud_deploy.py`; its state journal is `cloud-deployment-existing-state.json`. Cloud Build `b181eb44-3fc1-47df-999f-caa2d7c44048` completed successfully. The initial hub and dashboard revisions were configured with minimum 0 / maximum 1 instances, 1 vCPU and 512 MiB each, private IAM and dashboard IAP. Revision limits and service-level limits are distinct; these settings are not a hard spending ceiling.

- Hub: `https://runcrew-hub-kdhodumsza-uc.a.run.app`
- Dashboard: `https://runcrew-dashboard-kdhodumsza-uc.a.run.app`
- OAuth gateway: `https://runcrew-gateway-kdhodumsza-uc.a.run.app`

The read-only cloud smoke check verified MCP initialization, six manager tools, orchestration instructions, and hub status. Anonymous POST `/mcp` returned HTTP 403. No provider calls or cloud workers were started. See the parent directory's `cloud-hub-smoke.json`.

Dashboard IAP access now works. With the user's approval, the service-specific custom OAuth client was auto-generated and saved in Google Cloud Console. Its consent audience is external, with exactly the two requested owner Gmail accounts plus the gateway administrator as test users. Existing IAP viewer grants remain in place. Both owners successfully opened the live IAP-protected `/identity` route in Chrome. Verified identity observations are stored privately in `runcrew-private/verified-iap-identities.json`; subject IDs and assertions must not be copied into public documentation. CLI credentials were not repurposed as application OAuth credentials.

Cloud Build `823f2d86-b50f-4f8d-a9a9-204c22eb24fd` was cancelled when the owner paused work; a fresh read after resumption confirmed `CANCELLED`. The initial journal response alone is stale. The explicit `resume-cancelled-build` phase verifies that terminal state before archiving the old build and submitting another. Existing hub-token secret version 2 now includes Grok as the fifth worker principal; no IAM change or worker launch occurred.

The MCP manager initialization supplies orchestration instructions directly to connected clients, in addition to the packaged skill. ChatGPT passkey verification is complete. In Chrome, `MyHero` was registered for `ryan-owner@example.invalid` as connector `asdk_app_redacted_primary`. Its OAuth connection and discovery of all six manager tools are now complete. The second account's registration and connection were subsequently completed, as recorded below. The public OAuth gateway is the connection endpoint; the hub backend remains private.

The refreshed ChatGPT form showed the stable callback `https://chatgpt.com/connector_platform_oauth_redirect`. The narrowly scoped callback migration completed successfully, preserving both pinned identities, the existing client ID, scopes, and actual service URLs. Anonymous gateway MCP still returned HTTP 401, and discovery returned HTTP 200 afterward.

OAuth authorization-code/PKCE, token rotation/revocation, the HTTP gateway, IAP identity display, and browser consent are now deployed. Version `20260920-auth1` was built successfully by Cloud Build `636edcf5-886f-4c07-bba2-f30aa7db4217`; the dashboard image was updated. Access pins the two browser-verified IAP issuer/subject pairs, with one predefined public OAuth client, `myhero-chatgpt`. Email labels alone do not authorize a user.

The first live ChatGPT OAuth attempt was rejected because it included the optional `ui_locales=en-US` hint. A narrow fix now accepts a bounded locale hint and removes it before OAuth validation or persistence; exact client, redirect, resource, PKCE, and duplicate-parameter checks remain intact. The focused service and dashboard consent tests passed: 42 tests in 6.537 seconds. Cloud Build `79b65ae5-bca7-4978-b995-28cfebe8acd9` successfully built `20260920-auth2`. The dashboard's auth2 deployment completed successfully with IAP and its status secret preserved. The gateway remains on auth1 because its token runtime is unchanged.

The subsequent consent POST returned HTTP 403 before any redirect or token request. Real Chrome loopback reproductions independently confirmed two browser-policy issues: `no-referrer` caused the form's Origin header to become `null`, and `form-action 'self'` blocked a later cross-origin HTTP 303. The narrow fix uses `Referrer-Policy: same-origin` only on successful consent pages and adds the validated, registered callback to that page's CSP. Other responses retain `no-referrer`; exact Origin, CSRF, verified subject, PKCE, and callback checks remain enforced. The focused dashboard, identity, and consent suite passed 41 tests in 18.671 seconds. See the parent directory's `browser-oauth-regression-receipt.md` for the four Chrome observations and sanitized production evidence.

Cloud Build `b87a37cb-72a4-4fd4-abbe-047f3fc4b707` successfully built `20260920-auth3`. The dashboard is now deployed on auth3 with IAP and its status secret preserved; the gateway remains on auth1. The user completed approval in Chrome, the return callback was observed, and the popup closed automatically. ChatGPT's first-account plugin details show `Connected accounts → Primary → Ryan's MyHero account`. Refresh enumerated `hub_cancel`, `hub_get`, `hub_list`, `hub_retry`, `hub_start`, and `hub_status`, all with OAuth scope `hub:manage`. Sanitized cloud logs show approval HTTP 303, token HTTP 200, and MCP HTTP 200 and 202. First-account authorization and tool discovery are verified complete. The read-only chat status check subsequently succeeded; its result is recorded below.

The extension uses the parent directory's `cloud_oauth_deploy.py` and its separate `cloud-oauth-deployment-state.json` journal. The new auth database, gateway service account, manager-only secret, and scoped IAM grants were created and verified. The gateway alone is public at the Cloud Run invocation layer and enforces its own OAuth authorization. It uses service- and revision-level minimum 0 / maximum 1, 1 vCPU, 512 MiB, concurrency 8, timeout 60 seconds, request-based CPU, and no startup CPU boost. The deployment helper uses `beta run deploy` for the service-level `--max` flag and guards uncertain mutations against duplicate submission.

Configuration succeeded with exactly two pinned identities and one public client. Anonymous gateway MCP returned HTTP 401, and OAuth discovery returned HTTP 200 with the actual gateway and dashboard routes. `/healthz` returned a Google-edge 404, so deployment verification uses the actual application routes. The helper's 13 offline tests passed again after these adjustments. Dashboard IAP and its status-only secret remain preserved. No provider calls were made.

No production agent workers have been identified or connected; the live dashboard verifies 0 of 5 agents ready. Cloud Run jobs and worker pools were inventoried and none were found. The Compute Engine API is disabled; VM presence is unknown, not verified absent. Current local CLI logins do not establish cloud-worker authentication or remaining subscription allowance.

## Latest verification — 2026-09-20 23:08 UTC

Both ChatGPT connections and all six tool catalogs are now verified. The second account (Amber NightShift) has MyHero app `asdk_app_redacted_second`, version `asdk_app_v_redacted_second`, with `Amber's MyHero account` connected. The user completed its authorization in Chrome; Codex did not click a final consent grant. The first account's real `hub_status` call completed successfully at 22:31:40 UTC in chat `redacted-chatgpt-chat-id`. It returned live status and 0/5 workers. Earlier statements above that the second registration or first chat check is outstanding are superseded by this observation.

Claude's browser account is verified as the intended Ryan account, on personal Max 20x. Usage showed 6% weekly use and 12% of Fable's separate weekly allowance. Existing purchased credits are enabled, with $100 remaining, a $40 monthly spending cap, $0 spent this month, and auto-reload off. The owner explicitly authorized included usage first, then existing credits. This is browser evidence, not a live cloud credential or permanent quota guarantee.

Local implementation now includes distinct-model policy, account/CLI-bound launch selection, and a same-process Claude initialization/settings check before releasing a task. Offline tests and unsigned operator catalogs do not prove live availability. Live catalog collectors, coordinated room-wide model plans, all five cloud credentials, production workers, and full project execution remain incomplete. The cloud worker image is prepared but has not yet been built or started.

## Qwen connection

No existing Alibaba/Qwen key was found in the supplied key directory or the target project's secret names. The owner registered and later pasted a credential in the conversation. That value has not been copied into deployment files, installed, or used. Rotation was recommended. No Qwen call has been made.

The prepared `cloud_deploy.py qwen-key` phase was rejected by automatic approval review before execution. It would create the empty `runcrew-qwen-api-key` secret and grant, on that secret only, `secretVersionAdder` plus `viewer` to the observed browser account `cursor-owner@example.invalid`, and `secretAccessor` to `runcrew-runtime@project-0c6d31fa-509e-4116-a2c.iam.gserviceaccount.com`. Exact-scope user approval is pending. Do not retry, split, or indirectly execute this rejected operation without resolving the approval block. No secret resource, value, or grant was created by that attempt. The intended handoff is for the user to enter the Qwen key directly in Google Secret Manager, never in chat.

Initial proposed profile: Singapore/international, pinned `qwen3.7-flash-2026-07-15`, 128 completion tokens, thinking/search/tools disabled, no retries. Application limits: $0.01 per call, $0.10 per day, one outstanding request. Uncertain charges retain their reservation and block redispatch. See `COST_POLICY.md` and `docs/QWEN.md`.

The new HTTP connection is manager-only `POST /v1/qwen/completions`; requests cannot change price, provider, key, token cap, or budget. Activation requires `HUB_QWEN_ENABLED=1`, a private `QWEN_API_KEY`, Cloud Run, and Firestore. Key onboarding and a bounded real test remain outstanding.

The integrated offline suite including OAuth changes completed successfully: 382 tests, 3 Windows symlink capability skips (57.993 seconds). This includes revocation client binding, MCP protocol-version validation, and origin control-character rejection. Offline tests do not verify a live Qwen connection or authenticated cloud workers. New strict worker configurations reject execution until a trusted subscription-authentication probe is implemented, rather than silently falling back to paid API usage.

## Specification traceability

The contract/context/research/workflow/transfer/routing modules provide bounded engineering mechanisms and offline tests. They do not establish empirical recursive improvement or train a local model. The newer pasted compiler proposal additionally names an anytime-valid e-process, coordinated compiler packages, bounded value-of-computation, compiled subgraphs, and 37 acceptance tests. These new pieces have not yet been implemented or claimed complete; Cloud Qwen now takes priority.

The named full source documents `ryan_command_center_spec_2026-09-20.md`, `recursive_research_blueprint.md`, `incremental_research_compiler_spec.md`, and `qwen_cloud_adapter.md` were mentioned in pasted text but not found as separate attached files. Preserve this gap rather than inventing their missing detailed tests.

Actual external contributions exist from Grok Build, Cursor SDK, GitHub Copilot and a Claude desktop handoff. These are development receipts, not evidence that production cloud workers are connected. Both accounts now have verified private ChatGPT connections and six-tool discovery. All five production workers remain outstanding.

## Enrollment progress after Chrome reconnected

Chrome reconnected. Native Codex, Grok Build and GitHub Copilot CLI logins subsequently succeeded in fresh private homes outside OneDrive. Account identity is bound to the requested owner using browser identity evidence, with Codex additionally verified by the native account/read protocol. No desktop credential was copied or replaced. User explicitly approved the displayed Grok persistent/API/read-write scopes and Copilot repository/Codespaces/gist/profile/organization scopes; GitHub phone verification was completed by the user. Callback error pages did not invalidate the authoritative native CLI success results.

Cursor's existing user key is also account-verified. Its Ultra dashboard shows Cursor-model allowance 6% used, Other Models 100% used, and unlimited on-demand billing with $205.63 displayed spend. No account spending setting was changed and no new on-demand spending is authorized. Copilot Max shows 164/20,000 included AI credits used and additional usage disabled at a $0 budget.

Codex is authenticated as the intended Ryan account on ChatGPT Pro. Its live account-bound rate metadata returns ordinaryUsageAllowed=false and a fully used weekly window. A finite credit balance is present, but currency and purchase/auto-reload controls are not yet verified; no inference, credit purchase or reset was performed.

Claude's cloud image built successfully and passed its in-image integrity check. Its OAuth enrollment is still incomplete. All five production cloud workers remain unconnected. Local enrollment is preparation for Google Cloud deployment, not local production hosting. Live catalog collection, immutable fleet plan coordination, cloud credential restore/writeback, job triggering and actual end-to-end worker tests remain pending.
