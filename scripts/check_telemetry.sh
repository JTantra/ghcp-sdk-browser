#!/usr/bin/env bash
# Ask the DEPLOYED container where its spans are going.
#
# This is the check that was missing: a console fallback is silent, so
# "no traces in Foundry" and "tracing is broken" look identical from outside.
set -uo pipefail

BASE="https://jtantra-test-foundry.services.ai.azure.com/api/projects/jtantra-test-foundry"
AG="ghcp-sdk-browser"
TOKEN="$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)"

SID="$(curl -s --max-time 90 -X POST "${BASE}/agents/${AG}/endpoint/sessions?api-version=v1" \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' -d '{}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('agent_session_id') or d.get('id',''))")"

# /info is a GET on the container, reached through the invocations proxy path.
curl -s --max-time 90 \
  "${BASE}/agents/${AG}/endpoint/protocols/invocations?api-version=v1&agent_session_id=${SID}" \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d '{"mode":"info"}' | python3 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print('non-JSON response:', raw[:400]); raise SystemExit(1)
t = d.get('telemetry')
print('telemetry:', json.dumps(t, indent=2) if t else '(field absent - old build)')
if t:
    ok = t.get('exporter') == 'application-insights'
    print()
    print(('PASS' if ok else 'FAIL'), '- spans go to', t.get('exporter'))
"
