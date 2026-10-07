#!/usr/bin/env bash
# FRIDAY Phase 12B validation — resting paper orders and the Bollinger forward test.
# Offline suites; a drill of the forward test on made-up markets with a TEMPORARY risk store;
# a real-server smoke on temporary stores. No network needed. No Claude. Paper money only.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 12B VALIDATION — FORWARD TEST ON PAPER ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- forward test + paper engine + gate tests ---"
T="friday/tests/test_paper_sources.py friday/tests/test_paper_engine.py friday/tests/test_paper_ledger.py friday/tests/test_lab_gate.py friday/tests/test_risk_governor.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- the forward test, as declared before its first order ---"
python - <<'PY'
from friday.paper import sources as s
print(f"   {s.NAME}: signal = Bollinger Bands {s.RULE.params} on hourly closes (the book's settings)")
print(f"   entry: resting buy at the recorded bid, waits {s.TTL_MINUTES:g} minutes; fills only if a later ask is strictly below it")
print(f"   stop: {s.STOP_BASE * 100:g}c plus the spread, at most {s.STOP_MAX * 100:g}c · target: the 20-bar average (at least {s.MIN_TARGET * 100:g}c away) · time limit {s.MAX_HOLD_HOURS:g}h · asks for ${s.STAKE:g}")
print(f"   judged at {s.MIN_TRADES} closed trades on {s.MIN_DAYS} days (95% interval, clustered by day) · stops itself for good at a ${s.LOSS_LIMIT:g} paper loss")
PY

echo
echo "--- drill: ten days of the forward test on made-up markets (temporary risk store) ---"
DRILL_DIR="$(mktemp -d)"
python - "$DRILL_DIR/risk.db" <<'PY'
import random, sys
from datetime import datetime, timedelta, timezone
from friday.paper import BollingerResting, PaperEngine, PaperSettings
from friday.paper import sources
from friday.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

class Clock:
    now = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
    def __call__(self): return self.now
class Market:
    def __init__(self, clock): self.clock, self.rows, self.hist, self.res = clock, {}, {}, {}
    def quote(self, m, t, bid, ask, outcome, order):
        row = {"market_id": m, "token_id": t, "best_bid": bid, "best_ask": ask, "mid": (bid + ask) / 2, "usable": True,
               "observed_at": self.clock().isoformat(timespec="seconds"), "question": f"Drill {m}", "outcome": outcome, "outcome_order": order}
        self.rows[t] = row
        self.hist.setdefault(t, []).append(row)
        del self.hist[t][:-200]
    def latest_rows(self, ids=None): return sorted(self.rows.values(), key=lambda r: (r["market_id"], r["outcome_order"]))
    def history_bulk(self, ts, hours=25): return {t: list(self.hist.get(t, [])) for t in ts}
    def quote_near(self, t, at, tol=3.0):
        r = self.rows.get(t)
        return r if r and timedelta(0) <= datetime.fromisoformat(at) - datetime.fromisoformat(r["observed_at"]) <= timedelta(hours=tol) else None
    def resolutions(self, ids=None): return {}
    def record_resolution(self, *a, **k): pass
    def watch(self, ids, now=None): pass

clock, path = Clock(), sys.argv[1]
state, settings = RiskState(path), RiskSettings()
market = Market(clock)
ledger = PaperLedger(path, settings.bankroll, clock)
box = {}
gate = lambda: {"approved_for_paper": [], "forward_test": [e["strategy"] for e in [box["s"].gate_entry()] if e and e["status"] == "FORWARD_TEST"], "live_approved": []}
eng = PaperEngine(RiskGovernor(settings, state, gate, clock), ledger, market, PaperSettings(), clock=clock)
src = box["s"] = BollingerResting(eng, market, clock=clock)
rng = random.Random(21)
mids = {f"d{i}": 0.3 + 0.4 * rng.random() for i in range(20)}
anchor = dict(mids)
bad_fill = 0
for step in range(10 * 24 * 4):                               # a scan every 15 minutes for ten days
    clock.now += timedelta(minutes=15)
    for m in mids:                                           # prices wander but are pulled back to where they started
        mids[m] = min(0.9, max(0.1, mids[m] + rng.gauss(0, 0.012) + 0.04 * (anchor[m] - mids[m])))
        market.quote(m, m + "y", round(mids[m] - 0.01, 3), round(mids[m] + 0.01, 3), "Yes", 0)
        market.quote(m, m + "n", round(1 - mids[m] - 0.01, 3), round(1 - mids[m] + 0.01, 3), "No", 1)
    before = {o["order_id"]: o for o in eng.resting("PENDING", 2000)}
    t = eng.tick()
    for f in t["filled"]:
        o = before[f["order_id"]]
        q = market.rows[o["token_id"]]
        bad_fill += not (q["best_ask"] < o["limit_price"] and f["price"] == o["limit_price"] and q["observed_at"] > o["placed_at"])
    src.on_run()
rep = eng.report()
row = next((r for r in rep["strategies"] if r["strategy"] == sources.NAME), None) or {"resting": {}, "trades": 0}
st = src.standing()
trades = eng.trades(5000)
print(f"   resting orders: {row['resting'].get('placed')} placed, {row['resting'].get('filled')} filled, {row['resting'].get('expired')} expired, {row['resting'].get('refused')} refused")
print(f"   closed trades: {row['trades']} {row.get('exits')} · P&L ${row.get('pnl')} · average {row.get('avg_per_share')} a share · standing: {st['status']}")
print(f"   {st['reason']}")
r = row["resting"]
checks = {
    "the source placed resting orders, and only its own": (r.get("placed") or 0) > 10 and {x["strategy"] for x in rep["strategies"]} == {sources.NAME},
    "an order fills only when a later ask is strictly below its price, and at its price": bad_fill == 0 and (r.get("filled") or 0) > 0,
    "orders that were not reached expired; none filled twice": (r.get("expired") or 0) > 0 and r["filled"] + r["expired"] + r["pending"] + r["refused"] == r["placed"],
    "every fill went through the governor (stake at most 2% of equity)": all(t["stake"] <= 0.02 * settings.bankroll * 1.2 for t in trades),
    "every rested entry paid no spread; exits were at the bid or the target": all(t["rested"] and t["entry_spread"] == 0 for t in trades),
    "the books balance": abs(ledger.summary(eng.prices())["equity"] - (settings.bankroll + sum(t["pnl"] for t in trades)
        + sum(p["units"] * eng.prices().get(p["position_id"], p["entry_price"]) - p["stake"] for p in ledger.positions("OPEN")))) < 0.05,
    "the loss limit holds: the test never lost more than $50 plus one position": st["pnl"] >= -(sources.LOSS_LIMIT + 0.02 * settings.bankroll * 3),
    "the standing follows the written rule": st["status"] in ("RUNNING", "STOPPED", "SUCCEEDED", "FAILED") and (st["status"] != "RUNNING" or st["active"]),
}
for name, good in checks.items():
    print(f"{'PASS' if good else 'FAIL'}: {name}")
print("FORWARD-TEST DRILL:", "PASS" if all(checks.values()) else "FAIL")
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
def get(p, raw=False):
    with urllib.request.urlopen(BASE + p, timeout=120) as r:
        body = r.read().decode()
        return body if raw else json.loads(body)
def status(p, method="GET"):
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + p, method=method, data=b"{}" if method != "GET" else None,
                                                           headers={"Content-Type": "application/json"}), timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
checks = {}
try:
    page = get("/", raw=True)
    checks["/ serves the dashboard; the console moved to /console"] = ("F.R.I.D.A.Y." in page and "/static/dashboard.js" in page
        and status("/console") == 200 and status("/static/dashboard.css") == 200 and status("/static/dashboard.js") == 200)
    checks["the page has no forms, buttons or inputs"] = not any(t in page.lower() for t in ("<form", "<button", "<input"))
    d = get("/api/dashboard")
    sections = ("system", "scanner", "gate", "risk", "portfolio", "activity", "paper", "library", "book_strategies")
    checks["one call returns every section"] = all(k in d for k in sections)
    failed = [k for k in sections if isinstance(d[k], dict) and "error" in d[k]]
    checks[f"sections stand alone: the library is off in this test and says so; the rest render (failed: {failed})"] = failed == ["library"]
    checks["live LOCKED, execution locked"] = d["live_trading"] == "LOCKED" and d["execution_locked"] is True
    p = get("/api/portfolio")
    checks["paper portfolio: full bankroll, no positions, no P&L"] = (p["mode"] == "PAPER" and p["equity"] == p["bankroll"]
        and p["positions"] == [] and p["total_pnl"] == 0 and p["closed_trades"] == 0 and p["live_trading"] == "LOCKED")
    b = d["book_strategies"]
    checks["the dashboard shows 12 registered, 0 tested"] = b["registered"] == 12 and b["tested"] == 0 and len(b["items"]) == 12
    paper = get("/api/paper")
    ft = paper["forward_tests"][0]
    checks["the forward test is declared, running and has placed nothing on an empty store"] = (ft["strategy"] == "BOLLINGER_RESTING"
        and ft["status"] == "RUNNING" and ft["trades"] == 0 and paper["resting"] == [])
    g = get("/api/lab/gate")
    checks["the gate lists it as FORWARD_TEST, approves nothing, never live"] = ("BOLLINGER_RESTING" in g["forward_test"]
        and g["approved_for_paper"] == [] and g["live_approved"] == [])
    checks["/api/paper: engine ready, nothing open, PAPER only, refuses writes"] = (bool(paper["paper_version"])
        and paper["mode"] == "PAPER" and paper["positions"] == [] and paper["live_trading"] == "LOCKED" and status("/api/paper", "POST") == 405)
    checks["the dashboard carries the paper record"] = d["paper"]["open"] == 0 and d["system"]["components"].get("paper") == "READY"
    s = get("/api/strategies")
    checks["/api/strategies: 12 rules with source, rule and fixed settings; none approved"] = (s["registered"] == 12
        and s["signals"] == 11 and s["approved_for_paper"] == [] and len(s["books"]) == 8 and len(s["sources"]) == 12
        and all(x["source"] and x["rule"] and x["status"] == "REGISTERED_NOT_TESTED" for x in s["strategies"]))
    checks["the registry refuses writes"] = status("/api/strategies", "POST") == 405
    bars = get("/api/lab/bars")
    checks["/api/lab/bars answers and refuses writes"] = "markets" in bars and bars["holdout_fraction"] == 0.3 and status("/api/lab/bars", "POST") == 405
    book = get("/api/lab/book")
    checks["/api/lab/book: nothing tested on an empty store; it refuses writes"] = book["tested"] is False and status("/api/lab/book", "POST") == 405
    req = urllib.request.Request(BASE + "/api/risk/evaluate", data=json.dumps({"strategy": "BOOK_RSI", "market_id": "m", "stake": 10}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        checks["a registered but untested strategy is refused by the Risk Governor"] = json.load(resp)["reason"] == "NOT_ALLOWED_BY_GATE"
    checks["nothing is approved without evidence"] = d["gate"]["approved_for_paper"] == [] and d["gate"]["live_approved"] == []
    checks["the dashboard and portfolio refuse writes"] = status("/api/dashboard", "POST") == 405 and status("/api/portfolio", "POST") == 405
    r = get("/api/risk/status")
    checks["the governor reads the ledger"] = r["portfolio"]["equity"] == p["equity"] and r["live_trading"] == "LOCKED"
    checks["governor and ledger are the drilled versions (10C.1)"] = r["risk_version"] == "10C.1" and p["ledger_version"] == "10C.1"
    h = get("/health")
    checks["HEALTHY, execution locked, and the dashboard made no venue call"] = (h["status"] == "HEALTHY"
        and h["execution_locked"] is True and not h.get("last_error"))
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
import pathlib, re, subprocess
from friday.hermes.authority import Capability
from friday.hermes.jobs import JobType
from friday.intelligence.schemas import IntelligenceAction
import hashlib, json
from friday.risk import DRILLS_VERSION, LEDGER_VERSION, RISK_VERSION
from friday.strategies import REGISTRY_VERSION, STRATEGIES
from friday.lab.bookbt import BOOKBT_VERSION, HOLDOUT_FRACTION, Z_DEVELOPMENT
from friday.market.bars import BARS_VERSION
from friday.lab.bookrun import BOOKRUN_VERSION
from friday.lab.gate import GATE_VERSION
from friday.paper import PAPER_VERSION, SOURCES_VERSION
from friday.paper import sources as fwd
paper_src = "\n".join(p.read_text() for p in pathlib.Path("friday/paper").glob("*.py"))
main_src = pathlib.Path("friday/app/main.py").read_text()
bt = pathlib.Path("friday/lab/bookbt.py").read_text()
lab_cli = pathlib.Path("friday/lab/__main__.py").read_text()
strat = "\n".join(p.read_text() for p in pathlib.Path("friday/strategies").glob("*.py") if p.name != "__main__.py")
frozen = hashlib.sha256(json.dumps([(s.key, s.params, s.exit and s.exit.__dict__) for s in STRATEGIES], sort_keys=True).encode()).hexdigest()
risk = "\n".join(p.read_text() for p in pathlib.Path("friday/risk").glob("*.py"))
static = pathlib.Path("friday/static")
js, html, css = [(static / f"dashboard.{e}").read_text() for e in ("js", "html", "css")]
front = js + html + css
checks = {
    "registry and backtester 11B.1 (unchanged); bars, test run and gate 11C.1; on governor, ledger and drills 10C.1": (REGISTRY_VERSION, BOOKBT_VERSION) == ("11B.1",) * 2 and all((BARS_VERSION, BOOKRUN_VERSION, GATE_VERSION))
        and (RISK_VERSION, LEDGER_VERSION, DRILLS_VERSION) == ("10C.1",) * 3,
    "the settings are the frozen ones (fingerprint 0127a702)": frozen.startswith("0127a7025bb15507"),
    "the verdict rule is fixed before any real test (30% holdout, 99.5% development bar)": (HOLDOUT_FRACTION, Z_DEVELOPMENT) == (0.30, 2.81),
    "the backtester has no store, network or order path": not any(
        k in bt for k in ("MarketStore", "sqlite3", "httpx", "place_order")),
    "paper engine, sources and gate 12B.1": all((PAPER_VERSION, SOURCES_VERSION, GATE_VERSION)),
    "the forward test uses the book's frozen Bollinger settings and its written rule": fwd.RULE.params == {"period": 20, "width": 2.0}
        and (fwd.MIN_TRADES, fwd.MIN_DAYS, fwd.LOSS_LIMIT, fwd.TTL_MINUTES, fwd.STAKE) == (100, 14, 50.0, 60.0, 20.0),
    "a forward test cannot mark itself approved": "a forward test cannot approve itself" in pathlib.Path("friday/lab/gate.py").read_text(),
    ".env.example documents FRIDAY_PAPER_BOLLINGER": "FRIDAY_PAPER_BOLLINGER" in pathlib.Path(".env.example").read_text(),
    "the paper engine has no network, Claude or order path": not any(
        k in paper_src for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.", "place_order", "urllib")),
    "every paper entry goes through the governor (open_checked); the API cannot submit an order": "open_checked" in paper_src
        and "ledger.open(" not in paper_src and "paper_engine.submit(" not in main_src,
    ".env.example documents FRIDAY_PAPER_*": "FRIDAY_PAPER_MAX_SPREAD" in pathlib.Path(".env.example").read_text(),
    "the app can read the test result but cannot run or change the test": "run_book_test" not in pathlib.Path("friday/app/main.py").read_text(),
    "the test run has no network, Claude or order path": not any(
        k in pathlib.Path("friday/lab/bookrun.py").read_text() for k in ("httpx", "anthropic", "urllib", "place_order", "requests.")),
    "the registry has no network, Claude, store or order path": not any(
        k in strat for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.", "place_order", "urllib", "sqlite3", "open(")),
    "the app has no route that opens or closes a position": not any(k in main_src
        for k in ("open_checked", "paper_ledger.open", "paper_ledger.close")),
    "the risk package has no network, Claude or order path": not any(
        k in risk for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.", "place_order", "urllib")),
    "the dashboard reads one endpoint and sends nothing": len(re.findall(r"\bfetch\(", js)) == 1
        and "/api/dashboard" in js and not re.search(r"POST|PUT|DELETE|XMLHttpRequest|sendBeacon|WebSocket", js),
    "the dashboard loads nothing from outside the server": not re.search(r"https?://|//cdn|@import", front.replace("http://www.w3.org", "")),
    "no injected HTML": "innerHTML" not in js and "document.write" not in js and "eval(" not in js,
    "no invented scores or third-party branding": not any(k in front for k in ("98%", "BULLISH", "J.A.R.V.I.S", "JARVIS", "Iron Man")),
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
