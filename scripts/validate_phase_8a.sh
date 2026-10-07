#!/usr/bin/env bash
# FRIDAY Phase 8A validation — calibration dataset + analysis.
# Offline suites; a LIVE read-only backfill of ~150 resolved Polymarket
# markets into a TEMPORARY store with the first real calibration table; a
# real-server smoke. No Claude. Nothing can trade. Takes ~3-4 minutes.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 8A VALIDATION — CALIBRATION ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- calibration + quant + market tests ---"
T="friday/tests/test_calibration.py friday/tests/test_quant_scoring.py friday/tests/test_quant_strategies.py friday/tests/test_quant_core.py friday/tests/test_market_context.py friday/tests/test_market_store.py friday/tests/test_market_data.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: read-only backfill of ~150 resolved markets into a TEMPORARY store ---"
TMPDIR_8A="$(mktemp -d)"
python -m friday.market backfill --markets 150 --db "$TMPDIR_8A/market_data.db" > "$TMPDIR_8A/backfill.json"
python - "$TMPDIR_8A/backfill.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"[BACKFILL] pages {r['pages']} · new markets looked at {r['seen']} · added {r['added']} "
      f"({r['points']} price points) · skipped {r['skipped']} · {r['seconds']}s · stopped: {r['stopped']}")
for e in r["errors"][:3]:
    print(f"   error: {e}")
d = r["dataset"]
print(f"[DATASET] {d['markets']} resolved markets, {d['first_resolution']} → {d['last_resolution']}; "
      f"points {d['points_by_horizon']}")
PY
echo
python -m friday.quant calibration --horizon 24 --db "$TMPDIR_8A/market_data.db"
echo
python - "$TMPDIR_8A/market_data.db" <<'PY'
import sys
from datetime import datetime, timedelta
from friday.market.store import MarketStore
from friday.quant.calibration import build_calibration
store = MarketStore(sys.argv[1])
stats = store.calibration_stats()
briers = {}
for h in (1, 24, 168):
    c = build_calibration(store, horizon_hours=h)
    briers[h] = (c["brier"], c["baseline_brier"], c["samples"])
    print(f"[BRIER] {h:>3}h before resolution: {c['samples']:>4} samples · Brier {c['brier']} (no-skill {c['baseline_brier']})")
with store._db() as db:
    late = db.execute("SELECT COUNT(*) n FROM calib_points p JOIN calib_markets m ON m.market_id=p.market_id"
                      " WHERE p.price_time > m.reference_time").fetchone()["n"]
    bad_payoff = db.execute("SELECT COUNT(*) n FROM calib_points WHERE payoff NOT IN (0.0, 1.0)").fetchone()["n"]
    sums = db.execute("SELECT market_id, horizon_hours, SUM(payoff) s FROM calib_points GROUP BY market_id, horizon_hours"
                      " HAVING COUNT(*) >= 2 AND s != 1").fetchall()
b24, base24, n24 = briers[24]
checks = {
    "Gamma closed-market paging + CLOB price history answered": stats["markets"] >= 30,
    "enough resolved markets for a first table (>= 50)": stats["markets"] >= 50,
    "no price is ever later than the resolution": late == 0,
    "payoffs are 0/1 and exactly one outcome won per market": bad_payoff == 0 and not sums,
    "prices are informative 24h out (Brier below no-skill)": b24 is not None and base24 is not None and b24 < base24,
    "prices sharpen toward resolution (Brier 1h <= Brier 168h)": (briers[1][0] is None or briers[168][0] is None)
        or briers[1][0] <= briers[168][0] + 0.01,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("(one small live batch — verdicts sharpen as the service backfills thousands of markets)")
print("LIVE CALIBRATION:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$TMPDIR_8A"

echo
echo "--- REAL SERVER smoke (127.0.0.1:8765, temp DB + temp market store, backfill ON) ---"
SMOKE_DIR="$(mktemp -d)"
DB_PATH="$SMOKE_DIR/friday.db" FRIDAY_MARKET_DB="$SMOKE_DIR/market/market_data.db" \
HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 HERMES_HEALTH_INTERVAL_SECONDS=0 \
FRIDAY_KNOWLEDGE_ENABLED=false FRIDAY_LIBRARY_PATH="$SMOKE_DIR/no-library" FRIDAY_CALIBRATION_BATCH=5 \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - <<'PY'
import json, time, urllib.request
BASE = "http://127.0.0.1:8765"
def call(p, method="GET"):
    req = urllib.request.Request(BASE + p, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)
checks = {}
try:
    call("/api/scan")                                     # live snapshot -> pipeline -> starts a backfill batch
    strat = call("/api/quant/strategies")
    print(f"[PIPELINE] calibration backfill: {(strat['pipeline'].get('last') or {}).get('calibration_backfill')}")
    cal = {}
    for _ in range(90):
        cal = call("/api/quant/calibration")
        if not cal["backfill"]["running"] and cal["backfill"]["last"]:
            break
        time.sleep(1)
    last = cal["backfill"]["last"] or {}
    print(f"[BACKGROUND] batch of 5: added {last.get('added')} skipped {last.get('skipped')} "
          f"errors {len(last.get('errors') or [])}; dataset {cal['dataset']['markets']} markets")
    again = call("/api/market/calibration/backfill", "POST")
    checks["pipeline started a background backfill"] = (strat["pipeline"].get("last") or {}).get(
        "calibration_backfill") == "started"
    checks["background batch ran to completion"] = bool(last) and (
        (last.get("added") or 0) + sum((last.get("skipped") or {}).values()) >= 1)
    checks["manual backfill endpoint"] = "started" in again
    checks["calibration endpoint"] = "bins" in cal and len(cal["bins"]) == 14
    h = call("/health")
    checks["HEALTHY, execution locked"] = h["status"] == "HEALTHY" and h["execution_locked"] is True
except Exception as exc:
    checks[f"server reachable ({type(exc).__name__}: {exc})"] = False
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("SERVER SMOKE:", "PASS" if checks and all(checks.values()) else "FAIL")
PY
kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null
if grep -qiE "traceback" "$SMOKE_DIR/server.log"; then
  echo "server log (errors):"; grep -iE -A3 "traceback" "$SMOKE_DIR/server.log" | head -20
fi
rm -rf "$SMOKE_DIR"

echo
echo "--- full regression (live tests and soak skipped) ---"
python -m pytest -q -p no:warnings friday/tests --ignore=friday/tests/test_claude_live_tasks.py \
  --ignore=friday/tests/test_hermes_live.py --ignore=friday/tests/test_hermes_soak.py \
  --ignore=friday/tests/test_app_live.py --ignore=friday/tests/test_research_live.py \
  --deselect friday/tests/test_claude_api.py::test_claude_live_non_trading_request 2>&1 | tail -1

echo
echo "--- integration checks ---"
python - <<'PY'
import pathlib, subprocess
from friday.hermes.authority import Capability
from friday.hermes.jobs import JobType
from friday.intelligence.schemas import IntelligenceAction
from friday.market.backfill import BACKFILL_VERSION
from friday.market.store import MARKET_STORE_VERSION
from friday.quant.calibration import CALIBRATION_VERSION, MIN_BIN_SAMPLE
env = pathlib.Path(".env.example").read_text()
src = "\n".join(p.read_text() for p in pathlib.Path("friday/quant").glob("*.py"))
checks = {
    "backfill, calibration, store 8A.1": {BACKFILL_VERSION, CALIBRATION_VERSION, MARKET_STORE_VERSION} == {"8A.1"},
    "calibration verdicts need 30+ samples per bin": MIN_BIN_SAMPLE == 30,
    "no new capability (still 10)": len(Capability) == 10,
    "no new job type (still 8)": len(JobType) == 8,
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
    "quant core has no network or Claude path": not any(k in src for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.")),
    ".env.example documents FRIDAY_CALIBRATION_*": all(k in env for k in (
        "FRIDAY_CALIBRATION_BACKFILL", "FRIDAY_CALIBRATION_MIN_VOLUME", "FRIDAY_CALIBRATION_BATCH",
        "FRIDAY_CALIBRATION_INTERVAL_HOURS")),
    "no market data files tracked in Git": not any(p.endswith((".db", ".csv")) for p in
        subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()),
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

echo
echo "=== END — paste everything above back to Claude ==="
