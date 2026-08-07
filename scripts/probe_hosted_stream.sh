#!/usr/bin/env bash
# THE decisive test: does the Foundry proxy stream incrementally, or buffer
# the whole response?
#
# The container emits a tick every 1s for 5s, each stamped with its own
# server-side elapsed time. Comparing that to when the CLIENT sees each line
# tells us which:
#
#   streaming -> client_elapsed tracks server_elapsed  (0s, 1s, 2s, ...)
#   buffering -> every line lands at once at ~5s
#
# If it buffers, SSE degrades to request/response for the tool-call channel
# and the design needs polling instead.
set -uo pipefail
BASE="https://jtantra-test-foundry.services.ai.azure.com/api/projects/jtantra-test-foundry"
AG="ghcp-sdk-browser"
TOKEN=$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv 2>/dev/null)

echo "=== create a platform session ==="
SID=$(curl -s --max-time 90 -X POST "$BASE/agents/$AG/endpoint/sessions?api-version=v1" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('agent_session_id') or d.get('id',''))")
echo "session: $SID"

echo
echo "=== mode=probe -- timing each frame as it ARRIVES ==="
START=$(date +%s.%N)
curl -sN --max-time 120 -X POST \
  "$BASE/agents/$AG/endpoint/protocols/invocations?api-version=v1&agent_session_id=$SID" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"mode":"probe"}' \
| while IFS= read -r line; do
    [ -z "$line" ] && continue
    NOW=$(date +%s.%N)
    printf 'client_elapsed=%6.3fs  %s\n' "$(echo "$NOW - $START" | bc)" "$line"
  done

echo
echo "=== verdict ==="
echo "  ticks arriving ~1s apart  -> STREAMING (design works as built)"
echo "  all ticks at once at ~5s  -> BUFFERED  (need polling instead)"
