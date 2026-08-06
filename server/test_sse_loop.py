#!/usr/bin/env python3
"""End-to-end local proof of the SSE transport, with no model call.

Exercises the exact shape the extension will use:
  1. POST mode=attach   -> hold an SSE stream open (the tool_call channel)
  2. POST /debug/tool   -> dispatch a tool while the stream is open
  3. observe tool_call arriving ON THE STREAM
  4. POST mode=tool_result on a SEPARATE request -> resolves the pending call

Step 4 is the one that matters: it proves the split-channel design closes the
loop. If it hangs, the request/response split is broken.
"""
import asyncio
import contextlib
import json
import sys

import httpx

BASE = "http://127.0.0.1:8088"
SESSION = "ssetest0001"


async def main() -> int:
    ok = {"connected": False, "tool_call": False, "resolved": False}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. attach: hold the stream open in the background
        stream_ready = asyncio.Event()
        got_call = asyncio.Event()
        call_id: dict[str, str] = {}

        async def reader() -> None:
            async with client.stream(
                "POST", f"{BASE}/invocations?agent_session_id={SESSION}",
                json={"mode": "attach"},
            ) as resp:
                print(f"[attach] HTTP {resp.status_code} type={resp.headers.get('content-type')}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    msg = json.loads(line[6:])
                    print(f"[stream] {msg}")
                    if msg.get("type") == "connected":
                        ok["connected"] = True
                        stream_ready.set()
                    if msg.get("type") == "tool_call":
                        ok["tool_call"] = True
                        call_id["id"] = msg["call_id"]
                        got_call.set()
                        # Deliberately keep reading. Returning here would close
                        # the stream and trigger detach, cancelling the very
                        # call we are about to answer -- exactly the false
                        # failure this test hit on its first run.

        task = asyncio.create_task(reader())
        await asyncio.wait_for(stream_ready.wait(), 10)

        # 2. dispatch a tool while the stream is open (fire-and-forget: it
        #    blocks server-side until we answer in step 4)
        dispatch = asyncio.create_task(
            client.post(
                f"{BASE}/debug/tool?session={SESSION}",
                json={"name": "browser_read_text", "arguments": {"selector": "h1"}},
            )
        )

        # 3. the tool_call must arrive on the stream
        await asyncio.wait_for(got_call.wait(), 10)

        # 4. answer it on a SEPARATE request -- the crux of the design
        r = await client.post(
            f"{BASE}/invocations?agent_session_id={SESSION}",
            json={
                "mode": "tool_result",
                "call_id": call_id["id"],
                "output": json.dumps({"text": "hello from the fake browser"}),
                "is_error": False,
            },
        )
        print(f"[tool_result] HTTP {r.status_code} {r.text}")

        resp = await asyncio.wait_for(dispatch, 10)
        print(f"[debug/tool returned] HTTP {resp.status_code} {resp.text}")
        ok["resolved"] = "hello from the fake browser" in resp.text

        task.cancel()  # reader loops forever by design; stop it before exit
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    print()
    for k, v in ok.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
