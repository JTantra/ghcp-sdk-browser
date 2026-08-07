"""Master orchestrator + subagents.

Master owns the browser tools and delegates analysis. Each subagent carries its
own tools, skills and model -- configured declaratively via ``custom_agents``.
``default_agent.excluded_tools`` keeps the analysis tools off the master so it
must delegate rather than doing the work itself.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from copilot import CopilotClient

from .browser_tools import bind_session, browser_tools
from .config import COPILOT_HOME, COPILOT_MODEL, GITHUB_TOKEN, SKILLS_DIR
from .hub import hub
from .ops_tools import ops_tools
from .telemetry import tracer

log = logging.getLogger("agents")

MASTER_PROMPT = """You are the Fleet Operations orchestrator.

You are the only agent with browser tools. The browser runs inside the
operator's authenticated session, so you can read pages they are signed in to.

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
- If the browser is not connected, say so plainly and stop.
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
            bind_session(relay_session, relay_session)
            self._sessions[relay_session] = session
            log.info(
                "session created relay=%s subagents=%d",
                relay_session,
                len(CUSTOM_AGENTS),
            )
            return session

    async def ask(self, relay_session: str, prompt: str) -> str:
        """Run one turn. Streams progress to the browser, returns the final text."""
        await self.start()
        session = await self._session_for(relay_session)

        chunks: list[str] = []
        done = asyncio.Event()

        async def on_delta(event: Any) -> None:
            text = _delta_text(event)
            if text:
                chunks.append(text)
                await hub.send(relay_session, {"type": "assistant_delta", "text": text})

        async def on_message(event: Any) -> None:
            text = _message_text(event)
            if text:
                await hub.send(relay_session, {"type": "assistant_message", "text": text})

        async def on_idle(_event: Any) -> None:
            done.set()

        async def on_error(event: Any) -> None:
            await hub.send(
                relay_session, {"type": "error", "message": str(getattr(event, "data", event))}
            )
            done.set()

        session.on("assistant.message_delta", on_delta)
        session.on("assistant.message", on_message)
        session.on("session.idle", on_idle)
        session.on("session.error", on_error)

        with tracer().start_as_current_span("agent.turn") as span:
            span.set_attribute("session.relay", relay_session)
            span.set_attribute("prompt.chars", len(prompt))
            await session.send(prompt=prompt)
            await done.wait()
            answer = "".join(chunks).strip()
            span.set_attribute("answer.chars", len(answer))
        return answer


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
