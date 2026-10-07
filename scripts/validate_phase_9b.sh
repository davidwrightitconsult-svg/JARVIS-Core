#!/usr/bin/env bash
# FRIDAY Phase 9B validation — calibration data re-anchored on the SCHEDULED end
# date, fair-value tables built on it, and the robustness report.
# Offline suites; a LIVE read-only crawl + re-anchor into a TEMPORARY store; your
# REAL data read-only (nothing written); a real-server smoke. No Claude.
# Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 9B.2 VALIDATION — END-ANCHORED CALIBRATION, DATASET REBUILD, ROBUSTNESS ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- lab + fair value + edge tracking + calibration tests ---"
T="friday/tests/test_lab_robustness.py friday/tests/test_lab_backtest.py friday/tests/test_fair_value.py friday/tests/test_edge_tracking.py friday/tests/test_calibration.py friday/tests/test_market_context.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: markets that reached their end in the last 3 days, end-anchored, into a TEMPORARY store ---"
TMP_9B="$(mktemp -d)"
python -m friday.market rebuild --days 3 --markets 60 --min-volume 10000 --db "$TMP_9B/market_data.db" > "$TMP_9B/rebuild.json"
python - "$TMP_9B/rebuild.json" "$TMP_9B/market_data.db" <<'PY'
import json, sys
from collections import Counter
from datetime import datetime, timedelta
from friday.market.segments import category_of
from friday.market.store import MarketStore
r = json.load(open(sys.argv[1]))
store = MarketStore(sys.argv[2])
print(f"[REBUILD] windows {r['windows']} · pages {r['pages']} · looked at {r['seen']} · added {r['added']} "
      f"({r['end_points']} end-anchored points) · skipped {r['skipped']} · truncated windows {r['truncated_windows']} · "
      f"{r['seconds']}s · stopped: {r['stopped']}")
for e in r["errors"][:3]:
    print(f"   error: {e}")
print(f"[END-ANCHORED] {r['end_anchor']['markets']} · markets by horizon {r['end_anchor']['markets_by_horizon']}")
with store._db() as db:
    rows = [dict(x) for x in db.execute("SELECT question, end_date, closed_time, reference_time FROM calib_markets")]
    late = db.execute("SELECT COUNT(*) n FROM calib_points_end p JOIN calib_markets m ON m.market_id=p.market_id"
                      " WHERE p.price_time > m.reference_time").fetchone()["n"]
    past_target = 0
    for row in db.execute("SELECT p.price_time, p.horizon_hours, m.end_date FROM calib_points_end p"
                          " JOIN calib_markets m ON m.market_id=p.market_id"):
        end = datetime.fromisoformat(row["end_date"].replace("Z", "+00:00"))
        if datetime.fromisoformat(row["price_time"]) > end - timedelta(hours=row["horizon_hours"]):
            past_target += 1
    sums = db.execute("SELECT market_id, horizon_hours, SUM(payoff) s FROM calib_points_end GROUP BY market_id, horizon_hours"
                      " HAVING COUNT(*) >= 2 AND s != 1").fetchall()
print(f"[TYPES] {dict(Counter(category_of(x['question']) for x in rows))}")
for x in rows[:10]:
    print(f"   {x['question'][:80]}  (end {x['end_date'][:16]})")
long = sum(v for k, v in r["end_anchor"]["markets_by_horizon"].items() if k in ("336h", "720h"))
print(f"[LONG HORIZONS] {long} market-horizons at 14 or 30 days (needs markets that traded for weeks; information)")
checks = {
    "Gamma end-date windows answered and markets were added": r["pages"] >= 1 and r["added"] >= 1,
    "no request failed (no refused price range, no refused offset)": not r["errors"],
    "every added market was end-anchored in the same pass (nothing pending)": r["end_anchor"]["pending"] == 0,
    "no sports or five-minute market entered the dataset": not any(
        k in (x["question"] or "").lower() for x in rows for k in (" vs. ", "up or down -")),
    "no end-anchored price is later than the market's resolution": late == 0,
    "no end-anchored price is later than end - horizon": past_target == 0,
    "exactly one outcome won per market and horizon": not sums,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("LIVE REBUILD:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$TMP_9B"

echo
echo "--- your REAL data (read-only: nothing written) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import os, sys
from collections import Counter
from friday.lab import BacktestSettings, render_robustness, run_robustness
from friday.market.store import MarketStore
from friday.quant import QuantEngine, UniverseRules
from friday.quant.fair import MAX_HOURS, MIN_HOURS, EdgeSettings, FairValueModel
path = sys.argv[1]
if not os.path.exists(path):
    print("no real market store yet — SKIPPED"); sys.exit(0)
store = MarketStore(path)
rules, edge = UniverseRules.from_environment(), EdgeSettings.from_environment()
anchor = store.end_anchor_stats()
print(f"[END ANCHOR] {anchor['markets']} · pending {anchor['pending']} · markets by horizon {anchor['markets_by_horizon']}")
if anchor["pending"]:
    print("   (pending markets are re-anchored by the service after the restart)")
print("   The dataset below is still the old one; scripts/rebuild_and_report.sh rebuilds it after the restart.")
r = run_robustness(store, BacktestSettings(min_price=rules.min_price, max_price=rules.max_price, edge=edge),
                   frozenset(rules.excluded_segments))
print(render_robustness(r))
print()
model = FairValueModel(store, edge)
live = QuantEngine(store, rules).features()
edges = model.edges(live)
hours = sorted(m.hours_to_end for m in live if m.eligible and m.hours_to_end is not None)
print(f"[LIVE MODEL] {len(edges)} eligible two-outcome markets: {dict(Counter(e['status'] for e in edges))}")
if hours:
    inside = sum(1 for h in hours if MIN_HOURS <= h <= MAX_HOURS)
    print(f"[LIVE RANGE] {inside} of {len(hours)} eligible markets end within the calibrated range (6h-45 days); "
          f"median time to end {hours[len(hours) // 2] / 24:.0f} days")
checks = {
    "a market outside 6h-45 days gets no estimate (never a borrowed table)": all(
        (e["status"] == "OUT_OF_CALIBRATED_RANGE") == (e["horizon_hours"] is None) for e in edges)
        and all(e["fair"] == e["price"] and e["stake_fraction"] == 0 for e in edges if e["horizon_hours"] is None),
    "EDGE only inside the range, with >= 30 end-anchored samples": all(
        e["horizon_hours"] is not None and e["bin_samples"] >= 30 for e in edges if e["status"] == "EDGE"),
    "the model reads end-anchored points only": model.status()["anchor"] == "scheduled end date",
    "robustness report complete (5 horizons, leak, thresholds, real asks)": len(r["by_horizon"]) == 5
        and len(r["leak"]) == 3 and len(r["thresholds"]) == 5 and len(r["real_asks"]) == 5,
    "'robust' needs >= 2 horizons and >= 2 thresholds passing": r["robust"] == (
        sum(v["verdict"].startswith("PASSES") for v in r["by_horizon"].values()) >= 2
        and sum(v["verdict"].startswith("PASSES") for k, v in r["thresholds"].items() if k != "horizon") >= 2
        and not any(v["verdict"].startswith("NEGATIVE") for v in r["by_horizon"].values())),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("REAL DATA MECHANICS:", "PASS" if all(checks.values()) else "FAIL")
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
    with urllib.request.urlopen(BASE + p, timeout=120) as r:
        return json.load(r)
checks = {}
try:
    r = get("/api/lab/robustness")
    checks["robustness endpoint answers (empty store -> NOT SUPPORTED, never a made-up result)"] = (
        r["robustness_version"] == "9B.1" and r["robust"] is False and r["conclusion"].startswith("NOT SUPPORTED"))
    b = get("/api/lab/backtest")
    checks["backtest defaults to the end anchor"] = b["anchor"] == "end" and b["backtest_version"] == "9B.1"
    e = get("/api/quant/edges")
    checks["edges report the anchor and the calibrated range"] = (
        e["model"]["anchor"] == "scheduled end date" and e["model"]["calibrated_hours"] == [6.0, 1080.0])
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
from friday.lab import BACKTEST_VERSION, ROBUSTNESS_VERSION
from friday.market.backfill import BACKFILL_VERSION, END_HORIZONS
from friday.market.segments import SEGMENTS_VERSION, excluded_segments
from friday.market.context import MARKET_CONTEXT_VERSION
from friday.quant.fair import ANCHOR, FAIR_VERSION, HORIZONS
lab = "\n".join(p.read_text() for p in pathlib.Path("friday/lab").glob("*.py"))
checks = {
    "9B.1 fair value, backtest, robustness, context; 9B.2 backfill and segments": {FAIR_VERSION, BACKTEST_VERSION,
        ROBUSTNESS_VERSION, MARKET_CONTEXT_VERSION} == {"9B.1"} and BACKFILL_VERSION == SEGMENTS_VERSION == "9B.2",
    "sports and five-minute markets are out of scope by default": excluded_segments() == {"sports", "micro"},
    "fair value reads the end anchor at the re-anchored horizons": ANCHOR == "end" and tuple(HORIZONS) == tuple(
        float(h) for h in END_HORIZONS),
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
