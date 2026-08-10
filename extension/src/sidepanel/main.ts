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

/** Icon + css class per step kind. Keeps render logic declarative. */
const STEP_STYLE: Record<string, { icon: string; cls: string }> = {
  reasoning: { icon: "🧠", cls: "think" },
  subagent: { icon: "🤖", cls: "agent" },
  subagent_done: { icon: "✓", cls: "agent" },
  tool: { icon: "→", cls: "tool" },
  tool_done: { icon: "✓", cls: "tool" },
  skill: { icon: "📘", cls: "skill" },
  usage: { icon: "◷", cls: "usage" },
};

/**
 * Render one intermediate step as its OWN message.
 *
 * Reasoning can be long, so it renders inside a <details> the operator can
 * expand -- keeping the transcript scannable without hiding the content.
 */
function addStep(
  kind: string,
  title: string,
  detail?: string,
  ok?: boolean,
): HTMLElement {
  const style = STEP_STYLE[kind] ?? { icon: "•", cls: "tool" };
  const el = document.createElement("div");
  el.className = `msg step ${style.cls}${ok === false ? " bad" : ""}`;

  if (kind === "reasoning" && detail) {
    const box = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = `${style.icon} ${title}`;
    const body = document.createElement("div");
    body.className = "think-body";
    body.textContent = detail;
    box.append(sum, body);
    el.appendChild(box);
  } else {
    const head = document.createElement("div");
    head.className = "step-head";
    head.textContent = `${style.icon} ${title}`;
    el.appendChild(head);
    if (detail) {
      const sub = document.createElement("div");
      sub.className = "step-detail";
      sub.textContent = detail;
      el.appendChild(sub);
    }
  }

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

let localBackendUrl = "ws://127.0.0.1:8088/invocations_ws";

$("uselocal").onclick = () => {
  backend.value = localBackendUrl;
  add("tool", "endpoint set to local - press Save then Connect");
};

$("newsession").onclick = () => {
  chrome.runtime.sendMessage({ type: "new_session" }, () => {
    // Wipe the transcript: the previous session's steps and answers belong to
    // a sandbox that no longer exists, so keeping them on screen implies a
    // continuity the agent does not have.
    log.replaceChildren();
    streaming = null;
    sendBtn.disabled = false;
    add("tool", "new session - previous conversation cleared. Press Connect.");
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

    case "step":
      // Any step ends the current text bubble, so assistant text that resumes
      // afterwards starts a NEW message instead of being appended to the one
      // that was streaming before the step. This is what actually splits a
      // turn into per-step messages in the transcript.
      if (streaming) {
        streaming.classList.remove("cursor");
        if (!streaming.textContent?.trim()) streaming.remove();
        streaming = null;
      }
      addStep(msg.kind, msg.title, msg.detail, msg.ok);
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
      // Only stamp the full text when nothing streamed. Overwriting a live
      // bubble would duplicate content now that a step can split the answer
      // into several bubbles -- the last one holds just the final fragment,
      // while msg.text is the entire turn.
      if (streaming) {
        streaming.classList.remove("cursor");
        if (!streaming.textContent?.trim()) {
          if (msg.text) streaming.textContent = msg.text;
          else streaming.remove();
        }
        streaming = null;
      } else if (msg.text && !log.querySelector(".msg.bot")) {
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
  if (res.localBackend) localBackendUrl = res.localBackend;
  clientId.value = res.clientId ?? "";
  tenantId.value = res.tenantId ?? "";
  dot.classList.toggle("on", res.connected);
  state.textContent = res.connected ? `session ${res.sessionId.slice(0, 8)}` : "disconnected";
  setAuthState(!!res.signedIn);

  // Only the sign-in step is left to the operator now that endpoint, client
  // and tenant are pre-filled -- so open setup only when not signed in.
  if (!res.signedIn) {
    setupBox.open = true;
  } else if (!res.connected) {
    chrome.runtime.sendMessage({ type: "connect" });
  }
});
