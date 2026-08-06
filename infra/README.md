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
- **Ops tools run in-process.** `ops_node_capacity` and `ops_carrier_policy` are
  defined in `server/app/ops_tools.py` via `define_tool` and ship inside this
  container -- there is no MCP server and no sidecar to deploy. To move tool
  governance into Foundry, delete them and pass a toolbox consumer endpoint
  (`.../toolboxes/<name>/mcp?api-version=v1`) as `mcp_servers` in
  `agents.py`, authenticating with a token for `https://ai.azure.com/.default`.
  The subagent config does not change. Note that an `mcp_servers` entry
  pointing at an unreachable URL is tolerated by the SDK but adds ~1s of
  connect retries to the first session (1.9s -> 3.0s measured), so do not
  leave a stale one behind. The ~2s baseline on the *first* session is the
  Copilot CLI subprocess warming up; subsequent sessions create in ~0.02s.
