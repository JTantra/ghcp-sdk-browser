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

from opentelemetry.trace import set_span_in_context

from .browser_tools import (
    bind_session,
    browser_tools,
    clear_tool_parent,
    set_tool_parent,
)
from .config import (
    AGENTS_FILE,
    COPILOT_HOME,
    COPILOT_MODEL,
    FOUNDRY_AGENT_NAME,
    GITHUB_TOKEN,
    SKILLS_DIR,
)
from .hub import hub
from .ops_tools import ops_tools
from .telemetry import flush, session_context, tracer
from .skills_store import resolve_skill_directories

log = logging.getLogger("agents")

#: The demo ops board. Served over HTTP deliberately: a file:// tab cannot be
#: scripted by the extension (Chrome requires the per-extension "Allow access
#: to file URLs" opt-in, which a manifest cannot request), so the agent would
#: fail with a permissions error and ask the operator for a URL.
OPS_BOARD_URL = os.getenv("OPS_BOARD_URL", "http://localhost:5055")


def _load_agent_config() -> dict[str, Any]:
    """Read the declarative agent topology from agents.json.

    Kept out of Python so the prompt, subagents, models, tools and skills can
    be reviewed and changed in one readable place. Keys beginning with
    ``$comment`` are documentation and are ignored.
    """
    with AGENTS_FILE.open(encoding="utf-8") as fh:
        raw = json.load(fh)

    def clean(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items() if not k.startswith("$comment")}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj

    return clean(raw)


_CONFIG = _load_agent_config()

#: Prompt lines are stored as a JSON array purely for readability.
MASTER_PROMPT = "\n".join(_CONFIG["master"]["prompt"]).format(
    ops_board_url=OPS_BOARD_URL
)

CUSTOM_AGENTS: list[dict[str, Any]] = _CONFIG["subagents"]

#: Master model. Env wins (deployment override), then agents.json.
MODEL = os.getenv("COPILOT_MODEL") or _CONFIG.get("model") or COPILOT_MODEL

# Analysis tools live on subagents only. Excluding them from the default agent
# forces the master to delegate and keeps its context lean.
MASTER_EXCLUDED: list[str] = _CONFIG["master"]["excluded_tools"]


def _skill_catalogue() -> list[dict[str, Any]]:
    """Read the skills directory so the catalogue cannot drift from reality."""
    skills: list[dict[str, Any]] = []
    if not SKILLS_DIR.is_dir():
        return skills
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        name, description = path.parent.name, ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            skills.append({"name": name, "description": "", "path": str(path)})
            continue
        # Minimal frontmatter read: avoids a YAML dependency for two keys.
        if text.startswith("---"):
            end = text.find("\n---", 3)
            for line in text[3 : end if end > 0 else 0].splitlines():
                key, _, value = line.partition(":")
                key, value = key.strip(), value.strip().strip("\"'")
                if key == "name" and value:
                    name = value
                elif key == "description" and value:
                    description = value
        used_by = [a["name"] for a in CUSTOM_AGENTS if path.parent.name in a.get("skills", [])]
        skills.append({
            "name": name,
            "description": description,
            "used_by": used_by,
            "path": str(path.relative_to(SKILLS_DIR.parent)),
        })
    return skills


def catalogue() -> dict[str, Any]:
    """The agent's full tool + skill inventory.

    Built from the live ``define_tool`` definitions and the skills directory,
    so it stays truthful as those change. Foundry does not introspect a hosted
    agent's in-process tools, so this endpoint is how the inventory is
    published (see also scripts/publish_toolbox.sh for the MCP toolbox).
    """
    def describe(tools: list) -> list[dict[str, Any]]:
        out = []
        for t in tools:
            spec = getattr(t, "spec", t)
            out.append({
                "name": getattr(spec, "name", None) or getattr(t, "name", "?"),
                "description": getattr(spec, "description", None)
                or getattr(t, "description", ""),
            })
        return out

    browser = describe(browser_tools())
    ops = describe(ops_tools())
    return {
        "model": MODEL,
        "master": {
            "tools": [t["name"] for t in browser],
            "excluded_tools": MASTER_EXCLUDED,
        },
        "tools": browser + ops,
        "skills": _skill_catalogue(),
        "subagents": [
            {
                "name": a["name"],
                "display_name": a.get("display_name"),
                "description": a.get("description"),
                "model": a.get("model"),
                "tools": a.get("tools", []),
                "skills": a.get("skills", []),
            }
            for a in CUSTOM_AGENTS
        ],
    }


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

    async def end_session(self, relay_session: str) -> bool:
        """Drop one relay session's Copilot session.

        The platform mints a new agent_session_id for a new session, so a
        stale one would otherwise sit in memory for the life of the container
        holding its conversation history.
        """
        async with self._lock:
            session = self._sessions.pop(relay_session, None)
        if session is None:
            return False
        try:
            await session.disconnect()
        except Exception:  # noqa: BLE001 - teardown must not raise
            log.debug("disconnect failed for %s", relay_session, exc_info=True)
        log.info("session ended relay=%s", relay_session)
        return True

    async def _session_for(self, relay_session: str):
        async with self._lock:
            if relay_session in self._sessions:
                return self._sessions[relay_session]

            # Skills are pulled from Foundry at session start so a playbook can
            # be updated without rebuilding the image. resolve_skill_directories
            # does blocking HTTP, so it runs off the event loop; the result is
            # process-cached, so only the first session pays the download.
            skill_dirs = await asyncio.to_thread(resolve_skill_directories)

            assert self._client is not None
            session = await self._client.create_session(
                model=MODEL,
                streaming=True,
                # Without this the SDK reports only the MASTER's stream, so a
                # subagent's work is invisible between `subagent.started` and
                # `subagent.completed`.
                include_sub_agent_streaming_events=True,
                # MASTER_PROMPT was previously defined but never passed, so the
                # orchestrator ran with no instructions at all -- which is why
                # it improvised (e.g. asking the operator for the board URL).
                # `system_message` takes a CONFIG DICT, not a bare string:
                # passing a str raises "'str' object has no attribute 'get'"
                # inside the SDK and kills the turn. "append" keeps the CLI's
                # own tool-use foundation and adds ours, where "replace" would
                # drop the SDK's guardrails entirely.
                system_message={"mode": "append", "content": MASTER_PROMPT},
                enable_skills=True,
                skill_directories=skill_dirs,
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
        #: tool_call_id -> tool name, so the completion frame can be labelled.
        tool_names: dict[str, str] = {}
        #: reasoning_id -> accumulated reasoning text (delta-only models).
        reasoning_buf: dict[str, str] = {}

        # Root this turn in the PLATFORM's trace when we captured its
        # traceparent, so Foundry shows our spans under its `invoke_agent`
        # request instead of in a separate, invisible trace.
        parent_ctx = session_context(relay_session)
        turn_span = tracer().start_span(
            f"invoke_agent {FOUNDRY_AGENT_NAME}", context=parent_ctx
        )
        # Explicit context object: the turn runs in a detached asyncio task, and
        # OTel's implicit context does not cross that boundary -- which is why
        # tool spans previously formed their own traces.
        turn_ctx = set_span_in_context(turn_span)
        # browser/ops tool handlers run under the SDK's own tasks, so they read
        # the parent from here rather than from the ambient context.
        set_tool_parent(relay_session, turn_ctx)

        def emit(payload: dict) -> None:
            """Push one discrete frame to the panel."""
            loop.create_task(hub.send(relay_session, payload))

        def open_span(key: str, name: str, attrs: dict) -> None:
            if not key or key in spans:
                return
            # Parent explicitly: these are created from the SDK's event
            # callback, where the ambient context is not our turn.
            span = tracer().start_span(name, context=turn_ctx)
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

            elif etype == "assistant.reasoning_delta":
                # Some models stream reasoning only as deltas and never send a
                # consolidated assistant.reasoning, so buffer by reasoning_id
                # and flush when the model starts speaking.
                rid = str(f("reasoning_id") or "r")
                piece = as_text(f("delta_content"))
                if piece:
                    reasoning_buf[rid] = reasoning_buf.get(rid, "") + piece

            elif etype in ("assistant.message_start", "assistant.turn_end"):
                for rid, text in list(reasoning_buf.items()):
                    if text.strip():
                        emit({"type": "reasoning", "text": text.strip()})
                    reasoning_buf.pop(rid, None)

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
                tname = str(f("tool_name") or "tool")
                # execution_complete carries no tool_name, so remember it here
                # to label the completion frame with something meaningful
                # instead of a generic "tool".
                tool_names[str(f("tool_call_id"))] = tname
                emit({
                    "type": "tool_started",
                    "name": tname,
                    "arguments": jsonable(f("arguments", {})),
                })
                open_span(
                    f"tool:{f('tool_call_id')}",
                    f"execute_tool {tname}",
                    {
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": tname,
                        "gen_ai.tool.call.id": str(f("tool_call_id") or ""),
                        "gen_ai.tool.description": as_text(f("tool_description")),
                        "gen_ai.conversation.id": relay_session,
                    },
                )

            elif etype == "tool.execution_complete":
                ok = bool(f("success", True))
                cid = str(f("tool_call_id"))
                emit({
                    "type": "tool_finished",
                    "name": tool_names.pop(cid, None) or as_text(f("tool_description")) or "tool",
                    "success": ok,
                    "error": as_text(f("error")),
                })
                close_span(
                    f"tool:{cid}",
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
            span = turn_span
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", FOUNDRY_AGENT_NAME)
            span.set_attribute("gen_ai.agent.id", FOUNDRY_AGENT_NAME)
            span.set_attribute("gen_ai.request.model", MODEL)
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
            turn_span.end()
            clear_tool_parent(relay_session)
            # Ship them now rather than waiting on the 5s batch window; the
            # container can be scaled down between turns.
            flush()
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
