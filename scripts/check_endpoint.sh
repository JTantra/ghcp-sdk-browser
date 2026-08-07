#!/usr/bin/env bash
# Measures whether the Foundry endpoint is reliably reachable after the DNS
# change, separating name resolution from TCP/TLS connect.
H=jtantra-test-foundry.services.ai.azure.com

echo "=== 10x HTTPS connect ==="
ok=0
for i in $(seq 1 10); do
  code=$(timeout 20 curl -s -o /dev/null -w '%{http_code}' "https://$H/" 2>/dev/null)
  [ "$code" != "000" ] && ok=$((ok + 1))
  printf '  try%-3s http=%s\n' "$i" "$code"
done
echo "  --> $ok/10 connected"

echo
echo "=== 10x DNS resolution ==="
cd ~/dev/live/ghcp-sdk-browser/server || exit 1
.venv/bin/python - <<'PY'
import socket
ok = 0
for _ in range(10):
    try:
        socket.gethostbyname("jtantra-test-foundry.services.ai.azure.com")
        ok += 1
    except Exception as e:
        print("  FAIL", e)
print(f"  --> {ok}/10 resolved")
PY
