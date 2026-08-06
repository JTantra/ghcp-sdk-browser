"""SSE transport: the browser's tool-executor channel over plain HTTPS.

Why this exists
---------------
The extension used to attach over WebSocket. That cannot work against the
Foundry hosted endpoint: the upgrade needs an Entra bearer on the
Authorization header, and the browser WebSocket API cannot set headers.
Injecting it via declarativeNetRequest does not apply to the extension's own
requests, so the upgrade always 401s.

``fetch()`` *can* set headers. So the extension POSTs to /invocations with the
bearer applied directly and reads the response body as an SSE stream. No DNR,
no extra permission, no platform-specific header plumbing.

SSE is one-way (server -> client), so the reverse leg (tool results, prompts)
rides separate POSTs on the same ``agent_session_id``. That is sound because
concurrent POSTs on one session were verified to land in the same sandbox.

The sink below quacks like a Starlette WebSocket -- it exposes ``send_text`` --
so ``hub.py`` and the agent loop are untouched by this transport swap.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

log = logging.getLogger("sse")

#: Comment frames keep intermediaries from reaping an idle stream. Well under
#: the ~60s that most proxies use as an idle read timeout.
HEARTBEAT_S = 15.0


def frame(payload: dict) -> bytes:
    """Encode one SSE ``data:`` event.

    The trailing blank line is the record separator -- without it the client
    buffers indefinitely waiting for the end of the event.
    """
    return f"data: {json.dumps(payload)}\n\n".encode()


class SseSink:
    """A hub-compatible sink that pushes frames to an SSE reader.

    Duck-types the one method ``hub`` calls on a socket (``send_text``), which
    is why swapping WebSocket for SSE needed no change in ``hub.py``.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._closed = False

    async def send_text(self, text: str) -> None:
        """Hub-facing API. Takes a JSON string, as WebSocket.send_text does."""
        if not self._closed:
            await self._queue.put(json.loads(text))

    def push(self, payload: dict) -> None:
        """Fire-and-forget enqueue from non-async call sites."""
        if not self._closed:
            self._queue.put_nowait(payload)

    def close_sync(self) -> None:
        """Named to be unmistakable from WebSocket.close, which is async.

        ``hub.detach`` duck-types on this exact name so it can tear down an
        SSE sink without accidentally invoking a WebSocket coroutine.
        """
        self._closed = True
        self._queue.put_nowait(None)  # sentinel: unblock the reader

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield SSE frames until closed, emitting heartbeats while idle."""
        yield frame({"type": "connected", "session_id": self.session_id})
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"  # comment frame; ignored by EventSource
                continue
            if item is None:
                return
            yield frame(item)


async def probe_stream() -> AsyncIterator[bytes]:
    """Emit timed ticks to prove whether the platform streams or buffers.

    Each tick carries the server-side elapsed time. If the client sees the
    first byte at ~0s and the last at ~4s, the path streams incrementally. If
    every byte lands at once at ~4s, something between us and the client is
    buffering the whole response -- and SSE degrades to request/response.
    """
    start = time.monotonic()
    for i in range(5):
        yield frame({"type": "tick", "i": i, "server_elapsed_s": round(time.monotonic() - start, 3)})
        await asyncio.sleep(1.0)
    yield frame({"type": "probe_done", "server_total_s": round(time.monotonic() - start, 3)})


#: Disables proxy buffering where honoured (nginx and most Azure front doors).
#: Without these an intermediary may accumulate the whole body before flushing.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Type": "text/event-stream; charset=utf-8",
}
