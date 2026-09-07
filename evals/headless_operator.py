"""Headless synthetic operator — the fixture browser for eval runs.

Why this exists
---------------
`/invocations` returns **409 "no operator browser attached"** before the model
ever runs, because every browser tool relays to a human's extension. That makes
the agent un-evaluatable unattended.

This module speaks the exact relay protocol the extension speaks
(`extension/src/background/tools.ts` + `shared/protocol.ts`) against a static
fixture board, so an eval run needs no human, no Chrome and no side panel:

    POST mode=attach       -> long-lived SSE stream (we read tool_call frames)
    POST mode=tool_result  -> resolve one call_id
    POST (no mode)         -> synchronous turn; returns {output, activity}

Using a FIXTURE rather than the live demo-app is deliberate: evaluation needs a
frozen board, otherwise scores move when the demo data moves and a regression is
indistinguishable from a data edit. The rows here are copied verbatim from
`demo-app/index.html`.

The tool semantics below mirror tools.ts exactly -- same return shapes, same
error strings -- so what the agent sees is what a real operator's browser would
have returned.
"""
from __future__ import annotations

import json
import threading
from typing import Any

import httpx

# --- fixture board: verbatim from demo-app/index.html DATA ----------------
HEADERS = ["Ref", "Vessel", "Route", "ETA", "Delay (h)", "TEU", "Status"]

BOARD: list[list[Any]] = [
    ["SH-4417", "MV Coral Spire",   "Singapore → Rotterdam",   "2026-08-11", 34, 8200, "Delayed"],
    ["SH-4418", "MV Iron Meridian", "Busan → Long Beach",      "2026-08-07",  0, 6100, "On Time"],
    ["SH-4419", "MV Baltic Anthem", "Hamburg → Santos",        "2026-08-14", 12, 4700, "At Risk"],
    ["SH-4420", "MV Pacific Ember", "Shanghai → Oakland",      "2026-08-09", 41, 9400, "Delayed"],
    ["SH-4421", "MV Aegean Dawn",   "Piraeus → Alexandria",    "2026-08-06",  0, 2200, "On Time"],
    ["SH-4422", "MV Northern Kite", "Vancouver → Yokohama",    "2026-08-12",  7, 5300, "At Risk"],
    ["SH-4423", "MV Solent Trader", "Felixstowe → New York",   "2026-08-10", 26, 7100, "Delayed"],
    ["SH-4424", "MV Andaman Reach", "Colombo → Jeddah",        "2026-08-08",  3, 3900, "On Time"],
    ["SH-4425", "MV Gulf Lantern",  "Jebel Ali → Mundra",      "2026-08-13", 18, 4100, "At Risk"],
    ["SH-4426", "MV Cascade Vow",   "Seattle → Kaohsiung",     "2026-08-15",  0, 6600, "On Time"],
]

DETAIL = {
    "SH-4417": "Typhoon Ambo rerouting; 2 port calls compressed",
    "SH-4418": "Nominal",
    "SH-4419": "Berth congestion at origin, 12h slip",
    "SH-4420": "Crane outage; awaiting downstream slot",
    "SH-4421": "Nominal",
    "SH-4422": "Weather window narrowing",
    "SH-4423": "Labour action at origin terminal",
    "SH-4424": "Minor pilotage wait",
    "SH-4425": "Customs hold cleared late",
    "SH-4426": "Nominal",
}

BOARD_URL = "http://localhost:5055"


def _rows_as_objects() -> list[dict[str, Any]]:
    return [dict(zip(HEADERS, r)) for r in BOARD]


class FixtureBrowser:
    """Executes the browser tool vocabulary against the fixture board.

    Mirrors extension/src/background/tools.ts return shapes exactly, so the
    model cannot tell this from a real operator tab.
    """

    def __init__(self) -> None:
        self.url = "about:blank"
        self.calls: list[dict[str, Any]] = []

    def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"name": name, "arguments": args})
        fn = getattr(self, f"_{name}", None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(args)
        except Exception as exc:  # noqa: BLE001 - a tool error is data, not a crash
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- tool implementations ---------------------------------------------
    def _browser_navigate(self, args):
        self.url = str(args.get("url", ""))
        return {"ok": True, "url": self.url}

    def _browser_read_table(self, args):
        sel = args.get("selector", "table") or "table"
        # tools.ts does a real querySelector; only the shipments table exists.
        if sel not in ("table", "#shipments", "#shipments table"):
            return {"error": f"no element matches {sel}"}
        rows = _rows_as_objects()
        return {"selector": sel, "row_count": len(rows), "rows": rows}

    def _browser_read_text(self, args):
        sel = args.get("selector", "body") or "body"
        if sel == "#detail":
            return {"selector": sel, "text": ""}
        lines = ["Fleet Operations Console", f"{len(BOARD)} shipments"]
        for r in BOARD:
            lines.append(" | ".join(str(c) for c in r))
        return {"selector": sel, "text": "\n".join(lines)[:8000]}

    def _browser_click(self, args):
        target = str(args.get("text") or args.get("selector") or "")
        ref = next((r[0] for r in BOARD if r[0] in target), None)
        if ref is None:
            return {"error": f"no element for selector='{args.get('selector','')}' "
                             f"text='{args.get('text','')}'"}
        return {"ok": True, "clicked": ref, "detail": DETAIL[ref]}

    def _browser_fill(self, args):
        return {"ok": True, "selector": args.get("selector"), "value": args.get("value")}

    def _browser_wait_for(self, args):
        sel = args.get("selector", "")
        if sel in ("#shipments", "table", "#filter", "#status", "#detail"):
            return {"ok": True, "selector": sel}
        return {"error": f"timed out waiting for {sel}"}


class HeadlessOperator:
    """Attaches to the relay and serves browser tool calls for one session.

    Runs the SSE reader on a daemon thread: the synchronous turn POST blocks
    until the turn completes, and the turn itself blocks on tool calls that
    arrive on this stream -- so they must be serviced concurrently or the run
    deadlocks (the same reason start_turn() returns 202 for the extension).
    """

    def __init__(self, base_url: str, token: str, session_id: str,
                 timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.timeout = timeout
        self.browser = FixtureBrowser()
        self.frames: list[dict[str, Any]] = []
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    # -- plumbing ----------------------------------------------------------
    def _url(self) -> str:
        return (f"{self.base_url}/endpoint/protocols/invocations"
                f"?api-version=v1&agent_session_id={self.session_id}")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"}

    def post(self, body: dict[str, Any], timeout: float | None = None) -> httpx.Response:
        with httpx.Client(timeout=timeout or self.timeout) as c:
            return c.post(self._url(), headers=self._headers(), json=body)

    # -- SSE reader --------------------------------------------------------
    def _reader(self) -> None:
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, read=None)) as c:
                with c.stream("POST", self._url(), headers=self._headers(),
                              json={"mode": "attach"}) as resp:
                    if resp.status_code != 200:
                        self._error = f"attach failed HTTP {resp.status_code}"
                        self._ready.set()
                        return
                    for line in resp.iter_lines():
                        if self._stop.is_set():
                            return
                        if not line or not line.startswith("data: "):
                            continue
                        msg = json.loads(line[6:])
                        self.frames.append(msg)
                        if msg.get("type") == "connected":
                            self._ready.set()
                        elif msg.get("type") == "tool_call":
                            self._serve(msg)
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()

    def _serve(self, msg: dict[str, Any]) -> None:
        out = self.browser.execute(msg.get("name", ""), msg.get("arguments") or {})
        # Reverse leg is a separate POST on the same session -- SSE is one-way.
        self.post({"mode": "tool_result", "call_id": msg["call_id"],
                   "output": json.dumps(out),
                   "is_error": "error" in out}, timeout=60)

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "HeadlessOperator":
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        if not self._ready.wait(90):
            raise RuntimeError("headless operator: no 'connected' frame within 90s")
        if self._error:
            raise RuntimeError(f"headless operator: {self._error}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()

    # -- the thing evals call ---------------------------------------------
    def ask(self, prompt: str) -> dict[str, Any]:
        """Run one turn synchronously; returns {output, activity, tool_calls}."""
        r = self.post({"message": prompt})
        r.raise_for_status()
        data = r.json()
        return {
            # `output` is a STRING on this endpoint (main.py:260) -- the earlier
            # scaffold assumed {"output": {"content": ...}} and would have
            # scored every row against an empty response.
            "response": data.get("output", "") if isinstance(data.get("output"), str)
            else json.dumps(data.get("output")),
            "activity": data.get("activity", []),
            "tool_calls": list(self.browser.calls),
            "frames": list(self.frames),
        }
