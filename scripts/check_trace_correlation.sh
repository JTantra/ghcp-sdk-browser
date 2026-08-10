#!/usr/bin/env bash
# Are the platform's span and OUR spans in the SAME trace?
#
# Foundry's Traces view is rooted on the platform's `invoke_agent` request
# (cloud_RoleName=agentsv2). Child spans only render if they share that
# request's operation_Id. If our spans carry a DIFFERENT operation_Id we have
# started a second, parallel trace -- present in App Insights, invisible in
# the portal. That is exactly the symptom being debugged.
set -uo pipefail

AI_APP_ID="b2640966-c7f6-4223-9a36-2b41ffd24a71"
TOKEN="$(az account get-access-token --resource https://api.applicationinsights.io --query accessToken -o tsv)"

Q='union dependencies, requests
| where timestamp > ago(3h)
| where tostring(customDimensions) has "gen_ai"
| project timestamp, itemType, name, cloud_RoleName, operation_Id, operation_ParentId, id
| order by timestamp desc
| take 30'

curl -s -G "https://api.applicationinsights.io/v1/apps/${AI_APP_ID}/query" \
  --data-urlencode "query=${Q}" \
  -H "Authorization: Bearer ${TOKEN}" \
| python3 -c '
import sys, json, collections
d = json.load(sys.stdin)
if "error" in d:
    print("ERROR:", json.dumps(d["error"])[:400]); raise SystemExit(1)
t = d["tables"][0]
cols = [c["name"] for c in t["columns"]]
rows = [dict(zip(cols, r)) for r in t["rows"]]

print(f"{'time':25} {'role':18} {'op_Id':34} {'parent':18} name")
for r in rows:
    print(f"{str(r['timestamp'])[:23]:25} {str(r['cloud_RoleName'])[:18]:18} "
          f"{str(r['operation_Id'])[:32]:34} {str(r['operation_ParentId'])[:16]:18} {r['name']}")

print()
by_role = collections.defaultdict(set)
for r in rows:
    by_role[r["cloud_RoleName"]].add(r["operation_Id"])
for role, ops in by_role.items():
    print(f"{role}: {len(ops)} distinct operation_Id(s)")

platform = by_role.get("agentsv2", set())
ours = by_role.get("ghcp-sdk-browser", set())
shared = platform & ours
print()
print("shared operation_Id between platform and our spans:", len(shared))
print("VERDICT:", "SAME trace - should render" if shared
      else "SEPARATE traces - our spans will NOT appear under the platform trace")
'
