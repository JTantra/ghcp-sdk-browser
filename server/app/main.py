"""Foundry Hosted Agent container.

Contract (hosted-agent runtime):
  - listen on 8088, plain HTTP, bind 0.0.0.0
  - GET  /readiness  -> 200 when ready
  - POST /invocations -> the agent protocol endpoint
  - graceful shutdown on SIGTERM

Built on ``InvocationAgentServerHost`` (azure-ai-agentserver-invocations), the
documented hosting path. It is itself a Starlette subclass, so our extra routes
still work -- but we now inherit three things a hand-rolled app has to fake:

  * ``TraceContextMiddleware`` attaches the inbound W3C trace context as the
    AMBIENT OTel context, so every span created during a request is
    automatically a child of the platform's trace. This is what makes spans
    show up under `invoke_agent` in the Foundry portal. Our previous approach
    stashed the traceparent in a dict keyed by session and looked it up from a
    detached task -- which silently missed and root-ed every span.
  * ``configure_observability`` wires the exporters and GenAI semconv.
  * ``request.state.session_id``, resolved from the query param OR the
    platform's FOUNDRY_AGENT_SESSION_ID env var.

The browser extension attaches over HTTPS+SSE on the same /invocations path
(see sse.py). It is the tool executor; this server only reasons.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

import uvicorn
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .agents import runner
from .config import PORT
from .hub import hub
from .sse import SSE_HEADERS, SseSink, probe_stream
from .telemetry import setup_tracing, tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("main")

#: Process start time, used to stamp fsprobe markers so a marker written by an
#: EARLIER container is distinguishable from this one's.
_BOOT_TS = time.time()


def fs_probe() -> dict:
    """Report which candidate directories survive a container restart.

    Writes a marker stamped with this process's boot id into each candidate,
    then lists every marker present. A marker from an EARLIER boot means that
    path persists across restarts -- which is the precondition for using
    Copilot's resume_session(): its conversation state lives under
    COPILOT_HOME, and that currently points at /app/.copilot (an image layer
    that resets on every deploy).

    Also reports writability, because /home/session is the platform's session
    mount and previously raised EACCES for our non-root user -- the reason
    COPILOT_HOME was moved to /app in the first place.
    """
    import getpass
    import os
    import platform
    from pathlib import Path

    boot = f"{platform.node()}-{int(_BOOT_TS)}"
    candidates = [
        "/home/session",
        "/home/session/.state",
        os.environ.get("HOME", "/home/agent"),
        "/app/.copilot",
        "/tmp",
    ]

    results = []
    # Ownership/mode of $HOME decides whether the persistent mount is usable.
    # It is not writable for uid 1000 today, so the entrypoint falls back to
    # /app (ephemeral, no resume). Report the actual owner so we know whether a
    # USER change or a different subdir would fix it, rather than guessing.
    try:
        st = Path(os.environ.get("HOME", "/home/session")).stat()
        home_stat = {
            "uid": st.st_uid, "gid": st.st_gid, "mode": oct(st.st_mode & 0o7777),
        }
    except Exception as exc:  # noqa: BLE001
        home_stat = {"error": f"{type(exc).__name__}: {exc}"}

    for path in candidates:
        entry: dict = {"path": path}
        p = Path(path)
        try:
            entry["exists"] = p.is_dir()
            if not entry["exists"]:
                try:
                    p.mkdir(parents=True, exist_ok=True)
                    entry["created"] = True
                    entry["exists"] = True
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = f"mkdir: {type(exc).__name__}: {exc}"
                    results.append(entry)
                    continue

            # Markers already here -- any from another boot proves persistence.
            found = sorted(q.name for q in p.glob(".fsprobe-*"))
            entry["markers_found"] = found
            entry["from_earlier_boot"] = [m for m in found if boot not in m]

            marker = p / f".fsprobe-{boot}"
            marker.write_text(str(_BOOT_TS), encoding="utf-8")
            entry["writable"] = True
        except Exception as exc:  # noqa: BLE001 - a probe must never 500
            entry["writable"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)

    return {
        "boot_id": boot,
        "user": getpass.getuser(),
        "uid": os.getuid(),
        "HOME": os.environ.get("HOME"),
        "COPILOT_HOME": os.environ.get("COPILOT_HOME"),
        "home_stat": home_stat,
        "FOUNDRY_AGENT_SESSION_ID": os.environ.get("FOUNDRY_AGENT_SESSION_ID"),
        "candidates": results,
        "verdict": "run me again after a redeploy; a non-empty "
                   "from_earlier_boot means that path persists",
    }


async def readiness(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", status_code=200)


async def info(_request: Request) -> JSONResponse:
    from .agents import OPS_BOARD_URL, catalogue
    from .telemetry import telemetry_status
    from .skills_store import resolve_skill_directories, skills_status

    # Resolve on demand so /info reports the REAL fetch result rather than the
    # untouched initial state. Without this the report reads "image" with no
    # error before any session has run, which is indistinguishable from a
    # silent failure. Cached, so this costs nothing after the first call.
    await asyncio.to_thread(resolve_skill_directories)

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

    # No remember_trace_context() here: TraceContextMiddleware has already
    # attached the inbound W3C context as the AMBIENT OTel context, so any span
    # started below is a child of the platform's trace automatically.

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

    if mode == "fsprobe":
        # Which directories survive a container restart? The resume_session()
        # design depends on COPILOT_HOME living somewhere persistent, and it
        # currently points at /app/.copilot -- an image layer that resets on
        # every deploy. Writes a marker stamped with this boot, and reports
        # markers from EARLIER boots: any that show up prove persistence.
        return JSONResponse(fs_probe())

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


# The host owns POST /invocations and GET /readiness; our own routes are added
# on top because AgentServerHost subclasses Starlette. Note /readiness is NOT
# re-declared -- the host provides it, and duplicating it would shadow the
# platform's health contract.
app = InvocationAgentServerHost()


# The host builds its own lifespan and rejects a `lifespan=` kwarg; Starlette
# 1.x removed both `on_event` and `Router.on_startup`. So WRAP the host's
# lifespan rather than replace it -- its startup logging and shutdown handling
# still run, with ours nested inside.
_host_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def _lifespan(scope) -> AsyncIterator[None]:
    async with _host_lifespan(scope):
        setup_tracing()
        log.info("ghcp-sdk-browser listening on :%d", PORT)
        try:
            yield
        finally:
            await runner.stop()
            log.info("shutdown complete")


app.router.lifespan_context = _lifespan


@app.invoke_handler
async def _invoke(request: Request):
    """POST /invocations, dispatched by mode (see `invocations`)."""
    return await invocations(request)


app.add_route("/info", info, methods=["GET"])
app.add_route("/debug/tool", debug_tool, methods=["POST"])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
