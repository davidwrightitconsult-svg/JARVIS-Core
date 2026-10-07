#!/usr/bin/env bash
# FRIDAY Phase 10A validation — the Risk Governor.
# Offline suites; a drill against the validation gate built from your REAL data
# (read-only) with a TEMPORARY risk store; a real-server smoke. No network needed.
# No Claude. Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 10A VALIDATION — RISK GOVERNOR ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- risk governor + gate tests ---"
T="friday/tests/test_risk_governor.py friday/tests/test_lab_gate.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- drill: the governor against the gate from your REAL data (temporary risk store) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
DRILL_DIR="$(mktemp -d)"
python - "${REAL_DB/#\~/$HOME}" "$DRILL_DIR/risk.db" <<'PY'
import os, sys
from friday.risk import Portfolio, Position, Proposal, RiskGovernor, RiskSettings, RiskState
path, risk_db = sys.argv[1], sys.argv[2]
if os.path.exists(path):
    from friday.lab import BacktestSettings, build_gate
    from friday.market.store import MarketStore
    from friday.quant import UniverseRules
    from friday.quant.fair import EdgeSettings
    rules = UniverseRules.from_environment()
    gate = build_gate(MarketStore(path), BacktestSettings(min_price=rules.min_price, max_price=rules.max_price,
                      edge=EdgeSettings.from_environment()), frozenset(rules.excluded_segments))
    print(f"[GATE] approved for paper: {gate['approved_for_paper'] or 'none'} · forward test: {gate['forward_test']} · "
          f"live approved: {gate['live_approved'] or 'none'}")
else:
    gate = {"approved_for_paper": [], "forward_test": ["RESEARCH_FORECAST"], "live_approved": []}
    print("[GATE] no real market store yet - using the empty-store gate")
settings = RiskSettings.from_environment()
state = RiskState(risk_db)
g = RiskGovernor(settings, state, lambda: gate)
print(f"[SETTINGS] paper bankroll ${settings.bankroll:g} · position {settings.max_position_fraction:.0%} · market "
      f"{settings.max_market_fraction:.0%} · strategy {settings.max_strategy_fraction:.0%} · total "
      f"{settings.max_total_exposure:.0%} · max {settings.max_open_positions} positions · daily loss "
      f"{settings.daily_loss_limit:.0%} · drawdown halt {settings.max_drawdown:.0%}")
B = settings.bankroll
fresh = Portfolio.fresh(B)
allowed = (gate["approved_for_paper"] + gate["forward_test"])[0]
refused = next(s for s in ("CALIBRATION_EDGE", "BOOK_IMBALANCE", "MOMENTUM") if s not in gate["approved_for_paper"])
def show(label, d):
    print(f"   {label:<58} -> {d['verdict']:<8} ${d['approved_stake']:<7g} {d['reason']}")
    return d
r = {}
r["ok"] = show(f"{allowed}: ${B * 0.01:g} on a fresh portfolio", g.evaluate(Proposal(allowed, "m1", B * 0.01), fresh))
r["big"] = show(f"{allowed}: ${B * 0.2:g} (ten times the position cap)", g.evaluate(Proposal(allowed, "m1", B * 0.2), fresh))
r["gate"] = show(f"{refused}: ${B * 0.01:g} (not approved by the gate)", g.evaluate(Proposal(refused, "m1", B * 0.01), fresh))
r["live"] = show(f"{allowed}: ${B * 0.01:g} in LIVE mode", g.evaluate(Proposal(allowed, "m1", B * 0.01, mode="LIVE"), fresh))
full = Portfolio(B, B, B, tuple(Position(f"m{i}", f"S{i % 4}", B * 0.03) for i in range(2, 12)))
r["full"] = show(f"{allowed}: with the book already at 30% exposure", g.evaluate(Proposal(allowed, "m1", B * 0.01), full))
r["day"] = show(f"{allowed}: after losing 3% today", g.evaluate(Proposal(allowed, "m1", B * 0.01), Portfolio(B * 0.97, B, B)))
r["dd"] = show(f"{allowed}: equity 10% below its peak", g.evaluate(Proposal(allowed, "m1", B * 0.01), Portfolio(B * 0.9, B, B * 0.9)))
r["dd2"] = show(f"{allowed}: equity recovered, halt not resumed", g.evaluate(Proposal(allowed, "m1", B * 0.01), fresh))
state.resume("RESUME")
state.engage_kill_switch("validation drill", "validate_phase_10a")
r["kill"] = show(f"{allowed}: kill switch engaged", g.evaluate(Proposal(allowed, "m1", B * 0.01), fresh))
try:
    state.resume("please"); wrong_word = False
except PermissionError:
    wrong_word = True
state.resume("RESUME")
r["after"] = show(f"{allowed}: after RESUME", g.evaluate(Proposal(allowed, "m1", B * 0.01), fresh))
r["nan"] = show(f"{allowed}: stake NaN", g.evaluate(Proposal(allowed, "m1", float("nan")), fresh))
log = state.decisions(100)
checks = {
    "an allowed strategy within limits is approved in full": r["ok"]["verdict"] == "APPROVED" and r["ok"]["approved_stake"] == round(B * 0.01, 2),
    "an oversized stake is cut to the position cap, never raised": r["big"]["verdict"] == "REDUCED"
        and r["big"]["approved_stake"] == round(B * settings.max_position_fraction, 2),
    "a strategy the gate has not approved is refused": r["gate"]["reason"] == "NOT_ALLOWED_BY_GATE",
    "LIVE is refused": r["live"]["reason"] == "LIVE_TRADING_LOCKED",
    "a full book leaves no room": r["full"]["verdict"] == "REJECTED",
    "the daily loss limit stops new positions": r["day"]["reason"] == "DAILY_LOSS_LIMIT",
    "the drawdown halt engages and stays until RESUME": r["dd"]["reason"] == r["dd2"]["reason"] == "DRAWDOWN_HALT",
    "the kill switch stops everything; only the word RESUME releases it": r["kill"]["reason"] == "KILL_SWITCH"
        and wrong_word and r["after"]["verdict"] == "APPROVED",
    "garbage is refused": r["nan"]["reason"] == "INVALID_PROPOSAL",
    "no decision ever approved more than was asked": all(
        (d["requested_stake"] is None and d["approved_stake"] == 0) or d["approved_stake"] <= d["requested_stake"] for d in log),
    "every decision was recorded with its checks": len(log) == len(r) and all(d["checks"] for d in log),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("RISK DRILL:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$DRILL_DIR"

echo
echo "--- REAL SERVER smoke (127.0.0.1:8765, temp DB + temp market store + temp risk store) ---"
SMOKE_DIR="$(mktemp -d)"
DB_PATH="$SMOKE_DIR/friday.db" FRIDAY_MARKET_DB="$SMOKE_DIR/market/market_data.db" FRIDAY_RISK_DB="$SMOKE_DIR/risk/risk.db" \
HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 HERMES_HEALTH_INTERVAL_SECONDS=0 FRIDAY_CALIBRATION_BACKFILL=false \
FRIDAY_KNOWLEDGE_ENABLED=false FRIDAY_LIBRARY_PATH="$SMOKE_DIR/no-library" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - <<'PY'
import json, urllib.error, urllib.request
BASE = "http://127.0.0.1:8765"
def call(p, body=None):
    req = urllib.request.Request(BASE + p, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}
checks = {}
try:
    _, s = call("/api/risk/status")
    checks["status: governor ready, live LOCKED, gate read"] = (bool(s["risk_version"]) and s["live_trading"] == "LOCKED"
        and s["gate"]["available"] and s["gate"]["live_approved"] == [])
    ask = {"strategy": "RESEARCH_FORECAST", "market_id": "m1", "stake": 500}
    _, d = call("/api/risk/evaluate", ask)
    checks["dry run: reduced to the cap, nothing recorded"] = d["verdict"] == "REDUCED" and "decision_id" not in d
    checks["dry run: LIVE refused"] = call("/api/risk/evaluate", {**ask, "mode": "LIVE"})[1]["reason"] == "LIVE_TRADING_LOCKED"
    call("/api/risk/kill", {"reason": "smoke"})
    checks["kill switch over the API"] = call("/api/risk/evaluate", ask)[1]["reason"] == "KILL_SWITCH"
    checks["resume needs the word RESUME"] = call("/api/risk/resume", {"confirm": "yes"})[0] == 403 \
        and call("/api/risk/resume", {"confirm": "RESUME"})[1].get("resumed") is True
    _, h = call("/health")
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
from friday.risk import RISK_VERSION, RiskSettings
risk = "\n".join(p.read_text() for p in pathlib.Path("friday/risk").glob("*.py"))
env = pathlib.Path(".env.example").read_text()
d = RiskSettings()
checks = {
    "risk governor version present": bool(RISK_VERSION),
    "conservative defaults (2% position, 4% market, 15% strategy, 30% total, 3% day, 10% drawdown)": (
        d.max_position_fraction, d.max_market_fraction, d.max_strategy_fraction, d.max_total_exposure,
        d.daily_loss_limit, d.max_drawdown) == (0.02, 0.04, 0.15, 0.30, 0.03, 0.10),
    "the governor has no network, Claude or order path": not any(
        k in risk for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.", "place_order", "urllib")),
    ".env.example documents FRIDAY_RISK_*": all(k in env for k in (
        "FRIDAY_RISK_BANKROLL", "FRIDAY_RISK_MAX_POSITION", "FRIDAY_RISK_MAX_EXPOSURE", "FRIDAY_RISK_DAILY_LOSS",
        "FRIDAY_RISK_MAX_DRAWDOWN", "FRIDAY_RISK_DB")),
    "no new capability (still 10)": len(Capability) == 10,
    "no new job type (still 8)": len(JobType) == 8,
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
    "no data files tracked in Git": not any(p.endswith((".db", ".csv")) for p in
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
