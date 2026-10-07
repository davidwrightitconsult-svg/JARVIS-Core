#!/usr/bin/env bash
# Install / update the FRIDAY systemd service (Phase 5A).
#   sudo bash scripts/install_friday_service.sh            install or update, then (re)start
#   sudo bash scripts/install_friday_service.sh --remove   stop, disable and remove
# Safe to rerun. Stops a manually started (nohup) FRIDAY first so the port is free.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash scripts/install_friday_service.sh"; exit 1
fi
RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
  echo "Run via sudo from your normal account (FRIDAY must not run as root)."; exit 1
fi
UNIT=/etc/systemd/system/friday.service

if [ "${1:-}" = "--remove" ]; then
  systemctl disable --now friday.service 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload
  echo "FRIDAY service removed."; exit 0
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
RUN_GROUP="$(id -gn "$RUN_USER")"
[ -x "$REPO/.venv/bin/python" ] || { echo "No virtualenv at $REPO/.venv"; exit 1; }
[ -f "$REPO/.env" ] || echo "WARNING: $REPO/.env not found — FRIDAY will start without its settings."

sed -e "s#__USER__#$RUN_USER#g" -e "s#__GROUP__#$RUN_GROUP#g" \
    -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$RUN_HOME#g" \
    "$REPO/deploy/systemd/friday.service.template" > "$UNIT.new"
chmod 644 "$UNIT.new"; mv "$UNIT.new" "$UNIT"
systemd-analyze verify "$UNIT" 2>&1 | grep -v "^$" || true

# Stop a hand-started FRIDAY (nohup) owned by this user, if any.
if pgrep -u "$RUN_USER" -f "uvicorn friday.app.main:app" >/dev/null && ! systemctl is-active --quiet friday.service; then
  echo "Stopping manually started FRIDAY..."
  pkill -INT -u "$RUN_USER" -f "uvicorn friday.app.main:app" || true
  for _ in $(seq 1 20); do pgrep -u "$RUN_USER" -f "uvicorn friday.app.main:app" >/dev/null || break; sleep 1; done
fi

systemctl daemon-reload
systemctl enable friday.service >/dev/null
systemctl restart friday.service
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 1
done
echo "--- friday.service ---"
systemctl is-enabled friday.service | sed 's/^/enabled: /'
systemctl is-active friday.service | sed 's/^/active:  /'
curl -s http://127.0.0.1:8000/health | python3 -c "
import json,sys
h=json.load(sys.stdin)
print('health: ', h['status'], '| hermes', h['hermes'], '| execution_locked', h['execution_locked'], '| live_execution', h['live_execution'])
" 2>/dev/null || echo "health:  NOT RESPONDING — see: journalctl -u friday -n 50"
