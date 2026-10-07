#!/usr/bin/env bash
# FRIDAY Phase 3D validation — Hermes integrated into the FRIDAY app.
# Offline suites + 1 live Claude call (app -> Hermes -> contract)
# + a short real-server smoke test on 127.0.0.1:8765 with a temp DB.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 3D VALIDATION — HERMES INTEGRATION ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- Hermes + app integration tests (offline) ---"
T="friday/tests/test_hermes_contract.py friday/tests/test_hermes_runtime.py friday/tests/test_hermes_scheduler.py friday/tests/test_app_hermes.py friday/tests/test_claude_truncation.py friday/tests/test_claude_adapter.py"
python -m pytest -q $T 2>&1 | tail -1
python -O -m pytest -q $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: free-text command -> Hermes -> Claude contract (1 call) ---"
python -m pytest -q -s -rs friday/tests/test_app_live.py 2>&1 | grep -E "^\[APP\]|passed|failed|skipped|Error|assert" | tail -6

echo
echo "--- REAL SERVER smoke test (127.0.0.1:8765, temp DB, no market scan, no Claude) ---"
SMOKE_DIR="$(mktemp -d)"
DB_PATH="$SMOKE_DIR/friday_smoke.db" HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 FRIDAY_KNOWLEDGE_ENABLED=false \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break
  sleep 0.5
done
python - <<'PY'
import json, time, urllib.request
BASE = "http://127.0.0.1:8765"
def get(p):
    return json.load(urllib.request.urlopen(BASE + p, timeout=10))
def post(p, body=None):
    req = urllib.request.Request(BASE + p, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=10))
    except urllib.error.HTTPError as e:
        return {"http_status": e.code}
checks = {}
try:
    h = get("/health")
    checks["server up, HEALTHY, hermes RUNNING"] = h["status"] == "HEALTHY" and h["hermes"] == "RUNNING"
    checks["execution locked, live execution off"] = h["execution_locked"] is True and h["live_execution"] is False
    time.sleep(2)
    jobs = get("/api/hermes/jobs")
    checks["scheduled health check ran"] = any(j["job_type"] == "HEALTH_CHECK" and j["status"] == "SUCCEEDED" for j in jobs)
    checks["forbidden job refused over HTTP"] = post("/api/hermes/jobs", {"job_type": "PLACE_ORDER"}).get("http_status") == 400
    checks["deterministic command, no Claude"] = post("/api/command", {"command": "status"})["hermes_job_id"] is None
    k = post("/api/hermes/kill", {"reason": "3D smoke drill"})
    h2 = get("/health")
    checks["kill switch -> KILLED, DEGRADED, still locked"] = (
        k["state"] == "KILLED" and h2["status"] == "DEGRADED" and h2["execution_locked"] is True)
    checks["reset requires a name"] = post("/api/hermes/reset", {}).get("http_status") == 409
    checks["reset + start restores RUNNING"] = (
        post("/api/hermes/reset", {"confirmed_by": "validation"})["state"] == "STOPPED"
        and post("/api/hermes/start")["state"] == "RUNNING")
except Exception as exc:
    checks[f"server reachable ({type(exc).__name__}: {exc})"] = False
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("SERVER SMOKE:", "PASS" if checks and all(checks.values()) else "FAIL")
PY
kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null
if grep -qiE "traceback|error" "$SMOKE_DIR/server.log"; then
  echo "server log (errors):"; grep -iE -A3 "traceback|error" "$SMOKE_DIR/server.log" | head -20
fi
rm -rf "$SMOKE_DIR"

echo
echo "--- full regression (live tests and soak skipped) ---"
python -m pytest -q friday/tests --ignore=friday/tests/test_claude_live_tasks.py \
  --ignore=friday/tests/test_hermes_live.py --ignore=friday/tests/test_hermes_soak.py \
  --ignore=friday/tests/test_app_live.py \
  --deselect friday/tests/test_claude_api.py::test_claude_live_non_trading_request 2>&1 | tail -1

echo
echo "--- integration checks ---"
python - <<'PY'
import pathlib
from friday.intelligence.claude import MAX_OUTPUT_TOKENS
from friday.hermes.budget import DEFAULT_RESERVE_PER_CALL
src = pathlib.Path("friday/app/main.py").read_text()
checks = {
    "FridayBrain bypass removed": not pathlib.Path("friday/app/friday.py").exists() and "FridayBrain(" not in src,
    "free text routed via Hermes INTELLIGENCE_ANALYSIS": '"job_type": "INTELLIGENCE_ANALYSIS"' in src,
    "Claude output headroom 3000": MAX_OUTPUT_TOKENS == 3000,
    "budget reserve covers a full call": DEFAULT_RESERVE_PER_CALL >= 4700 + MAX_OUTPUT_TOKENS,
    ".env.example documents Hermes settings": "HERMES_DAILY_TOKEN_BUDGET" in pathlib.Path(".env.example").read_text(),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("ALL CHECKS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"
git ls-files | grep -q "^\.venv/" && echo "FAIL: .venv tracked" || echo "PASS: .venv is not tracked"

echo
echo "=== END — paste everything above back to Claude ==="
