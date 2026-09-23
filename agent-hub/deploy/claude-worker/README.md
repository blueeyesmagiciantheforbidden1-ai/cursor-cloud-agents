# Claude on-demand worker image

This directory packages a single CPU-only Claude worker job. It does not create cloud resources, log in, register a worker, or run an AI prompt during its build. It uses the existing private hub origin and executes at most one claimed task before exiting.

## Build and offline check

From the `agent-hub` source root, on a Linux-capable Docker host:

```sh
docker build --platform linux/amd64 -f deploy/claude-worker/Dockerfile -t runcrew-claude:2.1.275 .
docker run --rm --network none runcrew-claude:2.1.275 --check
python deploy/claude-worker/test_packaging.py
```

`Dockerfile.dockerignore` sends only Python source and the three build/runtime files into this build context. Runtime credentials, metadata receipts, tests, local provider homes and unrelated files are excluded. A separate Cloud Build upload must use an equally narrow allowlist; Docker ignore does not control an independently uploaded archive.

The builder downloads the official release key, manifest, detached signature and native Linux binary. It checks the documented signing-key fingerprint, verifies the detached signature, and checks the manifest against the independently recorded version/checksum/size below. Only after those checks does it run `claude --version`. GPG exists only in the builder stage. The runtime includes Python, system CA certificates and libstdc++; it installs no Node, Google Cloud SDK, Firestore client or OAuth server dependencies. Python starts in isolated mode so inherited `PYTHONPATH` cannot redirect imports.

| Pin | Value |
|---|---|
| CLI | `2.1.275`, `linux-x64` |
| Binary SHA256 | `13586f3150a7ca1655f36e1dba759fb404e0f7cf7021d4a3dcd5e6f604e56156` |
| Binary bytes | `232059192` |
| GPG primary fingerprint | `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE` |

Source: [official setup/integrity documentation](https://code.claude.com/docs/en/setup#binary-integrity-and-code-signing), [2.1.275 manifest](https://downloads.claude.ai/claude-code-releases/2.1.275/manifest.json). The version is deliberately the audited CLI contract; updating it also requires revalidating `subscription_auth.py` and adapter behavior. The Python base tag is not an immutable digest; record the resolved base/image digest in the deployment receipt before production use.

## Runtime contract

- Root-owned `/opt/runcrew/claude/claude`, mode0555, with root-owned non-writable parent paths.
- Root-owned `/opt/runcrew/claude/runtime-profile.json`, mode0444, exact schema: `{"schema":1,"cli_version":"2.1.275","sha256":"13586f3150a7ca1655f36e1dba759fb404e0f7cf7021d4a3dcd5e6f604e56156"}`.
- UID/GID10001; fresh `/home/worker` and `/home/worker/.claude`, mode0700. No imported desktop state or provider credential files.
- Fixed workspace `/workspace/default`; supply only the intended source snapshot here. Claude customization files are rejected by the current runtime audit even when other source files are present.
- Mount a small operator-owned JSON file at `/run/config/worker.json`. Required field: `expected_account_ref`, the SHA256 of the normalized intended provider-account email. Optional `worker_id` and integer `timeout_seconds` (30..300, default180). Never put credentials in it.
- Mount the real trusted selection bundle at `/run/config/model-catalog.json`. The entrypoint forces `model_policy_required=true`, the fixed catalog path and `billing_policy.mode=subscription_only`. It does not fabricate catalog observations or entitlements. A missing catalog does not prevent a no-task heartbeat; claimed-task launch must remain blocked by the worker's model policy until valid evidence exists.
- For task execution, inject `HUB_AGENT_TOKEN` for the Claude worker role and `CLAUDE_CODE_OAUTH_TOKEN` through runtime Secret Manager bindings only. The latter must come from the provider's supported account enrollment; this package does not obtain one. Do not pass API keys or a hub manager/admin credential. Heartbeat-only commissioning requires only the hub-role credential.

The entrypoint constructs the provider environment using `claude_environment`, retains only the worker-role hub credential for the controller, and clears inherited variables. The worker removes `HUB_*` before launching Claude. Google Cloud Run identity comes from its metadata service, not a downloaded service-account key. Restrict the job service account to the private hub invoker role and these exact required secrets.

Run as an on-demand **job**, one task and one parallel execution, no automatic retries after uncertain task execution. Do not configure an always-on worker service, GPU, or startup schedule. The package imposes no memory allocation: measure actual bounded startup and prompt memory before choosing the smallest reliable job limit. Anthropic's general setup recommendation is 4GB+; lower limits have not been demonstrated here and must not be presented as supported production sizing.

## Connectivity commissioning without inference

Pass `--heartbeat-only` to the entrypoint for a single commissioning execution.
Mount `/run/config/worker.json` and inject only the dedicated `HUB_AGENT_TOKEN`.
Do not mount a provider token or model catalog for this check. The image and
native binary integrity checks, bounded config validation, Cloud Run metadata
identity and exact hub role still apply. The heartbeat environment contains only
the fixed home, executable path, locale, temporary directory and hub credential;
it drops provider secrets even if accidentally inherited.

This mode publishes one `/v1/workers/report` receipt and exits. It never requests
a task lease, starts a provider process, reads the model catalog, or checks
provider login. Delivery errors and missing acknowledgements return nonzero.
The report is deliberately `status=offline`, `auth_status=unknown`, with no usage
or task result: the on-demand job has completed. The current dashboard will show
a configured offline worker and an offline/degraded warning, not inference
readiness. Confirm the timestamp of the hub's received record alongside the
execution result. `--once` is different: it polls and may execute one queued task.

Cloud Run job secret volumes are root-owned; ensure the mounted config is
readable by UID10001. Pin environment secrets to explicit versions. The
entrypoint retains its narrow rejection of a symlink at the fixed config path;
no general protected-path rule has been relaxed. Cloud Run's documentation does
not promise the file's symlink representation, so successful commissioning must
confirm the actual mounted layout. The job identity needs access only to the
commissioning config/hub secrets and invoker permission on the private hub.
See [Google's job secret documentation](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets).

## Verification and remaining work

The 17 offline packaging tests cover binary/version substitution, malformed manifests, wrong/missing signature evidence, immutable runtime choices, bounded execution settings and environment/secret isolation, including the provider-free heartbeat mode. The focused heartbeat and worker/billing/telemetry regression suite passes 44 tests. Public metadata verification can be run without downloading the binary:

```sh
python deploy/claude-worker/build_profile.py --metadata-only --output /tmp/claude-public-metadata
```

The official manifest signature was successfully verified on 2026-09-20. The public receipt is `verified-release-metadata/build-receipt.json`; manifest SHA256 is `dc27fe6bb284b4b97fdee416a21bda71ad591233724c3e36c56d4a60d11aecdc`. That metadata-only receipt deliberately records `binary_verified=false`; the later Cloud Build performed the full container verification.

`--check` verifies image integrity only; its output explicitly reports no provider login, inference or hub connection. A real deployed heartbeat is different from a successfully authenticated provider contribution.

The local Docker Linux engine was unavailable, but Google Cloud Build
`f0a6eb8b-8618-49b6-b502-f5f2dc76b171` subsequently succeeded for
`20260920-claude1`. Its recorded immutable image is:

```
us-central1-docker.pkg.dev/project-0c6d31fa-509e-4116-a2c/runcrew-hub/claude-worker@sha256:9d23d134ef2d083069907ef07bdaacd4a9ebaefbcfa294cd08dbe3a60dafcff3
```

That image predates `--heartbeat-only`; rebuild this source before using the new
commissioning mode. Provider enrollment exists separately, and its credentials
are not embedded in the image. At this documentation update, no live cloud
worker execution has been verified. A trusted live model catalog, same-process
effective-policy validation and real cloud dispatch remain separate from the
image build and connectivity heartbeat. Never replace unknown inference policy
evidence with an operator assertion or disable `subscription_only` merely to
make the image produce a result.

This commissioning change adds the worker's explicit heartbeat-only branch and
its packaging/tests; it makes no cloud resource, account setting or provider
inference change by itself.
