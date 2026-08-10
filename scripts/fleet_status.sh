#!/usr/bin/env bash
# Write container fleet status to logs/fleet.json for the dashboard /fleet
# endpoint. Runs HOST-SIDE — the dashboard container has no docker access, so a
# host systemd timer / cron runs this. Read-only: it only reads `docker compose
# ps`, never controls anything. Install (once, on the VM):
#
#   ( crontab -l 2>/dev/null; echo "* * * * * cd ~/vnedge && bash scripts/fleet_status.sh" ) | crontab -
#
set -euo pipefail
cd "$(dirname "$0")/.."
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker compose ps --format json 2>/dev/null | TS="$ts" python3 -c '
import sys, json, os
svcs = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    rows = d if isinstance(d, list) else [d]
    for r in rows:
        name = r.get("Service") or r.get("Name") or "?"
        status = str(r.get("Status") or r.get("State") or "")
        up = str(r.get("State", "")).lower() == "running" or "up" in status.lower()
        svcs.append({"name": name, "status": status, "up": up})
out = {"services": sorted(svcs, key=lambda s: s["name"]), "written_at": os.environ.get("TS")}
print(json.dumps(out))
' > logs/fleet.json.tmp && mv logs/fleet.json.tmp logs/fleet.json
