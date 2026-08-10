#!/usr/bin/env bash
# Are OUR spans actually in Application Insights?
#
# Distinguishes three very different situations that all look like
# "no traces in Foundry":
#   1. spans never left the container      -> nothing here at all
#   2. spans arrived but Foundry filters   -> rows here, absent in portal
#   3. spans arrived and render            -> rows here and in portal
set -uo pipefail

AI_APP_ID="b2640966-c7f6-4223-9a36-2b41ffd24a71"   # ApplicationId from the conn string
TOKEN="$(az account get-access-token --resource https://api.applicationinsights.io --query accessToken -o tsv)"

run() {
  local label="$1" q="$2"
  echo "=== ${label} ==="
  curl -s -G "https://api.applicationinsights.io/v1/apps/${AI_APP_ID}/query" \
    --data-urlencode "query=${q}" \
    -H "Authorization: Bearer ${TOKEN}" \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
if "error" in d:
    print("  ERROR:", json.dumps(d["error"])[:300]); raise SystemExit
for t in d.get("tables", []):
    cols = [c["name"] for c in t["columns"]]
    rows = t["rows"]
    if not rows:
        print("  (no rows)")
    for r in rows[:25]:
        print("  " + " | ".join(f"{c}={v}" for c, v in zip(cols, r)))
'
  echo
}

run "any telemetry in the last 3h, by table" \
"union withsource=T dependencies, requests, traces, customEvents
 | where timestamp > ago(3h)
 | summarize n=count(), last=max(timestamp) by T
 | order by n desc"

run "dependencies by name (our spans land here)" \
"dependencies
 | where timestamp > ago(3h)
 | summarize n=count(), last=max(timestamp) by name, cloud_RoleName
 | order by n desc | take 25"

run "requests by name" \
"requests
 | where timestamp > ago(3h)
 | summarize n=count(), last=max(timestamp) by name, cloud_RoleName
 | order by n desc | take 25"

run "anything carrying gen_ai attributes" \
"union dependencies, requests
 | where timestamp > ago(3h)
 | where tostring(customDimensions) has 'gen_ai'
 | project timestamp, itemType, name, cloud_RoleName,
           op=tostring(customDimensions['gen_ai.operation.name'])
 | order by timestamp desc | take 25"
