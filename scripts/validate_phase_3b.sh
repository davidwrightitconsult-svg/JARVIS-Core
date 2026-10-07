#!/usr/bin/env bash
# FRIDAY Phase 3B validation — Hermes runtime. Offline suite + 1 live Claude call.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 3B VALIDATION — HERMES RUNTIME ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- Hermes contract + runtime tests ---"
python -m pytest -q friday/tests/test_hermes_contract.py friday/tests/test_hermes_runtime.py 2>&1 | tail -1
python -O -m pytest -q friday/tests/test_hermes_contract.py friday/tests/test_hermes_runtime.py 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: 1 intelligence job through Hermes (needs ANTHROPIC_API_KEY in .env) ---"
python -m pytest -q -s -rs friday/tests/test_hermes_live.py 2>&1 | grep -E "^\[HERMES\]|passed|failed|skipped|Error|assert" | tail -6

echo
echo "--- full regression (live Claude tests skipped to save cost) ---"
python -m pytest -q friday/tests --ignore=friday/tests/test_claude_live_tasks.py \
  --ignore=friday/tests/test_hermes_live.py \
  --deselect friday/tests/test_claude_api.py::test_claude_live_non_trading_request 2>&1 | tail -1

echo
echo "--- authority check ---"
python - <<'PY'
from friday.hermes import (
    HERMES_AUTHORITY, HERMES_CONTRACT_VERSION, JOB_CAPABILITIES,
    HermesAuthorityError, create_job,
)
checks = {}
checks["contract version 3A.1"] = HERMES_CONTRACT_VERSION == "3A.1"
from friday.hermes import HERMES_RUNTIME_VERSION
from friday.hermes.handlers import HANDLERS
from friday.hermes.jobs import JobType
checks["runtime version 3B.1"] = HERMES_RUNTIME_VERSION == "3B.1"
checks["every job type has a handler"] = set(HANDLERS) == set(JobType)
checks["authority = read/route/report only"] = {c.value for c in HERMES_AUTHORITY} == {
    "READ_STATE", "READ_HEALTH", "READ_MARKET_DATA", "REQUEST_INTELLIGENCE", "EMIT_EVENT",
    "WRITE_KNOWLEDGE", "READ_KNOWLEDGE", "PUBLISH_KNOWLEDGE", "INGEST_RESEARCH", "READ_RESEARCH"}
checks["all job types within authority"] = all(c <= HERMES_AUTHORITY for c in JOB_CAPABILITIES.values())
blocked = 0
for name in ["PLACE_ORDER", "placeOrder", "unlock-execution", "MODIFY_RISK_LIMITS", "ACCESS_WALLET"]:
    try:
        create_job({"job_type": name})
    except HermesAuthorityError:
        blocked += 1
checks["forbidden job types blocked (5/5)"] = blocked == 5
job = create_job({"job_type": "MARKET_SCAN", "params": {"limit": 10}})
checks["jobs never authorize execution"] = job.execution_authorized is False
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
git ls-files | grep -q "^\.venv/" && echo "FAIL: .venv tracked" || echo "PASS: .venv is not tracked"

echo
echo "=== END — paste everything above back to Claude ==="
