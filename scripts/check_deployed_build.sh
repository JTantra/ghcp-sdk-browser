#!/usr/bin/env bash
# Is the DEPLOYED container running the new SSE code, or the old build?
#
#   mode=probe unknown -> old image (falls through to the "no message" 400)
#   text/event-stream  -> new image is live
set -uo pipefail
BASE="https://jtantra-test-foundry.services.ai.azure.com/api/projects/jtantra-test-foundry"
AG="ghcp-sdk-browser"
TOKEN=$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv 2>/dev/null)

SID=$(curl -s --max-time 90 -X POST "$BASE/agents/$AG/endpoint/sessions?api-version=v1" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('agent_session_id') or d.get('id',''))")
echo "session: $SID"
URL="$BASE/agents/$AG/endpoint/protocols/invocations?api-version=v1&agent_session_id=$SID"

echo
echo "=== mode=probe (only exists in the NEW code) ==="
curl -s --max-time 90 -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode":"probe"}' -w '\n--- HTTP %{http_code} type=%{content_type} ---\n' | head -20

echo
echo "=== mode=attach (new code -> SSE; old code -> 400 no message) ==="
curl -s --max-time 30 -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode":"attach"}' -w '\n--- HTTP %{http_code} type=%{content_type} ---\n' | head -10

echo
echo "=== control: old-style body, should work on BOTH builds ==="
curl -s --max-time 90 -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"ping"}' -w '\n--- HTTP %{http_code} ---\n' | head -10
