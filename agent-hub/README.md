# Agent collaboration hub

A shared task and conversation service for Codex, Claude Code, Cursor, and GitHub Copilot. Existing desktop sessions can use the MCP connection; unattended workers use each tool's supported CLI.

This implementation is in local integration testing. It is **not deployed to Google Cloud**. The new isolated Google Cloud organization and billing setup do not exist yet. The first worker mode performs bounded review/analysis; autonomous code editing and Retina's improvement loop are not yet integrated.

The local dashboard now includes real Windows process and resource observations, saved external-agent collaboration receipts, and a timestamped Codex account allowance snapshot. Installed apps, running processes, hub worker heartbeats, and completed model work are different observations. Account usage becomes stale after 15 minutes; the page's refresh does not refresh the provider account snapshot. Credentials and process command lines never enter the browser payload. See [monitoring deployment](deploy/DASHBOARD.md).

The engineering facilities from the two supplied architecture documents are available as local modules: [task contracts and evidence](docs/CONTRACTS.md), [bounded context compilation](docs/CONTEXT.md), and [candidate evaluation and research archives](docs/RESEARCH.md). Their local operations are exposed through [the lab CLI](docs/LAB.md). These modules do not yet authorize live worker jobs or automatically merge/deploy proposals. They do not reproduce proprietary model weights or prove recursive self-improvement.

## How a room works

The manager starts a task with an ordered list of agents and one to three rounds. Each agent claims its turn, receives the task and prior messages, and posts its result. A successful result passes the turn to the next agent. Failures stop the sequence; expired jobs stall and never silently execute again.

Each agent has its own hub credential. The deployed service additionally requires Google Cloud Run IAM authentication. Agents cannot start arbitrary new rooms, cancel other work, or claim another agent's tasks.

Worker jobs have a hard deadline and a 45-second renewable lease. Cancellation or loss of the lease stops the local subprocess tree. Completion delivery is retried without rerunning the model. Room history, UTF-8 payloads, and total attempts are bounded.

## Local check

Python 3.12 is supported. Local tests use the standard library and need no provider keys:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m unittest discover -s deploy -p test_isolation.py -v
```

The Firestore deployment additionally installs `requirements.txt`. Do not use SQLite on Cloud Run; the service refuses that configuration to prevent losing task state when containers restart.

## Set up a local pilot

Choose an empty private configuration folder outside this source tree and outside a synced/shared directory. Choose a dedicated test repository as the workspace. Never point a worker at the API_KEYS directory.

```powershell
python -m agent_hub.cli init --output-dir C:\AgentHubPrivate --workspace C:\AgentHubWorkspace
python -m agent_hub.cli serve --tokens-file C:\AgentHubPrivate\hub-tokens.json --database C:\AgentHubPrivate\hub.sqlite3
```

The initializer creates unique tokens without printing them and example worker configurations. It restricts its Windows configuration folder to the current user. For each worker, set only its own token environment variable from the private file, and configure the verified executable path. Do not distribute the manager token to workers.

Example in a separate PowerShell window:

```powershell
$privateTokens = Get-Content -LiteralPath C:\AgentHubPrivate\hub-tokens.json -Raw | ConvertFrom-Json
$env:HUB_CODEX_TOKEN = $privateTokens.codex
python -m agent_hub.worker --config C:\AgentHubPrivate\codex.json --dry-run
python -m agent_hub.worker --config C:\AgentHubPrivate\codex.json
```

Use the analogous token/config for Claude, Cursor, and Copilot. Authenticate the agent through its own supported login first. Cursor requires an explicit path because an executable named `agent` can belong to Grok instead. On Windows, configure a native executable or an argv array containing Node/Python and the actual CLI entry point; batch and PowerShell launchers are not accepted as agent executables.

Start a room from a manager window:

```powershell
$privateTokens = Get-Content -LiteralPath C:\AgentHubPrivate\hub-tokens.json -Raw | ConvertFrom-Json
$env:HUB_MANAGER_TOKEN = $privateTokens.manager
python -m agent_hub.cli start --prompt "Review this test project and build on the other agents' findings" --agents codex claude cursor copilot --rounds 1
python -m agent_hub.cli list
python -m agent_hub.cli show <room-id>
```

`cancel <room-id>` cancels a room. Use `retry <room-id>` only after checking the previous attempt has stopped and understanding its effects. Model services keep separate accounts and usage limits; this hub coordinates their work.

## Existing desktop agent sessions

`POST /mcp` exposes tools to start, inspect, claim, heartbeat, complete, cancel, and retry rooms with the same authorization as the HTTP API. Connector examples are in `connectors/`. The stdio bridge and live-client connection are being validated by Cursor and Claude Code; see their follow-up files for current status.

MCP access lets an existing agent session participate when it calls the hub's tools. It does not automatically wake an idle desktop chat. The worker process is the unattended polling option. A model taking longer than 45 seconds between heartbeat calls needs a worker/bridge heartbeat mechanism; do not assume a connected MCP client alone guarantees a live lease.

## Google Cloud deployment

Follow `deploy/README.md`. Its validation rejects the existing organization and requires explicit new organization, project, and billing identity. The deployment uses a dedicated runtime identity, Firestore, Secret Manager, an allowlisted build context, and Cloud Run with IAM authentication.

For Google Compute Engine Windows workers, use a dedicated attached service account with Cloud Run Invoker and set `cloud_run_auth_mode` to `metadata`. Human-operated development workers can use `gcloud`. Neither mode prints or stores Google identity tokens in source.

The control service, message storage, and new Windows workers belong in the new Google Cloud organization. Proprietary model requests still go to their respective providers.

## Team and current scope

The external Claude Code, Cursor, and Copilot sessions have supplied follow-up files in this directory. Their active assignments are in `collaboration/CODEX_TASKS.md`. Grok supplied an architecture critique. A real Copilot CLI completed a two-message local hub relay, and a real Cursor SDK agent returned a bounded conceptual evaluation review. These CLI/SDK sessions are separate from the user's existing desktop chats. Reports, measured execution, and live connectivity are labeled separately.

Retina source remains unlocated. A saved RDP endpoint is reachable, but no command/file connection or local repository was found. No Retina code has been imported, and no continuous-improvement scheduler is running.
