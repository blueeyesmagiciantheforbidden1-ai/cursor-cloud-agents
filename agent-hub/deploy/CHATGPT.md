# Private RunCrew connection in ChatGPT

This connection is prepared, **not registered, installed, or live**. It is for the owner's two ChatGPT accounts only. Expected account emails are recorded privately; they are not yet verified account/workspace subjects or technical app IDs. Keep them out of source and plugin archives.

Use the approved existing Google Cloud project `project-0c6d31fa-509e-4116-a2c` and standard Cloud Run `run.app` URLs. Earlier requirements for a new organization, a purchased domain, or a dedicated always-running VM were superseded. Read [CURRENT_STATE.md](CURRENT_STATE.md) for actual cloud resources and blockers. This runbook does not perform cloud changes or grant access.

## Choose the cloud connection

OpenAI supports either an HTTPS Streamable HTTP MCP endpoint or Secure MCP Tunnel. Private installation and private network reachability are separate choices: an HTTPS endpoint can be internet-reachable while every data/tool call requires authorization for the owner's two verified identities. [Connect a plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt)

For minimal idle compute, prefer request-driven HTTPS:

```text
Owner's two private ChatGPT connections
  -> authenticated HTTPS MCP endpoint on Google Cloud
  -> existing IAM-private RunCrew hub
  -> bounded cloud workers: Codex / Claude / Cursor / Copilot / Grok
```

The existing hub implements `POST /mcp` and a stdio bridge. Its authentication is currently a hub principal token, plus Google IAM at the private Cloud Run boundary. It does **not** yet implement ChatGPT-compatible OAuth discovery, linking, or a two-subject resource authorization policy. Pasting the dashboard URL, hub REST URL, or manager token into ChatGPT does not fill that gap.

First assess [Google's IAP MCP authentication preview](https://docs.cloud.google.com/run/docs/ai/authenticate-mcp-servers). It could avoid another gateway if it interoperates with ChatGPT. The Google examples demonstrate CLI clients, not this ChatGPT connection. Verify discovery, PKCE S256, exact redirect/client settings, token type/audience, refresh, and both owner grants. Do not mark IAP compatible merely because Google sign-in works on the dashboard.

If that interoperability is not established, the concrete alternative is a small OAuth-protected MCP gateway in the same project. Its HTTPS ingress must reach the OAuth/MCP handler without first requiring a Google IAM token that ChatGPT cannot mint. The gateway authenticates and authorizes users before forwarding manager-only calls; its service identity invokes only the private hub. Backend tokens and provider keys stay server-side. No anonymous manager tools, token-in-URL workaround, or broad project role is an acceptable substitute. [Service-to-service identity](https://docs.cloud.google.com/run/docs/authenticating/service-to-service)

Use request-based billing and minimum instances zero for request-driven services; check revision minimums too. Incoming HTTPS requests wake Cloud Run, including a gateway's request to the backend. Cold starts remain possible. Keep long-running agent work behind the existing queue: return room IDs promptly and poll bounded status. This reduces idle compute, not all storage/build/provider charges. [Cloud Run billing](https://docs.cloud.google.com/run/docs/configuring/billing-settings), [autoscaling](https://docs.cloud.google.com/run/docs/about-instance-autoscaling)

## Authentication requirements before direct connection

Implement and test the MCP OAuth 2.1 contract with an existing compatible authorization service where possible: protected-resource and issuer discovery, authorization-code flow with PKCE S256, correct resource/audience, and a supported client registration method. Predefined clients, CIMD, and DCR are documented options; do not add unnecessary dynamic registration for two private connections. Copy each connection's exact redirect URI from its management page instead of guessing a callback URL. Validate issuer, audience, expiry, scopes, and the verified owner subject on every tool call. [OpenAI authentication](https://developers.openai.com/plugins/build/auth)

The supplied emails are onboarding hints. Bind authorization to verified issuer/subject identities and separately verify the intended ChatGPT account/workspace contexts. An OAuth email claim alone does not prove which ChatGPT account owns an app. Keep each private app restricted to its intended owner; do not broaden to workspace-wide availability. The current backend maps manager requests to one manager role, so separate owner attribution is not yet implemented there.

No OAuth stub, empty discovery document, shared browser cookie, or dashboard IAP session should be represented as a completed connection. This is the main direct-HTTPS implementation blocker.

## Optional Secure MCP Tunnel

A tunnel can reach the existing stdio bridge without a public MCP listener. It needs a real tunnel ID, authorized OpenAI runtime key, and a running cloud `tunnel-client`. Associate only the verified Platform organizations and ChatGPT workspace contexts needed for the two accounts. Creating/editing requires Tunnels Read + Manage; using/selecting requires Read + Use. Developer mode is a separate permission. [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

This is optional, not a mandatory VM purchase. A cloud host must run its outbound polling while the connection is in use; a scale-to-zero HTTP service cannot wake itself from outgoing polls. An existing approved cloud runtime could host it on demand, with unavailable status when stopped. Do not use the personal PC. No tunnel host or tunnel has been created.

The optional `chatgpt/mcp-manager.sh` and `runcrew-tunnel.service` illustrate the stdio bridge and a systemd host. Their paths, service user, Google metadata identity, and environment require real setup. The host receives only the manager token, never the hub's full principal-token collection or provider inference keys. Secrets belong in the approved cloud secret store.

Use the download link in Platform tunnel settings or [OpenAI's latest release](https://github.com/openai/tunnel-client/releases/latest); verify the selected release before installation. With the real ID and protected runtime environment loaded, the documented profile commands are:

```sh
tunnel-client help quickstart
tunnel-client init --sample sample_mcp_stdio_local --profile runcrew-hub \
  --tunnel-id "$RUNCREW_TUNNEL_ID" \
  --mcp-command "/opt/runcrew-hub/chatgpt/mcp-manager.sh"
tunnel-client doctor --profile runcrew-hub --explain
tunnel-client run --profile runcrew-hub
```

Confirm the installed release's help and health before connection. If the two contexts cannot be associated through the supported process, resolve that account mapping rather than widening access.

## Register each private app and bind the package

Once its authentication works, enable developer mode in each intended account where policy permits it. Open [ChatGPT Plugins](https://chatgpt.com/plugins), choose the plus button, and create **RunCrew Hub** using the verified HTTPS MCP URL or authorized tunnel. Review discovered tools. Start a new chat, add the connection, and test it before packaging. [Connection steps](https://developers.openai.com/plugins/deploy/connect-chatgpt)

Copy each registration's actual `plugin_asdk_app...` technical ID from its URL. Build separate account archives when IDs differ:

```text
python chatgpt/build_plugin.py --app-id ACTUAL_ACCOUNT_A_APP_ID --output chatgpt/dist/runcrew-hub-account-a-0.2.0.zip
python chatgpt/build_plugin.py --app-id ACTUAL_ACCOUNT_B_APP_ID --output chatgpt/dist/runcrew-hub-account-b-0.2.0.zip
```

Uppercase values are instructions to supply verified IDs, not runnable examples. The builder refuses malformed IDs and existing output files, includes only allowlisted files, and adds the documented app mapping. It neither registers nor installs an app.

The compatibility manifest remains supported. The ZIP is not a documented universal web upload/install mechanism. Official packaging describes private local sources in supported desktop/Work surfaces; availability differs from a web developer-mode MCP app. Complete the skill package installation through the actual account's supported private source and test it separately. No public submission, workspace publishing, marketplace registration, or global Codex installation is performed by this runbook. [Plugin packaging](https://developers.openai.com/plugins/build/plugins)

## Completion evidence

Run [the evaluation cases](../chatgpt/evaluation.md) in both accounts. Prove health/status, room creation/read/cancel, and five separately attributed cloud-agent contributions for the all-five project test. A queue entry, roster, installed executable, or local development receipt is insufficient. Restricted review workers do not establish autonomous editing/deployment.

Record the endpoint/auth method, verified two-account mapping, app IDs, and test results in private deployment records. Test rejection of a third identity, wrong tokens/scopes, stale usage, cold starts or stopped tunnel, and cancellation. Until these pass, report the plugin as prepared and disconnected. A deployed status website and a working ChatGPT integration are separate milestones.
