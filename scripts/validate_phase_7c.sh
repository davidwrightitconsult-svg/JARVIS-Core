#!/usr/bin/env bash
# FRIDAY Phase 7C validation — signal scoring, resolutions, scorecard.
# Offline suites; a PREVIEW of scoring on your REAL recorded signals (nothing
# written); a read-only probe of Gamma's per-market endpoint used for
# resolutions; a real-server smoke. No Claude. Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 7C VALIDATION — SCORING, RESOLUTIONS, SCORECARD ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- quant + market + knowledge tests ---"
T="friday/tests/test_quant_scoring.py friday/tests/test_quant_strategies.py friday/tests/test_quant_core.py friday/tests/test_market_context.py friday/tests/test_market_store.py friday/tests/test_market_data.py friday/tests/test_knowledge_vault.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- scoring PREVIEW on your REAL recorded signals (nothing is written) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import os, statistics, sys
from collections import defaultdict
from friday.market.store import MarketStore
from friday.quant.scoring import Scorer
path = sys.argv[1]
if not os.path.exists(path):
    print("no real market store yet — SKIPPED"); sys.exit(0)
store = MarketStore(path)                        # adds the 7C tables if missing (schema only)
def n_outcomes():
    with store._db() as db:
        return db.execute("SELECT COUNT(*) n FROM signal_outcomes").fetchone()["n"]
before = n_outcomes()
with store._db() as db:
    total = db.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"]
preview = Scorer(store, record=False).score_due()
outcomes = preview.pop("outcomes")
print(f"[SIGNALS] {total} recorded; due now {preview['due']} → {preview['by_status']}, "
      f"waiting for an exit observation {preview['waiting_for_exit']}")
sigs = {s["signal_id"]: s for s in store.due_signals()}
by = defaultdict(list)
for o in outcomes:
    if o["status"] == "SCORED":
        by[sigs[o["signal_id"]]["strategy"]].append(o)
for name, rows in sorted(by.items()):
    nets = [r["net"] for r in rows]
    hits = sum(r["mid_move"] > 0 for r in rows)
    flats = sum(r["mid_move"] == 0 for r in rows)
    print(f"[EARLY] {name:<15} scored {len(rows):>3}: price moved the predicted way {hits}, flat {flats}, "
          f"against {len(rows) - hits - flats}; avg net after spread {statistics.fmean(nets) * 100:+.2f}c")
for o in [o for o in outcomes if o["status"] == "SCORED"][:5]:
    s = sigs[o["signal_id"]]
    print(f"   {s['strategy']:<15} {s['direction']:<4} entry {s['best_bid']}/{s['best_ask']} → exit "
          f"{o['exit_bid']}/{o['exit_ask']} at {o['exit_observed_at'][11:16]}  mid_move {o['mid_move']:+.4f} "
          f"net {o['net']:+.4f}")
def recheck(o):
    s = sigs[o["signal_id"]]
    want = (o["exit_bid"] - s["best_ask"]) if s["direction"] == "UP" else (s["best_bid"] - o["exit_ask"])
    return abs(want - o["net"]) < 1e-9 and o["exit_observed_at"] >= s["expires_at"]
checks = {
    "every scored signal used an observation at/after its expiry, net recomputes": all(
        recheck(o) for o in outcomes if o["status"] == "SCORED"),
    "nothing guessed (no-exit signals have no numbers)": all(
        o.get("net") is None for o in outcomes if o["status"] == "NO_EXIT_DATA"),
    "preview wrote nothing": n_outcomes() == before,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("(early numbers are a handful of signals — the scorecard's verdicts need 30+ per strategy)")
print("REAL SIGNALS PREVIEW:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- LIVE: read-only resolution probe (Gamma GET /markets/{id}) ---"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import os, sys
from friday.app.config import GAMMA_HOST, POLYMARKET_HOST
from friday.market.client import PolymarketData
from friday.market.store import MarketStore
path = sys.argv[1]
ids = []
if os.path.exists(path):
    store = MarketStore(path)
    with store._db() as db:
        ids = [r["market_id"] for r in db.execute(
            "SELECT market_id FROM markets ORDER BY last_seen ASC LIMIT 4").fetchall()]
        ids += [r["market_id"] for r in db.execute(
            "SELECT market_id FROM markets ORDER BY last_seen DESC LIMIT 2").fetchall()]
client = PolymarketData(clob_host=POLYMARKET_HOST, gamma_host=GAMMA_HOST)
if not ids:
    ids = [m.market_id for m in client.markets(3)]
results = client.market_resolutions(list(dict.fromkeys(ids)))
for r in results:
    print(f"[MARKET {r['market_id']}] closed={r.get('closed')} resolved={r['resolved']} "
          f"payoffs={r.get('payoffs')} {('error=' + r['error']) if r.get('error') else ''}")
ok = [r for r in results if not r.get("error")]
checks = {
    "Gamma per-market endpoint answers": len(ok) >= 1,
    "a resolved market always has 0/1 payoffs summing to 1": all(
        sum(r["payoffs"].values()) == 1 and set(r["payoffs"].values()) <= {0, 1} for r in results if r["resolved"]),
    "open markets are never called resolved": all(not r["resolved"] for r in ok if r.get("closed") is False),
}
for name, passed in checks.items():
    print(f"{'PASS' if passed else 'FAIL'}: {name}")
print("RESOLUTION PROBE:", "PASS" if all(checks.values()) else "FAIL")
PY

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
def call(p, method="GET"):
    req = urllib.request.Request(BASE + p, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)
checks = {}
try:
    rows = call("/api/scan")                              # live snapshot -> pipeline -> signals
    strat = call("/api/quant/strategies")
    last = strat["pipeline"].get("last") or {}
    print(f"[PIPELINE] signals {last.get('signals_new')} scoring {last.get('scoring')}")
    score = call("/api/quant/score", "POST")
    card = call("/api/quant/scorecard")
    outs = call("/api/quant/outcomes")
    print("[SCORECARD] " + "; ".join(f"{s['strategy']} {s['signals']} signals, {s['verdict']}" for s in card["strategies"]))
    checks["pipeline scores due signals after each run"] = "scoring" in last
    checks["score endpoint"] = "by_status" in score
    checks["scorecard lists four strategies with verdicts"] = len(card["strategies"]) == 4 and all(
        s["verdict"] for s in card["strategies"])
    checks["fresh signals are INSUFFICIENT_DATA, never PROMISING"] = all(
        s["verdict"].startswith("INSUFFICIENT_DATA") for s in card["strategies"])
    checks["outcomes endpoint"] = isinstance(outs["outcomes"], list)
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
from friday.knowledge.notes import NoteType
from friday.knowledge.vault import FRIDAY_FOLDERS
from friday.market.context import MARKET_CONTEXT_VERSION
from friday.market.store import MARKET_STORE_VERSION
from friday.quant.scorecard import MIN_SAMPLE, SCORECARD_VERSION
from friday.quant.scoring import SCORING_VERSION
env = pathlib.Path(".env.example").read_text()
src = "\n".join(p.read_text() for p in pathlib.Path("friday/quant").glob("*.py"))
checks = {
    "scoring, scorecard, store, context 7C.1": {SCORING_VERSION, SCORECARD_VERSION, MARKET_STORE_VERSION,
                                               MARKET_CONTEXT_VERSION} == {"7C.1"},
    "verdicts need 30+ scored signals": MIN_SAMPLE == 30,
    "FRIDAY/Strategies folder + scorecard note type": "Strategies" in FRIDAY_FOLDERS
        and NoteType.STRATEGY_SCORECARD.value == "strategy-scorecard",
    "no new capability (still 10)": len(Capability) == 10,
    "no new job type (still 8)": len(JobType) == 8,
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
    "quant core has no network or Claude path": not any(k in src for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.")),
    ".env.example documents FRIDAY_QUANT_RESOLUTIONS": "FRIDAY_QUANT_RESOLUTIONS" in env,
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
