#!/usr/bin/env bash
# FRIDAY Phase 6A validation — Market Data Engine (correct prices, quality flags).
# Offline suites + a LIVE read-only probe of Polymarket's public APIs
# (no keys, no orders, no Claude). Nothing on disk is changed.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 6A VALIDATION — MARKET DATA ENGINE ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- market data tests ---"
python -m pytest -q -p no:warnings friday/tests/test_market_data.py 2>&1 | tail -1
python -O -m pytest -q -p no:warnings friday/tests/test_market_data.py 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE Polymarket contract probe (read-only public API) ---"
python - <<'PY'
import collections, json
from friday.app.config import GAMMA_HOST, POLYMARKET_HOST
from friday.market import PolymarketData, parse_market

checks = {}
client = PolymarketData(clob_host=POLYMARKET_HOST, gamma_host=GAMMA_HOST)
try:
    raw = client.client.get(f"{client.gamma}/markets", params={
        "limit": 10, "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false"}).json()
    fields = ["question", "clobTokenIds", "outcomes", "outcomePrices", "bestBid", "bestAsk",
              "liquidityNum", "volume24hr", "enableOrderBook", "acceptingOrders", "endDate"]
    present = {f: sum(1 for m in raw if m.get(f) not in (None, "")) for f in fields}
    print(f"[GAMMA] {len(raw)} markets; field presence: " + ", ".join(f"{k}={v}" for k, v in present.items()))
    parsed = [m for m in (parse_market(x) for x in raw) if m]
    checks["Gamma markets parse (question + tokens)"] = len(parsed) >= 5
    checks["all parsed markets are open"] = all(m.closed is not True for m in parsed)

    tokens = [o.token_id for m in parsed[:5] for o in m.outcomes]
    books = client.books(tokens)
    got = [b for b in books.values() if b is not None]
    print(f"[CLOB] batch endpoint: {'POST /books' if client._batch_ok else 'fallback GET /book'}; "
          f"books {len(got)}/{len(tokens)}")
    worst_first = sum(1 for b in got if b.bids and len(b.bids) > 1 and b.raw_first_bid < b.best_bid)
    multi = sum(1 for b in got if len(b.bids) > 1)
    print(f"[CLOB] books whose FIRST bid is not the best bid: {worst_first}/{multi} "
          "(Phase 0 read the first level)")
    checks["order books fetched"] = len(got) >= max(1, int(0.8 * len(tokens)))
    checks["best bid < best ask on two-sided books"] = all(
        b.best_bid < b.best_ask for b in got if b.bids and b.asks and b.best_bid is not None and b.best_bid < 0.99)

    rows, report = client.snapshot(limit=25)
    flags = collections.Counter(f for r in rows for f in r["flags"])
    print(f"[SNAPSHOT] markets={report.markets} tokens={report.tokens} books_ok={report.books_ok} "
          f"usable_rows={report.usable_rows}/{len(rows)} flags={dict(flags)} errors={len(report.errors)}")
    for e in report.errors[:3]:
        print(f"   error: {e}")
    usable = [r for r in rows if r["usable"]]
    for r in usable[:3]:
        print(f"   e.g. {r['title'][:70]} | bid {r['best_bid']} ask {r['best_ask']} "
              f"mid {r['mid']} spread {r['spread']} depth ${r['depth_usd']:,.0f} flags {r['flags']}")
    checks["snapshot has usable rows"] = report.usable_rows >= 1
    checks["no usable row is crossed or placeholder"] = all(
        r["best_bid"] < r["best_ask"] and not (r["best_bid"] <= 0.01 and r["best_ask"] >= 0.99) for r in usable)
    checks["most books retrieved"] = report.books_ok >= 0.8 * max(1, report.tokens)
except Exception as exc:
    checks[f"Polymarket reachable ({type(exc).__name__}: {str(exc)[:120]})"] = False
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("LIVE PROBE:", "PASS" if checks and all(checks.values()) else "FAIL")
PY

echo
echo "--- full regression (live tests and soak skipped) ---"
python -m pytest -q -p no:warnings friday/tests --ignore=friday/tests/test_claude_live_tasks.py \
  --ignore=friday/tests/test_hermes_live.py --ignore=friday/tests/test_hermes_soak.py \
  --ignore=friday/tests/test_app_live.py --ignore=friday/tests/test_research_live.py \
  --deselect friday/tests/test_claude_api.py::test_claude_live_non_trading_request 2>&1 | tail -1

echo
echo "--- integration checks ---"
python - <<'PY'
import inspect, pathlib
import friday.market.client as client_mod
from friday.app.polymarket import PolyMarketClient
from friday.market import HARD_FLAGS, MARKET_DATA_VERSION, PolymarketData
src = inspect.getsource(client_mod)
checks = {
    "app client is the Market Data Engine": issubclass(PolyMarketClient, PolymarketData),
    "Phase 0 bids[0]/asks[0] reading removed": "bids[0]['price']" not in pathlib.Path("friday/app/polymarket.py").read_text(),
    "read-only: no order/cancel endpoints or keys": not any(w in src for w in ('"/order', "/order\"", "/cancel", "private_key", "api_key")),
    "hard flags block unusable prices": {"EMPTY_BOOK", "CROSSED", "PLACEHOLDER", "NO_BOOK"} <= HARD_FLAGS,
    "market data version 6A.1": MARKET_DATA_VERSION == "6A.1",
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
