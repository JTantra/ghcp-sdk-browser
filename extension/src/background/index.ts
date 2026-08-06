/**
 * Service worker: owns the WebSocket to the backend and executes tool calls.
 *
 * Topology (why it is this way):
 *   backend  --tool_call-->  THIS worker  --executeScript-->  operator's tab
 *
 * The backend cannot drive the browser itself. Same-origin policy stops a web
 * page from scripting another origin's DOM, and Playwright is a Node process
 * that cannot run inside a page. An extension is the only component that can
 * act inside the tab the operator is already authenticated in.
 *
 * Connecting to a Foundry hosted agent adds three constraints the local dev
 * setup did not have:
 *   1. The only WebSocket path the platform proxies is /invocations_ws.
 *   2. The upgrade must carry an Entra bearer token on the Authorization
 *      header, which the WebSocket API cannot set -- so we inject it with
 *      declarativeNetRequest (rules.ts) after acquiring it (auth.ts).
 *   3. Connections are capped at ~30 min (close 1001) and sessions idle out
 *      after 15 min, so we reconnect with the SAME agent_session_id.
 */
import { TOOLS } from "./tools";
import { getAccessToken, signIn, signOut, isSignedIn } from "./auth";
import { installAuthHeaderRule, clearAuthHeaderRule } from "./rules";
import type { PanelMsg, ServerMsg } from "../shared/protocol";

/**
 * Deployed defaults. Pre-filled so the panel works out of the box; every one
 * is still overridable from the side panel and whatever the operator saves in
 * chrome.storage.local wins over these.
 */
const DEFAULT_BACKEND =
  "wss://jtantra-test-foundry.services.ai.azure.com/api/projects/jtantra-test-foundry" +
  "/agents/ghcp-sdk-browser/endpoint/protocols/invocations_ws?api-version=v1";
/** Entra app registration `ghcp-sdk-browser-extension`. */
const DEFAULT_CLIENT_ID = "509df672-ebb9-4ab5-91d6-5c19996313c1";
const DEFAULT_TENANT_ID = "92def590-0f4c-43ab-be1e-493d616e2725";
/** Local dev fallback: `python -m app.main` then paste this into the panel. */
const LOCAL_BACKEND = "ws://127.0.0.1:8088/invocations_ws";

let socket: WebSocket | null = null;
let sessionId = "";
let connected = false;
/** Set while a deliberate teardown is in progress, to suppress auto-reconnect. */
let closingIntentionally = false;
let reconnectAttempts = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

function toPanel(msg: PanelMsg): void {
  chrome.runtime.sendMessage(msg).catch(() => {
    /* panel closed - nothing listening, safe to drop */
  });
}

function newSessionId(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 24);
}

/** Hosted endpoints are wss:// and need a bearer token; local dev does not. */
function isHosted(url: string): boolean {
  return url.startsWith("wss://");
}

/**
 * Builds the connect URL.
 *
 * IMPORTANT (hosted): `agent_session_id` must be a PLATFORM-ISSUED id, not one
 * we invent. The platform validates it and rejects unknown values with
 * HTTP 400 `invalid_session_id`. So we omit it on the first connect, let the
 * platform mint one, and reuse the id it hands back in the `connected` frame.
 * Local dev has no such validation, so it keeps the client-generated id.
 */
function buildUrl(base: string, session: string): string {
  const url = new URL(base);
  if (isHosted(base)) {
    if (!url.searchParams.has("api-version")) url.searchParams.set("api-version", "v1");
    // Only send it back if the platform issued it on an earlier connect.
    if (session) url.searchParams.set("agent_session_id", session);
    else url.searchParams.delete("agent_session_id");
  } else {
    url.searchParams.set("session", session || newSessionId());
  }
  return url.toString();
}

function scheduleReconnect(backendUrl: string): void {
  if (closingIntentionally || reconnectTimer) return;

  // Capped exponential backoff: 1s, 2s, 4s, 8s, 16s, then every 30s.
  const delay = Math.min(1000 * 2 ** reconnectAttempts, 30_000);
  reconnectAttempts += 1;
  toPanel({ type: "activity", tool: "reconnect", detail: `retrying in ${delay / 1000}s` });

  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connect(backendUrl);
  }, delay);
}

export async function connect(backendUrl?: string): Promise<void> {
  if (socket && socket.readyState <= WebSocket.OPEN) return;

  const stored = await chrome.storage.local.get(["backendUrl", "sessionId"]);
  const url = backendUrl || stored.backendUrl || DEFAULT_BACKEND;

  // Hosted: never invent a session id -- the platform validates it and 400s on
  // anything it did not issue. Start empty and adopt the id from the `connected`
  // frame, which is what lets us resume the same sandbox after the ~30 min cap.
  // Local: a client-generated id is fine and keeps dev sessions stable.
  sessionId = stored.sessionId || (isHosted(url) ? "" : newSessionId());
  await chrome.storage.local.set({ backendUrl: url });
  if (sessionId) await chrome.storage.local.set({ sessionId });

  closingIntentionally = false;

  if (isHosted(url)) {
    try {
      const token = await getAccessToken();
      await installAuthHeaderRule(url, token);
    } catch (err) {
      toPanel({ type: "error", message: `auth failed: ${String(err)}` });
      toPanel({ type: "status", connected: false, sessionId });
      return; // No point dialing an endpoint that will 401.
    }
  }

  socket = new WebSocket(buildUrl(url, sessionId));

  // Distinguishes "never reached the server" from "was connected and dropped",
  // so we only discard a session id that the platform actually refused.
  let connectedOnce = false;

  socket.onopen = () => {
    connected = true;
    connectedOnce = true;
    reconnectAttempts = 0;
    toPanel({ type: "status", connected: true, sessionId });
  };

  socket.onclose = (event) => {
    connected = false;
    socket = null;
    toPanel({ type: "status", connected: false, sessionId });

    // 1001 = platform shutdown drain (the ~30 min cap). Expected; resume quietly.
    if (event.code === 1001) {
      toPanel({ type: "activity", tool: "session", detail: "platform recycled, resuming" });
    }

    // A stored session id the platform no longer accepts (expired past the
    // 30-day life, or issued to another identity) makes every retry 400 with
    // the same result. Drop it once so the next attempt gets a fresh sandbox
    // instead of looping forever.
    if (isHosted(url) && !connectedOnce && sessionId) {
      toPanel({
        type: "activity",
        tool: "session",
        detail: "session rejected, requesting a new one",
      });
      sessionId = "";
      void chrome.storage.local.remove("sessionId");
    }

    if (!closingIntentionally) scheduleReconnect(url);
  };

  socket.onerror = () => {
    toPanel({ type: "error", message: `cannot reach backend at ${url}` });
  };

  socket.onmessage = async (event) => {
    const msg = JSON.parse(event.data) as ServerMsg;

    switch (msg.type) {
      case "connected":
        sessionId = msg.session_id;
        await chrome.storage.local.set({ sessionId });
        toPanel({ type: "status", connected: true, sessionId });
        break;

      case "tool_call": {
        toPanel({
          type: "activity",
          tool: msg.name,
          detail: JSON.stringify(msg.arguments).slice(0, 160),
        });
        let output: string;
        let isError = false;
        try {
          const handler = TOOLS[msg.name];
          if (!handler) throw new Error(`unknown tool: ${msg.name}`);
          output = JSON.stringify(await handler(msg.arguments));
        } catch (err) {
          isError = true;
          output = JSON.stringify({ error: String(err) });
        }
        socket?.send(
          JSON.stringify({
            type: "tool_result",
            call_id: msg.call_id,
            output,
            is_error: isError,
          }),
        );
        break;
      }

      case "assistant_delta":
        toPanel({ type: "delta", text: msg.text });
        break;

      case "turn_started":
        toPanel({ type: "started" });
        break;

      case "turn_finished":
        toPanel({ type: "final", text: msg.text });
        break;

      case "error":
        toPanel({ type: "error", message: msg.message });
        break;
    }
  };
}

async function disconnect(): Promise<void> {
  closingIntentionally = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  socket?.close(1000, "user disconnected");
  socket = null;
  connected = false;
  await clearAuthHeaderRule();
  toPanel({ type: "status", connected: false, sessionId });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.type === "connect") {
      await connect(msg.backendUrl);
      sendResponse({ ok: true });
    } else if (msg.type === "disconnect") {
      await disconnect();
      sendResponse({ ok: true });
    } else if (msg.type === "sign_in") {
      try {
        await signIn();
        sendResponse({ ok: true });
      } catch (err) {
        toPanel({ type: "error", message: `sign-in failed: ${String(err)}` });
        sendResponse({ ok: false, error: String(err) });
      }
    } else if (msg.type === "sign_out") {
      await signOut();
      await clearAuthHeaderRule();
      sendResponse({ ok: true });
    } else if (msg.type === "save_config") {
      await chrome.storage.local.set({
        clientId: msg.clientId,
        tenantId: msg.tenantId,
        backendUrl: msg.backendUrl,
      });
      sendResponse({ ok: true });
    } else if (msg.type === "new_session") {
      // Abandons the current sandbox. Clearing the id (rather than minting one)
      // is what makes the next hosted connect ask the platform for a new
      // session; local mode regenerates on connect.
      await chrome.storage.local.remove("sessionId");
      sessionId = "";
      await disconnect();
      sendResponse({ ok: true });
    } else if (msg.type === "prompt") {
      if (!connected || !socket) {
        toPanel({ type: "error", message: "not connected to the backend" });
      } else {
        socket.send(JSON.stringify({ type: "prompt", text: msg.text }));
      }
      sendResponse({ ok: true });
    } else if (msg.type === "get_status") {
      const stored = await chrome.storage.local.get(["backendUrl", "clientId", "tenantId"]);
      sendResponse({
        connected,
        sessionId,
        backendUrl: stored.backendUrl || DEFAULT_BACKEND,
        clientId: stored.clientId || DEFAULT_CLIENT_ID,
        tenantId: stored.tenantId || DEFAULT_TENANT_ID,
        localBackend: LOCAL_BACKEND,
        signedIn: await isSignedIn(),
      });
    }
  })();
  return true; // keep the message channel open for the async response
});

chrome.action.onClicked.addListener(async (tab) => {
  if (tab.windowId !== undefined) {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  }
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});
