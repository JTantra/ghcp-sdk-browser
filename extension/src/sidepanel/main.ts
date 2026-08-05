import type { PanelMsg } from "../shared/protocol";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

const log = $<HTMLDivElement>("log");
const input = $<HTMLTextAreaElement>("input");
const sendBtn = $<HTMLButtonElement>("send");
const backend = $<HTMLInputElement>("backend");
const dot = $<HTMLSpanElement>("dot");
const state = $<HTMLSpanElement>("state");

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

$("connect").onclick = () => {
  chrome.runtime.sendMessage({ type: "connect", backendUrl: backend.value.trim() || undefined });
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
  dot.classList.toggle("on", res.connected);
  state.textContent = res.connected ? `session ${res.sessionId.slice(0, 8)}` : "disconnected";
  if (!res.connected) chrome.runtime.sendMessage({ type: "connect" });
});
