#!/usr/bin/env bash
# FRIDAY Phase 9A validation — walk-forward (out-of-sample) backtest of the
# calibration edge, by market type. Offline suites; the backtest on your REAL
# calibration dataset (read-only, nothing written); a real-server smoke.
# No network needed for the backtest. No Claude. Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 9A VALIDATION — WALK-FORWARD BACKTEST ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- lab + edge tracking + fair value + calibration tests ---"
T="friday/tests/test_lab_backtest.py friday/tests/test_edge_tracking.py friday/tests/test_fair_value.py friday/tests/test_calibration.py friday/tests/test_quant_scoring.py friday/tests/test_quant_core.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- the backtest on your REAL calibration dataset (read-only) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import json, os, sys
from datetime import datetime, timedelta
from friday.lab.backtest import BacktestSettings, _walk, load_markets, render_text, run_backtest
from friday.market.store import MarketStore
from friday.quant import UniverseRules
from friday.quant.fair import EdgeSettings
from friday.quant.segments import excluded_segments
path = sys.argv[1]
if not os.path.exists(path):
    print("no real market store yet — SKIPPED"); sys.exit(0)
store = MarketStore(path)
rules, edge = UniverseRules.from_environment(), EdgeSettings.from_environment()
def settings(h):
    return BacktestSettings(horizon_hours=h, min_price=rules.min_price, max_price=rules.max_price, edge=edge)
r = run_backtest(store, settings(24))
print(render_text(r))
print()
for h in (72, 168):
    x = run_backtest(store, settings(h))
    o = x["out_of_sample"]
    print(f"[{h:>3}h] {x['dataset']['markets']} markets, {x['dataset']['in_price_band']} in band · {o['bets']} bets on "
          f"{o['days']} days · win {o['win_rate']} · net {o['avg_net']} · clustered CI {o['ci95_net_clustered']} → {o['verdict']}")
wide = run_backtest(store, BacktestSettings(horizon_hours=24, min_price=0.0, max_price=1.0, edge=edge))
w = wide["out_of_sample"]
print(f"[ANY PRICE, 24h — information only: FRIDAY does not look outside {rules.min_price}-{rules.max_price}] "
      f"{w['bets']} bets on {w['days']} days · win {w['win_rate']} · net {w['avg_net']} · clustered CI "
      f"{w['ci95_net_clustered']} → {w['verdict']}")
for k, v in wide["by_bought_price"].items():
    print(f"      bought {k:<10} {v['bets']:>5} bets · win {v['win_rate']} · net {v['avg_net']} · CI {v['ci95_net_clustered']}")
print()
print("[RECENT BETS] the last out-of-sample bets the rule would have made (24h):")
for b in r["recent_bets"][-8:]:
    print(f"   {b['ref'][:10]} {'WON ' if b['won'] else 'LOST'} bought {(b['outcome'] or b['side'])[:4]:<4} at {b['bought_at']} "
          f"(fair {b['fair']}, edge {b['edge'] * 100:+.1f}c) net {b['net'] * 100:+.0f}c [{b['category']}] {b['question'][:52]}")
if not r["recent_bets"]:
    print("   (none)")
s = settings(24)
markets, _ = load_markets(store, 24)
bets, folds = _walk(markets, s)
ids = {m["market_id"]: m for m in markets}
def no_lookahead(f):
    start = datetime.fromisoformat(f["test_from"])
    return (datetime.fromisoformat(f["train_until"]) == start - timedelta(hours=24)
            and f["train_markets"] == sum(1 for m in markets if m["ref"] < start - timedelta(hours=24)))
o = r["out_of_sample"]
checks = {
    "no look-ahead: every fold trains only on markets resolved before its first price": all(no_lookahead(f) for f in folds),
    "every bet is inside the live price band": all(
        rules.min_price <= ids[b["market_id"]]["tokens"][0][0] <= rules.max_price for b in bets),
    "no out-of-scope (sports) market in the backtest": all(m["segment"] not in excluded_segments() for m in markets),
    "every bet's training band had >= 30 samples, edge >= min and z >= min": all(
        b["bin_samples"] >= 30 and b["edge"] >= edge.min_edge and b["z"] >= edge.min_z for b in bets),
    "net is payoff - assumed ask - fee for every bet": all(
        abs(b["net"] - ((1.0 if b["won"] else 0.0) - b["cost"])) < 1e-3 for b in bets),
    "market-type split adds up to the total": sum(v["bets"] for v in r["by_category"].values()) == o["bets"] == len(bets),
    "deterministic (same data, same result)": json.dumps(run_backtest(store, s), sort_keys=True, default=str)
        == json.dumps(r, sort_keys=True, default=str),
    "verdict needs >= 30 bets on >= 5 days and the day-clustered interval": o["verdict"].startswith("INSUFFICIENT")
        == (o["bets"] < 30 or o["days"] < 5 or o["ci95_net_clustered"] is None),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print(f"RESULT (24h, out of sample): {o['verdict']}  — a finding about the edge, not a pass/fail of the build")
print("BACKTEST MECHANICS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
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
    with urllib.request.urlopen(BASE + p, timeout=60) as r:
        return json.load(r)
checks = {}
try:
    r = get("/api/lab/backtest")
    checks["backtest endpoint answers (empty store -> INSUFFICIENT_DATA, never a made-up result)"] = (
        r["backtest_version"] == "9A.1" and r["out_of_sample"]["bets"] == 0
        and r["out_of_sample"]["verdict"].startswith("INSUFFICIENT_DATA"))
    checks["cost sensitivity and market-type split present"] = len(r["cost_sensitivity"]) == 5 and "by_category" in r
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
from friday.lab import BACKTEST_VERSION
lab = "\n".join(p.read_text() for p in pathlib.Path("friday/lab").glob("*.py"))
checks = {
    "backtest 9A.1": BACKTEST_VERSION == "9A.1",
    "the lab has no network, Claude or order path": not any(
        k in lab for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.", "place_order")),
    "the backtest uses the live model's arithmetic (shrunk_fair, bin_table, kelly)": all(
        k in lab for k in ("shrunk_fair", "bin_table", "kelly")),
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
