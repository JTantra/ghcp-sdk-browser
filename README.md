# ghcp-sdk-browser

Multi-agent browser automation POC.

**Master agent** (GitHub Copilot SDK) owns browser tools and drives the operator's
authenticated tab through a Chromium extension. It delegates analysis to
subagents, each with its own tools + skills. Hosted on Foundry Hosted Agents
(Invocations protocol) with OpenTelemetry tracing to Application Insights.

```
Chromium extension (TS/Vite)          Python backend (Foundry Hosted Agent)
  chat UI  ──── WSS ──────────────►   /ws/agent  relay
  tool executor ◄─── tool_call ────   Copilot SDK master agent
  (authenticated tab)                   ├── risk-analyst   (mock MCP + skill)
                                        ├── recovery-planner (mock MCP + skill)
                                        └── reporter        (skill only)
```

## Why the extension

The backend never touches the browser. Same-origin policy blocks a normal web
page from scripting another origin's DOM, and Playwright is a Node process that
cannot run inside a page. A browser extension is the only component that can
execute in the operator's already-authenticated tab — so MFA/SSO stay solved.

## Quick start

    cd server && ./run-local.sh          # backend on :8088
    cd demo-app && python3 -m http.server 5055
    cd extension && npm install && npm run build   # then load dist/ unpacked

Open http://localhost:5055, click the extension icon, ask:
"Review the shipment board and draft a recovery brief for the top 3 at-risk shipments."

## Layout

    server/      Python: Invocations host, WSS relay, agents, mock MCP
    extension/   TypeScript + Vite: side-panel chat UI + browser tool executor
    demo-app/    Mock "Fleet Ops Console" to automate
    infra/       Dockerfile, deploy.sh, agent manifest
    azure.yaml   azd project definition (must live at the repo root)
