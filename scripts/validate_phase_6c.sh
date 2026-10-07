#!/usr/bin/env bash
# FRIDAY Phase 6C validation — settled-market filter, history export,
# recorded market data as Claude context.
# Offline suites + LIVE read-only Polymarket snapshots into a TEMPORARY store
# + a real-server smoke that makes ONE live Claude market analysis (advisory,
# analysis budget, ~5-8k tokens) if Claude is configured. Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 6C VALIDATION — CLEAN UNIVERSE, EXPORT, MARKET CONTEXT ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- market tests (6A + 6B + 6C) ---"
T="friday/tests/test_market_context.py friday/tests/test_market_store.py friday/tests/test_market_data.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: two real snapshots into a TEMPORARY store (read-only, filter ON) ---"
TMPDB="$(mktemp -d)/market_data.db"
for i in 1 2; do
  python -m friday.market snapshot --limit 50 --db "$TMPDB" > "$TMPDB.run$i.json"
  python - "$TMPDB.run$i.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"[RUN] candidates={r['candidates']} kept={r['markets']} excluded={r['excluded']} "
      f"reasons={r['excluded_reasons']} usable_rows={r['usable_rows']} books_fetched={r['books_ok']}/{r['tokens']} "
      f"errors={len(r['errors'])}")
for e in r["excluded_examples"]:
    print(f"      excluded → {e}")
PY
  sleep 3
done
python - "$TMPDB" <<'PY'
import sys
from friday.market.context import MarketContext
from friday.market.store import MarketStore
store = MarketStore(sys.argv[1])
s = store.stats()
d = s["depth_vs_gamma_liquidity"]
print(f"[STORE] runs={s['runs']} markets={s['counts']['markets']} snapshots={s['counts']['snapshots']} "
      f"usable={s['usable_ratio']} flags={s['flags']} excluded={s['excluded_settled']}")
print(f"[DEPTH CHECK] usable markets only: median {d['median_ratio']} over {d['markets']} markets")
with store._db() as db:
    decided = db.execute("SELECT COUNT(*) n FROM snapshots WHERE best_bid >= 0.98 OR best_ask <= 0.02").fetchone()["n"]
rows = store.export_rows()
csv_text = store.to_csv(rows)
tracked = store.markets(limit=50)
ctx = MarketContext(store)
target = next((m for m in tracked if len(store.terms(m["question"])) >= 2), None)
block = ctx.context_for(target["question"]) if target else None
hit = bool(block) and target["market_id"] in [m["market_id"] for m in block["markets"]]
if block:
    m = next(m for m in block["markets"] if m["market_id"] == target["market_id"]) if hit else block["markets"][0]
    o = m["outcomes"][0]
    print(f"[CONTEXT] \"{m['question'][:70]}\" — {o['outcome']}: bid {o['latest']['best_bid']} "
          f"ask {o['latest']['best_ask']} usable {o['latest']['usable']} obs {o['observations']} "
          f"change {o['mid_change']}")
print(f"[EXPORT] {len(rows)} rows, {len(csv_text.splitlines()) - 1} CSV lines, header: {csv_text.splitlines()[0][:60]}…")
checks = {
    "two runs recorded": s["runs"] == 2,
    "scan filled despite exclusions (>= 45 of 50 markets)": s["counts"]["markets"] >= 45,
    "no decided market (bid>=0.98 / ask<=0.02) left in the universe": decided == 0,
    "usable ratio >= 0.75 after the filter": (s["usable_ratio"] or 0) >= 0.75,
    "export matches stored snapshots": len(rows) == s["counts"]["snapshots"] == len(csv_text.splitlines()) - 1,
    "context finds a tracked market by its question": hit,
    "context is MEASURED_DATA with no authority": bool(block) and block["trust"] == "MEASURED_DATA"
        and block["instruction_authority"] == "NONE",
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("LIVE FILTER + CONTEXT:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$(dirname "$TMPDB")"

echo
echo "--- REAL SERVER smoke (127.0.0.1:8765, temp DB + temp market store; one live Claude market analysis) ---"
SMOKE_DIR="$(mktemp -d)"
DB_PATH="$SMOKE_DIR/friday.db" FRIDAY_MARKET_DB="$SMOKE_DIR/market/market_data.db" \
HERMES_MARKET_SCAN_INTERVAL_SECONDS=300 HERMES_MARKET_SCAN_LIMIT=40 HERMES_HEALTH_INTERVAL_SECONDS=0 \
FRIDAY_KNOWLEDGE_ENABLED=false FRIDAY_LIBRARY_PATH="$SMOKE_DIR/no-library" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - <<'PY'
import json, time, urllib.request
BASE = "http://127.0.0.1:8765"
def get(p, raw=False):
    with urllib.request.urlopen(BASE + p, timeout=30) as r:
        return r.read().decode() if raw else json.load(r)
def post(p, body):
    req = urllib.request.Request(BASE + p, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)
checks = {}
try:
    stats = {}
    for _ in range(40):                       # the scheduled scan runs at start-up
        stats = get("/api/market/stats")
        if stats["counts"]["runs"] >= 1:
            break
        time.sleep(1)
    checks["scheduled Hermes scan recorded a run"] = stats["counts"]["runs"] >= 1
    checks["filter active and reported"] = (stats.get("filter") or {}).get("exclude_settled") is True \
        and "excluded_settled" in stats
    checks["context enabled"] = (stats.get("context") or {}).get("version") == "6C.1"
    markets = get("/api/market/markets")
    csv_text = get(f"/api/market/export?market_id={markets[0]['market_id']}", raw=True)
    checks["CSV export endpoint"] = csv_text.startswith("observed_at,market_id,question") and len(csv_text.splitlines()) >= 2
    health = get("/health")
    if health.get("claude"):
        # Prefer a market whose data is usable; Claude must still stay advisory.
        pick = markets[0]
        for m in markets:
            hist = get(f"/api/market/history?token_id={m['outcomes'][0]['token_id']}")["rows"]
            if hist and hist[-1]["usable"]:
                pick = m
                break
        started = post("/api/market/analyse", {"market_id": pick["market_id"],
                                                "question": "Is the current price well supported by the order book?"})
        job = {}
        for _ in range(180):
            job = get(f"/api/hermes/jobs/{started['job_id']}")
            if job["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "REJECTED"):
                break
            time.sleep(1)
        result = job.get("result") or {}
        resp = result.get("response") or {}
        print(f"[CLAUDE] market: {pick['question'][:70]}")
        print(f"[CLAUDE] status {job.get('status')} action {result.get('action')} fallback {result.get('fallback')} "
              f"retried_brief {result.get('retried_brief')} tokens {result.get('input_tokens')}/{result.get('output_tokens')}")
        print(f"[CLAUDE] assessment: {' '.join(str(resp.get('assessment', '')).split())[:400]}")
        print(f"[CLAUDE] market data consulted: {result.get('market_sources')}")
        checks["live Claude market analysis succeeded"] = job.get("status") == "SUCCEEDED" and not result.get("fallback")
        checks["Claude was given FRIDAY's market data"] = bool(result.get("market_sources"))
        checks["advisory only (non-trading action, not authorized)"] = \
            result.get("action") in ("NO_TRADE", "ANALYSE", "WATCH", "RESEARCH") and result.get("execution_authorized") is False
    else:
        print("[CLAUDE] SKIPPED: Claude is not configured for this process")
    h = get("/health")
    checks["HEALTHY, execution locked"] = h["status"] == "HEALTHY" and h["execution_locked"] is True
    print(f"[SMOKE] runs={stats['counts']['runs']} snapshots={stats['counts']['snapshots']} "
          f"usable={stats['usable_ratio']} excluded={stats.get('excluded_settled')}")
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
from friday.hermes.jobs import JOB_CAPABILITIES, JobType
from friday.intelligence.schemas import IntelligenceAction
from friday.market.context import MARKET_CONTEXT_VERSION
from friday.market.models import MARKET_DATA_VERSION
from friday.market.store import MARKET_STORE_VERSION
env = pathlib.Path(".env.example").read_text()
checks = {
    "versions 6C.1 (data, store, context)": MARKET_DATA_VERSION == MARKET_STORE_VERSION == MARKET_CONTEXT_VERSION == "6C.1",
    "no new capability (still 10)": len(Capability) == 10,
    "analysis declares READ_MARKET_DATA": Capability.READ_MARKET_DATA in JOB_CAPABILITIES[JobType.INTELLIGENCE_ANALYSIS],
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
    ".env.example documents 6C settings": all(k in env for k in (
        "FRIDAY_MARKET_EXCLUDE_SETTLED", "FRIDAY_MARKET_NEAR_CERTAIN", "FRIDAY_MARKET_PAST_END_HOURS",
        "FRIDAY_MARKET_CONTEXT")),
    "no market data files tracked in Git": not any(p.endswith((".db", ".csv")) for p in
        subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("ALL CHECKS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- your real market store (read-only report; 6C columns are added on first open) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
if [ -f "${REAL_DB/#\~/$HOME}" ]; then
  python -m friday.market stats --db "$REAL_DB" | grep -E '"(runs|snapshots|usable_ratio|size_mb|last_run|median_ratio|total)"'
else
  echo "no market store yet"
fi

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"

echo
echo "=== END — paste everything above back to Claude ==="
