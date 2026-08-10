#!/usr/bin/env python3
"""Are the platform's span and OUR spans in the SAME trace?

Foundry's Traces view is rooted on the platform's `invoke_agent` request
(cloud_RoleName=agentsv2). Child spans render only if they share that
request's operation_Id. If ours carry a different operation_Id we have started
a second, parallel trace: present in App Insights, invisible in the portal.
"""
import collections
import json
import subprocess
import sys
import urllib.parse
import urllib.request

AI_APP_ID = "b2640966-c7f6-4223-9a36-2b41ffd24a71"

QUERY = """
union dependencies, requests
| where timestamp > ago(3h)
| where tostring(customDimensions) has "gen_ai"
| project timestamp, itemType, name, cloud_RoleName, operation_Id, operation_ParentId, id
| order by timestamp desc
| take 40
"""


def token() -> str:
    return subprocess.run(
        ["az", "account", "get-access-token", "--resource",
         "https://api.applicationinsights.io", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def main() -> int:
    url = (f"https://api.applicationinsights.io/v1/apps/{AI_APP_ID}/query"
           f"?query={urllib.parse.quote(QUERY)}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        d = json.load(resp)

    if "error" in d:
        print("ERROR:", json.dumps(d["error"])[:400])
        return 1

    table = d["tables"][0]
    cols = [c["name"] for c in table["columns"]]
    rows = [dict(zip(cols, r)) for r in table["rows"]]
    if not rows:
        print("No gen_ai spans in the last 3h.")
        return 1

    print("time".ljust(24), "role".ljust(17), "operation_Id".ljust(34), "name")
    for r in rows:
        print(
            str(r["timestamp"])[:23].ljust(24),
            str(r["cloud_RoleName"])[:17].ljust(17),
            str(r["operation_Id"])[:32].ljust(34),
            r["name"],
        )

    by_role = collections.defaultdict(set)
    for r in rows:
        by_role[r["cloud_RoleName"]].add(r["operation_Id"])

    print()
    for role, ops in by_role.items():
        print(f"  {role}: {len(ops)} distinct operation_Id(s)")

    platform = by_role.get("agentsv2", set())
    ours = by_role.get("ghcp-sdk-browser", set())
    shared = platform & ours
    print()
    print("shared operation_Id:", len(shared))
    print("VERDICT:", "SAME trace - renders in portal" if shared
          else "SEPARATE traces - our spans will NOT appear under the platform trace")
    return 0 if shared else 2


if __name__ == "__main__":
    sys.exit(main())
