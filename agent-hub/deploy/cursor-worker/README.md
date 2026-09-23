# Cursor on-demand Linux package

Prepared for a Google Cloud Run job. No image has been built or deployed for
this package, no Cursor credential has been copied into it, and no inference
has been performed. Task execution is currently blocked for the concrete
billing/model/integration requirements below; the package supports offline image
verification, the hub's explicit no-claim commissioning heartbeat, and a bounded
native account/model metadata mode prepared for later Linux execution.

## Release evidence

The [official installer](https://cursor.com/install), linked by Cursor's
[installation documentation](https://cursor.com/docs/cli/installation), selected
`2026.09.18-9a7762b` during this audit. The exact Linux x64 archive returned HTTP
200 and was downloaded for static inspection. The prior Windows archive failure
does not apply to this Linux artifact.

| Artifact | Recorded value |
|---|---|
| URL | `https://downloads.cursor.com/lab/2026.09.18-9a7762b/linux/x64/agent-cli-package.tar.gz` |
| Archive bytes | `182574768` |
| Archive SHA-256 | `b1308f5a2fc05458b9d8966752986bb23a971bbcc67c842c1df94c4b8132bad9` |
| Expanded bytes | `582097800` |
| Native CLI SEA SHA-256 | `a4bc62519fcb2aa17924454acadfe5176e3edab149da5986e32610a772fc552d` |
| Bundled Node SHA-256 | `e0e46d3a1c0667117303412647cafcbcefb1be7612493015ec8fd6b7440162a4` |

`provenance/release.json` records the archive and complete member inventory;
`native-audit.json` records selected native artifacts and the vendor launcher;
`cli-contract-audit.json` records bounded source observations;
`metadata-contract.json` records 23 verified source assumptions for the metadata
collector, including exact per-source-file hashes and offsets. This provenance is
an observation over official HTTPS with a locally pinned digest. No publisher
signature was discovered or verified; the receipts explicitly say so.

The official launcher is a small Bash script that invokes the packaged native
Node executable and `index.js`. The package also contains native ELF
`cursor-agent-sea` and `cursor-agent-worker-sea`, but the published installer
selects the launcher. This recipe retains that supported entrypoint rather than
assuming the alternate ELF has an identical standalone interface.

The source's signed-SEA re-execution path is guarded by **macOS plus computer
use**. It is not the Linux metadata entrypoint. The smallest supported entrypoint
to commission is the vendor Bash launcher plus bundled Node, JavaScript chunks
and their native dependencies. The two alternate SEA files total 283122048
expanded bytes. Removing them may reduce the image, but this first recipe keeps
the intact verified distribution until Linux runtime checks establish that the
intended metadata/project paths need neither. Source inspection does not prove
SEA/Bash entrypoint behavioral equivalence, so switching to SEA solely to save
dependencies is not justified.

The build fetches only the pinned URL, refuses redirects, checks size and digest,
validates every archive member, and rejects links, devices, duplicate paths and
path traversal before extraction. It writes a per-file integrity manifest.
Runtime files are root-owned; the job runs as UID10001 with a private home and
workspace. Runtime image verification hashes the distribution without executing
Cursor. Updating the CLI requires fresh provenance and contract validation.

## Build and commission later

From the source root, after an operator elects to build:

```sh
python -B -m unittest discover -s deploy/cursor-worker -p 'test_*.py'
docker build --platform linux/amd64 -f deploy/cursor-worker/Dockerfile -t runcrew-cursor:2026.09.18-9a7762b .
docker run --rm --network none runcrew-cursor:2026.09.18-9a7762b --check
```

All 31 package and metadata tests pass offline, with native subprocesses mocked.
The Docker recipe itself has not been executed.
Its narrow Docker ignore file excludes the local archive cache, private homes,
credentials and tests. An external Cloud Build upload must independently apply
the same narrow file allowlist. Pin the resulting image digest before any job
execution; the Python base tag is not yet an immutable digest.

For a later heartbeat, mount `/run/config/worker.json` containing only a verified
`expected_account_ref` (normalized owner-email SHA-256) and optional `worker_id`.
Inject only the dedicated Cursor `HUB_AGENT_TOKEN`; use the existing private hub
invoker service identity and `--heartbeat-only`. Do not mount `CURSOR_API_KEY`
for this check. The constructed environment removes provider/API credentials,
proxies, Node injection options and unrelated hub roles. Cloud Run identity is
obtained from metadata. One acknowledged report records `offline/auth unknown`
and exits without claiming work. Missing model metadata does not prevent this
connectivity check. Configure one task, one parallel execution and zero automatic
retries; no always-on resource is required. No production memory size has been
established for actual Cursor execution.

## Native metadata commissioning contract

`--metadata-only` is a separate mutually exclusive entrypoint mode. It requires
the same bounded config with `expected_account_ref` and an injected
`CURSOR_API_KEY`, but no hub token or hub access. It verifies the image first,
creates a new 0700 temporary HOME/config and empty working directory, and runs
the following exact native commands through `/opt/runcrew/cursor/cursor-agent`:

1. `models` invokes the CLI's supported API-key exchange and account catalog.
   Its human-readable output is captured and discarded. The native credential
   manager may write refreshed credentials only inside that temporary home.
2. `status --format json` calls `getMe` using those credentials. The collector
   requires a returned email matching the expected normalized SHA-256; a bare
   `authenticated` status after a failed identity fetch is rejected. Output
   contains the matching hash, not the email or any token.
3. `acp` receives only JSON-RPC `initialize` (protocolVersion 1, file and terminal
   capabilities false) followed by `cursor/list_available_models` with `{}`.
   It never calls `authenticate`, `session/new`, `session/load`, `session/prompt`,
   or any permissions/tool methods. The API-key pre-authentication and native
   refreshed credentials avoid the ACP browser-login method entirely.

The CLI's source-backed extension returns model IDs and selectable
`configOptions`. Parameters categorized `thought_level` reveal configurable
reasoning choices; values and their order are recorded without assuming an
effort ranking. The collector never selects a model or downgrades one. Source
inspection establishes that single-value parameters are omitted from this
projection and a failed fetch can become an empty list. Accordingly an absent
effort option is **`not_exposed_in_cli_catalog`**, never evidence for a
`fixed`/`not_configurable` maximum-model policy. An empty catalog fails explicitly.

The generated CLI settings are projected to selected model/parameters, Max Mode,
approval mode and disabled Bedrock state when present. This is an isolated
local-settings observation, not account spending or effective managed-policy
proof. No credential-store file is parsed. The entire temporary home, including
native refreshed credentials, is deleted after the probe. Persistent cloud
enrollment is a separate operation and must preserve credentials privately.

The child environment admits only fixed runtime paths, this one provider key,
the supported file credential store and presentation settings. All processes
share a 90-second deadline, bounded stdout/stderr, no retries, and process-group
cleanup. The ACP reader rejects unexpected server requests instead of approving
them. Fresh empty home/workspace paths contain no MCP configuration; Cursor's
official ACP documentation confirms dashboard-managed MCP servers are not
supported by ACP. [ACP](https://cursor.com/docs/cli/acp).

This uses the existing user API key and requests **no additional OAuth scopes**.
It does contact native authentication, account, catalog and settings/telemetry
services when a future operator runs it. It does not request model inference,
start a cloud agent, create a session, change billing, claim a hub task, or mark
a worker ready. Its result keeps `task_execution_enabled=false` and
`provider_on_demand_disabled_verified=false`. No credentialed metadata call has
been executed during package preparation.

## Billing prerequisite

Cursor documents two included usage pools and separate on-demand billing.
Selecting Composer does not by itself enforce included-only use. The model is
drawn from the Cursor Models pool, while overage can be charged when enabled.
[Models and pricing](https://cursor.com/docs/models-and-pricing),
[Composer 2.5](https://cursor.com/docs/models/cursor-composer-2-5).

The root task's fresh browser observation on 2026-09-21 at approximately 15:34Z
recorded Ultra, Cursor Models 6% used, Other Models 100% used, Grok Bot 39% used,
and on-demand usage $205.63 with Unlimited selected. Those are observed account
facts, not an authorization for this worker to spend more. The user is deciding
whether to disable account-wide on-demand spending; no approval or settings
change is assumed by this package.

The practical included-only prerequisite is a freshly verified provider-side
Disabled on-demand setting for the same authenticated account, plus fresh
included allowance. No documented CLI-only included-billing switch was found.
Local estimated task cost, a small timeout, or a positive remaining percentage
is not a substitute. Cursor's spending documentation permits disabling on-demand
and explains that ordinary cap enforcement may lag; leaving Unlimited active
does not bound new charges. [Spend limits](https://cursor.com/help/account-and-billing/spend-limits).

## Model, project tools and remaining integration

The current documented own-model candidate is `composer-2.5`; this is not yet a
fresh account-effective maximum-model selection. Cursor's model parameters are
model-dependent, and the inspected CLI source has neither a `--effort` nor
`--reasoning-effort` flag. It does support bracket overrides in `--model`,
including model-specific effort, context and speed parameters. The absence of
standalone effort flags therefore does not prove fixed effort. The Composer page does not supply a maximum-effort
value. Do not invent `max`, confuse Max Mode context with reasoning effort, use
an automatic alias, or claim fixed maximum effort without native/catalog
evidence. The shared fleet model plan must also enforce underlying-model
diversity across the other providers.

Cursor supports print-mode file and shell work and documented permission
allowlists. The future project adapter must use the intended workspace, verify
current CLI behavior, configure allowed project reads/edits/tests, isolate
customizations/MCP/hooks, and enforce the container/IAM/network boundary. The
package does not enable `--force`, `--yolo`, unrestricted permissions, or automatic
MCP approval. The existing shared adapter still uses ask mode, and its project
mode does not support Cursor yet. The `read_only` field in commissioning config
exists only to build a harmless startup command preview; it does not redefine
the requested eventual project capabilities.
[Parameters](https://cursor.com/docs/cli/reference/parameters),
[permissions](https://cursor.com/docs/cli/reference/permissions),
[configuration](https://cursor.com/docs/cli/reference/configuration).

The existing Cursor user key has already been account-verified separately by the
root task. It remains at its private existing location and is not read by this
package. Cursor documents `CURSOR_API_KEY` for CLI automation; that authentication
method alone does not prove the account's billing control or quota state.
[Authentication](https://cursor.com/docs/cli/reference/authentication).

Before inference, finish the provider-side billing choice, obtain fresh native
account/model/effort metadata, verify the Linux CLI in its rootless image, and
implement the reviewed project adapter with same-process actual-model checking
and secret isolation. This recipe launches no Cursor-hosted Cloud Agent VM or
`agent worker start` bridge; project compute stays on Google Cloud.
