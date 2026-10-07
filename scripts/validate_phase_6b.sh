#!/usr/bin/env bash
# FRIDAY Phase 6B validation — market data store (history, levels, stats).
# Offline suites + LIVE read-only Polymarket snapshots into a TEMPORARY
# store + a real-server smoke where the Hermes market scan records history.
# No Claude. Your real market database is only reported on.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 6B VALIDATION — MARKET DATA STORE ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- market store + market data tests ---"
T="friday/tests/test_market_store.py friday/tests/test_market_data.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: two real snapshots into a TEMPORARY store (read-only) ---"
TMPDB="$(mktemp -d)/market_data.db"
python -m friday.market snapshot --limit 50 --db "$TMPDB" | grep -iE '"(markets|tokens|books_ok|usable_rows|stored_run)"|error' | tr -d ' \n'; echo
sleep 3
python -m friday.market snapshot --limit 50 --db "$TMPDB" | grep -iE '"(usable_rows|stored_run)"|error' | tr -d ' \n'; echo
python - "$TMPDB" <<'PY'
import sys
from friday.market.store import MarketStore
store = MarketStore(sys.argv[1])
s = store.stats()
d = s["depth_vs_gamma_liquidity"]
print(f"[STORE] runs={s['runs']} markets={s['counts']['markets']} snapshots={s['counts']['snapshots']} "
      f"book_levels={s['counts']['book_levels']} usable={s['usable_ratio']} flags={s['flags']} size={s['size_mb']}MB")
print(f"[DEPTH CHECK] our ±2c USD depth ÷ Polymarket liquidityNum: median {d['median_ratio']} over {d['markets']} markets")
tracked = store.markets(limit=1)
hist, lv = [], {"bids": [], "asks": []}
if tracked:
    m = tracked[0]
    token = m["outcomes"][0]["token_id"]
    hist, lv = store.history(token), store.levels(token)
    print(f"[HISTORY] {m['question'][:60]} — {m['outcomes'][0]['name']}: {len(hist)} points; "
          f"latest bid {hist[-1]['best_bid']} ask {hist[-1]['best_ask']}; "
          f"top levels bid {lv['bids'][:2]} ask {lv['asks'][:2]}")
checks = {
    "two runs recorded": s["runs"] == 2,
    "markets + outcomes tracked": s["counts"]["markets"] >= 10 and s["counts"]["outcomes"] >= 20,
    "history has both runs": len(hist) == 2,
    "book levels best-first": all(lv["bids"][i][0] >= lv["bids"][i + 1][0] for i in range(len(lv["bids"]) - 1))
        and all(lv["asks"][i][0] <= lv["asks"][i + 1][0] for i in range(len(lv["asks"]) - 1)),
    "mostly usable data": (s["usable_ratio"] or 0) >= 0.7,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("LIVE STORE:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$(dirname "$TMPDB")"

echo
echo "--- REAL SERVER smoke (127.0.0.1:8765, temp DB + temp market store; Hermes scan records history) ---"
SMOKE_DIR="$(mktemp -d)"
DB_PATH="$SMOKE_DIR/friday.db" FRIDAY_MARKET_DB="$SMOKE_DIR/market/market_data.db" \
HERMES_MARKET_SCAN_INTERVAL_SECONDS=300 HERMES_MARKET_SCAN_LIMIT=20 HERMES_HEALTH_INTERVAL_SECONDS=0 \
FRIDAY_KNOWLEDGE_ENABLED=false FRIDAY_LIBRARY_PATH="$SMOKE_DIR/no-library" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - <<'PY'
import json, time, urllib.request
BASE = "http://127.0.0.1:8765"
def get(p):
    return json.load(urllib.request.urlopen(BASE + p, timeout=30))
checks = {}
try:
    stats = {}
    for _ in range(30):                       # the scheduled scan runs at start-up
        stats = get("/api/market/stats")
        if stats["counts"]["runs"] >= 1:
            break
        time.sleep(1)
    checks["scheduled Hermes scan recorded a run"] = stats["counts"]["runs"] >= 1
    checks["~10 markets / 20 rows stored"] = stats["counts"]["snapshots"] >= 15
    jobs = get("/api/hermes/jobs")
    scan = next((j for j in jobs if j["job_type"] == "MARKET_SCAN"), None)
    checks["Hermes job result stays small (no book levels)"] = scan is not None and "top_bids" not in json.dumps(scan)
    token = get("/api/market/markets")[0]["outcomes"][0]["token_id"]
    checks["history endpoint"] = len(get(f"/api/market/history?token_id={token}")["rows"]) >= 1
    lv = get(f"/api/market/levels?token_id={token}")
    checks["levels endpoint"] = lv["observed_at"] is not None and isinstance(lv["bids"], list)
    h = get("/health")
    checks["HEALTHY, execution locked"] = h["status"] == "HEALTHY" and h["execution_locked"] is True
    print(f"[SMOKE] runs={stats['counts']['runs']} snapshots={stats['counts']['snapshots']} usable={stats['usable_ratio']}")
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
import pathlib
from friday.hermes.bootstrap import HermesSettings
from friday.market.store import MARKET_STORE_VERSION, DEFAULT_MARKET_DB
env = pathlib.Path(".env.example").read_text()
checks = {
    "market store version 6B.1": MARKET_STORE_VERSION == "6B.1",
    "store default outside the repo": DEFAULT_MARKET_DB.startswith("~/friday/data/"),
    "market scan default 100 rows": HermesSettings().market_scan_limit == 100,
    ".env.example documents market store": all(k in env for k in ("FRIDAY_MARKET_DB", "FRIDAY_MARKET_RETENTION_DAYS")),
    "no market data files tracked in Git": not any(p.endswith(".db") for p in
        __import__("subprocess").run(["git", "ls-files"], capture_output=True, text=True).stdout.split()),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("ALL CHECKS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- your real market store + service (read-only) ---"
grep -E "^HERMES_MARKET_SCAN_LIMIT=" .env 2>/dev/null | sed 's/^/.env: /' || true
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
if [ -f "${REAL_DB/#\~/$HOME}" ]; then
  python -m friday.market stats --db "$REAL_DB" | grep -E '"(runs|snapshots|usable_ratio|size_mb|last_run)"'
else
  echo "no market store yet — it is created when the restarted service runs its first scan"
fi

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"

echo
echo "=== END — paste everything above back to Claude ==="
