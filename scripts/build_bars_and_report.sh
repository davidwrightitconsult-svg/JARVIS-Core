#!/usr/bin/env bash
# FRIDAY 11B: build the hourly price history the book strategies will be tested on.
# Reads Polymarket's public price history only (no account, no orders). No strategy is run.
#
#   bash ~/friday/FRIDAY-Autonomous-Trading-Platform/scripts/build_bars_and_report.sh          up to 240 minutes
#   bash ~/friday/FRIDAY-Autonomous-Trading-Platform/scripts/build_bars_and_report.sh 30       up to 30 minutes
#
# Safe to stop (Ctrl-C) and rerun: a market is fetched once and the build carries on where it stopped.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
MINUTES="${1:-240}"
echo "=== FRIDAY 11B — BUILD HOURLY PRICE HISTORY (up to $MINUTES minutes) ==="
python -m friday.lab bars --bar-days "${FRIDAY_BAR_DAYS:-30}" --max-markets 100000 --minutes "$MINUTES"
echo
echo "=== END — paste everything above back to Claude ==="
