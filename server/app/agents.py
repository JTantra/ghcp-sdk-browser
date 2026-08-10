"""Master orchestrator + subagents.

Master owns the browser tools and delegates analysis. Each subagent carries its
own tools, skills and model -- configured declaratively via ``custom_agents``.
``default_agent.excluded_tools`` keeps the analysis tools off the master so it
must delegate rather than doing the work itself.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Any

from copilot import CopilotClient

from .browser_tools import bind_session, browser_tools
from .config import (
    COPILOT_HOME,
    COPILOT_MODEL,
    FOUNDRY_AGENT_NAME,
    GITHUB_TOKEN,
    SKILLS_DIR,
)
from .hub import hub
from .ops_tools import ops_tools
from .telemetry import tracer

log = logging.getLogger("agents")

#: The demo ops board. Served over HTTP deliberately: a file:// tab cannot be
#: scripted by the extension (Chrome requires the per-extension "Allow access
#: to file URLs" opt-in, which a manifest cannot request), so the agent would
#: fail with a permissions error and ask the operator for a URL.
OPS_BOARD_URL = os.getenv("OPS_BOARD_URL", "http://localhost:5055")

MASTER_PROMPT = f"""You are the Fleet Operations orchestrator.

You are the only agent with browser tools. The browser runs inside the
operator's authenticated session, so you can read pages they are signed in to.

The operations console (the "ops board" / "shipment board") is always at:

    {OPS_BOARD_URL}

Never ask the operator for its URL. If the active tab is not already on that
page -- including when it is a file:// path, a blank tab, or anything else --
call `browser_navigate` with exactly {OPS_BOARD_URL} and carry on. A file://
tab cannot be read for security reasons, so navigating is the correct fix
rather than reporting failure.

Useful selectors on that page:
  #shipments  the shipment table   (use browser_read_table)
  #filter     status filter select (use browser_fill, then click #apply)
  #detail     detail pane revealed by clicking a shipment row

Your job:
1. Use browser tools to gather data from the operations console.
2. Delegate analysis to your subagents -- never do their work yourself:
   - `risk-analyst` scores shipment risk
   - `recovery-planner` proposes recovery levers
   - `reporter` formats the final brief
3. Return the reporter's markdown brief as your final answer, unchanged.

Rules:
- Read data before reasoning about it. Never invent a shipment or a number.
- Pass structured JSON between subagents, not prose summaries.
- If a browser tool fails, say which tool and why. Do not silently guess.
"""

CUSTOM_AGENTS: list[dict[str, Any]] = [
    {
        "name": "risk-analyst",
        "display_name": "Risk Analyst",
        "description": (
            "Scores shipment delay risk from ops-board rows. Use when you have "
            "shipment data and need risk scores and bands."
        ),
        "prompt": (
            "You score shipment operational risk. Apply the risk-scoring skill "
            "exactly as written. Return strict JSON only -- no prose, no markdown "
            "fences. Never score a shipment that was not in your input."
        ),
        "skills": ["risk-scoring"],
        "tools": [],
        "model": "claude-sonnet-5",
    },
    {
        "name": "recovery-planner",
        "display_name": "Recovery Planner",
        "description": (
            "Proposes schedule-recovery options for at-risk shipments. Use after "
            "risk scoring, for shipments in the critical or elevated bands."
        ),
        "prompt": (
            "You plan schedule recovery. Apply the recovery-playbook skill. "
            "You MUST call ops_node_capacity before recommending crane "
            "reallocation. Return strict JSON only."
        ),
        "skills": ["recovery-playbook"],
        "tools": ["ops_node_capacity", "ops_carrier_policy"],
        "model": "claude-sonnet-5",
    },
    {
        "name": "reporter",
        "display_name": "Reporter",
        "description": (
            "Formats scored shipments and recovery options into the standard "
            "markdown recovery brief. Use last, once analysis is complete."
        ),
        "prompt": (
            "You format operations briefs. Apply the report-format skill exactly. "
            "Output markdown only. Every number must come from your input."
        ),
        "skills": ["report-format"],
        "tools": [],
        "model": "claude-haiku-4.5",
    },
]

# Analysis tools live on subagents only. Excluding them from the default agent
# forces the master to delegate and keeps its context lean.
MASTER_EXCLUDED = ["ops_node_capacity", "ops_carrier_policy"]


class AgentRunner:
    """Owns one CopilotClient and one session per relay session id."""

    def __init__(self) -> None:
        self._client: CopilotClient | None = None
        self._sessions: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._client is not None:
            return
        if not GITHUB_TOKEN:
            raise RuntimeError("GITHUB_TOKEN is not set; the Copilot SDK cannot authenticate.")
        self._client = CopilotClient(
            github_token=GITHUB_TOKEN,
            use_logged_in_user=False,
            log_level="info",
            # The platform sets HOME=/home/session, a mount this container's
            # user cannot write to, so the SDK's default of $HOME fails with
            # EACCES on the first turn. base_directory becomes COPILOT_HOME
            # for the spawned Node CLI, pointing its session state somewhere
            # we own. (XDG_CACHE_HOME in the Dockerfile covers the separate
            # CLI *download* cache -- both are needed.)
            base_directory=COPILOT_HOME,
        )
        await self._client.start()
        log.info("copilot client started")

    async def stop(self) -> None:
        for sid in list(self._sessions):
            try:
                await self._sessions[sid].disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
        if self._client is not None:
            await self._client.stop()
            self._client = None

    async def _session_for(self, relay_session: str):
        async with self._lock:
            if relay_session in self._sessions:
                return self._sessions[relay_session]

            assert self._client is not None
            session = await self._client.create_session(
                model=COPILOT_MODEL,
                streaming=True,
                # Without this the SDK reports only the MASTER's stream, so a
                # subagent's work is invisible between `subagent.started` and
                # `subagent.completed`.
                include_sub_agent_streaming_events=True,
                # MASTER_PROMPT was previously defined but never passed, so the
                # orchestrator ran with no system prompt at all -- which is why
                # it improvised (e.g. asking the operator for the board URL)
                # instead of following the delegation rules.
                system_message=MASTER_PROMPT,
                enable_skills=True,
                skill_directories=[str(SKILLS_DIR)],
                # ops_tools() are in-process (see ops_tools.py) and ship inside
                # this container, so no MCP server is needed or contacted.
                # A stale mcp_servers entry pointing at an unreachable URL is
                # tolerated by the SDK but adds ~1s of connect retries to the
                # first session, so it is deliberately absent.
                tools=browser_tools() + ops_tools(),
                custom_agents=CUSTOM_AGENTS,
                default_agent={"excluded_tools": MASTER_EXCLUDED},
            )
            # The tool handlers receive inv.session_id -- the COPILOT session
            # id -- and map it back to the relay session to find the right
            # browser. Binding relay->relay never matched, so every tool call
            # fell through to the raw copilot id and found no attached browser.
            copilot_sid = str(
                getattr(session, "session_id", None) or getattr(session, "id", relay_session)
            )
            bind_session(copilot_sid, relay_session)
            self._sessions[relay_session] = session
            log.info(
                "session created relay=%s copilot=%s subagents=%d",
                relay_session,
                copilot_sid,
                len(CUSTOM_AGENTS),
            )
            return session

    async def ask(self, relay_session: str, prompt: str) -> str:
        """Run one turn. Streams progress to the browser, returns the final text."""
        await self.start()
        session = await self._session_for(relay_session)

        chunks: list[str] = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        # Open spans keyed by tool_call_id, so a subagent/tool span closes on
        # its OWN completion event rather than being guessed at by ordering.
        # Foundry renders nesting from parent/child span links, so these are
        # started as children of the current `invoke_agent` span.
        spans: dict[str, Any] = {}
        turn_ctx = tracer().start_as_current_span(
            f"invoke_agent {FOUNDRY_AGENT_NAME}"
        )

        def emit(payload: dict) -> None:
            """Push one discrete frame to the panel."""
            loop.create_task(hub.send(relay_session, payload))

        def open_span(key: str, name: str, attrs: dict) -> None:
            if not key or key in spans:
                return
            span = tracer().start_span(name)
            for k, v in attrs.items():
                if v is not None:
                    span.set_attribute(k, v)
            spans[key] = span

        def close_span(key: str, attrs: dict | None = None) -> None:
            span = spans.pop(key, None)
            if span is None:
                return
            for k, v in (attrs or {}).items():
                if v is not None:
                    span.set_attribute(k, v)
            span.end()

        def dispatch(event: Any) -> None:
            """Guarded wrapper: a telemetry bug must never kill a turn.

            An exception raised in here previously propagated into the SDK and
            aborted the session ("'str' object has no attribute 'get'" ended a
            turn at 5.8s). Progress reporting is strictly observability, so it
            fails soft and logs instead.
            """
            try:
                _dispatch(event)
            except Exception:  # noqa: BLE001 - never let reporting break a turn
                log.exception("event handler failed for %r", getattr(event, "type", "?"))

        def _dispatch(event: Any) -> None:
            """Single sync handler for every session event.

            The SDK's ``on()`` takes ONE handler and dispatches by
            ``event.type`` -- there is no per-event-name subscription. It also
            calls handlers synchronously, so this must not be a coroutine
            (an async handler is never awaited and silently does nothing).
            Anything async is therefore scheduled back onto the loop.
            """
            etype = getattr(event.type, "value", str(event.type))
            data = getattr(event, "data", None)

            def f(name: str, default: Any = "") -> Any:
                """Read a field off the typed dataclass or dict payload.

                Guards the case where ``data`` is neither -- some events carry
                a bare string payload, and calling ``.get`` on it raised
                "'str' object has no attribute 'get'", killing the turn.
                """
                if isinstance(data, dict):
                    return data.get(name, default)
                return getattr(data, name, default)

            def jsonable(value: Any) -> Any:
                """Coerce a payload field into something json.dumps can take.

                ``arguments`` is typed ``Any`` and arrives as a JSON *string*
                from the CLI, but as a dict in-process. Normalise so the panel
                always receives an object.
                """
                if isinstance(value, str):
                    try:
                        return json.loads(value)
                    except (ValueError, TypeError):
                        return {"raw": value[:200]}
                if isinstance(value, dict):
                    return value
                return {}

            def as_text(value: Any) -> str:
                """Some 'description'-style fields are objects, not strings."""
                if value is None:
                    return ""
                if isinstance(value, str):
                    return value
                for attr in ("text", "content", "name", "description"):
                    got = getattr(value, attr, None)
                    if isinstance(got, str):
                        return got
                if isinstance(value, dict):
                    for key in ("text", "content", "name", "description"):
                        if isinstance(value.get(key), str):
                            return value[key]
                return str(value)

            # ---- assistant text -------------------------------------------
            if etype == "assistant.message_delta":
                text = _delta_text(event)
                if text:
                    chunks.append(text)
                    emit({"type": "assistant_delta", "text": text})

            elif etype == "assistant.message":
                text = _message_text(event)
                if text:
                    emit({"type": "assistant_message", "text": text})

            # ---- reasoning ("thought process") ----------------------------
            # Its own frame type so the panel can render it as a separate,
            # collapsible bubble instead of concatenating it into the answer.
            elif etype == "assistant.reasoning":
                text = as_text(f("content"))
                if text:
                    emit({"type": "reasoning", "text": text})

            # ---- subagent lifecycle ---------------------------------------
            elif etype == "subagent.started":
                name = as_text(f("agent_display_name")) or as_text(f("agent_name")) or "subagent"
                emit({
                    "type": "subagent_started",
                    "name": name,
                    "model": as_text(f("model")),
                    "description": as_text(f("agent_description")),
                })
                open_span(
                    f"sub:{f('tool_call_id')}",
                    f"invoke_agent {as_text(f('agent_name')) or name}",
                    {
                        "gen_ai.operation.name": "invoke_agent",
                        "gen_ai.agent.name": as_text(f("agent_name")) or name,
                        "gen_ai.agent.description": as_text(f("agent_description")),
                        "gen_ai.request.model": as_text(f("model")),
                        "gen_ai.conversation.id": relay_session,
                    },
                )

            elif etype == "subagent.completed":
                name = as_text(f("agent_display_name")) or as_text(f("agent_name")) or "subagent"
                emit({
                    "type": "subagent_completed",
                    "name": name,
                    "duration_ms": _num(f("duration")),
                    "total_tokens": _num(f("total_tokens")),
                    "tool_calls": _num(f("total_tool_calls")),
                })
                close_span(
                    f"sub:{f('tool_call_id')}",
                    {
                        "gen_ai.usage.total_tokens": _num(f("total_tokens")),
                        "gen_ai.agent.tool_calls": _num(f("total_tool_calls")),
                    },
                )

            elif etype == "subagent.failed":
                emit({
                    "type": "subagent_failed",
                    "name": as_text(f("agent_name")) or "subagent",
                    "error": as_text(f("error")) or "failed",
                })
                close_span(f"sub:{f('tool_call_id')}", {"error": True})

            # ---- tool execution -------------------------------------------
            elif etype == "tool.execution_start":
                emit({
                    "type": "tool_started",
                    "name": str(f("tool_name") or "tool"),
                    "arguments": jsonable(f("arguments", {})),
                })
                open_span(
                    f"tool:{f('tool_call_id')}",
                    f"execute_tool {f('tool_name')}",
                    {
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": str(f("tool_name") or ""),
                        "gen_ai.tool.call.id": str(f("tool_call_id") or ""),
                        "gen_ai.tool.description": as_text(f("tool_description")),
                        "gen_ai.conversation.id": relay_session,
                    },
                )

            elif etype == "tool.execution_complete":
                ok = bool(f("success", True))
                emit({
                    "type": "tool_finished",
                    "name": as_text(f("tool_description")) or "tool",
                    "success": ok,
                    "error": as_text(f("error")),
                })
                close_span(
                    f"tool:{f('tool_call_id')}",
                    {"gen_ai.tool.success": ok, "error.type": as_text(f("error"))},
                )

            # ---- skills ----------------------------------------------------
            elif etype == "skill.invoked":
                emit({"type": "skill_invoked", "name": as_text(f("name")) or "skill"})

            # ---- token usage ----------------------------------------------
            elif etype == "assistant.usage":
                emit({
                    "type": "usage",
                    "input_tokens": _num(f("input_tokens")),
                    "output_tokens": _num(f("output_tokens")),
                })

            # ---- terminal --------------------------------------------------
            elif etype == "session.idle":
                loop.call_soon_threadsafe(done.set)

            elif etype == "session.error":
                emit({"type": "error", "message": str(data if data is not None else event)})
                loop.call_soon_threadsafe(done.set)

        unsubscribe = session.on(dispatch)

        try:
            with turn_ctx as span:
                span.set_attribute("gen_ai.operation.name", "invoke_agent")
                span.set_attribute("gen_ai.agent.name", FOUNDRY_AGENT_NAME)
                span.set_attribute("gen_ai.request.model", COPILOT_MODEL)
                span.set_attribute("gen_ai.conversation.id", relay_session)
                span.set_attribute("gen_ai.input.messages", prompt[:4000])
                await session.send(prompt=prompt)
                await done.wait()
                answer = "".join(chunks).strip()
                span.set_attribute("gen_ai.output.messages", answer[:4000])
        finally:
            # Without this, every turn stacks another handler on the session.
            unsubscribe()
            # A turn that errors mid-tool would otherwise leak an unended span,
            # which Foundry renders as a trace that never completes.
            for key in list(spans):
                close_span(key, {"error.type": "turn ended before completion"})
        return answer


def _num(value: Any) -> float | int:
    """Coerce a numeric-ish payload field to a JSON-safe number.

    ``duration`` arrives as a ``timedelta`` and several counters are
    ``| None``; both break json.dumps or arithmetic in the panel.
    """
    if value is None:
        return 0
    if isinstance(value, timedelta):
        return round(value.total_seconds() * 1000)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _delta_text(event: Any) -> str:
    data = getattr(event, "data", None) or {}
    if isinstance(data, dict):
        return data.get("deltaContent") or data.get("delta_content") or ""
    return getattr(data, "delta_content", "") or getattr(data, "deltaContent", "") or ""


def _message_text(event: Any) -> str:
    data = getattr(event, "data", None) or {}
    if isinstance(data, dict):
        return data.get("content") or ""
    return getattr(data, "content", "") or ""


runner = AgentRunner()
