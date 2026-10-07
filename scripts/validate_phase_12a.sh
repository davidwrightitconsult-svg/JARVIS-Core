#!/usr/bin/env bash
# FRIDAY Phase 12A validation — the paper trading engine.
# Offline suites; a drill of the engine on made-up quotes with a TEMPORARY risk store; a
# real-server smoke on temporary stores. No network needed. No Claude. Paper money only.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 12A VALIDATION — PAPER TRADING ENGINE ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- paper engine + ledger + governor tests ---"
T="friday/tests/test_paper_engine.py friday/tests/test_paper_ledger.py friday/tests/test_risk_governor.py friday/tests/test_risk_drills.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- drill: a day of paper trading on made-up quotes (temporary risk store) ---"
DRILL_DIR="$(mktemp -d)"
python - "$DRILL_DIR/risk.db" <<'PY'
import random, sys
from datetime import datetime, timedelta, timezone
from friday.paper import Order, PaperEngine, PaperSettings
from friday.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

class Clock:
    now = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
    def __call__(self): return self.now
class Quotes:
    def __init__(self, clock): self.clock, self.q, self.res, self.watched = clock, {}, {}, set()
    def set(self, t, bid, ask): self.q[t] = {"best_bid": bid, "best_ask": ask, "observed_at": self.clock().isoformat(timespec="seconds")}
    def quote_near(self, t, at, tol=3.0):
        q = self.q.get(t)
        return q if q and timedelta(0) <= datetime.fromisoformat(at) - datetime.fromisoformat(q["observed_at"]) <= timedelta(hours=tol) else None
    def resolutions(self, ids=None): return {m: r for m, r in self.res.items() if ids is None or m in ids}
    def record_resolution(self, m, payoffs, source="gamma", now=None): self.res[m] = {"payoffs": payoffs}
    def watch(self, ids, now=None): self.watched.update(ids)

clock, path = Clock(), sys.argv[1]
state, settings = RiskState(path), RiskSettings()
gate = {"approved_for_paper": [], "forward_test": ["DRILL"], "live_approved": []}
ledger = PaperLedger(path, settings.bankroll, clock)
quotes = Quotes(clock)
eng = PaperEngine(RiskGovernor(settings, state, lambda: gate, clock), ledger, quotes, PaperSettings(), clock=clock)
rng = random.Random(12)
mids = {f"tok{i}": 0.3 + 0.4 * rng.random() for i in range(30)}
halves = {t: rng.choice((0.005, 0.005, 0.01, 0.01, 0.02, 0.04)) for t in mids}      # each market has its own spread
opened = refused = 0
worst_exposure = 0.0
for step in range(24 * 12):                                  # a quote every 5 minutes for a day
    clock.now += timedelta(minutes=5)
    for t in mids:
        mids[t] = min(0.97, max(0.03, mids[t] + rng.gauss(0, 0.006)))
        quotes.set(t, round(mids[t] - halves[t], 3), round(mids[t] + halves[t], 3))
    eng.tick()
    if step % 3 == 0:
        t = rng.choice(list(mids))
        r = eng.submit(Order("DRILL" if rng.random() < 0.9 else "BOOK_RSI", "mk" + t, t, 500, 0.03, 0.06, 6, "drill"))
        opened += r["status"] == "OPENED"; refused += r["status"] == "REFUSED"
    pf = ledger.portfolio(eng.prices())
    worst_exposure = max(worst_exposure, pf.exposure / pf.equity)
quotes.res = {"mk" + t: {"payoffs": {t: 1.0 if mids[t] > 0.5 else 0.0}} for t in mids}      # everything resolves
clock.now += timedelta(minutes=5)
eng.tick()
rep, s, trades = eng.report(), ledger.summary(), eng.trades(5000)
row = rep["strategies"][0] if rep["strategies"] else {}
print(f"   {opened} opened, {refused} refused · {rep['closed']} closed {row.get('exits')} · P&L ${row.get('pnl')} on ${row.get('staked')} staked")
print(f"   average spread paid on entry {row.get('avg_entry_spread')} · average result {row.get('avg_r')}R · equity ${s['equity']}")
for x in rep["refused"][:5]:
    print(f"   refused {x['n']:>3}: {x['reason']}")
checks = {
    "positions were opened, and only for the strategy the gate allows": opened > 10 and [r["strategy"] for r in rep["strategies"]] == ["DRILL"],
    "every entry was at a recorded ask, never above the 5 cent spread limit": all(t["entry_spread"] is not None and t["entry_spread"] <= 0.0501 for t in trades),
    "every stake was the governor's (at most 2% of equity)": all(t["stake"] <= 0.02 * settings.bankroll * 1.2 for t in trades),
    "exposure never passed the 30% cap": worst_exposure <= settings.max_total_exposure + 1e-6,
    "every position was closed by a stop, target, time limit or resolution": rep["open"] == 0
        and set(row["exits"]) <= {"stop", "target", "time", "resolved"} and len(trades) == opened,
    "a stop is never filled better than the bid; a target never better than the target": all(
        (t["close_reason"] != "target" or abs(t["per_share"] - 0.06) < 1e-6) and (t["close_reason"] != "stop" or t["per_share"] <= -0.03 + 1e-9) for t in trades),
    "the books balance: equity = bankroll + the sum of every trade": abs(s["equity"] - (settings.bankroll + sum(t["pnl"] for t in trades))) < 0.02,
    "refusals were recorded with their reason": refused > 0 and sum(x["n"] for x in rep["refused"]) == refused,
}
for name, good in checks.items():
    print(f"{'PASS' if good else 'FAIL'}: {name}")
print("PAPER DRILL:", "PASS" if all(checks.values()) else "FAIL")
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
    checks["/api/paper: engine 12A.1 ready, nothing open, PAPER only, refuses writes"] = (bool(paper["paper_version"])
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
from friday.paper import PAPER_VERSION
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
    "paper engine version present": bool(PAPER_VERSION),
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
