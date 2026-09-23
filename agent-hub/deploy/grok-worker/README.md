# Grok Linux commissioning package

Prepared 2026-09-21. This package is not deployed or connected. Default invocation
and `--once` refuse task execution before any provider process or task claim.
After a successful image build, `--heartbeat-only` can test the hub without Grok
credentials. Project work still requires the integrations listed below.

## Pinned public artifact

- Grok Build 1.0.40, Linux x86_64 ELF, 165,587,968 bytes.
- Binary SHA-256: `92c997dfd109c0672d40d5ae6fbd15835d53ffaf12cf9ea124d22aaef3ff23fc`.
- Compressed artifact SHA-256:
  `de95a1d1a17eaa3eaa4218212c7e1c2e80a36e3ed6a5033b7dd117be02a128e9`.
- The version pointer and download pattern came from the official installer,
  saved under `provenance/`. The installer was inspected, never executed. The
  digest records bytes downloaded over HTTPS from the official origin; it is
  **not a vendor-published signature or independent checksum**.
- The ELF architecture was inspected locally. No GLIBC version strings were
  observed; that is not proof of a fully static binary or runtime compatibility.
  The build performs native version/help checks without credentials or inference.

[Official installation](https://docs.x.ai/build/overview),
[installer source](https://x.ai/cli/install.sh).
The CLI is pinned by digest. Base OS/Python/apt inputs still resolve at build time;
pin them and record the final image digest for a fully reproducible image.

## Build and connectivity

From the agent-hub root:

```sh
docker build -f deploy/grok-worker/Dockerfile -t runcrew-grok-worker:1.0.40 .
docker run --rm --network none runcrew-grok-worker:1.0.40 --check
```

Runtime is UID 10001, root-owned `/opt/runcrew/grok/grok`, a fixed native profile,
and `/workspace/default`. No login state is baked into the image. The package
uses only the Python standard library and existing hub code. Git and native
runtime libraries are present. Exclude public-download `.cache/`, `.testscratch/`
and `__pycache__/` from any Cloud Build upload snapshot; Docker has a strict
Dockerfile-specific ignore file as well.

For `--heartbeat-only`, mount trusted `/run/config/worker.json` and inject only
the dedicated Grok hub role token as `HUB_AGENT_TOKEN`. Google metadata identity
authenticates to the fixed private hub. Use one task, zero retries, 60 seconds,
one CPU, and 512 MiB. No provider process starts and no schedule is installed.

## Enrollment and refresh

The official device login succeeded for the browser-verified blueeyes owner.
Its independent reference is
`9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc`.
The exact fresh auth file has one OIDC scope and fields including `key`,
`auth_mode`, `refresh_token`, `expires_at`, `oidc_issuer`, `oidc_client_id`
and owner metadata. Only keys/types were inspected for this packaging pass;
values are not in source or logs. Native sign-in success does not establish quota.

`credential_state.py` accepts opaque credential bytes only from the intended
owner's durable fenced lease. It restores `HOME/.grok/auth.json` with private
permissions and requires the native process to stop before writing refreshed
bytes back through the broker. Failed/uncertain writeback quarantines the lease
and preserves the updated local file. Set `GROK_HOME=HOME/.grok`; keep state writable
for native automatic refresh. The shared durable broker is implemented separately;
its cloud service and protected account binding still require commissioning.
One task per job or a local file lock does not serialize distinct executions.

The root-owned `/etc/grok/requirements.toml` disables first-party API-key auth,
permission bypass, auto-update, inference retries, subagents, memory, envrc loading,
and Claude/Cursor customization scanners. Construct the child environment and
reject XAI_API_KEY, GROK_* endpoint/model/OIDC overrides, external auth commands,
custom model sections, and unintended configuration layers. This policy alone
does not disable third-party BYOK routes; validate the actual effective endpoint.
[Authentication and managed policy](https://docs.x.ai/build/enterprise),
[configuration reference](https://docs.x.ai/build/settings/reference).

## Required before task execution

1. Execute the pinned Linux binary's read-only `grok inspect --json` and `grok models`
   against the isolated fresh enrollment under the durable lease. The previous
   enrolled Windows CLI's `models` command crashed with exit 3221225477; that failure says
   nothing conclusive about Linux 1.0.40. Save raw metadata privately and emit
   only sanitized facts. Neither inspect output nor a model list proves quota.
2. Bind the same inference process to the verified account route, actual endpoint,
   exact native model ID and its supported maximum effort. Populate the trusted
   room-wide distinct-model policy using live evidence; this package sets no
   guessed model, effort, ranking, plan or quota.
3. Add the project-work profile to shared adapters without bypass flags. A
   prospective profile uses `--permission-mode dontAsk`, explicit file edit/read
   permissions, a finite tool set, and only approved project commands. Verify
   the Linux sandbox actually enforces workspace and credential restrictions;
   if Cloud Run cannot provide the required backend, stop rather than silently
   falling back. Do not enable `--always-approve` or `--yolo`.
4. Confirm current account allowance and existing extra-credit/Auto Top Up controls
   in the authorized provider UI. Preserve provider caps and stop on exhaustion.
   Login is not permission to buy credits, enable top-up, or use an API key.
5. Integrate real claim/complete, bounded deadline, output validation and artifact
   delivery only after the preceding checks pass. A heartbeat is connectivity
   evidence, not a completed provider task.

[Read-only commands and selection flags](https://docs.x.ai/build/cli/reference),
[sandbox documentation](https://docs.x.ai/build/features/sandbox),
[Grok plan FAQ](https://docs.x.ai/grok/faq).

## Metadata commissioning

`--metadata-only` runs only the documented `inspect --json` and `models` commands
under one durable credential lease, in separate bounded native processes. It
never creates a session, sends a prompt, claims work, or retries inference. A
clean environment excludes API keys and endpoint overrides. Both processes stop
before refreshed native state is committed and the lease released; uncertainty
quarantines the lease rather than permitting another execution.

Sanitized output records inspection shape and literal model-name mentions.
Model mentions are diagnostics, not an account-eligible catalog. The full Linux
text schema has not yet been observed, and no documented account/quota command
was found. Account, quota, effective route, maximum model and maximum effort are
therefore explicitly unverified or unavailable. Raw metadata remains private in
the leased home for an authorized schema audit; it is never printed. The
collector does not substitute embedded defaults or invent JSON/status flags.

Cloud acquisition uses the HTTP broker client and protected
`/run/config/credential-broker.json` with a controller-issued execution grant.
The execution must match the Cloud Run environment. No direct Firestore or
Secret Manager fallback is present, and acquisition is journaled first.

Validation: 21 offline tests pass. The earlier native-only image successfully
passed uncredentialed Linux version/help checks in Cloud Build
`6d59edf2-24ae-4fc7-9006-69b6714f77d4`, with image digest
`sha256:826bf5615e1e7b514e3d5555f6bc19dfe9de29df04f472429ec58fd51e4da2f8`.
The new metadata helper is not part of that earlier image and has not run with
credentials. No inference or provider task readiness is claimed.
