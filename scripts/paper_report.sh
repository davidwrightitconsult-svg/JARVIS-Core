#!/usr/bin/env bash
# FRIDAY: the paper trading record and the forward test's standing. Read-only.
#   bash ~/friday/FRIDAY-Autonomous-Trading-Platform/scripts/paper_report.sh
set -uo pipefail
cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
echo "=== FRIDAY PAPER REPORT — $(date -u +%Y-%m-%dT%H:%MZ) ==="
python -m friday.paper report
echo
echo "--- what the running service last did ---"
curl -s --max-time 10 "${FRIDAY_URL:-http://127.0.0.1:8000}/api/paper" | python -c '
import json, sys
try:
    d = json.load(sys.stdin)
    f = (d.get("forward_tests") or [{}])[0]
    print("last look at the markets:", f.get("last_run"))
    print("last engine check       :", {k: v for k, v in (d.get("last_tick") or {}).items() if k != "closed"})
    print("orders waiting now      :", len(d.get("resting") or []))
except Exception as exc:
    print("the service did not answer:", type(exc).__name__)'
echo
echo "--- last closed trades ---"
python -m friday.paper trades --limit 10
echo "=== END — paste everything above back to Claude ==="
