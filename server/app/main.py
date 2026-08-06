"""Foundry Hosted Agent container.

Contract (hosted-agent runtime):
  - listen on 8088, plain HTTP, bind 0.0.0.0
  - GET  /readiness  -> 200 when ready
  - POST /invocations -> the agent protocol endpoint
  - graceful shutdown on SIGTERM

Plus a WebSocket at /ws/agent used by the browser extension. The extension is
the tool executor; this server only reasons.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .agents import CUSTOM_AGENTS, runner
from .config import PORT
from .hub import hub
from .telemetry import setup_tracing, tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def readiness(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", status_code=200)


async def info(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "agent": "ghcp-sdk-browser",
            "master_tools": [
                "browser_navigate", "browser_read_table", "browser_read_text",
                "browser_click", "browser_fill", "browser_wait_for",
            ],
            "subagents": [
                {
                    "name": a["name"],
                    "model": a.get("model"),
                    "tools": a.get("tools", []),
                    "skills": a.get("skills", []),
                }
                for a in CUSTOM_AGENTS
            ],
        }
    )


async def invocations(request: Request) -> JSONResponse:
    """Foundry Invocations protocol.

    Session binding is by the ``agent_session_id`` QUERY parameter -- the
    platform reads it only from the query string. We reuse it as the relay
    session so the hosted sandbox and the operator's browser stay paired.
    """
    session_id = request.query_params.get("agent_session_id") or uuid.uuid4().hex
    body = await request.json()
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


async def ws_agent(websocket: WebSocket) -> None:
    """The extension connects here and becomes the tool executor.

    Route is ``/invocations_ws`` because that is the ONLY WebSocket path the
    Foundry hosted-agent proxy will relay -- arbitrary paths are not routed.

    Session resolution follows the platform contract: prefer the injected
    ``FOUNDRY_AGENT_SESSION_ID``, else the ``agent_session_id`` query param
    (the platform forwards unknown query params unchanged), else generate one.
    The legacy ``session`` param is still accepted for local development.
    """
    session_id = (
        os.getenv("FOUNDRY_AGENT_SESSION_ID")
        or websocket.query_params.get("agent_session_id")
        or websocket.query_params.get("session")
        or uuid.uuid4().hex
    )
    await websocket.accept()
    hub.attach(session_id, websocket)
    await websocket.send_text(json.dumps({"type": "connected", "session_id": session_id}))

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "tool_result":
                hub.resolve_tool(msg)

            elif kind == "prompt":
                text = msg.get("text", "")
                await websocket.send_text(json.dumps({"type": "turn_started"}))
                try:
                    answer = await runner.ask(session_id, text)
                    await websocket.send_text(
                        json.dumps({"type": "turn_finished", "text": answer})
                    )
                except Exception as exc:  # noqa: BLE001 - surface to the UI
                    log.exception("turn failed")
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": str(exc)})
                    )

            elif kind == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        pass
    finally:
        hub.detach(session_id)


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
        WebSocketRoute("/invocations_ws", ws_agent),
        # Legacy local-dev alias; the hosted proxy only relays /invocations_ws.
        WebSocketRoute("/ws/agent", ws_agent),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
