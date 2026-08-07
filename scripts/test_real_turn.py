#!/usr/bin/env python3
"""Does a REAL model turn dispatch a browser tool call?

test_sse_loop.py used /debug/tool, which bypasses the model entirely. This
drives an actual turn via mode=prompt and asserts the model calls a browser
tool -- catching session-binding and tool-registration faults that the debug
path cannot see.

Needs GITHUB_TOKEN (spends one model call).
"""
import asyncio
import contextlib
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8088"
SESSION = "realturn01"


async def main() -> int:
    ok = {"attached": False, "turn_started": False, "tool_call": False,
          "turn_finished": False}
    t0 = time.monotonic()

    async with httpx.AsyncClient(timeout=180.0) as client:
        attached = asyncio.Event()
        done = asyncio.Event()
        url = f"{BASE}/invocations?agent_session_id={SESSION}"

        async def reader() -> None:
            async with client.stream("POST", url, json={"mode": "attach"}) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    msg = json.loads(line[6:])
                    kind = msg.get("type")
                    if kind != "assistant_delta":
                        print(f"  [{time.monotonic() - t0:6.2f}s] {str(msg)[:130]}")

                    if kind == "connected":
                        ok["attached"] = True
                        attached.set()
                    elif kind == "turn_started":
                        ok["turn_started"] = True
                    elif kind == "tool_call":
                        ok["tool_call"] = True
                        await client.post(url, json={
                            "mode": "tool_result",
                            "call_id": msg["call_id"],
                            "output": json.dumps({"text": "STATUS: ALL BERTHS CLEAR"}),
                            "is_error": False,
                        })
                    elif kind in ("turn_finished", "error"):
                        ok["turn_finished"] = kind == "turn_finished"
                        done.set()
                        return

        task = asyncio.create_task(reader())
        await asyncio.wait_for(attached.wait(), 30)

        r = await client.post(url, json={
            "mode": "prompt",
            "text": "Read the text at CSS selector '#status' using your browser "
                    "tool and tell me exactly what it says.",
        })
        print(f"[prompt] HTTP {r.status_code}")

        try:
            await asyncio.wait_for(done.wait(), 150)
        except asyncio.TimeoutError:
            print("!! turn did not finish in time")
        task.cancel()
        with contextlib.suppress(Exception):
            await task

    print()
    for k, v in ok.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
