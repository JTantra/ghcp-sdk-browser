"""Correlation glue between the agent loop and the operator's browser.

The backend never touches the browser. It emits a tool_call over the session's
WebSocket and awaits a tool_result carrying the same call_id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from .config import BROWSER_TOOL_TIMEOUT_S

log = logging.getLogger("hub")


class Hub:
    def __init__(self) -> None:
        self._sockets: dict[str, Any] = {}
        self._pending: dict[str, asyncio.Future] = {}
        #: call_id -> session_id, so a detach only cancels its OWN session's
        #: in-flight calls. Without this, one operator disconnecting would
        #: fail every other operator's pending tool call.
        self._call_session: dict[str, str] = {}
        self._activity: dict[str, list[dict]] = {}

    # -- socket lifecycle -------------------------------------------------
    def attach(self, session_id: str, ws: Any) -> None:
        self._sockets[session_id] = ws
        self._activity.setdefault(session_id, [])
        log.info("browser attached session=%s", session_id)

    def detach(self, session_id: str) -> None:
        sock = self._sockets.pop(session_id, None)
        # SSE sinks need an explicit close to unblock their reader; WebSockets
        # are torn down by Starlette. `close_sync` marks the former -- checking
        # for a plain `close` would find the WebSocket's *coroutine* method and
        # call it without awaiting.
        close = getattr(sock, "close_sync", None)
        if callable(close):
            close()
        for cid, fut in list(self._pending.items()):
            if self._call_session.get(cid) == session_id and not fut.done():
                fut.set_result({"call_id": cid, "output": '{"error":"browser disconnected"}'})
        log.info("browser detached session=%s", session_id)

    def is_connected(self, session_id: str) -> bool:
        return session_id in self._sockets

    # -- outbound ---------------------------------------------------------
    async def send(self, session_id: str, payload: dict) -> None:
        ws = self._sockets.get(session_id)
        if ws is not None:
            await ws.send_text(json.dumps(payload))

    async def dispatch_tool(self, session_id: str, name: str, args: dict) -> str:
        """Ask the browser to run a tool. Returns the JSON string it replied with."""
        ws = self._sockets.get(session_id)
        if ws is None:
            return json.dumps({"error": "operator browser is not connected"})

        call_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        self._call_session[call_id] = session_id

        await ws.send_text(
            json.dumps(
                {"type": "tool_call", "call_id": call_id, "name": name, "arguments": args}
            )
        )
        self.record(session_id, {"kind": "tool_call", "name": name, "arguments": args})

        try:
            result = await asyncio.wait_for(fut, BROWSER_TOOL_TIMEOUT_S)
            out = result.get("output", "{}")
            self.record(
                session_id,
                {"kind": "tool_result", "name": name, "preview": str(out)[:400]},
            )
            return out
        except asyncio.TimeoutError:
            return json.dumps({"error": f"browser timed out after {BROWSER_TOOL_TIMEOUT_S}s"})
        finally:
            self._pending.pop(call_id, None)
            self._call_session.pop(call_id, None)

    # -- inbound ----------------------------------------------------------
    def resolve_tool(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("call_id", ""))
        if fut and not fut.done():
            fut.set_result(msg)

    # -- activity log (also surfaced to the UI) ---------------------------
    def record(self, session_id: str, entry: dict) -> None:
        self._activity.setdefault(session_id, []).append(entry)

    def activity(self, session_id: str) -> list[dict]:
        return self._activity.get(session_id, [])


hub = Hub()
