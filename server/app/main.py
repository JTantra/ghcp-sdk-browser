"""Foundry Hosted Agent container.

Contract (hosted-agent runtime):
  - listen on 8088, plain HTTP, bind 0.0.0.0
  - GET  /readiness  -> 200 when ready
  - POST /invocations -> the agent protocol endpoint
  - graceful shutdown on SIGTERM

The browser extension attaches over HTTPS+SSE on the same /invocations path
(see sse.py). It is the tool executor; this server only reasons.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from .agents import CUSTOM_AGENTS, runner
from .config import PORT
from .hub import hub
from .sse import SSE_HEADERS, SseSink, probe_stream
from .telemetry import remember_trace_context, setup_tracing, tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def readiness(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", status_code=200)


async def info(_request: Request) -> JSONResponse:
    from .agents import OPS_BOARD_URL, catalogue
    from .telemetry import telemetry_status
    from .skills_store import skills_status

    return JSONResponse(
        {
            "agent": "ghcp-sdk-browser",
            "ops_board_url": OPS_BOARD_URL,
            # Reports whether spans can actually reach Application Insights.
            # Without this the exporter falls back to console and traces
            # vanish silently -- which looks identical to "tracing is broken".
            "telemetry": telemetry_status(),
            # Whether skills came from Foundry or the baked-in image, so a
            # silent fallback is visible rather than looking like a stale skill.
            "skills_source": skills_status(),
            # Full tool + skill catalogue, read from the live tool definitions
            # and the skills directory rather than hand-maintained here (the
            # previous hardcoded list drifted from the real tools).
            **catalogue(),
        }
    )


async def invocations(request: Request):
    """Foundry Invocations protocol -- and the extension's whole transport.

    Session binding is by the ``agent_session_id`` QUERY parameter -- the
    platform reads it only from the query string. We reuse it as the relay
    session so the hosted sandbox and the operator's browser stay paired.

    ``/invocations`` is the only POST path the platform relays, so the
    extension multiplexes over it with a ``mode`` field in the body:

        mode=attach       -> long-lived SSE stream (the tool_call channel)
        mode=tool_result  -> resolves a pending tool call
        mode=prompt       -> starts a turn, output arrives on the SSE stream
        mode=info         -> tool/skill catalogue + telemetry status
        mode=end_session  -> release the Copilot session
        mode=probe        -> streaming-vs-buffering diagnostic
        (absent)          -> classic request/response, unchanged

    Why not WebSocket: the upgrade needs an Authorization header, which the
    browser WebSocket API cannot set. ``fetch()`` can, so SSE needs no
    declarativeNetRequest and no extra permission. See sse.py.
    """
    session_id = request.query_params.get("agent_session_id") or uuid.uuid4().hex
    body = await request.json()
    mode = body.get("mode")

    # Adopt the platform's trace context from the inbound W3C traceparent
    # header. Without this every span we create starts a NEW trace, so the
    # portal -- which roots its Traces view on the platform's `invoke_agent`
    # request -- never shows our spans even though they reach App Insights.
    remember_trace_context(session_id, request.headers)

    if mode == "probe":
        # Deliberately not session-bound: answers one question, costs nothing.
        return StreamingResponse(probe_stream(), headers=SSE_HEADERS)

    if mode == "attach":
        return await attach_sse(session_id)

    if mode == "tool_result":
        hub.resolve_tool(body)
        return JSONResponse({"ok": True}, headers={"agent_session_id": session_id})

    if mode == "info":
        # /info is a GET, but the hosted proxy only relays POST /invocations,
        # so the same payload is reachable here for remote diagnosis.
        return await info(request)

    if mode == "end_session":
        # The extension calls this when the operator starts a new session, so
        # the old Copilot session (and its conversation history) is released
        # rather than lingering for the life of the container.
        ended = await runner.end_session(session_id)
        return JSONResponse({"ok": True, "ended": ended})

    if mode == "prompt":
        return await start_turn(session_id, body.get("text", ""))

    prompt = body.get("message") or body.get("input") or ""

    if not prompt:
        return JSONResponse({"error": "no 'message' or 'input' in body"}, status_code=400)

    with tracer().start_as_current_span("invocations") as span:
        span.set_attribute("agent.session_id", session_id)
        span.set_attribute("browser.connected", hub.is_connected(session_id))
        if not hub.is_connected(session_id):
            return JSONResponse(
                {
                    "error": "no operator browser attached for this session",
                    "session_id": session_id,
                    "hint": "open the extension side panel and connect first",
                },
                status_code=409,
                headers={"agent_session_id": session_id},
            )
        answer = await runner.ask(session_id, prompt)

    return JSONResponse(
        {"session_id": session_id, "output": answer, "activity": hub.activity(session_id)},
        headers={"agent_session_id": session_id},
    )


async def attach_sse(session_id: str) -> StreamingResponse:
    """Open the operator browser's long-lived tool-call channel.

    Registering the sink under ``hub.attach`` is what flips ``is_connected``,
    so the rest of the codebase cannot tell this is not a WebSocket.
    """
    sink = SseSink(session_id)
    hub.attach(session_id, sink)

    async def body():
        try:
            async for chunk in sink.stream():
                yield chunk
        finally:
            # Fires when the client disconnects, so a closed side panel does
            # not leave the session looking permanently attached.
            hub.detach(session_id)

    return StreamingResponse(
        body(), headers={**SSE_HEADERS, "agent_session_id": session_id}
    )


async def start_turn(session_id: str, text: str) -> JSONResponse:
    """Kick off a turn; its output flows out over the attached SSE stream.

    This returns immediately rather than awaiting the turn. A turn blocks on
    browser tool calls, and those results arrive as *separate POSTs* -- so
    holding this request open would deadlock the client if it serialised
    requests.
    """
    if not hub.is_connected(session_id):
        return JSONResponse(
            {"error": "no operator browser attached for this session", "session_id": session_id},
            status_code=409,
        )
    if not text:
        return JSONResponse({"error": "no 'text' in body"}, status_code=400)

    async def run() -> None:
        await hub.send(session_id, {"type": "turn_started"})
        try:
            answer = await runner.ask(session_id, text)
            await hub.send(session_id, {"type": "turn_finished", "text": answer})
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            log.exception("turn failed")
            await hub.send(session_id, {"type": "error", "message": str(exc)})

    asyncio.create_task(run())
    return JSONResponse({"ok": True, "session_id": session_id}, status_code=202)


async def debug_tool(request: Request) -> JSONResponse:
    """Dispatch one browser tool directly, bypassing the model.

    Invaluable for developing tools and for demos: proves the relay and the
    extension's executor work without spending a model call.

        curl -X POST 'localhost:8088/debug/tool?session=<id>' \
          -d '{"name":"browser_read_table","arguments":{"selector":"#shipments"}}'
    """
    session_id = request.query_params.get("session", "")
    body = await request.json()
    name = body.get("name", "")
    args = body.get("arguments", {})

    if not hub.is_connected(session_id):
        return JSONResponse(
            {"error": "no browser attached", "session_id": session_id}, status_code=409
        )

    output = await hub.dispatch_tool(session_id, name, args)
    return JSONResponse({"tool": name, "output": json.loads(output)})


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncIterator[None]:
    """Startup/shutdown. Starlette >=1.0 removed on_startup/on_shutdown."""
    setup_tracing()
    log.info("ghcp-sdk-browser listening on :%d", PORT)
    try:
        yield
    finally:
        await runner.stop()
        log.info("shutdown complete")


app = Starlette(
    routes=[
        Route("/readiness", readiness, methods=["GET"]),
        Route("/info", info, methods=["GET"]),
        Route("/invocations", invocations, methods=["POST"]),
        Route("/debug/tool", debug_tool, methods=["POST"]),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
