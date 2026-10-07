#!/usr/bin/env bash
# FRIDAY Phase 10B validation — the paper ledger and the read-only dashboard.
# Offline suites; a ledger drill with a TEMPORARY risk store; a real-server smoke
# of the dashboard on temporary stores. No network needed. No Claude. Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 10B VALIDATION — PAPER LEDGER + DASHBOARD ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- ledger + governor + gate tests ---"
T="friday/tests/test_paper_ledger.py friday/tests/test_risk_governor.py friday/tests/test_lab_gate.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- drill: paper ledger under the governor (temporary risk store, paper money) ---"
DRILL_DIR="$(mktemp -d)"
python - "$DRILL_DIR/risk.db" <<'PY'
import sys
from friday.risk import LedgerError, PaperLedger, Proposal, RiskGovernor, RiskSettings, RiskState
risk_db = sys.argv[1]
gate = {"approved_for_paper": ["DRILL"], "forward_test": [], "live_approved": []}
settings = RiskSettings()
state = RiskState(risk_db)
g = RiskGovernor(settings, state, lambda: gate)
ledger = PaperLedger(risk_db, settings.bankroll)
B = settings.bankroll
def refused(fn):
    try:
        fn(); return False
    except LedgerError:
        return True
def line(label, text):
    print(f"   {label:<52} {text}")
r = {}
s0 = ledger.summary()
line("start", f"equity ${s0['equity']:.2f} · cash ${s0['cash']:.2f}")
d1 = g.evaluate(Proposal("DRILL", "m1", 500), ledger.portfolio())
p1 = ledger.open(d1, 0.40, "tok1", "drill market one")
line("ask $500 at 0.40", f"{d1['verdict']} ${d1['approved_stake']:g} -> {p1['units']:g} units")
r["cut"] = d1["verdict"] == "REDUCED" and p1["stake"] == round(B * settings.max_position_fraction, 2)
r["once"] = refused(lambda: ledger.open(d1, 0.40))
r["dry"] = refused(lambda: ledger.open(g.evaluate(Proposal("DRILL", "m2", 10), ledger.portfolio(), record=False), 0.5))
r["rejected"] = refused(lambda: ledger.open(g.evaluate(Proposal("OTHER", "m2", 10), ledger.portfolio()), 0.5))
r["live"] = refused(lambda: ledger.open(g.evaluate(Proposal("DRILL", "m2", 10, mode="LIVE"), ledger.portfolio()), 0.5))
up = ledger.summary({p1["position_id"]: 0.50})
line("price 0.40 -> 0.50", f"equity ${up['equity']:.2f} · P&L {up['total_pnl']:+.2f}")
r["mark"] = abs(up["equity"] - (B + p1["units"] * 0.10)) < 0.011
c1 = ledger.close(p1["position_id"], 0.50, "drill")
after = ledger.summary()
line("close at 0.50", f"pnl {c1['pnl']:+.2f} · equity ${after['equity']:.2f}")
r["close"] = abs(c1["pnl"] - p1["units"] * 0.10) < 0.011 and abs(after["equity"] - (B + c1["pnl"])) < 0.011 \
    and refused(lambda: ledger.close(p1["position_id"], 0.5))
# a losing run: each position goes to zero until the governor stops the day
losses, reason = 0, ""
for i in range(40):
    d = g.evaluate(Proposal("DRILL", f"loss{i}", 20), ledger.portfolio())
    if d["verdict"] == "REJECTED":
        reason = d["reason"]; break
    ledger.close(ledger.open(d, 0.5)["position_id"], 0.0, "drill loss"); losses += 1
day = ledger.summary()
line(f"{losses} straight total losses", f"equity ${day['equity']:.2f} -> next proposal: {reason}")
r["day"] = reason == "DAILY_LOSS_LIMIT" and day["equity"] >= B * (1 - settings.daily_loss_limit) - B * settings.max_position_fraction
r["cash"] = day["cash"] >= 0 and all(p["status"] == "CLOSED" for p in ledger.positions(None))
again = PaperLedger(risk_db, settings.bankroll * 5).summary()      # a restart, even with a changed setting, keeps the books
r["restart"] = (again["equity"], again["peak_equity"], again["daily_pnl"], again["closed_trades"]) == (
    day["equity"], day["peak_equity"], day["daily_pnl"], day["closed_trades"])
r["peak"] = day["peak_equity"] >= day["equity"] and day["drawdown"] > 0
checks = {
    "the stake that opens is the governor's stake, cut to the cap": r["cut"],
    "one decision opens one position, once": r["once"],
    "a dry run cannot open a position": r["dry"],
    "a rejected decision cannot open a position": r["rejected"],
    "LIVE cannot reach the ledger": r["live"],
    "open positions are valued at the current price": r["mark"],
    "closing realises the profit; a closed position cannot close twice": r["close"],
    "a losing day ends at the daily loss limit, using the ledger's own equity": r["day"],
    "cash never went below zero": r["cash"],
    "the peak and drawdown are tracked": r["peak"],
    "equity, peak and day start survive a restart": r["restart"],
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("LEDGER DRILL:", "PASS" if all(checks.values()) else "FAIL")
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
    sections = ("system", "scanner", "gate", "risk", "portfolio", "activity", "library", "book_strategies")
    checks["one call returns every section"] = all(k in d for k in sections)
    failed = [k for k in sections if isinstance(d[k], dict) and "error" in d[k]]
    checks[f"sections stand alone: the library is off in this test and says so; the rest render (failed: {failed})"] = failed == ["library"]
    checks["live LOCKED, execution locked"] = d["live_trading"] == "LOCKED" and d["execution_locked"] is True
    p = get("/api/portfolio")
    checks["paper portfolio: full bankroll, no positions, no P&L"] = (p["mode"] == "PAPER" and p["equity"] == p["bankroll"]
        and p["positions"] == [] and p["total_pnl"] == 0 and p["closed_trades"] == 0 and p["live_trading"] == "LOCKED")
    checks["the 12 book strategies are shown as NOT yet tested"] = (not d["book_strategies"].get("tested")
        and len(d["book_strategies"]["names"]) == 12)
    checks["nothing is approved without evidence"] = d["gate"]["approved_for_paper"] == [] and d["gate"]["live_approved"] == []
    checks["the dashboard and portfolio refuse writes"] = status("/api/dashboard", "POST") == 405 and status("/api/portfolio", "POST") == 405
    r = get("/api/risk/status")
    checks["the governor reads the ledger"] = r["portfolio"]["equity"] == p["equity"] and r["live_trading"] == "LOCKED"
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
from friday.risk import LEDGER_VERSION, RISK_VERSION
risk = "\n".join(p.read_text() for p in pathlib.Path("friday/risk").glob("*.py"))
static = pathlib.Path("friday/static")
js, html, css = [(static / f"dashboard.{e}").read_text() for e in ("js", "html", "css")]
front = js + html + css
checks = {
    "paper ledger and governor versions present": bool(LEDGER_VERSION and RISK_VERSION),
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
