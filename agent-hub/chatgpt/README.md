# Private RunCrew Hub package

RunCrew brings Codex, Claude Code, Cursor, GitHub Copilot, and Grok Build into project collaboration through a Google Cloud hub. This package is for the owner's **two ChatGPT accounts only**. It has not been registered or installed in either account. It contains no credentials, account emails, invented app IDs, or runtime model code.

The approved hosting target is the existing Google Cloud project `project-0c6d31fa-509e-4116-a2c`. A new organization, purchased domain, and dedicated always-running tunnel VM are not prerequisites. [The deployment checkpoint](../deploy/CURRENT_STATE.md) records actual resources; the plugin ZIP is separate from cloud deployment evidence.

The low-idle-cost target is a cloud HTTPS MCP endpoint with compatible OAuth restricted to the owner's verified identities. The existing hub has MCP transport but not that OAuth integration. Google's IAP MCP preview is a candidate only after ChatGPT compatibility is verified. Secure MCP Tunnel is another option if its live cloud poller and account associations are configured. None is connected yet. See [the connection runbook](../deploy/CHATGPT.md).

`runcrew-hub/` contains a skill plus the supported `.codex-plugin/plugin.json` compatibility manifest. The skill assigns bounded roles to all five agents and distinguishes requested work from verified contributions. The actual registered app supplies its tools. The allowlisted builder adds a single `.app.json` mapping only when given a real technical app ID. [OpenAI packaging](https://developers.openai.com/plugins/build/plugins)

The MCP server also returns the coordinator workflow in its manager `initialize` response. A connected client receives the instructions with tool discovery, even before a separate skill package is installed. This metadata does not create a connection, grant permission, guarantee model behavior, or make missing workers available; verify orchestration in both real ChatGPT accounts.

Build a skills-only preview with a new output filename:

```text
python chatgpt/build_plugin.py --skills-only --output chatgpt/dist/runcrew-hub-preview-0.2.0.zip
```

The earlier unversioned preview is superseded. After each account registers its real private MCP connection, pass its copied `plugin_asdk_app_...` ID to `--app-id` instead of `--skills-only`, using separate output names such as `runcrew-hub-account-a-0.2.0.zip` and `runcrew-hub-account-b-0.2.0.zip`. Labels A and B are filenames, not account identities. The builder's conservative ID format check does not prove registration or account ownership, and it refuses to replace existing archives. Keep the two-account identity mapping in private deployment configuration.

Only the manifest, skill, and optional app mapping enter an archive. The ZIP is a packaging artifact, not a documented one-click web installer. First test the private MCP app in each account. Complete skill installation using that account's supported private package source and verify it separately; local package availability varies by ChatGPT surface. No public submission, publishing, or marketplace registration is performed here.

[Evaluation cases](evaluation.md) cover all-five attribution, both accounts, denial of unrelated users, usage freshness, and truthful unavailable states. `mcp-manager.sh` and `runcrew-tunnel.service` are optional tunnel reference files, excluded from the ZIP; they are not installed services or mandatory infrastructure.
