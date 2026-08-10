#!/usr/bin/env bash
# Put the project's App Insights connection string into the azd env.
#
# Uses the ARM REST API rather than `az monitor app-insights`, which needs the
# application-insights extension and returned empty under --query on this box.
set -euo pipefail

SUB="98517cf5-e3e8-4d6e-9e54-a93d702df3d9"
RG="jtantra-test-rg"
AI="jtantratestmlw3852624202"

TOKEN="$(az account get-access-token --resource https://management.azure.com --query accessToken -o tsv)"
URL="https://management.azure.com/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.Insights/components/${AI}?api-version=2020-02-02"

CONN="$(curl -s -H "Authorization: Bearer ${TOKEN}" "${URL}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["properties"]["ConnectionString"])')"

if [ -z "${CONN}" ] || [ "${CONN}" = "None" ]; then
  echo "FAILED: empty connection string; refusing to set it"
  exit 1
fi

cd "$HOME/dev/live/ghcp-sdk-browser"
azd env set APPLICATIONINSIGHTS_CONNECTION_STRING "${CONN}" >/dev/null 2>&1

# Read it back: setting it is not proof it stuck.
GOT="$(azd env get-values 2>/dev/null | grep '^APPLICATIONINSIGHTS_CONNECTION_STRING=' || true)"
echo "length: ${#CONN} chars"
echo "ikey:   $(echo "${CONN}" | grep -o 'InstrumentationKey=[0-9a-f-]\{8\}')…"
echo "in azd: $(echo "${GOT}" | cut -c1-60)…"
[ ${#CONN} -gt 50 ] && echo "OK" || echo "SUSPICIOUS: too short"
