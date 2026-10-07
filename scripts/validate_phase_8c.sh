#!/usr/bin/env bash
# FRIDAY Phase 8C validation (8C.2: sports out of scope) — edge estimates recorded and scored at resolution;
# the sports calibration gap closed (closedTime crawl, volume fields, fight/prop
# wording); z with a model-uncertainty floor.
# Offline suites; a LIVE read-only backfill into a TEMPORARY store; your REAL
# calibration + latest run read-only (nothing written); a real-server smoke.
# No Claude. Nothing can trade.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 8C.2 VALIDATION — EDGE TRACKING, SPORTS OUT OF SCOPE ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- edge tracking + fair value + calibration + quant + market tests ---"
T="friday/tests/test_edge_tracking.py friday/tests/test_fair_value.py friday/tests/test_calibration.py friday/tests/test_quant_scoring.py friday/tests/test_quant_strategies.py friday/tests/test_quant_core.py friday/tests/test_market_context.py friday/tests/test_market_store.py friday/tests/test_market_data.py"
python -m pytest -q -p no:warnings $T 2>&1 | tail -1
python -O -m pytest -q -p no:warnings $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: read-only backfill of ~150 resolved markets, NEWEST CLOSURES FIRST, into a TEMPORARY store ---"
TMP_8C="$(mktemp -d)"
python -m friday.market backfill --markets 150 --db "$TMP_8C/market_data.db" > "$TMP_8C/backfill.json"
python - "$TMP_8C/backfill.json" "$TMP_8C/market_data.db" <<'PY'
import json, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from friday.market.store import MarketStore
from friday.quant.segments import segment_of
r = json.load(open(sys.argv[1]))
print(f"[BACKFILL] pages {r['pages']} · looked at {r['seen']} · added {r['added']} ({r['points']} points) · "
      f"skipped {r['skipped']} · {r['seconds']}s · stopped: {r['stopped']}")
for e in r["errors"][:3]:
    print(f"   error: {e}")
store = MarketStore(sys.argv[2])
with store._db() as db:
    rows = [dict(x) for x in db.execute("SELECT question, neg_risk, reference_time FROM calib_markets")]
segs = Counter(segment_of(x["question"], x["neg_risk"]) for x in rows)
newest = max((x["reference_time"] for x in rows), default=None)
print(f"[SEGMENTS] added markets by segment: {dict(segs)} · newest resolution {newest}")
print("[ADDED] a sample of what entered the dataset (tell Claude if any of these is a sports market):")
for x in rows[:12]:
    print(f"   {x['question'][:88]}")
week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
checks = {
    "closed markets reached (crawl answered)": r["pages"] >= 1 and r["seen"] >= 1,
    "newest closures first: a market resolved in the last 7 days": bool(newest and newest >= week_ago),
    "sports are out of scope: skipped before any price history is fetched": r["skipped"].get("out of scope: sports", 0) >= 1,
    "no sports market entered the dataset": segs.get("sports", 0) == 0,
    "non-sports event markets still enter the dataset": r["added"] >= 1,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("LIVE CRAWL:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$TMP_8C"

echo
echo "--- your REAL calibration data and latest run (read-only: previews, nothing written) ---"
REAL_DB="${FRIDAY_MARKET_DB:-$HOME/friday/data/market_data.db}"
python - "${REAL_DB/#\~/$HOME}" <<'PY'
import os, re, sys
from collections import Counter
from friday.market.store import MarketStore
from friday.quant import QuantEngine, UniverseRules
from friday.quant.calibration import build_calibration
from friday.quant.fair import EdgeSettings, FairValueModel
from friday.quant.pipeline import QuantPipeline
from friday.quant.segments import excluded_segments, segment_of
from friday.quant.strategies import EDGE_STRATEGY, LIBRARY
path = sys.argv[1]
if not os.path.exists(path):
    print("no real market store yet — SKIPPED"); sys.exit(0)
store = MarketStore(path)
with store._db() as db:
    qs = [dict(x) for x in db.execute("SELECT question, neg_risk FROM calib_markets")]
    skips = dict(db.execute("SELECT reason, COUNT(*) FROM calib_skipped GROUP BY reason").fetchall())
print(f"[DATASET] {len(qs)} resolved markets · skipped so far {skips}")
pct = [x["question"] for x in qs if segment_of(x["question"], x["neg_risk"]) == "sports"
       and re.search(r"over/under \d+(\.\d+)?\s*%", x["question"] or "", re.I)]
old_sports = sum(1 for x in qs if segment_of(x["question"], x["neg_risk"]) == "sports")
print(f"[SCOPE] excluded segments: {sorted(excluded_segments())} · {old_sports} sports markets already in the "
      f"calibration data stay there unused · election 'Over/Under N%' markets classed as sports: {len(pct)}")
for seg, v in build_calibration(store, horizon_hours=24)["by_segment"].items():
    ls, fv = v["longshots"], v["favourites"]
    print(f"[SEGMENT {seg:<6}] 24h: {v['markets']:>5} markets · long shots priced {ls['mean_price']} won "
          f"{ls['win_rate']} → {ls['verdict']} · favourites priced {fv['mean_price']} won {fv['win_rate']} → {fv['verdict']}")
settings = EdgeSettings.from_environment()
model = FairValueModel(store, settings)
engine = QuantEngine(store, UniverseRules.from_environment())
live = engine.features()
edges = model.edges(live)
print(f"[EDGES] {len(edges)} eligible binary markets: {dict(Counter(e['status'] for e in edges))} · "
      f"model-uncertainty floor {settings.model_se}")
for e in [x for x in edges if x["status"] == "EDGE"][:10]:
    print(f"   EDGE {e['question'][:46]:<46} [{e['segment']} {e['horizon_hours']:g}h n={e['bin_samples']}] "
          f"buy {e['best_side'][:8]} edge {e['edge'] * 100:+.2f}c z {e['z']} (statistical {e['z_statistical']}) "
          f"stake {e['stake_fraction']}")
preview = QuantPipeline(engine, LIBRARY, record=False, fair_model=model).evaluate()
would = [s for s in preview["signals"] if s["strategy"] == EDGE_STRATEGY]
print(f"[TRACKING] on the next scan the service records {len(would)} new CALIBRATION_EDGE signal(s) "
      f"(already tracked: {len(store.signalled_markets(EDGE_STRATEGY))} markets)")
edge_rows = [e for e in edges if e["status"] == "EDGE"]
scope = excluded_segments()
in_scope_ok = [m for m in live if m.eligible and m.segment in scope]
print(f"[UNIVERSE] {sum(1 for m in live if m.eligible)} eligible of {len(live)} recorded · "
      f"{sum(1 for m in live if m.segment in scope)} out-of-scope markets still in the last run "
      "(they drop out on the first scan after the restart)")
checks = {
    "sports are excluded by default": "sports" in scope,
    "no out-of-scope market is eligible, has an edge or would be signalled": not in_scope_ok
        and all(e["segment"] not in scope for e in edges)
        and all(s["inputs"].get("segment") not in scope for s in would),
    "no election 'Over/Under N%' market is classed as sports": not pct,
    "z never exceeds edge / model floor (no 'z 17' certainty)": all(
        e["z"] is None or abs(e["z"]) <= abs(e["edge"]) / settings.model_se + 0.01 for e in edges) if settings.model_se else True,
    "EDGE needs >= 30 bin samples, >= min edge and floored z >= min z": all(
        e["bin_samples"] >= 30 and e["edge"] >= settings.min_edge and e["z"] >= settings.min_z for e in edge_rows),
    "every would-be signal is an EDGE row on the side to buy (UP), once per market": all(
        s["direction"] == "UP" for s in would) and len({s["market_id"] for s in would}) == len(would)
        and {s["market_id"] for s in would} <= {e["market_id"] for e in edge_rows},
    "signals carry the model inputs (fair, edge, z, segment, bin)": all(
        {"fair", "edge", "z", "segment", "bin"} <= set(s["inputs"]) for s in would),
    "every row labelled estimated/unvalidated": all("UNVALIDATED" in e["label"] for e in edges),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
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
    from friday.quant.segments import segment_of
    rows = get("/api/scan")
    stats = get("/api/market/stats")
    print(f"[SCAN] {len(rows)} rows · scope filter {stats['filter'].get('exclude_segments')}")
    for q in list(dict.fromkeys(r["question"] for r in rows))[:12]:
        print(f"   {q[:88]}")
    checks["live scan contains no sports market"] = bool(rows) and all(segment_of(r["question"]) != "sports" for r in rows)
    checks["scope filter reported (sports excluded)"] = stats["filter"].get("exclude_segments") == ["sports"]
    strat = {s["name"]: s for s in get("/api/quant/strategies")["strategies"]}
    card = {s["strategy"]: s for s in get("/api/quant/scorecard")["strategies"]}
    edges = get("/api/quant/edges")
    checks["CALIBRATION_EDGE listed, enabled, judged on resolution"] = (
        "CALIBRATION_EDGE" in strat and strat["CALIBRATION_EDGE"]["enabled"]
        and strat["CALIBRATION_EDGE"]["judged_on"] == "resolution")
    checks["scorecard carries a held-to-resolution verdict"] = all(
        "verdict" in s["held_to_resolution"] and "ci95_net" in s["held_to_resolution"] for s in card.values())
    checks["edges report the model-uncertainty floor"] = edges["model"]["settings"].get("model_se") is not None
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
from friday.market.backfill import BACKFILL_VERSION
from friday.market.context import MARKET_CONTEXT_VERSION
from friday.quant.fair import FAIR_VERSION, EdgeSettings
from friday.quant.scorecard import SCORECARD_VERSION
from friday.quant.strategies import STRATEGIES_VERSION
from friday.market.segments import SEGMENTS_VERSION
env = pathlib.Path(".env.example").read_text()
src = "\n".join(p.read_text() for p in pathlib.Path("friday/quant").glob("*.py"))
d = EdgeSettings()
checks = {
    "8C.1 fair value, context, strategies, scorecard; 8C.2 backfill and segments": {FAIR_VERSION,
        MARKET_CONTEXT_VERSION, STRATEGIES_VERSION, SCORECARD_VERSION} == {"8C.1"}
        and BACKFILL_VERSION == SEGMENTS_VERSION == "8C.2",
    "conservative defaults (2c edge, z>=2 with 1c floor, 1/4 Kelly, 2% cap)": (
        d.min_edge, d.min_z, d.model_se, d.kelly_fraction, d.max_stake) == (0.02, 2.0, 0.01, 0.25, 0.02),
    "no new capability (still 10)": len(Capability) == 10,
    "no new job type (still 8)": len(JobType) == 8,
    "no trade action exists": {a.value for a in IntelligenceAction} == {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"},
    "quant core has no network or Claude path": not any(k in src for k in ("httpx", "anthropic", "ClaudeIntelligence", "requests.")),
    ".env.example documents FRIDAY_EDGE_MODEL_SE and FRIDAY_EXCLUDED_SEGMENTS": "FRIDAY_EDGE_MODEL_SE" in env
        and "FRIDAY_EXCLUDED_SEGMENTS=sports" in env,
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
