/**
 * Service worker: owns the transport to the backend and executes tool calls.
 *
 * Topology (why it is this way):
 *   backend  --tool_call-->  THIS worker  --executeScript-->  operator's tab
 *
 * The backend cannot drive the browser itself. Same-origin policy stops a web
 * page from scripting another origin's DOM, and Playwright is a Node process
 * that cannot run inside a page. An extension is the only component that can
 * act inside the tab the operator is already authenticated in.
 *
 * Transport is HTTPS + SSE, not WebSocket. The hosted endpoint requires an
 * Entra bearer on the Authorization header; the WebSocket API cannot set one,
 * and declarativeNetRequest does not apply to an extension's own requests, so
 * the upgrade always 401d. `fetch()` sets the header directly -- which is why
 * this build needs no DNR rule and no declarativeNetRequest permission.
 * See transport.ts.
 */
import { TOOLS } from "./tools";
import { getAccessToken, signIn, signOut, isSignedIn } from "./auth";
import { SseTransport } from "./transport";
import type { PanelMsg, ServerMsg } from "../shared/protocol";

/**
 * Deployed defaults. Pre-filled so the panel works out of the box; every one
 * is still overridable from the side panel and whatever the operator saves in
 * chrome.storage.local wins over these.
 *
 * Note this is https:// and /invocations -- the SSE stream, the tool-result
 * POST and the prompt POST all multiplex over this single URL by `mode`.
 */
const DEFAULT_BACKEND =
  "https://jtantra-test-foundry.services.ai.azure.com/api/projects/jtantra-test-foundry" +
  "/agents/ghcp-sdk-browser/endpoint/protocols/invocations?api-version=v1";
/** Entra app registration `ghcp-sdk-browser-extension`. */
const DEFAULT_CLIENT_ID = "509df672-ebb9-4ab5-91d6-5c19996313c1";
const DEFAULT_TENANT_ID = "92def590-0f4c-43ab-be1e-493d616e2725";
/** Local dev fallback: `python -m app.main` then paste this into the panel. */
const LOCAL_BACKEND = "http://127.0.0.1:8088/invocations";

let transport: SseTransport | null = null;
let sessionId = "";
let connected = false;
let closingIntentionally = false;
let reconnectAttempts = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

function toPanel(msg: PanelMsg): void {
  chrome.runtime.sendMessage(msg).catch(() => {
    /* panel closed - nothing listening, safe to drop */
  });
}

/** Hosted endpoints need a bearer token; local dev does not. */
function isHosted(url: string): boolean {
  return url.startsWith("https://");
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

/** Runs one tool call and posts the result back on a separate request. */
async function handleToolCall(
  msg: Extract<ServerMsg, { type: "tool_call" }>,
): Promise<void> {
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

  // Separate POST, not a frame on the stream: SSE is one-way. The server is
  // blocked awaiting this exact call_id.
  try {
    await transport?.post({
      mode: "tool_result",
      call_id: msg.call_id,
      output,
      is_error: isError,
    });
  } catch (err) {
    toPanel({ type: "error", message: `failed to return tool result: ${String(err)}` });
  }
}

export async function connect(backendUrl?: string): Promise<void> {
  if (transport?.isConnected) return;

  const stored = await chrome.storage.local.get(["backendUrl", "sessionId"]);
  const url = backendUrl || stored.backendUrl || DEFAULT_BACKEND;

  // Never invent a session id for a hosted endpoint -- the platform validates
  // it and 400s on anything it did not issue. Start empty and adopt the id
  // from the `connected` frame so we can resume the same sandbox.
  sessionId = stored.sessionId || "";
  await chrome.storage.local.set({ backendUrl: url });

  closingIntentionally = false;

  transport = new SseTransport(
    url,
    // Local dev has no auth; sending an empty bearer keeps one code path.
    async () => (isHosted(url) ? await getAccessToken() : ""),
    {
      onOpen: (sid) => {
        connected = true;
        reconnectAttempts = 0;
        sessionId = sid;
        void chrome.storage.local.set({ sessionId: sid });
        toPanel({ type: "status", connected: true, sessionId });
      },
      onClose: (reason) => {
        connected = false;
        toPanel({ type: "status", connected: false, sessionId });

        // A stored session the platform no longer accepts makes every retry
        // fail identically. Drop it once so the next attempt gets a fresh
        // sandbox instead of looping forever.
        if (isHosted(url) && sessionId && /HTTP 4(00|04)/.test(reason)) {
          toPanel({
            type: "activity",
            tool: "session",
            detail: "session rejected, requesting a new one",
          });
          sessionId = "";
          void chrome.storage.local.remove("sessionId");
        }

        if (!closingIntentionally) scheduleReconnect(url);
      },
      onError: (message) => toPanel({ type: "error", message }),
      onMessage: async (msg) => {
        switch (msg.type) {
          case "tool_call":
            await handleToolCall(msg);
            break;
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
      },
    },
  );

  // Deliberately not awaited: connect() runs for the life of the stream.
  void transport.connect(sessionId);
}

async function disconnect(): Promise<void> {
  closingIntentionally = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  transport?.disconnect();
  transport = null;
  connected = false;
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
      sendResponse({ ok: true });
    } else if (msg.type === "save_config") {
      await chrome.storage.local.set({
        clientId: msg.clientId,
        tenantId: msg.tenantId,
        backendUrl: msg.backendUrl,
      });
      sendResponse({ ok: true });
    } else if (msg.type === "new_session") {
      // Clearing the id (rather than minting one) is what makes the next
      // connect ask the platform for a new session.
      await chrome.storage.local.remove("sessionId");
      sessionId = "";
      await disconnect();
      sendResponse({ ok: true });
    } else if (msg.type === "prompt") {
      if (!connected || !transport) {
        toPanel({ type: "error", message: "not connected to the backend" });
      } else {
        // Returns 202 immediately; the turn's output arrives on the SSE
        // stream. Awaiting the turn here would deadlock -- it blocks on tool
        // calls whose results must come in on their own requests.
        try {
          const resp = await transport.post({ mode: "prompt", text: msg.text });
          if (!resp.ok) {
            toPanel({
              type: "error",
              message: `prompt rejected: HTTP ${resp.status} ${(await resp.text()).slice(0, 200)}`,
            });
          }
        } catch (err) {
          toPanel({ type: "error", message: `prompt failed: ${String(err)}` });
        }
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
