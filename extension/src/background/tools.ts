/**
 * Browser tool implementations.
 *
 * Each runs via chrome.scripting.executeScript in the operator's ACTIVE tab, so
 * every request carries their existing session cookies. The backend never sees
 * a credential; SSO and MFA are satisfied because the human already signed in.
 *
 * Tool vocabulary is deliberately Playwright-shaped (selectors, waits,
 * structured reads) rather than CUA-shaped (screenshots, x/y coordinates):
 * deterministic, far cheaper per step, and resilient to layout shifts.
 */

async function activeTabId(): Promise<number> {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id) throw new Error("no active tab");
  return tab.id;
}

async function waitForComplete(tabId: number, timeoutMs = 15000): Promise<void> {
  const started = Date.now();
  for (;;) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return;
    if (Date.now() - started > timeoutMs) throw new Error("page load timed out");
    await new Promise((r) => setTimeout(r, 150));
  }
}

async function run<A extends unknown[], R>(
  fn: (...args: A) => R,
  args: A,
): Promise<R> {
  const tabId = await activeTabId();
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    args,
    func: fn as (...a: unknown[]) => unknown,
  });
  return res.result as R;
}

export type ToolHandler = (args: Record<string, any>) => Promise<unknown>;

export const TOOLS: Record<string, ToolHandler> = {
  async browser_navigate({ url }) {
    const tabId = await activeTabId();
    await chrome.tabs.update(tabId, { url });
    await waitForComplete(tabId);
    return { ok: true, url };
  },

  async browser_read_table({ selector = "table" }) {
    return run(
      (sel: string) => {
        const table = document.querySelector(sel);
        if (!table) return { error: `no element matches ${sel}` };
        const heads = Array.from(table.querySelectorAll("thead th")).map((th) =>
          (th as HTMLElement).innerText.trim(),
        );
        const rows = Array.from(table.querySelectorAll("tbody tr")).map((tr) => {
          const cells = Array.from((tr as HTMLTableRowElement).cells).map((td) =>
            td.innerText.trim(),
          );
          if (!heads.length) return cells;
          return Object.fromEntries(heads.map((h, i) => [h || `col${i}`, cells[i] ?? ""]));
        });
        return { selector: sel, row_count: rows.length, rows };
      },
      [selector],
    );
  },

  async browser_read_text({ selector = "body" }) {
    return run(
      (sel: string) => {
        const el = document.querySelector(sel);
        if (!el) return { error: `no element matches ${sel}` };
        return { selector: sel, text: (el as HTMLElement).innerText.trim().slice(0, 8000) };
      },
      [selector],
    );
  },

  async browser_click({ selector = "", text = "" }) {
    return run(
      (sel: string, txt: string) => {
        let el: Element | null = null;
        if (sel) el = document.querySelector(sel);
        if (!el && txt) {
          el =
            Array.from(document.querySelectorAll("button,a,td,th,[role=button]")).find(
              (n) => (n as HTMLElement).innerText.trim() === txt,
            ) ?? null;
        }
        if (!el) return { error: `no element for selector='${sel}' text='${txt}'` };
        (el as HTMLElement).click();
        return { ok: true, clicked: (el as HTMLElement).innerText.trim().slice(0, 80) };
      },
      [selector, text],
    );
  },

  async browser_fill({ selector, value }) {
    return run(
      (sel: string, val: string) => {
        const el = document.querySelector(sel) as HTMLInputElement | HTMLSelectElement | null;
        if (!el) return { error: `no element matches ${sel}` };
        el.value = val;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true, selector: sel, value: val };
      },
      [selector, value],
    );
  },

  async browser_wait_for({ selector, timeout_ms = 8000 }) {
    const deadline = Date.now() + Number(timeout_ms);
    for (;;) {
      const found = await run(
        (sel: string) => Boolean(document.querySelector(sel)),
        [selector],
      );
      if (found) return { ok: true, selector };
      if (Date.now() > deadline) return { error: `timed out waiting for ${selector}` };
      await new Promise((r) => setTimeout(r, 200));
    }
  },
};
