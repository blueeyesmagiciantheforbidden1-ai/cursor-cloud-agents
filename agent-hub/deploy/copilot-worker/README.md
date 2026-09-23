# Copilot Linux commissioning package

Prepared 2026-09-21. The native image built successfully on Cloud Build and passed
uncredentialed Linux version/help checks. The metadata additions below have only
been tested offline and are not part of that earlier image. It cannot
claim a task: default invocation and `--once` stop before any hub or provider call.
The shared worker still supports verified project work only for Claude.

## Pinned release

- Official CLI 1.0.86, Linux x86_64; native size 166,792,000 bytes.
- ELF SHA-256: `be0152ea29b06d54dd23e1fc5512e978b4e84097f6da5314df96b3731a2dd6aa`.
- The archive was fetched from the official GitHub release and matched its
  published SHA-256. Full public metadata and the audited one-file archive layout
  are in `provenance/`. No installer or package lifecycle script was executed.
- Static ELF inspection found GLIBC symbol versions through 2.28. This is useful
  compatibility evidence, not a native launch test. The Docker build must pass
  the exact binary's version/help checks as UID 10001 with a fresh empty home.
- Native release bytes are reproducible by digest. The Python base image and
  Debian packages still resolve at build time; record the built image digest and
  pin those inputs before claiming a fully reproducible OS image.

[GitHub installation documentation](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli)
identifies this vendor release repository. [Pinned release](https://github.com/github/copilot-cli/releases/tag/v1.0.86).

## Build and commissioning

From the agent-hub root:

```sh
docker build -f deploy/copilot-worker/Dockerfile -t runcrew-copilot-worker:1.0.86 .
docker run --rm --network none runcrew-copilot-worker:1.0.86 --check
```

The image owns `/opt/runcrew/copilot/copilot` and its digest profile as root;
the process runs as UID 10001. The only Python dependencies are the standard
library and existing hub source. Git and native runtime libraries are present.
No provider/hub credentials or enrollment files belong in the build context.

`--heartbeat-only` accepts a small trusted `/run/config/worker.json` and the
dedicated Copilot role's `HUB_AGENT_TOKEN`. It uses Google metadata identity for
the fixed private hub URL, removes inherited provider credentials, does not claim
work, and does not start Copilot. Use one task, zero retries, a 60-second deadline,
one CPU and 512 MiB for that connectivity check. No schedule is created here.

Exclude `.cache/`, `.testscratch/` and `__pycache__/` when constructing the
Cloud Build upload snapshot, as well as honoring the Dockerfile-specific ignore
file. The local caches contain public downloads only and are unnecessary inputs.

## Credential contract

The owner binding is the blueeyes account; verified GitHub login:
`blueeyesmagiciantheforbidden1-ai`. The independent account reference is
`9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc`.
The fresh enrollment's `config.json` starts with a generated comment banner and
contains `authTokens`, `lastLoggedInUser`, `loggedInUsers`, and `firstLaunchAt`.
Preserve its opaque bytes; it is not plain JSON until the comment is handled.
No credential is copied into this source directory.

`credential_state.py` implements the restore/commit boundary for a real durable,
fenced broker. Restore only the fresh enrolled credential to a new private
`HOME/.copilot/config.json`, set `COPILOT_HOME=HOME/.copilot`, serialize each account
across all executions, and commit the native CLI's updated bytes before releasing
the lease. Uncertain commits retain private state and quarantine the lease.
The shared broker is implemented separately; its cloud service and protected
account binding must be commissioned before this package can acquire a lease.
A read-only Secret Manager mount
reseeded on every run is not refresh persistence. Neither is a local file lock.

Construct a minimal child environment: no `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`,
`GITHUB_TOKEN`, `COPILOT_PROVIDER_*`, `COPILOT_PROVIDERS_CONFIG`, custom proxy,
model override, or inherited SDK token. Use only the intended stored GitHub login.
Keep providers.json/settings/plugins/hooks outside the fresh home until audited.
[Official authentication](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli).

## Required before project work

1. Implement the fenced credential broker and preserve its updated state even
   if a job fails. Check Linux recognition of the fresh enrollment.
2. Add a same-process native handshake. The official SDK uses `--headless --stdio`
   and exposes `status.get`, `auth.getStatus`, `models.list`; these are metadata
   calls, not inference. Validate login, subscription route and actual session
   model/effort before sending a prompt. The Windows metadata receipt does not
   prove the new Linux binary's behavior.
3. Integrate the trusted catalog/room-wide assignment into the shared adapter.
   Require explicit `--model` and `--reasoning-effort`, no Auto alias or fallback.
   No model, maximum effort, ranking or complete entitlement catalog is invented
   by this package. A supported picker entry alone does not prove current quota.
4. Enable project file tools `view,grep,glob,create,edit,apply_patch` and only the
   approved project command tools/rules required for a task. Keep `--no-ask-user`,
   built-in MCPs/custom instructions/update disabled, and deny external URL and
   undeclared shell operations. Do not add allow-all/yolo/path bypass. Verify an
   enforceable filesystem/secret boundary before enabling shell tools; permissions
   are not a replacement for a sandbox.
5. Preserve the provider's existing spending control. The recorded Sep 20 browser
   receipt was Max, 164/20,000 included credits used, extra spending off. Recheck
   freshness before inference. `--max-ai-credits 30`, if verified in the pinned
   binary, is a soft session cap; it is not a hard dollar budget or permission to
   enable new spending. Stop on exhaustion; no API fallback.

[CLI flags/tools](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference),
[official SDK metadata methods](https://github.com/github/copilot-sdk/blob/main/python/copilot/client.py),
[session limits](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/set-session-limit).

## Metadata commissioning

`--metadata-only` never claims a task, creates a session, or sends a prompt.
`metadata.py` implements the official SDK's Content-Length JSON-RPC transport
using only `connect`, `auth.getStatus`, `models.list`, `account.getQuota`, and
`runtime.shutdown`, in that order, with protocol version 3. It starts one native
process with an exact clean environment and an isolated writable profile restored
from the durable broker. The process stops before refreshed credentials are
committed and the lease released. Timeout, unrecognized framing, owner mismatch,
or uncertain refresh quarantines the lease; no automatic retry occurs.

The account check verifies the known GitHub login and user-auth route. The native
status does not return an email or immutable subject; those bindings must come
from independently verified enrollment. Model effort choices remain literal
choices, with no guessed strength ordering. Quota pools use the durable canonical
account reference; unlimited and missing quotas are never treated as free usage.
No quota response alone authorizes spending or establishes a provider spend cap.

The transport/schema contract comes from the installed official Copilot SDK's
`client`/generated RPC definitions, not an inference request. It remains subject
to the pinned Linux CLI's first credentialed metadata commissioning.

Cloud acquisition uses the HTTP broker client, a protected
`/run/config/credential-broker.json` containing the controller-issued execution
grant, and Google metadata identity. It never falls back to direct Firestore or
Secret Manager access. The execution must match the Cloud Run environment, and
the acquisition request ID is written before the request.

Validation: 24 offline tests pass, including native protocol, owner, quota,
refresh-ordering and failure cases. No credentialed Linux metadata call has run.
The earlier native-only image digest is
`sha256:d9120c4fcb3a658d1cbfc50c0e85972989c4ef362039298bb70a36a6a41b7abb`;
Cloud Build `0176d74c-2d65-458c-a8b4-b28168a5b514` succeeded. This is not provider
authentication, task readiness, or an inference result.
