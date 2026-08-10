#!/usr/bin/env bash
# Does the Foundry project have Application Insights attached?
#
# This is the question that decides whether our spans can EVER reach the
# portal. The container's exporter falls back to console silently, so a
# missing connection looks identical to broken instrumentation.
set -uo pipefail

SUB="98517cf5-e3e8-4d6e-9e54-a93d702df3d9"
RG="jtantra-test-rg"
ACC="jtantra-test-foundry"
PROJ="jtantra-test-foundry"
BASE="https://management.azure.com/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.CognitiveServices/accounts/${ACC}"

TOKEN="$(az account get-access-token --resource https://management.azure.com --query accessToken -o tsv)"

for V in "2025-06-01" "2025-04-01-preview"; do
  echo "=== project connections @ ${V} ==="
  code=$(curl -s -o /tmp/proj.json -w '%{http_code}' \
    -H "Authorization: Bearer ${TOKEN}" \
    "${BASE}/projects/${PROJ}/connections?api-version=${V}")
  echo "HTTP ${code}"
  if [ "${code}" = "200" ]; then
    python3 - <<'PY'
import json
d=json.load(open('/tmp/proj.json'))
vals=d.get('value',[])
print(f"  {len(vals)} connection(s)")
for v in vals:
    p=v.get('properties',{})
    print("   -", v.get('name'), "|", p.get('category'), "|", str(p.get('target'))[:70])
ai=[v for v in vals if 'insight' in str(v.get('properties',{}).get('category','')).lower()]
print("  APP INSIGHTS ATTACHED:", bool(ai))
PY
    break
  else
    head -c 250 /tmp/proj.json; echo
  fi
done
