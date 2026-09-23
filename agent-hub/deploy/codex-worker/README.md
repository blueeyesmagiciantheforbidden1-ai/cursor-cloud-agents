# Codex Linux worker package foundation

This directory prepares a pinned, on-demand Linux amd64 Codex package. The native image build `20260921-native1` completed successfully; the recorded digest is `sha256:010a8a97cf7771f132b694bf04aaaa67ee3df1ab6b5718e1b07748292581b26e`. Credentialed Linux metadata, project execution and inference remain uncommissioned. Its default image entrypoint prints `dispatch_enabled: false`; it does not consume hub tasks.

## Cloud heartbeat commissioning

`package_status.py --heartbeat-only` now sends the same payload and success receipt as the shared worker's no-inference commissioning mode. It performs one fixed metadata identity GET and one fixed HTTPS `POST /v1/workers/report` to `https://runcrew-hub-kdhodumsza-uc.a.run.app`. It reports `status: offline`, `auth_status: unknown`, empty usage and no current task, because this on-demand process exits after its acknowledgement. It does not claim provider login, model availability or project readiness.

No provider credential or native executable is read or started. Only the dedicated Codex hub-role token is supplied in `HUB_AGENT_TOKEN`, using the deployment's exact secret version. The configuration mount is exactly `/run/config/worker.json`, a root-owned, non-writable regular file, at most 16 KiB. Its `expected_account_ref` is a required owner configuration hash, not verified provider evidence. `worker_id` defaults to `codex-cloud`; the intended deployment uses Ryan's protected owner binding. Project, hub, provider, token environment name, executable path, workspace and authentication mode cannot be overridden. Inherited environment variables, including the hub token, are removed before the network calls. No redirects, proxies, subprocesses or retries are used. The completed acknowledgement is:

```json
{"mode":"heartbeat-only","telemetry_delivered":true,"worker_status":"offline","provider_login_verified":false,"inference_performed":false,"claims_or_completes_tasks":false}
```

The heartbeat overlay is prepared and tested locally; this change does **not** itself build or deploy it. Reuse the successful native image to avoid downloading or testing the native CLI again:

```sh
docker build --platform linux/amd64 -f deploy/codex-worker/Dockerfile.heartbeat \
  --build-arg CODEX_BASE='us-central1-docker.pkg.dev/project-0c6d31fa-509e-4116-a2c/runcrew-hub/codex-worker@sha256:010a8a97cf7771f132b694bf04aaaa67ee3df1ab6b5718e1b07748292581b26e' \
  -t codex-worker:heartbeat deploy/codex-worker
```

The package-only build context stays unchanged; no core worker dependencies are copied. The overlay accepts only the exact prior digest and copies an allowlisted heartbeat module, entrypoint and offline tests. It runs as UID/GID 10001. The approved commissioning deployment should retain one task, parallelism 1, zero retries, 60 seconds, 512 MiB, no schedule, no provider mounts, resource-scoped hub invoker access, and access only to `runcrew-worker-codex-hub` plus `runcrew-worker-codex-config` secrets. Configure the Cloud Run argument `--heartbeat-only`. `--once` remains blocked with exit 2, and the default remains a local readiness report.

The shared cloud commissioning helper must add Codex as a supported agent and verify this package's `heartbeat.py` and `package_status.py` snapshot, rather than expect `agent_hub/worker.py` in the image. Native authenticated transport remains separate work below.

## Reproducible native package

The official stable release is `0.155.1`, published September 18, 2026. The complete `codex-package-x86_64-unknown-linux-musl.tar.gz` archive is 138,838,055 bytes with SHA256:

```text
a65b895c6ac1a73629bbe4b864640c86133e94a43b4d67b3103044e1a306d5a2
```

The downloaded archive and **every regular member** were actually hashed. The initial archive audit did not execute it; the subsequent cloud build ran only version/schema checks. `provenance/archive-audit.json` pins every path, size, type and content digest. The complete layout includes Codex, its code-mode host, ripgrep, bubblewrap, zsh and bundled resources; copying only `bin/codex` would lose helpers. Native `bin/codex` SHA256 is `0753dfe1d8b87a52436deb13eb1c549661ef4c84fee2c5aa688385eebeccb761`.

`provenance/release.json` is the official GitHub release response; `npm.json` is the matching official npm metadata. `npm-source-audit.json` records SHA512 integrity verification of the tiny npm wrapper archive and SHA256s of the wrapper sources. Those sources were read: they resolve the platform package, select `vendor/<target>/bin/codex`, forward argv/signals, and add package-manager metadata. We invoke the native release directly instead. No npm install scripts or remote installer ran. The documented installer URL returned HTTP 403 during this audit. GitHub provides a companion Sigstore file, but its signature was **not** independently verified; the current pin is a content digest retrieved from the official release API and checked against the archive.

`build_native.py` downloads only that fixed release URL, checks byte count and SHA256, verifies all member hashes before extraction, rejects links/traversal/duplicates, and keeps executable permissions without setuid/setgid. It then runs only `--version` and `app-server generate-json-schema` in a fresh unauthenticated home. Its schema gate checks required protocol fields and records generated schema digests. This proves a field surface, not runtime semantics. The previously authenticated Windows binary is `0.155.0-alpha.9.2`; authenticated Linux0.155.1 protocol and subscription compatibility remain commissioning checks.

The original Dockerfile requires an explicitly digest-pinned Linux amd64 Python 3.12 base containing certificates, bash and the desired project tools (including git). There is no mutable default and no apt operation. Use it for a full native rebuild; use the cheaper overlay above for this heartbeat change. A full build must use this directory alone as its build context:

```sh
docker build --platform linux/amd64 \
  --build-arg WORKER_BASE='reviewed-registry/python-project-tools@sha256:<actual-reviewed-digest>' \
  -t codex-worker:0.155.1 deploy/codex-worker
```

The placeholder deliberately is not runnable. Never put auth files, private enrollment directories, provider keys, or account metadata in the build context. The image runs as UID/GID 10001 and supplies `/workspace/default`; project dispatch remains disabled.

## Two accounts, refresh-safe state

`ryan` and `blueeyes` are distinct profile selectors. Their expected owner/account hashes, provider account identities, quota-pool references and secret references come from private trusted configuration; no account email or credential is embedded here. Switching profiles means a new native process and a new private `CODEX_HOME`. Do not copy desktop auth or reuse one profile's file for the other.

`credential_state.RefreshSession` restores opaque bytes from a durable broker lease into a **new protected home**, never an existing home. It does not parse tokens. After the native process and all its refresh work have stopped, it reads the resulting bounded `auth.json`, commits through the broker, and only then releases the lease. Uncertain writes, stale fences, or lost acknowledgments quarantine the profile and retain the refreshed file for reconciliation; they do not trigger login, inference retry, stale restoration, or lease release.

The concrete Google REST broker now exists at `agent_hub/cloud_credential_broker.py`, with its service/worker integration still separate. It is **not automatically imported or provisioned by this package**. The runtime must provide:

1. Atomic acquisition keyed by the underlying provider account, returning profile, owner hash, monotonic fence, current credential version and opaque bytes. Profiles sharing an actual account must share the lock.
2. `assert_current(lease)` and lease renewal throughout the native process lifetime. Loss of ownership stops dispatch and the process. Expiry alone cannot authorize a successor while an old process could still refresh credentials.
3. `commit(lease, opaque_bytes) -> version`, atomically checking fence and predecessor version. Idempotency must identify the exact intended write; a timed-out commit must be reconciled before another process starts.
4. `release(lease, version)` only after durable writeback; `quarantine(lease, reason)` on uncertainty. Process death must leave a recoverable hold, not silently restore the old secret.

Secret Manager can hold versioned encrypted credentials, but creating a secret version alone supplies neither a per-account lease nor compare-and-swap publication. A controller transaction must publish which version is current. The helper's `native_stopped=True` and protected-directory assumptions must be guaranteed by the owning supervisor; they are not OS enforcement. It does not clean up private files or implement cloud ACLs.

Metadata-only commissioning can use this same refresh-safe lifecycle. It does not require a task prompt, a funded inference route, or an available included quota. Account/config/model/rate reads and sanitized heartbeat reports may proceed while project dispatch is not yet commissioned. The existing enrollment collector is an example of the read-only RPC sequence; it is not copied with credentials into this image.

## Minimal runtime integration contract

The controller must bind one immutable room-wide model plan to all participating workers. Independent per-worker catalog refreshes can otherwise choose the same underlying model across revisions. The existing shared model-policy module remains responsible for strongest eligible distinct canonical models, alias resolution, account availability, freshness and cost policy. This package does not collect rankings or replace that policy.

Create one native app-server process with the pinned native executable, a minimal environment, isolated `CODEX_HOME`, forced ChatGPT authentication and file credential storage. The verified Windows contract uses `app-server --stdio --disable hooks --disable multi_agent --disable multi_agent_v2`; confirm these exact flags in the pinned Linux binary before enabling the Linux profile. Do not inherit API keys, provider endpoints, proxy credentials, desktop home/config, or unrelated environment variables. The transport owns bounded JSON-lines framing, request IDs, a deadline, process termination, and `initialize`/`initialized`. It must reject unsolicited tool/approval requests during preflight.

`protocol_gate.PrePromptGate` is an executable **adapter contract**, not a subprocess transport. Its injected `rpc(method, params)` must remain bound to that one process. It makes only these metadata calls:

```text
account/read (refreshToken=false)
model/list (all pages, bounded)
account/rateLimits/read
config/read (effective project cwd, includeLayers=true)
configRequirements/read
```

It checks the expected owner hash, backend account hash, visible exact model, highest supported effort, provider-reported permission for included usage, and the digest of a previously reviewed effective configuration. Unknown future effort levels fail closed for a policy update. The returned receipt is sanitized and never claims the worker is connected. A config hash must come from a trusted review of actual effective config; accepting the first observed hash would defeat the check.

After preflight, `thread_request()` prepares an empty ephemeral thread request for `workspace-write`, approval `never`, the exact model and effort. The transport may submit it only within the commissioned project-work profile. `accept_thread()` checks the same-process applied model/provider/effort/cwd/approval/sandbox response before `turn_request(prompt)` prepares one bounded request. The module **never sends a thread or turn**, and request data is not authorization to dispatch. Applied thread metadata verifies configuration; it is not proof of the model that actually served a later turn. The future worker must retain any provider execution metadata, reject downgrades/mismatches, and never claim model independence from labels alone.

The project profile keeps native `workspaceWrite`, writable roots exactly `/workspace/default`, network access false, and excludes implicit `/tmp`/`TMPDIR` write roots. It supports normal project reads, writes and shell commands within that sandbox. It never chooses `dangerFullAccess`, `externalSandbox`, privileged containers or the sandbox-bypass flag. `approvalPolicy=never` means denied operations fail rather than escalating.

**Credits are authorized by the user but not yet integrated into this package gate.** Included permission is used first. This gate currently blocks inference when `ordinaryUsageAllowed` is false or unknown; it does not block metadata collection. A later credit branch should use verified existing provider balance/cap enforcement and autoreload/purchase state, preserving the provider's cap without inventing a new hub cap. Native `credits.balance` alone has no verified currency/cap semantics and is insufficient. No credit purchases, reset redemptions or API fallback are implemented.

## Concrete commissioning checks still needed

- Run the pinned Linux authenticated metadata and runtime flag checks. Version/schema field checks passed during the native image build. Bind its actual native SHA256 to a fresh account catalog; a Windows executable digest is not a Linux digest.
- Exercise normal native workspace-write on the actual Linux host: allowed repository edit/command, denied outside write and denied network. Bubblewrap/user-namespace support may differ in Cloud Run. Failure is a host/profile issue, never grounds for sandbox bypass.
- Inspect whether native read/shell tools can access `CODEX_HOME/auth.json`, account state, control-plane mounts or other profiles. `chmod 600` does not protect against tools running as the same UID. Use an appropriate credential/process/mount separation if necessary. This is a concrete confidentiality check, not a requirement for cryptographic isolation certification.
- Review effective configuration and tool inventory for project/user/system MCP servers, hooks, apps, skills and plugins before dispatch. `--ignore-user-config` does not by itself establish project or plugin isolation. The observed schema explicitly says `disabledPluginIds` **does not yet filter plugin capabilities**, so it is not an enforcement mechanism.
- Wire the durable broker and bounded transport, then the existing hub worker claim/heartbeat/completion contracts. Keep native exit, timeout and uncertain outcome handling fenced; never replay a possibly accepted task automatically.

These gaps do not prevent no-inference account metadata commissioning or a cloud heartbeat proving an idle worker process is alive. Such a heartbeat must say `project_dispatch_ready: false` until the relevant project-work checks and bindings pass.

## Verification

Run from the repository root:

```sh
python -m unittest discover -s deploy/codex-worker -p 'test_*.py' -v
```

The 19 existing package tests cover archive containment/digest/link/duplicate failures, schema drift, profile separation, refresh persistence, stale/uncertain lease outcomes, wrong account, exhausted/unknown included quota, effort downgrade, changing config, pagination loops, applied sandbox mismatch, and stale preflight. Ten additional heartbeat tests check exact metadata audience/hub report, environment stripping, no native calls, rejected target changes, no redirect/retry, response bounds, root rejection, sanitized failures and explicit mode selection. They use opaque synthetic bytes and fake HTTP/native metadata transports, with **no provider/network calls**. They do not prove deployed IAM, Linux sandbox behavior, cloud isolation, real credential refresh, or live inference.

Official sources reviewed: [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), [authentication](https://learn.chatgpt.com/docs/auth), [app-server protocol](https://learn.chatgpt.com/docs/app-server), [non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode), and the explicitly requested [official pinned release source](https://github.com/openai/codex/releases/tag/rust-v0.155.1).
