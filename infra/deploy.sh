#!/usr/bin/env bash
# One-shot deploy to Foundry Hosted Agents.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${GITHUB_TOKEN:?set GITHUB_TOKEN (PAT with Copilot entitlement)}"

command -v azd >/dev/null || { echo "azd not installed: https://aka.ms/azd-install"; exit 1; }

azd ext install azure.ai.agents || true
azd auth login

# Requires the Foundry Project Manager role on the target project.
azd env set GITHUB_TOKEN "$GITHUB_TOKEN"
azd env set COPILOT_MODEL "${COPILOT_MODEL:-claude-sonnet-4.6}"

azd provision
azd deploy

cat <<'MSG'

Deployed. Invoke it with:

  BASE_URL="https://<account>.services.ai.azure.com/api/projects/<project>"
  az rest --method POST \
    --url "${BASE_URL}/agents/ghcp-sdk-browser/endpoint/protocols/invocations?api-version=v1&agent_session_id=<session>" \
    --resource "https://ai.azure.com" \
    --body '{"message":"Review the shipment board and draft a recovery brief."}'

The agent_session_id MUST match the session the extension connected with -
the platform reads it from the query string only.
MSG
