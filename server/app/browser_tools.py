"""Browser tools owned by the MASTER agent.

Every handler forwards to the operator's browser over the relay and awaits the
result. Nothing here touches a browser directly -- the extension executes in the
operator's already-authenticated tab, so SSO/MFA are never re-solved server side.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from copilot import Tool, ToolInvocation, ToolResult, define_tool

from .hub import hub
from .telemetry import tracer


class NavigateParams(BaseModel):
    url: str = Field(description="Absolute URL to open in the operator's active tab.")


class ReadTableParams(BaseModel):
    selector: str = Field(
        default="table",
        description="CSS selector for the table element, e.g. '#shipments'.",
    )


class ClickParams(BaseModel):
    selector: str = Field(default="", description="CSS selector of the element to click.")
    text: str = Field(
        default="",
        description="Optional visible text to match instead of a selector.",
    )


class FillParams(BaseModel):
    selector: str = Field(description="CSS selector of the input or select element.")
    value: str = Field(description="Value to set.")


class ReadTextParams(BaseModel):
    selector: str = Field(default="body", description="CSS selector to read text from.")


class WaitForParams(BaseModel):
    selector: str = Field(description="CSS selector to wait for.")
    timeout_ms: int = Field(default=8000, description="Milliseconds before giving up.")


def _relay(tool_name: str):
    """Build a handler that ships the call to the browser and returns its JSON."""

    async def handler(inv: ToolInvocation) -> ToolResult:
        args = inv.arguments if isinstance(inv.arguments, dict) else {}
        session_id = _SESSION_BINDING.get(inv.session_id, inv.session_id)
        # Named per the OTel GenAI semantic conventions ("execute_tool {name}")
        # so Foundry's agent view renders this as a tool step, and parented
        # explicitly to the turn -- the SDK calls this from its own task, where
        # the ambient OTel context is not the turn's.
        with tracer().start_as_current_span(
            f"execute_tool {tool_name}", context=_TOOL_PARENT.get(session_id)
        ) as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", tool_name)
            span.set_attribute("gen_ai.tool.type", "browser")
            span.set_attribute("gen_ai.conversation.id", session_id)
            span.set_attribute("gen_ai.tool.call.arguments", json.dumps(args)[:2000])
            output = await hub.dispatch_tool(session_id, tool_name, args)
            span.set_attribute("gen_ai.tool.call.result", str(output)[:2000])
        return ToolResult(text_result_for_llm=output)

    return handler


# Copilot session id -> relay session id. Populated by agents.py at session start.
_SESSION_BINDING: dict[str, str] = {}

#: relay session id -> the OTel context of the in-flight turn.
#: Tool handlers are invoked from the SDK's own tasks, where the ambient
#: context is NOT the turn, so the parent has to be looked up explicitly.
#: Without this each tool span started its own trace and never appeared under
#: the turn in Foundry.
_TOOL_PARENT: dict[str, Any] = {}


def set_tool_parent(relay_session_id: str, ctx: Any) -> None:
    _TOOL_PARENT[relay_session_id] = ctx


def clear_tool_parent(relay_session_id: str) -> None:
    _TOOL_PARENT.pop(relay_session_id, None)


def bind_session(copilot_session_id: str, relay_session_id: str) -> None:
    _SESSION_BINDING[copilot_session_id] = relay_session_id


def browser_tools() -> list[Tool]:
    return [
        define_tool(
            "browser_navigate",
            description="Open a URL in the operator's active browser tab.",
            params_type=NavigateParams,
            handler=_relay("browser_navigate"),
            skip_permission=True,
        ),
        define_tool(
            "browser_read_table",
            description=(
                "Read an HTML table from the current page as structured JSON rows. "
                "Prefer this over reading raw text when the data is tabular."
            ),
            params_type=ReadTableParams,
            handler=_relay("browser_read_table"),
            skip_permission=True,
        ),
        define_tool(
            "browser_read_text",
            description="Read visible text from the current page for a CSS selector.",
            params_type=ReadTextParams,
            handler=_relay("browser_read_text"),
            skip_permission=True,
        ),
        define_tool(
            "browser_click",
            description="Click an element by CSS selector, or by visible text.",
            params_type=ClickParams,
            handler=_relay("browser_click"),
            skip_permission=True,
        ),
        define_tool(
            "browser_fill",
            description="Set the value of an input or select element and fire change events.",
            params_type=FillParams,
            handler=_relay("browser_fill"),
            skip_permission=True,
        ),
        define_tool(
            "browser_wait_for",
            description="Wait until an element matching the selector exists.",
            params_type=WaitForParams,
            handler=_relay("browser_wait_for"),
            skip_permission=True,
        ),
    ]
