"""Mocked ops-data tools for the subagents.

These stand in for a Foundry Toolbox. To swap in the real thing, drop these and
point ``mcp_servers`` at the toolbox consumer endpoint:

    https://<account>.services.ai.azure.com/api/projects/<project>/toolboxes/<name>/mcp?api-version=v1

authenticating with a token for ``https://ai.azure.com/.default``. The subagent
config does not change -- only where the tools come from.
"""
from __future__ import annotations

import contextlib
import json

from pydantic import BaseModel, Field

from copilot import Tool, ToolInvocation, ToolResult, define_tool

from .telemetry import tracer
from .browser_tools import _SESSION_BINDING, _TOOL_PARENT

# --- tracing --------------------------------------------------------------
@contextlib.contextmanager
def _tool_span(tool_name: str, inv: ToolInvocation, args: dict):
    """Span an in-process tool, parented to the agent that called it.

    These handlers run on the SDK's own tasks, where the ambient OTel context
    is EMPTY -- so a bare start_as_current_span() makes each call the root of
    its own trace and it vanishes from the Foundry trace view. Measured: three
    ops_node_capacity calls inside a recovery-planner turn each landed on a
    separate operation_Id with operation_Id == operation_ParentId.

    _TOOL_PARENT is maintained by agents.py and points at the ACTIVE agent --
    the running subagent, or the turn when no subagent is in flight -- which is
    what makes these nest under the subagent that actually invoked them.
    """
    session_id = _SESSION_BINDING.get(inv.session_id, inv.session_id)
    with tracer().start_as_current_span(
        f"execute_tool {tool_name}", context=_TOOL_PARENT.get(session_id)
    ) as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", tool_name)
        span.set_attribute("gen_ai.tool.type", "ops")
        span.set_attribute("gen_ai.conversation.id", session_id)
        span.set_attribute("gen_ai.tool.call.arguments", json.dumps(args)[:2000])
        yield span


# --- mock backing data ---------------------------------------------------
NODE_CAPACITY = {
    "Rotterdam": {"free_crane_hours": 14, "berth_slots": 2, "congestion": "moderate"},
    "Long Beach": {"free_crane_hours": 4, "berth_slots": 0, "congestion": "severe"},
    "Santos": {"free_crane_hours": 22, "berth_slots": 3, "congestion": "light"},
    "Oakland": {"free_crane_hours": 9, "berth_slots": 1, "congestion": "moderate"},
    "New York": {"free_crane_hours": 17, "berth_slots": 2, "congestion": "light"},
    "Mundra": {"free_crane_hours": 11, "berth_slots": 1, "congestion": "moderate"},
    "Yokohama": {"free_crane_hours": 19, "berth_slots": 3, "congestion": "light"},
}

CARRIER_POLICY = {
    "default": {
        "port_call_swap_allowed": True,
        "omission_requires": "commercial director sign-off",
        "max_recovery_cost_band": "high",
        "notice_hours": 12,
    }
}


class NodeCapacityParams(BaseModel):
    node: str = Field(description="Destination or intermediate port name, e.g. 'Rotterdam'.")


class CarrierPolicyParams(BaseModel):
    carrier: str = Field(default="default", description="Carrier name; 'default' if unknown.")


async def _node_capacity(inv: ToolInvocation) -> ToolResult:
    args = inv.arguments if isinstance(inv.arguments, dict) else {}
    node = str(args.get("node", "")).strip()
    with _tool_span("ops_node_capacity", inv, args) as span:
        data = NODE_CAPACITY.get(node)
        if data is None:
            payload = {"node": node, "known": False,
                       "note": "No capacity record. Do not assume availability."}
        else:
            payload = {"node": node, "known": True, **data}
        span.set_attribute("gen_ai.tool.call.result", json.dumps(payload)[:2000])
    return ToolResult(text_result_for_llm=json.dumps(payload))


async def _carrier_policy(inv: ToolInvocation) -> ToolResult:
    args = inv.arguments if isinstance(inv.arguments, dict) else {}
    carrier = str(args.get("carrier", "default")) or "default"
    with _tool_span("ops_carrier_policy", inv, args) as span:
        payload = {"carrier": carrier, **CARRIER_POLICY.get(carrier, CARRIER_POLICY["default"])}
        span.set_attribute("gen_ai.tool.call.result", json.dumps(payload)[:2000])
    return ToolResult(text_result_for_llm=json.dumps(payload))


def ops_tools() -> list[Tool]:
    return [
        define_tool(
            "ops_node_capacity",
            description=(
                "Look up free crane hours, berth slots and congestion for a port node. "
                "Required before recommending crane reallocation."
            ),
            params_type=NodeCapacityParams,
            handler=_node_capacity,
            skip_permission=True,
        ),
        define_tool(
            "ops_carrier_policy",
            description="Look up the carrier's policy limits for schedule recovery actions.",
            params_type=CarrierPolicyParams,
            handler=_carrier_policy,
            skip_permission=True,
        ),
    ]
