/**
 * HTTPS + SSE transport to the hosted agent.
 *
 * Why not WebSocket
 * -----------------
 * A hosted Foundry endpoint requires an Entra bearer on the Authorization
 * header. The browser WebSocket API cannot set headers, and injecting one via
 * declarativeNetRequest does not apply to the extension's own requests -- the
 * upgrade always 401s.
 *
 * `fetch()` sets headers directly, so reading an SSE stream from a fetch body
 * needs no DNR rule and no extra permission. Note this is deliberately NOT
 * `EventSource`, which also cannot set headers.
 *
 * SSE is server->client only, so the reverse leg (tool results, prompts) rides
 * separate POSTs on the same agent_session_id. Verified: concurrent POSTs on
 * one session land in the same sandbox.
 */
import type { ServerMsg } from "../shared/protocol";

export interface TransportHooks {
  onMessage: (msg: ServerMsg) => void | Promise<void>;
  onOpen: (sessionId: string) => void;
  onClose: (reason: string) => void;
  onError: (message: string) => void;
}

/** Splits a byte stream into SSE events, tolerating chunk-split records. */
async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });

    // Events are separated by a blank line. A chunk boundary can fall
    // anywhere, so we only consume complete records and keep the remainder.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of raw.split("\n")) {
        if (line.startsWith("data: ")) yield line.slice(6);
        // ": keepalive" comment frames are ignored, as the spec requires.
      }
    }
  }
}

export class SseTransport {
  private controller: AbortController | null = null;
  private sessionId = "";
  private closingIntentionally = false;

  constructor(
    private readonly baseUrl: string,
    private readonly getToken: () => Promise<string>,
    private readonly hooks: TransportHooks,
  ) {}

  get session(): string {
    return this.sessionId;
  }

  get isConnected(): boolean {
    return this.controller !== null;
  }

  /** Builds the invocations URL, carrying agent_session_id when we have one. */
  private url(): string {
    const url = new URL(this.baseUrl);
    if (!url.searchParams.has("api-version")) url.searchParams.set("api-version", "v1");
    // Only send an id the platform issued; it 400s on values it did not mint.
    if (this.sessionId) url.searchParams.set("agent_session_id", this.sessionId);
    else url.searchParams.delete("agent_session_id");
    return url.toString();
  }

  /** One-shot POST on the same session. Used for tool results and prompts. */
  async post(payload: Record<string, unknown>): Promise<Response> {
    const token = await this.getToken();
    return fetch(this.url(), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });
  }

  /** Opens the long-lived stream and pumps frames into the hooks. */
  async connect(sessionId = ""): Promise<void> {
    if (this.controller) return;
    this.sessionId = sessionId;
    this.closingIntentionally = false;

    const controller = new AbortController();
    this.controller = controller;

    try {
      const token = await this.getToken();
      const resp = await fetch(this.url(), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
          Accept: "text/event-stream",
        },
        body: JSON.stringify({ mode: "attach" }),
        signal: controller.signal,
      });

      if (!resp.ok) {
        // Surfacing the body matters: the platform explains 400/401 in it.
        const detail = await resp.text().catch(() => "");
        throw new Error(`attach failed: HTTP ${resp.status} ${detail.slice(0, 300)}`);
      }
      if (!resp.body) throw new Error("attach returned no body to stream");

      // The platform echoes the session it bound us to.
      const issued = resp.headers.get("agent_session_id");
      if (issued) this.sessionId = issued;

      for await (const data of parseSse(resp.body)) {
        const msg = JSON.parse(data) as ServerMsg;
        if (msg.type === "connected") {
          this.sessionId = msg.session_id;
          this.hooks.onOpen(this.sessionId);
        }
        await this.hooks.onMessage(msg);
      }
      this.hooks.onClose("stream ended");
    } catch (err) {
      if (!this.closingIntentionally) {
        this.hooks.onError(String(err));
        this.hooks.onClose(String(err));
      }
    } finally {
      if (this.controller === controller) this.controller = null;
    }
  }

  disconnect(): void {
    this.closingIntentionally = true;
    this.controller?.abort();
    this.controller = null;
  }
}
