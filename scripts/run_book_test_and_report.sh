#!/usr/bin/env bash
# FRIDAY 11C: run the eleven book rules ONCE on the hourly history and print the verdicts.
# Reads FRIDAY's own stored prices only. No network, no Claude, nothing can trade.
#
#   bash ~/friday/FRIDAY-Autonomous-Trading-Platform/scripts/run_book_test_and_report.sh
#
# The test runs detached, so a dropped SSH connection does not stop it. Run the same command
# again at any time: it re-attaches to a running test, or prints the stored result. The result
# is stored once and is never recomputed for the same rule settings.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
LOG="$HOME/friday/book_test.log"
PIDFILE="$HOME/friday/book_test.pid"

echo "=== FRIDAY 11C — BOOK STRATEGY TEST ==="
python -m friday.lab bars-status
echo
if python -m friday.lab book-results >/dev/null 2>&1; then
  python -m friday.lab book-results
  echo
  echo "=== END — paste everything above back to Claude ==="
  exit 0
fi
PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "a test is already running (pid $PID) - following it"
else
  : > "$LOG"
  setsid nohup python -m friday.lab book-test >> "$LOG" 2>&1 < /dev/null &
  PID=$!
  echo "$PID" > "$PIDFILE"
  echo "test started (pid $PID); progress once a minute. Safe to disconnect: rerun this command to come back."
fi
tail --pid="$PID" -n +1 -f "$LOG" | grep --line-buffered -E "^  \.\.\. |ERROR|Traceback|Error"
rm -f "$PIDFILE"
echo
if python -m friday.lab book-results >/dev/null 2>&1; then
  python -m friday.lab book-results
else
  echo "the test did not finish. Last lines of $LOG:"
  tail -20 "$LOG"
fi
echo
echo "=== END — paste everything above back to Claude ==="
