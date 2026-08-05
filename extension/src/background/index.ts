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
 */
import { TOOLS } from "./tools";
import type { PanelMsg, ServerMsg } from "../shared/protocol";

const DEFAULT_BACKEND = "ws://127.0.0.1:8088/ws/agent";

let socket: WebSocket | null = null;
let sessionId = "";
let connected = false;

function toPanel(msg: PanelMsg): void {
  chrome.runtime.sendMessage(msg).catch(() => {
    /* panel closed - nothing listening, safe to drop */
  });
}

function newSessionId(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 24);
}

async function connect(backendUrl?: string): Promise<void> {
  if (socket && socket.readyState <= WebSocket.OPEN) return;

  const stored = await chrome.storage.local.get(["backendUrl", "sessionId"]);
  const url = backendUrl || stored.backendUrl || DEFAULT_BACKEND;
  sessionId = stored.sessionId || newSessionId();
  await chrome.storage.local.set({ backendUrl: url, sessionId });

  socket = new WebSocket(`${url}?session=${sessionId}`);

  socket.onopen = () => {
    connected = true;
    toPanel({ type: "status", connected: true, sessionId });
  };

  socket.onclose = () => {
    connected = false;
    socket = null;
    toPanel({ type: "status", connected: false, sessionId });
  };

  socket.onerror = () => {
    toPanel({ type: "error", message: `cannot reach backend at ${url}` });
  };

  socket.onmessage = async (event) => {
    const msg = JSON.parse(event.data) as ServerMsg;

    switch (msg.type) {
      case "connected":
        sessionId = msg.session_id;
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

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.type === "connect") {
      await connect(msg.backendUrl);
      sendResponse({ ok: true });
    } else if (msg.type === "prompt") {
      if (!connected || !socket) {
        toPanel({ type: "error", message: "not connected to the backend" });
      } else {
        socket.send(JSON.stringify({ type: "prompt", text: msg.text }));
      }
      sendResponse({ ok: true });
    } else if (msg.type === "get_status") {
      const stored = await chrome.storage.local.get(["backendUrl"]);
      sendResponse({
        connected,
        sessionId,
        backendUrl: stored.backendUrl || DEFAULT_BACKEND,
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
