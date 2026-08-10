#!/usr/bin/env python3
"""Verify the PER-STEP event stream against the DEPLOYED endpoint.

test_hosted_loop.py proves the transport and one tool call. This proves the
thing the operator actually asked for: that intermediate steps arrive as
DISTINCT frames (so the panel can render one bubble each) and that a subagent
invocation is visible.

Drives a prompt that forces delegation, then asserts on the frame types seen.
"""
import asyncio
import json
import subprocess
import sys
import time
from collections import Counter

import httpx

BASE = (
    "https://jtantra-test-foundry.services.ai.azure.com/api/projects"
    "/jtantra-test-foundry/agents/ghcp-sdk-browser/endpoint"
)

# Stand-in board rows, so the run needs no real browser. Mirrors the shape
# browser_read_table returns from the demo app.
ROWS = [
    {"id": "SH-4417", "vessel": "MV-ATLAS", "status": "Delayed", "delay_h": 6},
    {"id": "SH-4419", "vessel": "MV-ORION", "status": "At Risk", "delay_h": 2},
    {"id": "SH-4420", "vessel": "MV-VEGA", "status": "Delayed", "delay_h": 9},
]


def token() -> str:
    return subprocess.run(
        ["az", "account", "get-access-token", "--resource",
         "https://ai.azure.com", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


async def main() -> int:
    hdr = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    seen: Counter[str] = Counter()
    subagents: list[str] = []
    tools: list[str] = []
    navigated_to: list[str] = []

    async with httpx.AsyncClient(timeout=240.0, headers=hdr) as client:
        r = await client.post(f"{BASE}/sessions?api-version=v1", json={})
        sid = r.json().get("agent_session_id") or r.json().get("id")
        print(f"session: {sid}")
        url = f"{BASE}/protocols/invocations?api-version=v1&agent_session_id={sid}"

        attached = asyncio.Event()
        done = asyncio.Event()
        t0 = time.monotonic()

        async def reader() -> None:
            async with client.stream("POST", url, json={"mode": "attach"}) as resp:
                print(f"[attach] HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    msg = json.loads(line[6:])
                    kind = msg.get("type", "?")
                    seen[kind] += 1
                    el = time.monotonic() - t0

                    # Deltas are noisy; count them but do not print each one.
                    if kind != "assistant_delta":
                        print(f"  [{el:6.2f}s] {str(msg)[:160]}")

                    if kind == "connected":
                        attached.set()
                    elif kind == "subagent_started":
                        subagents.append(msg.get("name", "?"))
                    elif kind == "tool_started":
                        tools.append(msg.get("name", "?"))
                    elif kind == "tool_call":
                        name = msg.get("name", "")
                        args = msg.get("arguments", {}) or {}
                        if name == "browser_navigate":
                            navigated_to.append(str(args.get("url", "")))
                        if name == "browser_read_table":
                            out = json.dumps({"rows": ROWS})
                        elif name == "browser_navigate":
                            out = json.dumps({"ok": True, "url": args.get("url")})
                        else:
                            out = json.dumps({"text": "STATUS: 2 delayed, 1 at risk"})
                        await client.post(url, json={
                            "mode": "tool_result",
                            "call_id": msg["call_id"],
                            "output": out,
                            "is_error": False,
                        })
                    elif kind in ("turn_finished", "error"):
                        done.set()
                        return

        task = asyncio.create_task(reader())
        await asyncio.wait_for(attached.wait(), 60)

        # Phrased to force the full pipeline: read the board, then delegate.
        await client.post(url, json={
            "mode": "prompt",
            "text": "Review the shipment board and draft a recovery brief for the top 2 at-risk shipments.",
        })

        try:
            await asyncio.wait_for(done.wait(), 220)
        except asyncio.TimeoutError:
            print("!! timed out")
        task.cancel()

    print("\n--- frame types seen ---")
    for k, v in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"  {v:4d}  {k}")

    checks = {
        "streamed assistant text": seen["assistant_delta"] > 0,
        "per-step frames (not one blob)": (
            seen["tool_started"] + seen["subagent_started"] + seen["reasoning"] > 0
        ),
        "subagent invocation visible": seen["subagent_started"] > 0,
        "tool steps visible": seen["tool_started"] > 0,
        "turn finished": seen["turn_finished"] > 0,
    }
    print("\n--- checks ---")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    if subagents:
        print(f"\n  subagents invoked: {', '.join(subagents)}")
    if tools:
        print(f"  tools called:      {', '.join(tools)}")
    if navigated_to:
        print(f"  navigated to:      {', '.join(navigated_to)}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
