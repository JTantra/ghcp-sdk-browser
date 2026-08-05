# Architecture

## Why the browser extension exists

The backend cannot drive the operator's browser. Three constraints force this:

1. **Same-origin policy.** A page served from origin A cannot read or script the
   DOM of origin B. Iframes are opaque cross-origin; `window.open` returns a
   handle with only `close`/`focus`/`postMessage`.
2. **Playwright is a Node process.** It speaks CDP to a browser from outside. It
   cannot run inside a page, so "the frontend uses Playwright" is not possible.
   Playwright's own extension mode is a *bridge* for an external Node process.
3. **Credentials must not leave the laptop.** Driving a headless browser server
   side means re-solving SSO and MFA with a service account.

A browser extension is the sanctioned exception: `chrome.scripting.executeScript`
with host permissions runs inside the tab the operator already authenticated.

## Flow

    ┌──────────────── operator's laptop ─────────────────┐
    │  side panel (chat UI)                              │
    │        │ chrome.runtime                            │
    │  service worker  ──chrome.scripting──►  target tab │
    │        │                                (authenticated)
    └────────┼───────────────────────────────────────────┘
             │ WSS  /ws/agent?session=<id>
    ┌────────▼───────────── Azure ───────────────────────┐
    │  Foundry Hosted Agent (container, port 8088)       │
    │    Copilot SDK master  ── browser tools ───────────┤ (relayed)
    │      ├── risk-analyst      skills: risk-scoring    │
    │      ├── recovery-planner  skills + ops tools      │
    │      └── reporter          skills: report-format   │
    │    OTel ──► Application Insights                   │
    └────────────────────────────────────────────────────┘

## Agent topology

The master is the only agent with browser tools. Analysis tools are removed from
it via `default_agent.excluded_tools`, so it *must* delegate — this both enforces
the boundary and keeps the master's context lean as the tool count grows.

Each subagent declares its own `tools`, `skills`, and `model`. Skills are
eagerly injected at startup and are opt-in per agent, so each subagent carries
only its own playbook rather than the union of all of them.

## Tool vocabulary

Tools are Playwright-shaped (selectors, structured reads, waits) rather than
CUA-shaped (screenshots, vision, x/y coordinates). `browser_read_table` returns
JSON rows, so the model reasons over structured data instead of pixels —
cheaper per step, faster, and far more resilient to layout shifts.

## Security notes

- Page content flows back through tool results into the reasoning loop. Treat it
  as untrusted input; a production build should allow-list permitted origins and
  selectors server side.
- The extension's `host_permissions` should be narrowed from `https://*/*` to the
  specific apps in scope before any real deployment.
- Tool approval: if you move tools into a Foundry Toolbox, `require_approval:
  always` is **not** enforced by the MCP endpoint. The runtime must gate it.
