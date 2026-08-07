#!/usr/bin/env python3
"""Full split-channel loop against the DEPLOYED endpoint.

test_sse_loop.py proved this locally. This proves it survives the Foundry
proxy -- the hop that broke every previous transport. Impersonates the
extension exactly: attach over SSE, receive tool_call, answer on a separate
POST.

Uses /invocations only (the sole relayed path), so no /debug/tool shortcut --
the tool call is driven by a real agent turn via mode=prompt.
"""
import asyncio
import json
import subprocess
import sys
import time

import httpx

BASE = (
    "https://jtantra-test-foundry.services.ai.azure.com/api/projects"
    "/jtantra-test-foundry/agents/ghcp-sdk-browser/endpoint"
)


def token() -> str:
    return subprocess.run(
        ["az", "account", "get-access-token", "--resource",
         "https://ai.azure.com", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


async def main() -> int:
    tok = token()
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    ok = {"attached": False, "turn_started": False, "tool_call": False,
          "tool_result_accepted": False, "turn_finished": False}

    async with httpx.AsyncClient(timeout=180.0, headers=hdr) as client:
        r = await client.post(f"{BASE}/sessions?api-version=v1", json={})
        sid = r.json().get("agent_session_id") or r.json().get("id")
        print(f"session: {sid}")
        url = f"{BASE}/protocols/invocations?api-version=v1&agent_session_id={sid}"

        attached = asyncio.Event()
        done = asyncio.Event()
        t0 = time.monotonic()

        async def reader() -> None:
            """Holds the SSE stream open, answering tool calls as they arrive."""
            async with client.stream("POST", url, json={"mode": "attach"}) as resp:
                print(f"[attach] HTTP {resp.status_code} {resp.headers.get('content-type')}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    msg = json.loads(line[6:])
                    el = time.monotonic() - t0
                    print(f"  [{el:6.2f}s] {str(msg)[:150]}")

                    kind = msg.get("type")
                    if kind == "connected":
                        ok["attached"] = True
                        attached.set()
                    elif kind == "turn_started":
                        ok["turn_started"] = True
                    elif kind == "tool_call":
                        ok["tool_call"] = True
                        # Answer on a SEPARATE request -- the crux of the design.
                        rr = await client.post(url, json={
                            "mode": "tool_result",
                            "call_id": msg["call_id"],
                            "output": json.dumps({
                                "text": "Vessel MV-ATLAS delayed 6h at Singapore",
                            }),
                            "is_error": False,
                        })
                        ok["tool_result_accepted"] = rr.status_code == 200
                        print(f"           -> tool_result HTTP {rr.status_code}")
                    elif kind in ("turn_finished", "error"):
                        ok["turn_finished"] = kind == "turn_finished"
                        done.set()
                        return

        task = asyncio.create_task(reader())
        await asyncio.wait_for(attached.wait(), 60)

        r = await client.post(url, json={
            "mode": "prompt",
            "text": "Use browser_read_text on selector '#status' and tell me what it says.",
        })
        print(f"[prompt] HTTP {r.status_code} {r.text[:120]}")

        try:
            await asyncio.wait_for(done.wait(), 150)
        except asyncio.TimeoutError:
            print("!! timed out waiting for the turn to finish")
        task.cancel()

    print()
    for k, v in ok.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
