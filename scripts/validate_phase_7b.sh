#!/usr/bin/env bash
# FRIDAY Phase 7B validation — strategy library, signals, sticky watchlist.
# Offline suites; a READ-ONLY preview of the strategies on your REAL recorded
# history (nothing written); two live read-only snapshots into a TEMPORARY
# store with the full pipeline; a real-server smoke. No Claude. Nothing trades.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 7B VALIDATION — STRATEGIES, SIGNALS, WATCHLIST ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- quant + market tests ---"
T="friday/tests/test_quant_strategies.py friday/tests/test_quant_core.py friday/tests/test_market_context.py friday/tests/test_market_store.py friday/tests/test_market_data.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- strategies on your REAL recorded history (PREVIEW: nothing is written) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import os, sys
from datetime import datetime, timezone
from friday.market.store import MarketStore
from friday.quant import LIBRARY, QuantEngine, QuantPipeline, UniverseRules
from friday.quant.features import _ts
path = sys.argv[1]
if not os.path.exists(path):
    print("no real market store yet — SKIPPED"); sys.exit(0)
store = MarketStore(path)          # adds the 7B tables if missing (schema only)
def counts():
    with store._db() as db:
        return (db.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"],
                db.execute("SELECT COUNT(*) n FROM watchlist").fetchone()["n"])
before = counts()
engine = QuantEngine(store, UniverseRules.from_environment())
now = datetime.now(timezone.utc)
markets = engine.features(now=now)
preview = QuantPipeline(engine, LIBRARY, record=False).evaluate(now=now)
print(f"[PREVIEW] {preview['markets']} markets, {preview['eligible']} eligible → {preview['signals_new']} signals "
      f"would fire now: {preview['by_strategy']}")
for sig in preview["signals"][:8]:
    shown = {k: v for k, v in sig["inputs"].items() if k not in ("question", "attention")}
    print(f"   {sig['strategy']:<15} {sig['direction']:<4} str {sig['strength']:.2f} {sig['horizon_hours']:g}h | "
          f"{sig['inputs']['question'][:48]} — {sig['outcome']} mid {sig['mid']} | {shown}")
# The corrected 7A check: any market observed for more than an hour has a 1h change.
long_enough = []
for m in markets:
    o = m.outcomes[0]
    hist = store.history(o.token_id, hours=2)
    usable = [h for h in hist if h["usable"] and h["mid"] is not None]
    if o.usable and usable and (now - _ts(usable[0]["observed_at"])).total_seconds() > 3900:
        long_enough.append(m)
covered = [m for m in long_enough if m.outcomes[0].change_1h is not None]
print(f"[1H CHECK] {len(long_enough)} markets observed > 1h: {len(covered)} have a 1h change")
still_same = counts() == before
n_sig = before[0]
checks = {
    "strategies evaluated on real data": preview["eligible"] >= 1,
    "every signal is on an eligible market's primary outcome": all(
        any(m.market_id == s["market_id"] and m.eligible and m.outcomes[0].token_id == s["token_id"] for m in markets)
        for s in preview["signals"]),
    "every signal records its inputs, prices and horizon": all(
        s["inputs"] and s["mid"] is not None and s["best_bid"] is not None and s["best_ask"] is not None
        and s["horizon_hours"] > 0 for s in preview["signals"]),
    "1h change present for every market observed > 1h (corrected 7A check)": len(covered) == len(long_enough),
    "preview wrote nothing": still_same,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print(f"(real store currently holds {n_sig} recorded signals; the service records them after restart)")
print("REAL HISTORY PREVIEW:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- LIVE: two snapshots into a TEMPORARY store with the full pipeline (read-only Polymarket) ---"
TMPDIR_7B="$(mktemp -d)"
python - "$TMPDIR_7B/market_data.db" <<'PY'
import sys, time
from friday.app.config import GAMMA_HOST, POLYMARKET_HOST
from friday.app.polymarket import market_filter_settings
from friday.market.client import PolymarketData
from friday.market.store import MarketStore
from friday.quant import LIBRARY, QuantEngine, QuantPipeline
store = MarketStore(sys.argv[1])
client = PolymarketData(clob_host=POLYMARKET_HOST, gamma_host=GAMMA_HOST, store=store, **market_filter_settings())
pipe = QuantPipeline(QuantEngine(store), LIBRARY)
client.observers.append(pipe.on_run)
client.tracked_source = lambda: store.watchlist_markets(48)
_, r1 = client.snapshot(limit=50)
wl1 = store.watchlist()
print(f"[RUN 1] kept={r1.markets} tracked={r1.tracked} excluded={r1.excluded} errors={len(r1.errors)} quant={r1.quant}")
time.sleep(3)
_, r2 = client.snapshot(limit=25)            # smaller page: watchlist must fill in the rest
print(f"[RUN 2] kept={r2.markets} tracked={r2.tracked} excluded={r2.excluded} errors={len(r2.errors)} quant={r2.quant}")
wl = store.watchlist()
sigs = store.signals(hours=1, limit=5000)
print(f"[WATCHLIST] {len(wl)} markets; [SIGNALS] {len(sigs)} recorded: {store.signal_counts(hours=1)}")
checks = {
    "pipeline ran after each stored run": bool(r1.quant) and bool(r2.quant),
    "watchlist populated from eligible markets": len(wl1) == r1.quant.get("eligible"),
    "watchlist kept markets observed beyond the top-25 page": r2.tracked >= 1,
    "no double counting while horizons are open": len({(s["strategy"], s["token_id"], s["direction"]) for s in sigs}) == len(sigs),
    "no fetch or observer errors": not r1.errors and not r2.errors,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("LIVE PIPELINE:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$TMPDIR_7B"

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
import json, urllib.request
BASE = "http://127.0.0.1:8765"
def get(p):
    with urllib.request.urlopen(BASE + p, timeout=60) as r:
        return json.load(r)
checks = {}
try:
    rows = get("/api/scan")
    strat = get("/api/quant/strategies")
    sigs = get("/api/quant/signals")
    wl = get("/api/quant/watchlist")
    with_sig = [r for r in rows if r.get("signals")]
    print(f"[SCAN] {len(rows)} rows, {sum(r['decision'] == 'WATCH' for r in rows)} WATCH, {len(with_sig)} with open signals")
    for r in with_sig[:3]:
        print(f"       {r['title'][:55]} → {r['signals']}")
    print(f"[STRATEGIES] " + ", ".join(f"{s['name']}={s['counts']['signals']}" for s in strat["strategies"]))
    checks["scan rows carry open signals field"] = all("signals" in r for r in rows)
    checks["pipeline ran on the scan's live snapshot"] = strat["pipeline"].get("runs", 0) >= 1
    checks["four strategies, all marked hypotheses"] = len(strat["strategies"]) == 4 and all(
        s["status"].startswith("HYPOTHESIS") for s in strat["strategies"])
    checks["signals endpoint (no size/order fields)"] = all(
        not any(k in s for k in ("size", "stake", "order", "amount")) for s in sigs["signals"])
    checks["watchlist endpoint"] = isinstance(wl["markets"], list) and wl["hours"] == 48
    checks["edge is None everywhere"] = all(r["edge"] is None for r in rows)
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
import pathlib, sqlite3, subprocess, tempfile
from friday.hermes.authority import Capability
from friday.hermes.jobs import JobType
from friday.intelligence.schemas import IntelligenceAction
from friday.market.context import MARKET_CONTEXT_VERSION
from friday.market.store import MARKET_STORE_VERSION, MarketStore
from friday.quant import LIBRARY, STRATEGIES_VERSION
env = pathlib.Path(".env.example").read_text()
src = "\n".join(p.read_text() for p in pathlib.Path("friday/quant").glob("*.py"))
with tempfile.TemporaryDirectory() as d:
    s = MarketStore(pathlib.Path(d) / "m.db")
    with s._db() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(signals)")}
checks = {
    "strategies 7B.1, store 7B.1, context 7B.1": STRATEGIES_VERSION == MARKET_STORE_VERSION == MARKET_CONTEXT_VERSION == "7B.1",
    "four strategies": [s.name for s in LIBRARY] == ["BOOK_IMBALANCE", "MEAN_REVERSION", "MOMENTUM", "LONGSHOT_BIAS"],
    "signals table has no size/order columns": not cols & {"size", "stake", "order", "amount", "quantity"},
    "no new capability (still 10)": len(Capability) == 10,
    "no new job type (still 8)": len(JobType) == 8,
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
    "quant core has no network or Claude path": not any(k in src for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.")),
    ".env.example documents 7B settings": all(k in env for k in (
        "FRIDAY_QUANT_SIGNALS", "FRIDAY_QUANT_STRATEGIES", "FRIDAY_MARKET_WATCHLIST_HOURS", "FRIDAY_MARKET_WATCHLIST_MAX")),
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
