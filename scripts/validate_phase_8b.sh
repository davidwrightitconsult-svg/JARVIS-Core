#!/usr/bin/env bash
# FRIDAY Phase 8B validation — fair probability + edge vs the live ask.
# Offline suites; fair values and edges computed READ-ONLY from your REAL
# calibration dataset and latest market run; a real-server smoke. No Claude.
# Nothing can trade: stake fractions are arithmetic only.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 8B VALIDATION — FAIR VALUE + EDGE ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- fair value + calibration + quant + market tests ---"
T="friday/tests/test_fair_value.py friday/tests/test_calibration.py friday/tests/test_quant_scoring.py friday/tests/test_quant_strategies.py friday/tests/test_quant_core.py friday/tests/test_market_context.py friday/tests/test_market_store.py friday/tests/test_market_data.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- fair value + edges from your REAL calibration data and latest run (read-only) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import os, sys
from collections import Counter
from friday.market.store import MarketStore
from friday.quant import QuantEngine, UniverseRules
from friday.quant.calibration import build_calibration
from friday.quant.fair import EdgeSettings, FairValueModel
path = sys.argv[1]
if not os.path.exists(path):
    print("no real market store yet — SKIPPED"); sys.exit(0)
store = MarketStore(path)
cal = build_calibration(store, horizon_hours=168)
print(f"[CALIBRATION] {cal['dataset']['markets']} resolved markets so far; 168h: {cal['samples']} samples, "
      f"Brier {cal['brier']} (no-skill {cal['baseline_brier']}); long shots priced "
      f"{cal['longshots']['mean_price']} won {cal['longshots']['win_rate']} → {cal['longshots']['verdict']}; "
      f"favourites priced {cal['favourites']['mean_price']} won {cal['favourites']['win_rate']} → {cal['favourites']['verdict']}")
for seg, v in build_calibration(store, horizon_hours=24)["by_segment"].items():
    ls, fv = v["longshots"], v["favourites"]
    print(f"[SEGMENT {seg:<6}] 24h: {v['markets']:>5} markets · long shots priced {ls['mean_price']} won "
          f"{ls['win_rate']} → {ls['verdict']} · favourites priced {fv['mean_price']} won {fv['win_rate']} → {fv['verdict']}")
settings = EdgeSettings.from_environment()
model = FairValueModel(store, settings)
print(f"[MODEL] {model.status()}")
live = QuantEngine(store, UniverseRules.from_environment()).features()
seg_of = {m.market_id: m.segment for m in live}
edges = model.edges(live)
print(f"[EDGES] {len(edges)} eligible binary markets: {dict(Counter(e['status'] for e in edges))} · by segment "
      f"{dict(Counter((e['segment'], e['status']) for e in edges))}")
for e in edges[:10]:
    side = {x["outcome"]: x for x in e["sides"]}[e["best_side"]]
    print(f"   {e['status']:<24} {e['question'][:44]:<44} p {e['price']:.3f}→fair {e['fair']:.3f} "
          f"[{e['segment']} {e['horizon_hours']:g}h {e['bin']} n={e['bin_samples']} w={e['weight']}] best {e['best_side'][:8]} "
          f"ask {side['ask']} edge {e['edge'] * 100:+.2f}c z {e['z']} stake {e['stake_fraction']}")
def recompute(e):
    return all(abs(s["edge"] - round(s["fair"] - s["ask"] - settings.fee, 4)) < 1e-9 for s in e["sides"])
checks = {
    "fair values computed for the live universe": len(edges) >= 1,
    "every edge is fair - ask - fee (never the mid)": all(recompute(e) for e in edges),
    "binary fair values sum to 1": all(abs(sum(s["fair"] for s in e["sides"]) - 1) < 1e-3 for e in edges),
    "EDGE only with >= 30 bin samples, >= min edge and z >= min z": all(
        e["bin_samples"] >= 30 and e["edge"] >= settings.min_edge and (e["z"] or 0) >= settings.min_z
        for e in edges if e["status"] == "EDGE"),
    "stake fractions within the cap, zero unless EDGE": all(
        0 <= e["stake_fraction"] <= settings.max_stake and (e["status"] == "EDGE" or e["stake_fraction"] == 0)
        for e in edges),
    "every row labelled estimated/unvalidated": all("UNVALIDATED" in e["label"] for e in edges),
    "each market calibrated only against its own segment": all(e["segment"] == seg_of[e["market_id"]] for e in edges),
    "sports markets use the 24h horizon (end date is only a resolution buffer)": all(
        e["horizon_hours"] == 24 for e in edges if e["segment"] == "sports"),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
edge_values = [e["edge"] for e in edges if e["status"] == "EDGE"]
print(f"[DIVERSITY] {len(edge_values)} EDGE rows with {len(set(edge_values))} distinct edge values "
      "(identical values across unrelated markets = one pooled bias, the 8B.1 flaw)")
print("(EDGE rows are estimates to be recorded and tested in 8C-9 — not trades)")
print("REAL EDGES:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- REAL SERVER smoke (127.0.0.1:8765, temp DB + temp market store) ---"
SMOKE_DIR="$(mktemp -d)"
DB_PATH="$SMOKE_DIR/friday.db" FRIDAY_MARKET_DB="$SMOKE_DIR/market/market_data.db" \
HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 HERMES_HEALTH_INTERVAL_SECONDS=0 FRIDAY_CALIBRATION_BACKFILL=false \
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
    edges = get("/api/quant/edges")
    watch = [r for r in rows if r["decision"] == "WATCH"]
    print(f"[SCAN] {len(rows)} rows; edge statuses {sorted({r['edge_status'] for r in rows if r['edge_status']})}")
    checks["scan rows carry edge fields"] = all({"edge", "edge_status", "edge_side", "fair"} <= set(r) for r in rows)
    checks["no calibration -> edge stays None (never a made-up number)"] = all(
        r["edge"] is None for r in rows if r["edge_status"] in (None, "INSUFFICIENT_CALIBRATION"))
    checks["edges endpoint + model status"] = "model" in edges and isinstance(edges["edges"], list)
    if watch:
        ctx = get(f"/api/market/context?market_id={watch[0]['market_id']}")
        checks["Claude's context carries fair_value"] = "fair_value" in ctx["markets"][0].get("quant", {}) or \
            len(ctx["markets"][0]["outcomes"]) != 2
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
from friday.quant.fair import FAIR_VERSION, EdgeSettings
env = pathlib.Path(".env.example").read_text()
src = "\n".join(p.read_text() for p in pathlib.Path("friday/quant").glob("*.py"))
d = EdgeSettings()
checks = {
    "fair value 8B.2, market context 8B.2 (per-segment calibration)": FAIR_VERSION == MARKET_CONTEXT_VERSION == "8B.2",
    "conservative defaults (2c edge, z>=2, 1/4 Kelly, 2% cap)": (d.min_edge, d.min_z, d.kelly_fraction, d.max_stake)
        == (0.02, 2.0, 0.25, 0.02),
    "no new capability (still 10)": len(Capability) == 10,
    "no new job type (still 8)": len(JobType) == 8,
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
    "quant core has no network or Claude path": not any(k in src for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.")),
    ".env.example documents FRIDAY_EDGE_*": all(k in env for k in (
        "FRIDAY_EDGE_FEE", "FRIDAY_EDGE_MIN", "FRIDAY_EDGE_MIN_Z", "FRIDAY_EDGE_KELLY_FRACTION", "FRIDAY_EDGE_MAX_STAKE")),
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
