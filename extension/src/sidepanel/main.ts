import type { PanelMsg } from "../shared/protocol";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

const log = $<HTMLDivElement>("log");
const input = $<HTMLTextAreaElement>("input");
const sendBtn = $<HTMLButtonElement>("send");
const backend = $<HTMLInputElement>("backend");
const clientId = $<HTMLInputElement>("clientId");
const tenantId = $<HTMLInputElement>("tenantId");
const dot = $<HTMLSpanElement>("dot");
const state = $<HTMLSpanElement>("state");
const authstate = $<HTMLDivElement>("authstate");
const setupBox = $<HTMLDetailsElement>("setupBox");

let streaming: HTMLDivElement | null = null;

function add(cls: string, text: string): HTMLDivElement {
  const el = document.createElement("div");
  el.className = `msg ${cls}`;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function send(text: string): void {
  const trimmed = text.trim();
  if (!trimmed) return;
  add("user", trimmed);
  input.value = "";
  sendBtn.disabled = true;
  chrome.runtime.sendMessage({ type: "prompt", text: trimmed });
}

sendBtn.onclick = () => send(input.value);

input.onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send(input.value);
  }
};

document.querySelectorAll<HTMLButtonElement>(".chip").forEach((chip) => {
  chip.onclick = () => send(chip.dataset.p ?? "");
});

function setAuthState(signedIn: boolean): void {
  authstate.textContent = signedIn ? "signed in ✓" : "not signed in";
  authstate.className = signedIn ? "hint ok" : "hint";
}

$("save").onclick = () => {
  chrome.runtime.sendMessage(
    {
      type: "save_config",
      clientId: clientId.value.trim(),
      tenantId: tenantId.value.trim(),
      backendUrl: backend.value.trim(),
    },
    () => add("tool", "config saved"),
  );
};

$("signin").onclick = () => {
  add("tool", "opening Microsoft sign-in…");
  chrome.runtime.sendMessage({ type: "sign_in" }, (res) => {
    if (res?.ok) {
      setAuthState(true);
      add("tool", "signed in");
    }
  });
};

$("signout").onclick = () => {
  chrome.runtime.sendMessage({ type: "sign_out" }, () => {
    setAuthState(false);
    add("tool", "signed out");
  });
};

$("connect").onclick = () => {
  chrome.runtime.sendMessage({
    type: "connect",
    backendUrl: backend.value.trim() || undefined,
  });
};

$("disconnect").onclick = () => {
  chrome.runtime.sendMessage({ type: "disconnect" });
};

$("newsession").onclick = () => {
  chrome.runtime.sendMessage({ type: "new_session" }, () => {
    add("tool", "started a new session - press Connect");
  });
};

chrome.runtime.onMessage.addListener((msg: PanelMsg) => {
  switch (msg.type) {
    case "status":
      dot.classList.toggle("on", msg.connected);
      state.textContent = msg.connected ? `session ${msg.sessionId.slice(0, 8)}` : "disconnected";
      break;

    case "activity":
      add("tool", `→ ${msg.tool} ${msg.detail}`);
      break;

    case "started":
      streaming = add("bot", "");
      streaming.classList.add("cursor");
      break;

    case "delta":
      if (!streaming) {
        streaming = add("bot", "");
        streaming.classList.add("cursor");
      }
      streaming.textContent = (streaming.textContent ?? "") + msg.text;
      log.scrollTop = log.scrollHeight;
      break;

    case "final":
      if (streaming) {
        streaming.classList.remove("cursor");
        if (msg.text) streaming.textContent = msg.text;
        streaming = null;
      } else if (msg.text) {
        add("bot", msg.text);
      }
      sendBtn.disabled = false;
      break;

    case "error":
      streaming?.classList.remove("cursor");
      streaming = null;
      add("err", msg.message);
      sendBtn.disabled = false;
      break;
  }
});

// Restore state and auto-connect on open.
chrome.runtime.sendMessage({ type: "get_status" }, (res) => {
  if (!res) return;
  backend.value = res.backendUrl;
  clientId.value = res.clientId ?? "";
  tenantId.value = res.tenantId ?? "";
  dot.classList.toggle("on", res.connected);
  state.textContent = res.connected ? `session ${res.sessionId.slice(0, 8)}` : "disconnected";
  setAuthState(!!res.signedIn);

  // Nudge the operator toward setup when the extension is not usable yet.
  if (!res.clientId || !res.tenantId || !res.signedIn) {
    setupBox.open = true;
  } else if (!res.connected) {
    chrome.runtime.sendMessage({ type: "connect" });
  }
});
