# Connect the existing coding applications

These examples launch the same small stdio bridge from each application. They contain absolute Python and import paths for this machine, so opening another project does not break `python -m agent_hub.mcp`. Update both paths when installing on another Windows user or VM.

Set only that application's named environment variable to its own hub token before launching the application. The names are `AGENT_HUB_CODEX_TOKEN`, `AGENT_HUB_CLAUDE_TOKEN`, `AGENT_HUB_CURSOR_TOKEN`, and `AGENT_HUB_COPILOT_TOKEN`. Keep the token outside repository configuration and command arguments. Do not give an individual application the manager token or the full server token file. The bridge intentionally has no all-principals token-file option.

Merge the relevant example into the application's existing MCP configuration. Preserve other servers. The VS Code example is for GitHub Copilot in VS Code; it does not configure the separate Copilot CLI. These examples configure connections only; they do not launch applications, wake idle chats, or prove a model has participated.

For the future isolated Cloud Run service, replace `--url` with its HTTPS origin and append `--cloud-run-auth-mode gcloud` for a local signed-in operator, or `--cloud-run-auth-mode metadata` on the intended GCE worker. The bridge shares the worker's identity-token cache and refreshes once after HTTP 401/403. The attached GCE identity must have permission to invoke this specific service. The hub's per-agent token remains required.

On Windows, if `gcloud` is a command-script wrapper, use `--gcloud-command` with a JSON array containing the absolute Python executable and installed Google Cloud SDK `gcloud.py` path. This passes fixed arguments directly without a shell. Do not put credential values in this option.

The bridge and HTTP endpoint support MCP versions `2025-03-26` and `2025-06-18`, advertise only tools, and never issue server-to-client requests. The HTTP leg uses the hub's JSON responses. For this stateless service, initialized/cancellation/progress notifications have no operation; room cancellation is the manager's explicit `hub_cancel` tool. A coding application claiming a task must heartbeat within 45 seconds or use the worker for managed execution.

The manager principal can list, read, start, cancel, and retry rooms, plus read `hub_status` health and usage. Worker principals can claim their own assigned tasks, read rooms they participate in, heartbeat, and complete tasks. Tool lists reflect those permissions; calling an unlisted tool cannot bypass authorization.

`manager.mcp.json.example` is for a trusted administrative MCP host or bridge, using `AGENT_HUB_MANAGER_TOKEN`. Keep this separate from the four worker application examples. ChatGPT requires a reachable HTTP MCP service and its own authentication setup: importing a local stdio example does not install a ChatGPT app. When configuring the ChatGPT-facing gateway, route its authorized administrative calls as `manager`; keep the private hub token inside the gateway. Do not expose an unauthenticated manager endpoint.

The [MCP transport specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) requires newline-separated UTF-8 stdio messages. Input frames are limited to 100,000 bytes; output messages to 512,000 bytes. A malformed line returns a protocol error; an oversized or unterminated frame closes the bridge. Redirects are refused before credentials can be forwarded.

The [MCP base protocol](https://modelcontextprotocol.io/specification/2025-06-18/basic) requires string/integer request IDs: an explicit null ID is rejected, whereas a notification has no ID and gets no reply. [Tool failures](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) return `isError`; malformed arguments use JSON-RPC errors. Room lists contain summaries so bounded history does not multiply into a huge list response.
