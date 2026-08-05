# Deployment

## Local
    cd server && ./run-local.sh

## Foundry Hosted Agents
    export GITHUB_TOKEN=ghp_xxx
    ./infra/deploy.sh

### Notes
- **Node in the image.** The Copilot SDK spawns the bundled `copilot` CLI as a
  child process, so the container needs a Node runtime even though the app is
  Python. Omitting it fails only at first session creation, not at startup.
- **Session pairing.** Invocations binds a call to a sandbox via the
  `agent_session_id` *query parameter* only. Body fields and `x-agent-session-id`
  headers are forwarded but ignored for routing. The extension's session id must
  be passed through as that query param.
- **Tracing.** `APPLICATIONINSIGHTS_CONNECTION_STRING` is injected by the
  platform. Locally, spans fall back to the console exporter.
- **Toolbox.** Swap `OPS_MCP_URL` for a toolbox consumer endpoint
  (`.../toolboxes/<name>/mcp?api-version=v1`) to move tool governance into
  Foundry. Node/Python clients authenticate with a token for
  `https://ai.azure.com/.default`. The subagent config does not change.
