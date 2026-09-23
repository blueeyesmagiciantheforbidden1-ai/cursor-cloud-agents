# RunCrew Cloud deployment bootstrap

The user's current instruction is to use the existing Google Cloud setup and deploy the website on Cloud Run. **No domain purchase or new organization is required for this deployment.** Use the standard HTTPS `run.app` address returned by Cloud Run. The earlier domain cart was preparation only and is canceled; do not resume checkout or Cloud Identity signup.

The separation boundary is a new dedicated project, with its own hub/dashboard service identities, storage, secrets, and deployment configuration. Existing organization policies still apply. Existing projects and services are not moved or overwritten.

## Exact target before provisioning

The saved CLI authorization exposes organization `833782852711` (LanegecKo). The browser is also signed into an account with existing organization `1091262552755`. These are different accounts and targets; use only the one explicitly selected for this deployment. The current deployment direction uses the saved CLI authorization and organization `833782852711`. Read and pin its exact display name, authorized account, and open billing account before changes. This document intentionally does not guess an account email, project ID, or billing ID.

Follow [README.md](README.md) with:

- `--isolation project` and the exact selected organization ID/display name.
- A brand-new globally unique project ID, created directly in that organization and labeled `agent-hub-isolated=true`.
- An explicit deployment account and an open billing account whose parent is that same organization.
- The exact user-authorized credential-file path passed as `--credential-file` when the saved authorization uses that override. Never copy its contents into source or logs. Ordinary `gcloud auth login` remains available without that flag.

The guard requires the expected credential-file override to be active and rejects a mismatch or fallback to another login. Impersonation, explicit access-token overrides, and unspecified authentication customizations remain prohibited. The file pin establishes which user-authorized credential source is used; it does not independently discover the credential's underlying email. Confirm the account/file association from the existing authorized setup.

In project mode, the project creation time must be on or after `2026-09-20T00:00:00Z`, the start of this task. A later `--created-after` cutoff is permitted. Older projects cannot be relabeled and reused through this flow. A project's label and creation time alone are not a complete security audit; the deployment also checks exact parent and billing linkage.

## Deployment defaults

Use `us-central1`, Cloud Run, Firestore, Secret Manager, separate service identities, and the generated `run.app` URLs. A new domain, paid Google Workspace subscription, or separately purchased TLS certificate is unnecessary for these URLs. Build only the allowlisted source directory, never the API key workspace. Provider authentication remains with the agent workers.

The stricter organization-isolation guard remains available for a future separately authorized request, and still bans both known existing organizations in that mode. It is not selected now. Google's domain-free standalone organization route is limited to new Free Trial customers and is unnecessary for the current existing-account deployment. [Google's standalone organization eligibility](https://docs.cloud.google.com/resource-manager/docs/standalone-organization-overview)

No cloud resources are created by this bootstrap document. The completed deployment must be verified through actual service configuration, authenticated requests, and a returned Cloud Run URL before it is reported as live.
