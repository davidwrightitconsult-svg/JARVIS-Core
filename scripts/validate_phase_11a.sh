#!/usr/bin/env bash
# FRIDAY Phase 11A validation — the strategy registry built from the books.
# Offline suites; the registry printed in full; a read-only check of which of the six
# books your library holds; a real-server smoke on temporary stores.
# No network needed. No Claude. Nothing is tested for profit here and nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 11A VALIDATION — STRATEGY REGISTRY ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- registry tests ---"
T="friday/tests/test_strategy_registry.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- the registry: every rule, its source and its fixed settings ---"
python -m friday.strategies list

echo
echo "--- which of the six books are in YOUR library (read-only) ---"
python -m friday.strategies books

echo
echo "--- drill: no rule can see the future (random price paths, no real data) ---"
python - <<'PY'
import random
from friday.strategies import STRATEGIES, signal, signals
paths = []
for seed in range(8):
    rng, p, c = random.Random(seed), 0.5, []
    for i in range(900):
        p = min(0.97, max(0.03, p + rng.gauss(0, 0.008) + (0.0005 if (i // 150) % 2 else -0.0005)))
        c.append(round(p, 3))
    paths.append(c)
ok_all = True
flip = {"LONG": "SHORT", "SHORT": "LONG"}
for s in STRATEGIES:
    if s.kind != "SIGNAL":
        continue
    fired = [dict(signals(s, c)) for c in paths]
    n = sum(len(f) for f in fired)
    longs = sum(1 for f in fired for v in f.values() if v == "LONG")
    rng = random.Random(99)
    same = all(signal(s, c[:i + 1]) == f.get(i) for c, f in zip(paths, fired) for i in rng.sample(range(60, 900), 40))
    twisted = all({i: v for i, v in signals(s, c[:500] + [round(1 - x, 3) for x in c[500:]]) if i < 500}
                  == {i: v for i, v in f.items() if i < 500} for c, f in zip(paths[:3], fired[:3]))
    mirror = all([(i, flip[v]) for i, v in sorted(f.items())] == signals(s, [round(1 - x, 3) for x in c])
                 for c, f in zip(paths[:3], fired[:3]))
    ok = n > 0 and same and twisted and mirror
    ok_all &= ok
    print(f"{'PASS' if ok else 'FAIL'}: {s.name:<22} {n:>4} signals on 8 paths ({longs} long, {n - longs} short); "
          f"same decision with or without the future; long and short mirror exactly")
print("LOOKAHEAD DRILL:", "PASS" if ok_all else "FAIL")
PY

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
    b = d["book_strategies"]
    checks["the dashboard shows 12 registered, 0 tested"] = b["registered"] == 12 and b["tested"] == 0 and len(b["items"]) == 12
    s = get("/api/strategies")
    checks["/api/strategies: 12 rules with source, rule and fixed settings; none approved"] = (s["registered"] == 12
        and s["signals"] == 11 and s["approved_for_paper"] == [] and len(s["books"]) >= 6
        and all(x["source"] and x["rule"] and x["status"] == "REGISTERED_NOT_TESTED" for x in s["strategies"]))
    checks["the registry refuses writes"] = status("/api/strategies", "POST") == 405
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
strat = "\n".join(p.read_text() for p in pathlib.Path("friday/strategies").glob("*.py") if p.name != "__main__.py")
frozen = hashlib.sha256(json.dumps([(s.key, s.params, s.exit and s.exit.__dict__) for s in STRATEGIES], sort_keys=True).encode()).hexdigest()
risk = "\n".join(p.read_text() for p in pathlib.Path("friday/risk").glob("*.py"))
static = pathlib.Path("friday/static")
js, html, css = [(static / f"dashboard.{e}").read_text() for e in ("js", "html", "css")]
front = js + html + css
checks = {
    "strategy registry present on governor, ledger and drills 10C.1": bool(REGISTRY_VERSION)
        and (RISK_VERSION, LEDGER_VERSION, DRILLS_VERSION) == ("10C.1",) * 3,
    "the settings are the frozen ones (fingerprint 0127a702)": frozen.startswith("0127a7025bb15507"),
    "the registry has no network, Claude, store or order path": not any(
        k in strat for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.", "place_order", "urllib", "sqlite3", "open(")),
    "the app has no route that opens or closes a position": not any(k in pathlib.Path("friday/app/main.py").read_text()
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
