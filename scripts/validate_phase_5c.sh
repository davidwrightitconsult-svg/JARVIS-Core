#!/usr/bin/env bash
# FRIDAY Phase 5C validation — Claude on research (recall + on-request digests).
# Offline suites + 2 LIVE Claude calls (temp library/vault/DB: one analysis
# with library recall, one digest section) + a server smoke without Claude.
# Your real library, vault and budgets are only reported on.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 5C VALIDATION — CLAUDE ON RESEARCH ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- 5C recall + digest tests ---"
python -m pytest -q -p no:warnings friday/tests/test_research_digest.py 2>&1 | tail -1
python -O -m pytest -q -p no:warnings friday/tests/test_research_digest.py 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- research (5A/5B) + knowledge + Hermes + app regression ---"
R="friday/tests/test_research_library.py friday/tests/test_research_ingest.py friday/tests/test_knowledge_recorder.py friday/tests/test_knowledge_sync_recall.py friday/tests/test_hermes_contract.py friday/tests/test_hermes_runtime.py friday/tests/test_hermes_scheduler.py friday/tests/test_app_hermes.py"
python -m pytest -q -p no:warnings $R 2>&1 | tail -1

echo
echo "--- LIVE: analysis with library recall + one real digest section (2 Claude calls) ---"
FRIDAY_LIVE_RESEARCH_TEST=1 python -m pytest -q -s -rs -p no:warnings friday/tests/test_research_live.py 2>&1 \
  | grep -E "^\[RESEARCH\]|passed|failed|skipped|Error|assert" | tail -8

echo
echo "--- REAL SERVER smoke (127.0.0.1:8765, temp DB + library + vault; no Claude) ---"
SMOKE_DIR="$(mktemp -d)"
python -m friday.research init --library "$SMOKE_DIR/library" >/dev/null
python -m friday.knowledge init --vault "$SMOKE_DIR/vault" >/dev/null
DB_PATH="$SMOKE_DIR/friday.db" HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 ANTHROPIC_API_KEY= \
FRIDAY_KNOWLEDGE_ENABLED=true FRIDAY_VAULT_PATH="$SMOKE_DIR/vault" FRIDAY_LIBRARY_PATH="$SMOKE_DIR/library" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - <<'PY'
import json, urllib.request
BASE = "http://127.0.0.1:8765"
def get(p):
    return json.load(urllib.request.urlopen(BASE + p, timeout=10))
def post(p, body):
    req = urllib.request.Request(BASE + p, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        return {"http_status": e.code}
checks = {}
try:
    h = get("/health")
    s = get("/api/research/status")
    checks["HEALTHY, execution locked"] = h["status"] == "HEALTHY" and h["execution_locked"] is True
    checks["research recall enabled"] = s["recall"]["enabled"] is True
    rb = s.get("research_budget") or {}
    checks["separate research budget (200k/day, 14k reserve)"] = (
        rb.get("daily_limit") == 200000 and rb.get("reserve_per_call") == 14000 and rb.get("day", "").startswith("research:"))
    hs = get("/api/hermes/status")
    checks["analysis budget reserve 12k"] = hs["runtime"]["budget"]["reserve_per_call"] == 12000
    checks["bad document id refused"] = post("/api/research/digest", {"doc_id": "nope"}).get("http_status") == 400
    checks["digest of unknown document fails cleanly"] = post(
        "/api/research/digest", {"doc_id": "0123456789abcdef", "sections": 1}).get("status") == "FAILED"
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
from friday.hermes.authority import Capability, HERMES_AUTHORITY, is_forbidden
from friday.hermes.budget import DEFAULT_RESERVE_PER_CALL, DEFAULT_RESEARCH_DAILY_TOKENS
from friday.hermes.jobs import JOB_CAPABILITIES, JOB_TIMEOUT_SECONDS, JobType
from friday.hermes.scheduler import MIN_INTERVAL_SECONDS
from friday.intelligence.claude import MAX_OUTPUT_TOKENS
from friday.intelligence.prompts import PROMPT_ARCHITECTURE_VERSION
from friday.knowledge.vault import FRIDAY_FOLDERS
from friday.research.recall import MAX_TOTAL_CHARS, RESEARCH_RECALL_VERSION
caps = {j: c for j, c in JOB_CAPABILITIES.items()}
checks = {
    "READ_RESEARCH granted, not forbidden": Capability.READ_RESEARCH in HERMES_AUTHORITY and not is_forbidden("READ_RESEARCH"),
    "no execution capability exists": not any(is_forbidden(c.value) for c in Capability),
    "only analysis + digest may ask Claude": {j.value for j, c in caps.items() if Capability.REQUEST_INTELLIGENCE in c}
        == {"INTELLIGENCE_ANALYSIS", "RESEARCH_DIGEST"},
    "only analysis + digest read research": {j.value for j, c in caps.items() if Capability.READ_RESEARCH in c}
        == {"INTELLIGENCE_ANALYSIS", "RESEARCH_DIGEST"},
    "digest never scheduled more than hourly": MIN_INTERVAL_SECONDS[JobType.RESEARCH_DIGEST] >= 3600,
    "digest has its own 900 s timeout": JOB_TIMEOUT_SECONDS[JobType.RESEARCH_DIGEST] == 900,
    "recall fits the 12k reserve (6.2k + 2.4k/3 + 3k)": 6200 + MAX_TOTAL_CHARS // 3 + MAX_OUTPUT_TOKENS <= DEFAULT_RESERVE_PER_CALL,
    "research budget default 200k": DEFAULT_RESEARCH_DAILY_TOKENS == 200_000,
    "prompt contract unchanged (2C.3)": PROMPT_ARCHITECTURE_VERSION == "2C.3",
    "vault FRIDAY/Research zone": "Research" in FRIDAY_FOLDERS,
    "recall version 5C.1": RESEARCH_RECALL_VERSION == "5C.1",
    ".env.example documents 5C settings": all(k in pathlib.Path(".env.example").read_text()
        for k in ("FRIDAY_RESEARCH_RECALL", "FRIDAY_RESEARCH_DAILY_TOKEN_BUDGET")),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("ALL CHECKS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- your real library (read-only) ---"
python -m friday.research digest-status --library "${FRIDAY_LIBRARY_PATH:-$HOME/friday/library}" 2>&1 | head -10

echo
echo "--- running FRIDAY service (read-only; reflects the code it was last started with) ---"
curl -s --max-time 5 http://127.0.0.1:8000/api/knowledge/status | python3 -c "
import json, sys
try:
    s = json.load(sys.stdin)
except Exception:
    print('service not reachable'); sys.exit(0)
sync = s.get('sync') or {}
print('notes enabled:', s.get('enabled'), '| written:', s.get('notes_written'), '| failures:', s.get('write_failures'),
      '| sync:', sync.get('enabled'), '| reason:', s.get('reason'))
" 2>/dev/null || echo "service not reachable"

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"
git ls-files | grep -qiE "\.(pdf|epub)$" && echo "FAIL: book/paper files tracked in Git" || echo "PASS: no PDF/EPUB files tracked in Git"

echo
echo "=== END — paste everything above back to Claude ==="
