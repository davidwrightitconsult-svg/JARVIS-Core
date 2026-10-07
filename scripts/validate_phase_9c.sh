#!/usr/bin/env bash
# FRIDAY Phase 9C validation — the validation gate (what FRIDAY may PAPER-trade),
# the pricing-inconsistency scan, and the Phase 9 close.
# Offline suites; your REAL data read-only (nothing written, no network needed);
# a real-server smoke. No Claude. Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 9C VALIDATION — THE VALIDATION GATE ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- gate + lab + scorecard tests ---"
T="friday/tests/test_lab_gate.py friday/tests/test_lab_robustness.py friday/tests/test_lab_backtest.py friday/tests/test_history_crawl.py friday/tests/test_quant_scoring.py friday/tests/test_edge_tracking.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- your REAL data (read-only: nothing written) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import os, sys
from friday.lab import BacktestSettings, build_gate, render_gate, render_inconsistencies, run_robustness, scan_inconsistencies
from friday.lab.gate import APPROVED, FORWARD, MIN_DAYS, MIN_SCORED, NOT_APPROVED
from friday.market.store import MarketStore
from friday.quant import UniverseRules
from friday.quant.fair import EdgeSettings
path = sys.argv[1]
if not os.path.exists(path):
    print("no real market store yet — SKIPPED"); sys.exit(0)
store = MarketStore(path)
rules, edge = UniverseRules.from_environment(), EdgeSettings.from_environment()
scope = frozenset(rules.excluded_segments)
settings = BacktestSettings(min_price=rules.min_price, max_price=rules.max_price, edge=edge)
rob = run_robustness(store, settings, scope)
print(f"[CALIBRATION EDGE] {rob['conclusion']}")
for h, v in rob["by_horizon"].items():
    print(f"   {h:<5} {v['markets']:>5} markets, {v['in_band']:>5} in band, {v['bets']:>4} bets → {v['verdict'].split(' (')[0]}")
print()
print(render_inconsistencies(scan_inconsistencies(store, fee=edge.fee, excluded=scope)))
print()
g = build_gate(store, settings, scope, robustness=rob)
print(render_gate(g))
by = {s["strategy"]: s for s in g["strategies"]}
def backed(s):
    e = s["evidence"]
    if "scored" in e:
        return e["scored"] >= MIN_SCORED and e["days"] >= MIN_DAYS and e["ci95_net"] and e["ci95_net"][0] > 0
    if s["strategy"] == "CALIBRATION_EDGE":
        return rob["robust"]
    return s["reason"].startswith("PRESENT")
checks = {
    "every strategy has exactly one valid status": all(s["status"] in (APPROVED, FORWARD, NOT_APPROVED) for s in g["strategies"])
        and len(by) == len(g["strategies"]) == 7,
    "the gate approves live trading for nothing": g["live_approved"] == [] and g["execution_locked"] is True,
    "every approval is backed by its evidence": all(backed(s) for s in g["strategies"] if s["status"] == APPROVED),
    "nothing with passing evidence was left unapproved": not any(
        backed(s) for s in g["strategies"] if s["status"] == NOT_APPROVED),
    "the calibration edge follows the robustness report": (by["CALIBRATION_EDGE"]["status"] == APPROVED) == rob["robust"],
    "research forecasts are a forward test, not an approval": by["RESEARCH_FORECAST"]["status"] == FORWARD
        and "RESEARCH_FORECAST" in g["forward_test"],
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("GATE MECHANICS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo "--- REAL SERVER smoke (127.0.0.1:8765, temp DB + temp market store) ---"
SMOKE_DIR="$(mktemp -d)"
DB_PATH="$SMOKE_DIR/friday.db" FRIDAY_MARKET_DB="$SMOKE_DIR/market/market_data.db" \
HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 HERMES_HEALTH_INTERVAL_SECONDS=0 FRIDAY_CALIBRATION_BACKFILL=false \
FRIDAY_KNOWLEDGE_ENABLED=false FRIDAY_LIBRARY_PATH="$SMOKE_DIR/no-library" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - <<'PY'
import json, urllib.request
BASE = "http://127.0.0.1:8765"
def get(p):
    with urllib.request.urlopen(BASE + p, timeout=120) as r:
        return json.load(r)
checks = {}
try:
    g = get("/api/lab/gate")
    checks["gate endpoint: empty store -> nothing approved, research forecasts forward-test, live never"] = (
        bool(g["gate_version"]) and g["approved_for_paper"] == [] and "RESEARCH_FORECAST" in g["forward_test"]
        and g["live_approved"] == [] and g["execution_locked"] is True)
    i = get("/api/lab/inconsistencies")
    checks["inconsistency endpoint answers (no recorded books -> NO_DATA)"] = i["verdict"].startswith("NO_DATA")
    h = get("/health")
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
from friday.lab import GATE_VERSION, INCONSISTENCY_VERSION
lab = "\n".join(p.read_text() for p in pathlib.Path("friday/lab").glob("*.py"))
gate = pathlib.Path("friday/lab/gate.py").read_text()
checks = {
    "gate 9C.1, inconsistency scan 9C.1": bool(GATE_VERSION) and INCONSISTENCY_VERSION == "9C.1",
    "the gate hard-codes no live approval": '"live_approved": []' in gate and '"execution_locked": True' in gate,
    "the lab has no network, Claude or order path": not any(
        k in lab for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.", "place_order")),
    "no new capability (still 10)": len(Capability) == 10,
    "no new job type (still 8)": len(JobType) == 8,
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
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
