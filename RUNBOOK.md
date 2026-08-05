# Runbook

## 1. Backend

    cd server
    cp .env.example .env      # add your GitHub PAT (needs Copilot entitlement)
    ./run-local.sh            # creates .venv on first run, serves :8088

Verify:

    curl localhost:8088/readiness      # -> ok
    curl localhost:8088/info | jq      # -> master tools + subagent topology

## 2. Demo app

    cd demo-app && python3 -m http.server 5055
    # http://localhost:5055  — "Fleet Ops Console" with a #shipments table

## 3. Extension

    cd extension && npm install && npm run build

Load it: `chrome://extensions` → Developer mode → **Load unpacked** → pick
`extension/dist`. Click the toolbar icon to open the side panel; it auto-connects
to `ws://127.0.0.1:8088/ws/agent`.

## 4. Drive it

Open the demo app in the active tab, then in the side panel:

> Review the shipment board and draft a recovery brief for the top 3 at-risk shipments.

Expected trace in the panel:

    → browser_read_table {"selector":"#shipments"}
    → (master delegates to risk-analyst)
    → ops_node_capacity {"node":"Rotterdam"}      (recovery-planner)
    → (reporter formats)
    ## Recovery Brief — ...

## Testing tools without the model

`/debug/tool` dispatches one browser tool straight through the relay — no model
call, no cost. Use it while developing tool handlers:

    curl -X POST 'localhost:8088/debug/tool?session=<session-from-panel>' \
      -H 'Content-Type: application/json' \
      -d '{"name":"browser_read_table","arguments":{"selector":"#shipments"}}'

## Deploy to Foundry

    export GITHUB_TOKEN=ghp_xxx
    ./infra/deploy.sh

Then point the extension's backend field at the deployed WSS endpoint.

## Verified on this build

- `npm run build` — tsc clean, 4 bundles emitted
- server boots, `/readiness` 200, `/info` returns the agent topology
- `/invocations` returns 409 when no browser is attached (correct: the backend
  cannot act without an operator session)
- full relay round-trip: server `tool_call` → extension executes → `tool_result`
  → server correlates by `call_id`
- OTel spans emitted (console exporter locally, App Insights when the connection
  string is injected)

Not yet exercised: a live model turn (needs your PAT) and a real Foundry deploy.

## Gotchas hit while building this

- **Starlette >= 1.0 removed `on_startup`/`on_shutdown`.** Use the `lifespan`
  async context manager. Failure is at import time, not runtime.
- **The Copilot SDK needs Node in the image** even though the app is Python — it
  spawns the bundled `copilot` CLI as a child process. Missing Node fails only
  at first session creation, so it survives a naive healthcheck.
- **Invocations binds sessions by query string only.** `agent_session_id` in the
  body or an `x-agent-session-id` header is forwarded but ignored for routing.
- **`enable_skills=True` is required**, not just `skill_directories`. Skills are
  discovered but not loaded without it, silently.
