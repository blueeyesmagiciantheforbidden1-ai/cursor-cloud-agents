#!/bin/sh
# Optional cloud-hosted tunnel bridge; not deployed by this package.
# Stdout belongs to MCP only. Direct HTTPS/OAuth does not use this script.
set -eu
: "${HUB_URL:?Set the deployed HTTPS hub origin}"
: "${HUB_MANAGER_TOKEN:?Load the manager token from its protected environment}"
cd /opt/runcrew-hub
exec /opt/runcrew-hub/.venv/bin/python -m agent_hub.mcp \
  --agent manager --token-env HUB_MANAGER_TOKEN --cloud-run-auth-mode metadata
