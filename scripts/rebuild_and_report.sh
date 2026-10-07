#!/usr/bin/env bash
# FRIDAY 9B.2: ask the RUNNING service to rebuild its calibration dataset from
# markets that reached their scheduled end (background, read-only Polymarket),
# show progress, then print the robustness report from the real store (read-only).
#
#   bash scripts/rebuild_and_report.sh            wait up to 240 minutes, then report
#   bash scripts/rebuild_and_report.sh 30         wait up to 30 minutes, then report on what is there
#   bash scripts/rebuild_and_report.sh 0          report now (the service keeps working in the background)
#   FRIDAY_HISTORY_DAYS=365 bash scripts/rebuild_and_report.sh     go further back (default 180 days)
#
# Run after:  sudo systemctl restart friday
# Safe to stop (Ctrl-C) and rerun: the service keeps a cursor and never fetches a market twice.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
BASE="${FRIDAY_URL:-http://127.0.0.1:8000}"
MAX_MINUTES="${1:-240}"
DAYS="${FRIDAY_HISTORY_DAYS:-180}"

status() {
  curl -s "$BASE/api/quant/calibration" | python -c '
import json, sys
try:
    b = json.load(sys.stdin)["backfill"]
    a = b["end_anchor"]
    last = b.get("last") or {}
    done = int(last.get("stopped") == "history complete" and float(last.get("days_covered") or 0) >= float(sys.argv[1]) - 1)
    print(int(bool(b["running"])), a["pending"], a["markets"].get("done", 0), (b.get("history_cursor") or "-")[:10], done,
          b.get("min_volume"), ",".join(b.get("exclude_segments") or []))
except Exception:
    print("x x x x x x x")' "$DAYS"
}

echo "=== FRIDAY 9B.2 — REBUILD THE CALIBRATION DATASET (last $DAYS days) ==="
read -r RUNNING PENDING DONE CURSOR COMPLETE MINVOL SCOPE <<<"$(status)"
if [ "$RUNNING" = "x" ]; then
  echo "The FRIDAY service did not answer at $BASE (is it restarted? is FRIDAY_CALIBRATION_BACKFILL on?) - stopping."
  exit 1
fi
echo "settings: minimum volume \$$MINVOL · out of scope: $SCOPE"
echo "start: $DONE markets with end-anchored prices · $PENDING pending · history reached $CURSOR"
START=$(date +%s)
while [ "$COMPLETE" != "1" ] || [ "$PENDING" != "0" ]; do
  if [ $(( ($(date +%s) - START) / 60 )) -ge "$MAX_MINUTES" ]; then
    echo "not finished after $MAX_MINUTES minutes - reporting on what is there (the service continues every few hours; rerun to push it on)"
    break
  fi
  if [ "$RUNNING" = "0" ]; then
    curl -s -X POST "$BASE/api/market/calibration/backfill?markets=20000&days=$DAYS" >/dev/null
  fi
  sleep "${FRIDAY_POLL_SECONDS:-60}"
  read -r RUNNING PENDING DONE CURSOR COMPLETE MINVOL SCOPE <<<"$(status)"
  [ "$RUNNING" = "x" ] && { echo "service stopped answering - stopping"; exit 1; }
  echo "$(date +%H:%M:%S)  $DONE markets with end-anchored prices · $PENDING pending · history reached $CURSOR"
done

echo
python -m friday.lab robustness
for H in 24 168; do
  echo
  python -m friday.lab backtest --horizon "$H"
done
echo
curl -s "$BASE/api/quant/edges?limit=200" | python -c '
import json, sys
from collections import Counter
d = json.load(sys.stdin)
print("[LIVE MODEL]", dict(Counter(e["status"] for e in d["edges"])), "· pending", d["model"]["reanchor_pending"])
for e in d["edges"]:
    if e["status"] == "EDGE":
        print("   EDGE {} [{:g}h n={}] buy {} edge {:+.2f}c z {}".format(
            e["question"][:60], e["horizon_hours"], e["bin_samples"], e["best_side"], e["edge"] * 100, e["z"]))'
echo
echo "=== END — paste everything above back to Claude ==="
