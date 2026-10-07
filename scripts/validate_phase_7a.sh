#!/usr/bin/env bash
# FRIDAY Phase 7A validation — Quant Core: features + tradable universe.
# Offline suites; the Quant Core run READ-ONLY on your REAL recorded history;
# one live read-only snapshot (no-book exclusion); a real-server smoke proving
# the UI scan is served from recorded data. No Claude call. Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 7A VALIDATION — QUANT CORE: FEATURES + UNIVERSE ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- quant + market tests ---"
T="friday/tests/test_quant_core.py friday/tests/test_market_context.py friday/tests/test_market_store.py friday/tests/test_market_data.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- Quant Core on your REAL recorded history (read-only) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import os, sys, time
from friday.market.store import MarketStore
from friday.quant import QuantEngine, UniverseRules
path = sys.argv[1]
if not os.path.exists(path):
    print("no real market store yet — SKIPPED"); sys.exit(0)
store = MarketStore(path)
engine = QuantEngine(store, UniverseRules.from_environment())
t0 = time.monotonic()
markets = engine.features()
elapsed = time.monotonic() - t0
u = engine.universe()
s = store.stats(days=1)
print(f"[HISTORY] runs in last 24h={s['runs']} first_run={s['first_run']} last_run={s['last_run']}")
print(f"[UNIVERSE] latest run {u['run']['run_id'] if u['run'] else None}: {u['markets']} markets, "
      f"{u['eligible']} eligible; rejected_by={u['rejected_by']}  ({elapsed:.2f}s)")
eligible = [m for m in markets if m.eligible]
for m in eligible[:5]:
    o = m.outcomes[0]
    print(f"[ELIGIBLE] {m.question[:52]:<52} att {m.attention:>3} | {o.outcome[:10]} bid {o.best_bid} ask {o.best_ask} "
          f"depth ${o.depth_usd:,.0f} | 1h {o.change_1h} 6h {o.change_6h} 24h {o.change_24h} vol {o.volatility} "
          f"imb {o.imbalance} | ask_sum {m.ask_sum}")
binary = [m for m in markets if m.ask_sum is not None and len(m.outcomes) == 2 and all(o.usable for o in m.outcomes)]
under = [m for m in binary if m.ask_sum < 1 - 1e-9]
over = [m for m in binary if m.bid_sum is not None and m.bid_sum > 1 + 1e-9]
print(f"[PRICING] usable binary markets {len(binary)}: ask_sum<1 in {len(under)}, bid_sum>1 in {len(over)} "
      f"(reported as facts only)")
covered = [m for m in eligible if m.outcomes[0].change_1h is not None]
rules = engine.rules
checks = {
    "features computed for the latest run": len(markets) >= 10,
    "fast enough for the UI (< 3s)": elapsed < 3,
    "some markets eligible": len(eligible) >= 1,
    "every eligible market passes every rule": all(
        all(o.usable and o.spread is not None and o.spread <= rules.max_spread + 1e-9
            and o.depth_usd >= rules.min_depth_usd for o in m.outcomes)
        and (m.volume_24h or 0) >= rules.min_volume_24h
        and rules.min_price <= m.outcomes[0].mid <= rules.max_price for m in eligible),
    "every ineligible market has a reason": all(m.reasons for m in markets if not m.eligible),
    "1h change available once history covers 1h": (s["runs"] < 5) or len(covered) >= max(1, len(eligible) // 2),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("REAL HISTORY:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- LIVE: one snapshot into a TEMPORARY store (read-only; no-book markets excluded) ---"
TMPDB="$(mktemp -d)/market_data.db"
python -m friday.market snapshot --limit 50 --db "$TMPDB" > "$TMPDB.run.json"
python - "$TMPDB.run.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"[RUN] candidates={r['candidates']} kept={r['markets']} excluded={r['excluded']} reasons={r['excluded_reasons']} "
      f"books {r['books_ok']}/{r['tokens']} errors={len(r['errors'])}")
for e in r["excluded_examples"]:
    print(f"      excluded → {e}")
ok = r["markets"] >= 45 and not r["errors"]
print(f"{'PASS' if ok else 'FAIL'}: scan filled (>= 45 of 50) with no fetch errors")
PY
python -m friday.quant universe --db "$TMPDB" | python -c "import json,sys; u=json.load(sys.stdin); print(f\"[TEMP UNIVERSE] {u['markets']} markets, {u['eligible']} eligible; {u['rejected_by']}\")"
rm -rf "$(dirname "$TMPDB")"

echo
echo "--- REAL SERVER smoke (127.0.0.1:8765, temp DB + temp market store) ---"
SMOKE_DIR="$(mktemp -d)"
DB_PATH="$SMOKE_DIR/friday.db" FRIDAY_MARKET_DB="$SMOKE_DIR/market/market_data.db" \
HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 HERMES_HEALTH_INTERVAL_SECONDS=0 \
FRIDAY_KNOWLEDGE_ENABLED=false FRIDAY_LIBRARY_PATH="$SMOKE_DIR/no-library" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - <<'PY'
import json, time, urllib.parse, urllib.request
BASE = "http://127.0.0.1:8765"
def get(p):
    with urllib.request.urlopen(BASE + p, timeout=60) as r:
        return json.load(r)
checks = {}
try:
    t0 = time.monotonic(); first = get("/api/scan"); t_first = time.monotonic() - t0
    runs_after_first = get("/api/market/stats")["counts"]["runs"]
    t0 = time.monotonic(); second = get("/api/scan"); t_second = time.monotonic() - t0
    runs_after_second = get("/api/market/stats")["counts"]["runs"]
    watch = [r for r in first if r["decision"] == "WATCH"]
    print(f"[SCAN] first {t_first:.1f}s (live snapshot) → {len(first)} rows, {len(watch)} WATCH; "
          f"second {t_second:.2f}s (recorded); runs {runs_after_first} → {runs_after_second}")
    for r in watch[:3]:
        print(f"       WATCH {r['title'][:60]} price {r['price']} spread {r['spread']} score {r['score']}")
    checks["first scan took one live snapshot"] = runs_after_first == 1
    checks["refresh served from recorded data (no new run)"] = runs_after_second == runs_after_first
    checks["edge is None everywhere (nothing measured yet)"] = all(r["edge"] is None for r in first)
    checks["IGNORE rows carry reasons"] = all(r["reasons"] for r in first if r["decision"] == "IGNORE")
    u = get("/api/quant/universe")
    checks["universe endpoint"] = u["markets"] >= 10 and u["status"]["enabled"] is True
    f = get("/api/quant/features?eligible_only=true&limit=5")
    checks["features endpoint"] = isinstance(f["markets"], list)
    if watch:
        ctx = get(f"/api/market/context?market_id={watch[0]['market_id']}")
        checks["Claude's market context carries quant features"] = "quant" in ctx["markets"][0]
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
from friday.market.context import MARKET_CONTEXT_VERSION
from friday.quant import QUANT_VERSION
env = pathlib.Path(".env.example").read_text()
src = "\n".join(p.read_text() for p in pathlib.Path("friday/quant").glob("*.py"))
checks = {
    "quant 7A.1, market context 7A.1": QUANT_VERSION == MARKET_CONTEXT_VERSION == "7A.1",
    "no new capability (still 10)": len(Capability) == 10,
    "no new job type (still 8)": len(JobType) == 8,
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
    "quant core has no network or Claude path": not any(k in src for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.")),
    ".env.example documents FRIDAY_QUANT_*": all(k in env for k in (
        "FRIDAY_QUANT_MIN_DEPTH_USD", "FRIDAY_QUANT_MAX_SPREAD", "FRIDAY_QUANT_MIN_VOLUME_24H",
        "FRIDAY_QUANT_MIN_PRICE", "FRIDAY_QUANT_MAX_PRICE", "FRIDAY_QUANT_MAX_AGE_SECONDS")),
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
